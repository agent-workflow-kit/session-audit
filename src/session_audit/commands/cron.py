"""``cron install`` and ``cron uninstall``: the managed crontab block (interface, Cron).

The tool owns exactly one block of the user's crontab, the lines from ``# session-audit managed``
to ``# session-audit managed end``. Everything outside it is carried through byte for byte (the
crontab is split on ``\\n`` only, and the gateway keeps bytes that are not UTF-8), so a
malformed block (anything but exactly one start line followed later by exactly one end line) is
refused with nothing changed. Every value that reaches a cron line is single-quoted and refused
(exit 2, naming its key) when it holds ``%`` (cron turns it into a newline), a control character
or a lone surrogate. Untrusted text never travels in argv or the environment: the crontab goes
to ``crontab -`` on stdin, through the gateway.
"""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..config import ScheduleRetro, home_dir
from ..errors import CliError
from ..gateway import CallResult, CallStatus, Gateway
from ..grammar import GrammarError, IngestEvery, parse_ago, parse_ingest_every
from ..textsafe import sanitize

START = "# session-audit managed"
END = "# session-audit managed end"
PRUNE_SCHEDULE = "17 3 * * *"
RETRO_MINUTE = 23
RETRO_ACTOR = "auto:schedule"
# Directories every cron job needs anyway (cron's own default is /usr/bin:/bin).
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
TOOLS_ON_PATH = ("claude-session", "claude")
# Many cron implementations truncate or drop a line past 1000 bytes.
LINE_MAX = 1000
SCOPE_FIELD = "schedule.retro.scope"
INTERVAL_KEY = "ingest_interval"
DEFAULT_INTERVAL = IngestEvery(60)
OUTPUT_LINES_MAX = 64  # schemas/cli/cron.json
OUTPUT_LINE_CHARS = 4096


def malformed() -> CliError:
    return CliError(
        "refused",
        "the managed cron block is malformed",
        details={"reason": "cron_block_malformed"},
        hint=(
            f"The crontab needs exactly one '{START}' line followed later by exactly one "
            f"'{END}' line, or neither. Fix it by hand; nothing was changed."
        ),
    )


def unavailable(result: CallResult | None = None) -> CliError:
    retryable = result is not None and result.status in (CallStatus.TIMEOUT, CallStatus.SIGNALED)
    return CliError(
        "failed",
        "the crontab is not available",
        details={"reason": "crontab_unavailable", "retryable": retryable},
        hint="Check that crontab is installed and that you may use it. Nothing was changed.",
    )


# --- values that reach a cron line ------------------------------------------------------------


# Code points the sanitized ``block`` output would show differently from the text written: the
# line and paragraph separators and the bidirectional controls.
_UNSAFE_CODE_POINTS = frozenset(
    {0x2028, 0x2029, 0x061C, 0x200E, 0x200F}
    | set(range(0x202A, 0x202F))
    | set(range(0x2066, 0x206A))
)


def check_line_value(value: str, field: str) -> str:
    """Refuse a value that cannot sit in a cron line: ``%``, any control character (newline, CR,
    NUL, tab, DEL, the C1 range), U+2028/2029, a bidirectional control or a lone surrogate (it
    cannot be written)."""
    for ch in value:
        code = ord(ch)
        if (
            ch == "%"
            or code < 0x20
            or 0x7F <= code <= 0x9F
            or code in _UNSAFE_CODE_POINTS
            or "\ud800" <= ch <= "\udfff"
        ):
            raise errors.usage(
                "a value cannot be written into a cron line",
                field=field,
                hint=(
                    "It must not contain %, a newline, NUL or another control character, a line "
                    "separator or a bidirectional control."
                ),
            )
    return value


def check_scope_value(value: str) -> str:
    """A ``schedule.retro.scope`` value: writable, and not starting with ``-`` (``retro run``
    would read it as the next option)."""
    check_line_value(value, SCOPE_FIELD)
    if value.startswith("-"):
        raise errors.usage(
            "a scope value cannot start with a dash",
            field=SCOPE_FIELD,
            hint="A value that starts with - would be read as an option by retro run.",
        )
    return value


def quote(value: str) -> str:
    """POSIX single-quote ``value`` (always quoted, so a line reads the same for every value)."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def check_env_file(gateway: Gateway, path: str) -> str:
    """Absolute, a regular file, owned by the user, not group or other writable, not a symlink
    (checked on the file itself, without following it), in a directory that is not group or
    other writable (unless it has the sticky bit). Every failure is exit 2 naming
    ``--env-file``; ``validate`` re-checks it later."""
    field = "--env-file"
    a.check_path(path, field)
    check_line_value(path, field)
    if not path.startswith("/"):
        raise errors.usage("the env file must be an absolute path", field=field)
    try:
        info = gateway.lstat(path)
    except OSError:
        raise errors.usage("the env file cannot be read", field=field) from None
    if stat.S_ISLNK(info.st_mode):
        raise errors.usage("the env file is a symlink", field=field)
    if not stat.S_ISREG(info.st_mode):
        raise errors.usage("the env file is not a regular file", field=field)
    if info.st_uid != gateway.uid():
        raise errors.usage("the env file is not owned by the user", field=field)
    if info.st_mode & 0o022:
        raise errors.usage("the env file is group or other writable", field=field)
    try:
        parent = gateway.lstat(os.path.dirname(path) or "/")
    except OSError:
        raise errors.usage("the env file's directory cannot be read", field=field) from None
    if parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise errors.usage("the env file's directory is group or other writable", field=field)
    return path


def path_value(gateway: Gateway, env_path: str, home: str) -> str:
    """``PATH`` for the cron lines: ``~/.local/bin``, then the directory of the first
    ``claude-session`` and of the first ``claude`` on the installing process's ``PATH``, then
    the usual system directories."""
    directories = [os.path.join(home, ".local", "bin")]
    for tool in TOOLS_ON_PATH:
        for directory in env_path.split(os.pathsep):
            if not directory.startswith("/"):
                continue
            try:
                info = gateway.stat(os.path.join(directory, tool))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                directories.append(os.path.normpath(directory))
                break
    directories.extend(SYSTEM_PATH)
    unique = list(dict.fromkeys(directories))
    for directory in unique:
        check_line_value(directory, "PATH")
    return os.pathsep.join(unique)


def on_path(gateway: Gateway, path: str, program: str) -> bool:
    """Whether a directory of ``path`` holds an executable regular file named ``program``."""
    for directory in path.split(os.pathsep):
        try:
            info = gateway.stat(os.path.join(directory, program))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
            return True
    return False


def ingest_schedule(interval: IngestEvery) -> str:
    minutes = interval.minutes
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = minutes // 60
    return "7 3 * * *" if hours == 24 else f"7 */{hours} * * *"


def retro_flags(retro: ScheduleRetro) -> list[str]:
    """The ``retro run`` scope flags of ``schedule.retro.scope``, each value quoted. The window
    is checked again here (the config loader already did): ``until`` must be a shorter time ago
    than ``since``."""
    scope = retro.scope
    try:
        since = parse_ago(scope.since, field=SCOPE_FIELD)
        if scope.until is not None and parse_ago(scope.until, field=SCOPE_FIELD) >= since:
            raise errors.usage("the scope window is empty", field=SCOPE_FIELD)
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    flags: list[str] = []
    for label in scope.labels:
        flags += ["--label", quote(check_scope_value(label))]
    pairs = (
        ("--project", scope.project),
        ("--since", scope.since),
        ("--until", scope.until),
        ("--kind", scope.kind),
        ("--outcome", scope.outcome),
    )
    for flag, value in pairs:
        if value is not None:
            flags += [flag, quote(check_scope_value(value))]
    return flags


def line_bytes(line: str) -> int:
    """The size of ``line`` in bytes as the crontab holds it (cron's limit is in bytes)."""
    return len(line.encode("utf-8", "surrogateescape"))


def build_block(
    interval: IngestEvery,
    retro: ScheduleRetro | None,
    path: str,
    env_file: str | None,
) -> list[str]:
    """The managed block's lines, start line to end line."""
    prefix = f"PATH={quote(path)} session-audit"
    lines = [
        (f"{ingest_schedule(interval)} {prefix} ingest --quiet", "PATH"),
        (f"{PRUNE_SCHEDULE} {prefix} prune --quiet", "PATH"),
    ]
    if retro is not None:
        flags = " ".join(retro_flags(retro))
        command = f"{prefix} retro run {flags} --actor {RETRO_ACTOR} --quiet"
        if env_file is not None:
            command = f". {quote(env_file)} && {command}"
        # The line's largest variable part is the one to blame when it is too long.
        parts = {
            SCOPE_FIELD: line_bytes(flags),
            "--env-file": 0 if env_file is None else line_bytes(quote(env_file)),
            "PATH": line_bytes(path),
        }
        field = max(parts, key=lambda name: parts[name])
        lines.append((f"{RETRO_MINUTE} {retro.hour} * * {retro.weekday} {command}", field))
    for line, field in lines:
        if line_bytes(line) > LINE_MAX:
            raise errors.usage(
                "a cron line is too long",
                field=field,
                hint=(
                    f"A cron line may hold {LINE_MAX} bytes. Shorten {field} (the largest part); "
                    f"{SCOPE_FIELD}, --env-file and PATH all count toward it."
                ),
            )
    return [START, *(line for line, _ in lines), END]


# --- the crontab text -------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """A crontab cut at its managed block: ``before`` and ``after`` are the elements of
    ``text.split("\\n")`` outside it (so the last element of ``after`` is ``""`` when the text
    ended with a newline), ``block`` the lines from the start line to the end line, none when
    there is no block."""

    before: list[str]
    block: list[str] | None
    after: list[str]


def split_crontab(text: str) -> Split:
    """Find the block. Only a line exactly equal to a marker counts (``\\n`` is the only line
    break; a CR, trailing space or a marker inside another line is ordinary text). Exactly one
    start line followed later by exactly one end line, or neither; anything else raises
    ``cron_block_malformed``."""
    elements = text.split("\n")
    starts = [i for i, line in enumerate(elements) if line == START]
    ends = [i for i, line in enumerate(elements) if line == END]
    if not starts and not ends:
        return Split(elements, None, [])
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        raise malformed()
    first, last = starts[0], ends[0]
    return Split(elements[:first], elements[first : last + 1], elements[last + 1 :])


def join_crontab(before: list[str], block: list[str], after: list[str]) -> str:
    """``before``, ``block`` and ``after`` as crontab text that ends with a newline."""
    text = "\n".join([*before, *block, *after])
    return text if text.endswith("\n") or not text else text + "\n"


def install_text(text: str, block: list[str]) -> tuple[str, str]:
    """``(new text, outcome)`` of installing ``block`` into the crontab ``text``."""
    split = split_crontab(text)
    if split.block is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n".join(block) + "\n", "installed"
    if split.block == block:
        return text, "unchanged"
    return join_crontab(split.before, block, split.after), "replaced"


def uninstall_text(text: str) -> tuple[str, str, list[str]]:
    """``(new text, outcome, removed lines)`` of removing the block from ``text``."""
    split = split_crontab(text)
    if split.block is None:
        return text, "unchanged", []
    # Each line before the block keeps its newline (the start line begins a line), so a block whose
    # end line is the last, unterminated line leaves the text before it complete.
    head = "".join(f"{line}\n" for line in split.before)
    return head + "\n".join(split.after), "removed", split.block


def read_crontab(gateway: Gateway) -> str:
    """The user's crontab text; ``""`` when there is none. ``crontab -l`` exits 1 with nothing on
    stdout when there is no crontab (the message names it, or is absent); any other failure is
    exit 12 ``crontab_unavailable``."""
    result = gateway.crontab_read()
    if result.ok:
        return result.stdout
    if result.exit_is(1) and not result.stdout:
        message = result.stderr.strip().lower()
        if not message or "no crontab" in message:
            return ""
    raise unavailable(result)


def write_crontab(gateway: Gateway, text: str) -> None:
    result = gateway.crontab_write(text)
    if not result.ok:
        raise unavailable(result)


# --- the commands -----------------------------------------------------------------------------


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ingest-every", metavar="INTERVAL", help="the ingest interval (default 1h)"
    )
    parser.add_argument("--env-file", metavar="PATH", help="an environment file for the retro line")
    a.add_dry_run(parser)


def check_install(ctx: Context, args: argparse.Namespace) -> None:
    if args.ingest_every is not None:
        try:
            args.ingest_every = parse_ingest_every(args.ingest_every, field="--ingest-every")
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
    if args.env_file is not None:
        check_env_file(ctx.gateway, args.env_file)


def add_uninstall_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_dry_run(parser)


def check_uninstall(ctx: Context, args: argparse.Namespace) -> None:
    return None


def report(ctx: Context, command: str, outcome: str, block: list[str]) -> None:
    shown = [sanitize(line, OUTPUT_LINE_CHARS) for line in block[:OUTPUT_LINES_MAX]]
    if ctx.json:
        ctx.emit_json(
            {"command": command, "outcome": outcome, "dry_run": ctx.dry_run, "block": shown}
        )
        return
    if ctx.quiet:
        return
    suffix = " (dry run)" if ctx.dry_run else ""
    ctx.say(f"cron {command}: {outcome}{suffix}")
    if ctx.dry_run:
        for line in shown:
            ctx.say(f"  {line}")


def run_install(ctx: Context, args: argparse.Namespace) -> int:
    interval: IngestEvery = args.ingest_every or DEFAULT_INTERVAL
    retro = ctx.settings.schedule.retro
    if args.env_file is not None and retro is None:
        ctx.note("session-audit: note: --env-file is used only by the retro line; none is set")
    path = path_value(ctx.gateway, os.environ.get("PATH", ""), home_dir(os.environ))
    block = build_block(interval, retro, path, args.env_file)
    if not on_path(ctx.gateway, path, "session-audit"):
        ctx.note(
            "session-audit: note: no directory of the cron PATH holds an executable "
            "session-audit; the scheduled jobs will not find it"
        )
    # The store opens first, so an unwritable state directory fails before the crontab changes.
    store = None if ctx.dry_run else ctx.store()
    text = read_crontab(ctx.gateway)
    new_text, outcome = install_text(text, block)
    if not ctx.dry_run:
        if outcome != "unchanged":
            write_crontab(ctx.gateway, new_text)
        assert store is not None
        try:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (INTERVAL_KEY, interval.canonical),
                )
        except CliError as exc:
            if outcome == "unchanged":
                raise
            raise CliError(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
                details=exc.details,
                hint=(
                    "The crontab was already updated; run the command again to record the "
                    "interval. " + (exc.hint or "")
                ).strip(),
            ) from None
    report(ctx, "install", outcome, block)
    return 0


def run_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    text = read_crontab(ctx.gateway)
    new_text, outcome, removed = uninstall_text(text)
    if outcome == "removed" and not ctx.dry_run:
        write_crontab(ctx.gateway, new_text)
    report(ctx, "uninstall", outcome, removed)
    return 0


COMMANDS = (
    Command(
        ("cron", "install"),
        "install the managed cron block",
        add_install_arguments,
        check_install,
        run_install,
        refuses_unsafe_config=True,
    ),
    Command(
        ("cron", "uninstall"),
        "remove the managed cron block",
        add_uninstall_arguments,
        check_uninstall,
        run_uninstall,
        refuses_unsafe_config=True,
    ),
)
