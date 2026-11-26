"""Command-line entry point for session-audit: parsing, global flags, dispatch and the exits.

Flow of ``main``: parse (argparse, errors become the closed envelope), build the ``Context`` from
the global flags, ``Command.check`` (ranges, combinations, confirmation), ``Command.run``. Every
failure is a ``CliError`` (``errors.py``) reported once, by ``_report``: sanitized on stderr and,
with ``--json``, as the envelope alone on stdout. Exits 129, 130 and 143 (SIGHUP, SIGINT, SIGTERM)
print nothing. A failed write to standard output (a closed pipe, a closed descriptor) ends with
exit 1 whatever the command was doing.

Which commands exist, with their flags and checks, is the registry in ``commands/``: this module
knows no command by name.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NoReturn

from . import __version__, errors
from .args import check_actor, check_path, default_actor
from .command import Command, Context, Output, StdoutFailed, stderr_line
from .commands import COMMANDS
from .config import load_config
from .errors import CliError
from .gateway import Gateway, SystemGateway, discard_stream
from .textsafe import dumps_json

PROG = "session-audit"
DESCRIPTION = "Analyze Claude session transcripts and outcomes: retros and usage analytics."
HELP_HINT = "Run session-audit --help (or the command with --help) for usage."
_COMMAND_ATTR = "_session_audit_command"


class Interrupted(BaseException):
    """Raised by the signal handlers; a ``BaseException`` so no ``except Exception`` swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


_stderr = stderr_line


# argparse's own messages echo user text, so none of them is used: only their kind and, when
# they name an option, the option (taken from the parser's definition, never from the input).
_ARGUMENT = re.compile(
    r"argument ((?:-[A-Za-z0-9]|--[a-z][a-z0-9-]*)(?:/(?:-[A-Za-z0-9]|--[a-z][a-z0-9-]*))*): "
)
_REQUIRED = re.compile(r"the following arguments are required: (--[a-z][a-z0-9-]*)(?:,|$)")
# argparse words a message as ``argument NAME: KIND ...`` (NAME is the parser's own definition,
# which never holds ``: ``; text after it may be user text), or as one of two whole-message forms.
# The kind is read only from the start of what follows NAME, never searched for in the message.
_ARGUMENT_KIND = re.compile(r"argument [^:]*: ", re.DOTALL)
_KINDS = (
    ("expected one argument", "an option is missing its value"),
    ("expected at least one argument", "an option is missing its value"),
    ("invalid choice", "invalid choice"),
    ("ignored explicit argument", "an option takes no value"),
    ("not allowed with argument", "these options cannot be combined"),
)


def usage_from_argparse(message: str) -> CliError:
    """The closed-envelope usage error for an argparse message: a fixed text, and ``field`` when
    the message names a long option. The message kind is classified from its structure (its
    start), because the rest of it can hold user text."""
    field: str | None = None
    found = _ARGUMENT.match(message) or _REQUIRED.match(message)
    if found:
        names = found.group(1).split("/")
        field = next((n for n in names if n.startswith("--")), None)
    text = "invalid command line"
    if message.startswith("unrecognized arguments:"):
        text = "unrecognized argument"
    elif message.startswith("the following arguments are required:"):
        text = "a required argument is missing"
    else:
        prefix = _ARGUMENT_KIND.match(message)
        if prefix:
            rest = message[prefix.end() :]
            text = next(
                (fixed for needle, fixed in _KINDS if rest.startswith(needle)), "invalid value"
            )
    return errors.usage(text[:1].upper() + text[1:], field=field, hint=HELP_HINT)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)

    def __call__(self, parser: argparse.ArgumentParser, *_: Any, **__: Any) -> NoReturn:
        assert isinstance(parser, _Parser)
        parser.out.write(f"{PROG} {__version__}\n")
        parser.exit(0)


class _Parser(argparse.ArgumentParser):
    """argparse with the envelope's rules: no abbreviations, fixed usage errors, and every write
    to standard output through ``Output`` (argparse swallows an ``OSError`` there)."""

    def __init__(self, *args: Any, out: Output | None = None, **kwargs: Any) -> None:
        # Abbreviations would change meaning when a later release adds an option.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self.out = out if out is not None else Output()

    def error(self, message: str) -> NoReturn:
        for line in self.format_usage().rstrip("\n").splitlines():
            _stderr(line)
        raise usage_from_argparse(message)

    def print_help(self, file: Any = None) -> None:
        text = self.format_help()
        if file is sys.stderr:
            for line in text.rstrip("\n").splitlines():
                _stderr(line)
        else:
            self.out.write(text)

    def print_usage(self, file: Any = None) -> None:
        text = self.format_usage()
        if file is sys.stderr:
            for line in text.rstrip("\n").splitlines():
                _stderr(line)
        else:
            self.out.write(text)


def _add_global_flags(parser: _Parser, *, top: bool) -> None:
    """The global flags. On a subcommand's parser they default to ``SUPPRESS`` so that a value
    given before the command is not overwritten by the subparser's default."""
    off: dict[str, Any] = {} if top else {"default": argparse.SUPPRESS}
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON output", **off
    )
    config_default: Any = None if top else argparse.SUPPRESS
    parser.add_argument("--config", metavar="PATH", help="the config file", default=config_default)
    parser.add_argument("--verbose", action="store_true", help="print more", **off)
    parser.add_argument("--quiet", action="store_true", help="print less", **off)
    parser.add_argument("--version", action=_VersionAction, help="print the version and exit")


def build_parser(commands: Sequence[Command], out: Output) -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, out=out)
    _add_global_flags(parser, top=True)
    top = parser.add_subparsers(dest="command", metavar="<command>")
    groups: dict[str, Any] = {}
    for command in commands:
        if len(command.path) == 1:
            leaf = top.add_parser(command.path[0], help=command.summary, out=out)
        else:
            first = command.path[0]
            if first not in groups:
                group = top.add_parser(first, help=f"{first} commands", out=out)
                _add_global_flags(group, top=False)
                groups[first] = group.add_subparsers(dest=f"{first}_command", metavar="<command>")
            leaf = groups[first].add_parser(command.path[1], help=command.summary, out=out)
        _add_global_flags(leaf, top=False)
        command.add_arguments(leaf)
        if command.accepts_actor:
            leaf.add_argument(
                "--actor", metavar="ACTOR", help="who runs it, human:<name> or auto:<name>"
            )
        leaf.set_defaults(**{_COMMAND_ATTR: command})
    return parser


def _wants_json(argv: Sequence[str]) -> bool:
    """Whether ``--json`` is among the flags (before any ``--``): for an error found in parsing."""
    for token in argv:
        if token == "--":
            return False
        if token == "--json":
            return True
    return False


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


@contextlib.contextmanager
def _signals() -> Iterator[None]:
    """SIGHUP, SIGINT and SIGTERM raise ``Interrupted``; a signal that was ignored on entry
    (``nohup``, a background job) stays ignored. The first delivery ignores the three again, so a
    second signal while the exit is unwinding cannot raise out of ``main`` (a signal during a
    command's own cleanup is the command's concern). SIGPIPE stays ignored so a closed pipe is an
    ``EPIPE`` error on the write, which ends the run with exit 1. Everything is put back on exit.

    Contract: after the first SIGHUP/SIGINT/SIGTERM the three signals stay ``SIG_IGN`` until
    ``main`` returns, so a command's cleanup is not interrupted by a second signal. A child
    spawned during that cleanup inherits ``SIG_IGN`` for them (ignored dispositions survive
    ``exec``).

    TODO(#28): #28 owns the second-signal policy for command cleanup. It must reset the child
    signal dispositions when spawning (``restore_signals=True`` or a ``preexec_fn``)."""
    previous: dict[int, Any] = {}
    owned: list[int] = []

    def handler(signum: int, _frame: object) -> None:
        # Contract: stay SIG_IGN until main returns (see the docstring; TODO(#28) for children).
        for sig in owned:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, signal.SIG_IGN)
        raise Interrupted(signum)

    try:
        try:
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                current = signal.getsignal(sig)
                if current is signal.SIG_IGN:
                    continue
                previous[sig] = current
                signal.signal(sig, handler)
                owned.append(sig)
            previous[signal.SIGPIPE] = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except ValueError:  # not the main thread: the host owns the signals
            pass
        yield
    finally:
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, TypeError, OSError):
                signal.signal(sig, old)


def _report(error: CliError, *, json_mode: bool, out: Output) -> int:
    """Print the error once: sanitized on stderr, and with ``--json`` as the envelope on stdout."""
    head = f"{PROG}: error: {error.message}"
    for key in ("field", "reason"):
        if key in error.details:
            head += f" ({error.details[key]})"
    _stderr(head)
    matches = error.details.get("matches")
    if matches:
        _stderr("  matches: " + ", ".join(matches))
    if error.hint:
        _stderr(f"  hint: {error.hint}")
    if json_mode:
        out.write(dumps_json(error.envelope()) + "\n")
    return error.exit_code


def _stdout_failed(out: Output) -> int:
    discard_stream(out.stream())
    _stderr(f"{PROG}: error: cannot write to standard output")
    return 1


def _context(
    namespace: argparse.Namespace, command: Command, gateway: Gateway, out: Output, tty: bool
) -> Context:
    ctx = Context(
        gateway=gateway,
        out=out,
        json=bool(namespace.json),
        quiet=bool(namespace.quiet),
        verbose=bool(namespace.verbose),
        config=namespace.config,
        dry_run=bool(getattr(namespace, "dry_run", False)),
        yes=bool(getattr(namespace, "yes", False)),
        stdin_is_tty=tty,
    )
    if ctx.verbose and ctx.quiet:
        raise errors.usage(
            "--verbose and --quiet cannot be combined", field="--quiet", hint="Drop one of them."
        )
    if ctx.config is not None:
        check_path(ctx.config, "--config")
    # The composition root of the config: loaded once, never raising. A broken config is reported
    # by ``_dispatch`` after the command's own checks (Order of checks: flags first, then config).
    loaded = load_config(gateway, os.environ, ctx.config)
    ctx.settings = loaded.settings
    ctx.config_issues = loaded.issues
    ctx.config_error = loaded.error
    if command.accepts_actor:
        given = getattr(namespace, "actor", None)
        ctx.actor = (
            check_actor(given)
            if given is not None
            else default_actor(is_tty=tty, me_name=ctx.settings.me.name)
        )
    return ctx


def _check_config(ctx: Context, command: Command) -> None:
    """The config step of every command: a command that changes something refuses an unsafe
    config (exit 20), any other command reports it on stderr; an invalid config is exit 2 unless
    the command reports it itself (``validate``)."""
    if ctx.config_issues and command.refuses_unsafe_config:
        raise errors.CliError(
            "refused",
            "the config is unsafe",
            details={"reason": "config_unsafe"},
            hint="; ".join(sorted({i.describe() for i in ctx.config_issues}))
            + ". Fix the owner or the mode, and do not use symlinks.",
        )
    if ctx.config_error is not None and command.requires_config:
        raise ctx.config_error
    if ctx.config_issues and command.requires_config:
        for text in sorted({i.describe() for i in ctx.config_issues}):
            ctx.note(f"{PROG}: warning: config_unsafe: {text}")


def _dispatch(
    argv: Sequence[str],
    commands: Sequence[Command],
    gateway: Gateway,
    out: Output,
    tty: bool,
    state: dict[str, bool],
) -> int:
    parser = build_parser(commands, out)
    namespace = parser.parse_args(list(argv))
    state["json"] = bool(namespace.json)
    state["verbose"] = bool(namespace.verbose)
    command: Command | None = getattr(namespace, _COMMAND_ATTR, None)
    if command is None:
        raise errors.usage("A command is required", hint=HELP_HINT)
    ctx = _context(namespace, command, gateway, out, tty)
    try:
        command.check(ctx, namespace)
        _check_config(ctx, command)
        return command.run(ctx, namespace)
    finally:
        ctx.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    gateway: Gateway | None = None,
    commands: Sequence[Command] | None = None,
    stdout: Callable[[], Any] | None = None,
    stdin_is_tty: bool | None = None,
) -> int:
    """Run the CLI and return the exit code, an int from 0 to 255 (never raises).

    ``argv`` defaults to ``sys.argv[1:]``. The keyword arguments are for tests: a fake gateway,
    an own registry, a stream getter for standard output and the terminal state of standard input.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = Output(stdout)
    state = {"json": _wants_json(arguments), "verbose": False}
    tty = _isatty(sys.stdin) if stdin_is_tty is None else stdin_is_tty
    with _signals():
        try:
            return _run(arguments, commands, gateway, out, tty, state)
        except Interrupted as interrupt:
            return errors.interrupt_exit(interrupt.signum)
        except KeyboardInterrupt:
            return errors.interrupt_exit(signal.SIGINT)


def _is_exit_code(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def _run(
    arguments: list[str],
    commands: Sequence[Command] | None,
    gateway: Gateway | None,
    out: Output,
    tty: bool,
    state: dict[str, bool],
) -> int:
    """Dispatch and turn every outcome into an exit code. Only an interrupt escapes."""

    def internal() -> int:
        try:
            return _report(CliError("internal", "internal error"), json_mode=state["json"], out=out)
        except StdoutFailed:
            return _stdout_failed(out)

    try:
        try:
            code = _dispatch(
                arguments,
                COMMANDS if commands is None else commands,
                gateway or SystemGateway(),
                out,
                tty,
                state,
            )
            return code if _is_exit_code(code) else internal()
        except SystemExit as exit_:  # --help and --version
            exit_code = exit_.code
            if exit_code is None:
                return 0
            if isinstance(exit_code, int) and _is_exit_code(exit_code):
                return exit_code
            return internal()
        except CliError as error:
            return _report(error, json_mode=state["json"], out=out)
        except StdoutFailed:
            return _stdout_failed(out)
        except (Interrupted, KeyboardInterrupt):
            raise
        except BaseException:  # Exception and the rest (GeneratorExit, a custom BaseException)
            if state["verbose"]:
                for line in traceback.format_exc().splitlines():
                    _stderr(line)
            return internal()
    except StdoutFailed:  # the envelope itself could not be written
        return _stdout_failed(out)
