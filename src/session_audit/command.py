"""The command registry's types: ``Command``, the ``Context`` a command runs with and ``Output``.

A command is one ``Command`` value in its own module under ``session_audit/commands/``. The
module is already registered in ``commands/__init__.py``; the issue that implements the command
replaces the module's ``run`` (and extends ``check``) and touches no other file of the CLI.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .gateway import Gateway, discard_stream
from .textsafe import dumps_json, show, write_stderr

if TYPE_CHECKING:
    from .config import ConfigIssue, Settings
    from .errors import CliError
    from .interrupt import Signals
    from .store import Store


class StdoutFailed(Exception):
    """Standard output could not be written (closed, a broken pipe, or any I/O error). The run
    ends with exit 1."""


def stderr_line(line: str) -> None:
    """One sanitized line on stderr. A closed, full or broken stderr is not an error worth dying
    for: the stream is pointed at /dev/null (``discard_stream``) so the exit flush cannot fail."""
    try:
        write_stderr(line)
    except (OSError, ValueError, AttributeError):
        discard_stream(sys.stderr)


class Output:
    """The only way to write standard output: every failure is a ``StdoutFailed``."""

    def __init__(self, stream: Callable[[], Any] | None = None) -> None:
        self._stream = stream or (lambda: sys.stdout)

    def stream(self) -> Any:
        return self._stream()

    def write(self, text: str) -> None:
        stream = self._stream()
        if stream is None:
            raise StdoutFailed("standard output is closed")
        try:
            try:
                stream.write(text)
            except UnicodeEncodeError:
                # An output encoding that cannot hold the (sanitized) text: degrade, never crash.
                encoding = getattr(stream, "encoding", None) or "ascii"
                stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            stream.flush()
        except (OSError, ValueError) as exc:  # EPIPE, EBADF, "I/O operation on closed file"
            raise StdoutFailed(type(exc).__name__) from None


def _default_settings() -> Settings:
    from .config import Settings  # lazy: config imports args, which imports this module

    return Settings()


@dataclass
class Context:
    """What a command runs with: the gateway (clock, processes, files), the global flags and the
    output. ``actor`` is set only for a command that accepts ``--actor``."""

    gateway: Gateway
    out: Output
    json: bool = False
    quiet: bool = False
    verbose: bool = False
    config: str | None = None
    dry_run: bool = False
    yes: bool = False
    actor: str | None = None
    stdin_is_tty: bool = False
    # The loaded config (the built-in defaults when it is invalid), its ownership problems and
    # the error a command reports (set by ``cli._context``).
    settings: Settings = field(default_factory=_default_settings)
    config_issues: tuple[ConfigIssue, ...] = ()
    config_error: CliError | None = None
    # The state directory; None means the standard location (``store.state_dir``). Tests and
    # embedders may set it; nothing else does.
    state_directory: str | None = None
    _store: Store | None = field(default=None, init=False, repr=False)
    # A dry run of ``retro run`` reads and ingests a private in-memory copy of the store (its
    # ``open_memory_copy``): when set, every read of the store (``sessionquery.open_reader``,
    # ``reader.discover``) uses it and the real store is never opened for writing.
    memory: Store | None = field(default=None, repr=False)
    # The signal controller of the run (``interrupt.Signals``, set by ``cli.main``); a command
    # that must end in an orderly way (``retro run``) defers the first signal and polls it.
    interrupts: Signals | None = field(default=None, repr=False)

    def state_path(self) -> str:
        """The state directory: ``state_directory`` or the standard location."""
        from .config import home_dir  # lazy: config imports args, which imports this module
        from .store import state_dir

        return self.state_directory or state_dir(os.environ, home_dir(os.environ))

    def store(self) -> Store:
        """The store, opened on first use (migrations applied) and closed by ``close``. Opening
        can raise ``CliError`` (``store_unwritable``, ``store_newer``); a command that never calls
        this (``--version``, ``validate``, usage errors) never touches the store."""
        if self._store is None:
            from .store import open_store

            self._store = open_store(self.gateway, self.state_path())
        return self._store

    def close(self) -> None:
        """Close the store (and the in-memory copy) if opened. Safe to call twice."""
        if self.memory is not None:
            self.memory.close()
            self.memory = None
        if self._store is not None:
            self._store.close()
            self._store = None

    def emit_json(self, document: Any) -> None:
        """Print one ``--json`` document (ASCII, lone surrogates fixed, no NaN) and a newline."""
        self.out.write(dumps_json(document) + "\n")

    def say(self, text: str) -> None:
        """Print human text on stdout, each line through the sanitizer."""
        for line in text.split("\n"):
            self.out.write(show(line) + "\n")

    def note(self, text: str) -> None:
        """Print a progress line on stderr unless ``--quiet``."""
        if not self.quiet:
            stderr_line(text)


@dataclass(frozen=True)
class Command:
    """One command of the interface.

    ``path`` is the words that select it (``("retro", "run")``). ``add_arguments`` adds its own
    flags (the global flags are added for every command). ``check`` makes the checks that need
    more than one flag or the clock (ranges, combinations, confirmation) and raises ``CliError``;
    it runs before ``run`` and must not read the config or the store. ``run`` returns the exit
    code. ``accepts_actor`` adds ``--actor`` (commands that change the store).
    """

    path: tuple[str, ...]
    summary: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    check: Callable[[Context, argparse.Namespace], None]
    run: Callable[[Context, argparse.Namespace], int]
    accepts_actor: bool = False
    # False only for a command that reports a broken config itself (``validate``).
    requires_config: bool = True
    # True for the commands that change something: they exit 20 ``config_unsafe`` on any
    # ownership issue of the config; the others report it on stderr.
    refuses_unsafe_config: bool = False
