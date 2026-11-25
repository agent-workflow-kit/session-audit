"""Gateway for session-audit: the only boundary to the outside world.

Every process the tool starts, every file it opens outside its own store, the clock and the
crontab go through this module, so tests substitute fakes (``tests/fake_gateway.py``) and
``tests/test_boundary.py`` can check that no other module reaches around it.

The API is defined here once; later modules extend it only when they need a new kind of access.

- ``run`` starts a program from an argv list, never through a shell. The child leads its own
  process group. A call past its timeout, or one whose stdout passes the cap, gets SIGTERM to the
  group, ``kill_grace`` seconds (5 by default), then SIGKILL to the group. Such a call is a
  failed call: its result is never read as an exit code, so it can never be mistaken for exit 0
  (success) or exit 10 (not found, for claude-session).
- Untrusted or agent-generated text goes in ``stdin`` or in files, never in argv or env.

todo(#28): ``run`` is the only way to start a program, and it returns when the program is gone.
The hooks and the retro need a spawn/handle API instead: start the program, hand back its exact
child PID (to signal that process, not the group), wait up to a given time (30 s for the retro),
then stop it, with a ``kill_grace`` per call rather than per gateway. Add it with #28, together
with a matching fake in ``tests/fake_gateway.py``.
"""

from __future__ import annotations

import enum
import math
import os
import selectors
import signal
import stat as statmod
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, BinaryIO, Protocol

DEFAULT_STDOUT_LIMIT = 8 * 1024 * 1024
KILL_GRACE_SECONDS = 5.0
CRONTAB_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.05
_READ_CHUNK = 65536
_DRAIN_CHUNKS = 16
_GROUP_POLL_SECONDS = 0.02


class CallStatus(enum.Enum):
    EXITED = "exited"  # the program ran to its own exit; only then is ``returncode`` set
    TIMEOUT = "timeout"  # past its timeout: killed by process group
    OUTPUT_LIMIT = "output_limit"  # stdout passed the cap: killed by process group
    SIGNALED = "signaled"  # ended by a signal nobody here sent
    SPAWN_FAILED = "spawn_failed"  # the program could not be started


@dataclass(frozen=True)
class CallResult:
    """What a call produced. ``returncode`` is None unless the status is ``EXITED``, so a failed
    call never compares equal to an exit code."""

    status: CallStatus
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    signal: int | None = None
    detail: str = ""

    @property
    def exited(self) -> bool:
        return self.status is CallStatus.EXITED

    @property
    def ok(self) -> bool:
        """True only for a program that exited with status 0."""
        return self.status is CallStatus.EXITED and self.returncode == 0

    def exit_is(self, *codes: int) -> bool:
        """True only when the program exited on its own with one of ``codes``."""
        return self.status is CallStatus.EXITED and self.returncode in codes


@dataclass(frozen=True)
class FileContent:
    """What ``read_file`` returns: the bytes read (at most ``limit``), the ``fstat`` of the open
    file (the authority on its type, owner and mode: it is the file that was read, not a path
    that may have changed) and ``truncated`` when the file held more than ``limit`` bytes. A file
    that is not a regular file is not read: ``data`` is empty and the caller checks ``stat``."""

    data: bytes
    stat: os.stat_result
    truncated: bool = False


class Gateway(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    ) -> CallResult: ...

    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float: ...

    def open_read(self, path: str | bytes | os.PathLike[str]) -> BinaryIO:
        """Open a file for reading without following a final symlink or blocking on a FIFO.
        The caller checks what it opened (``os.fstat``); nothing is checked here."""
        ...

    def lstat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result: ...

    def list_dir(self, path: str | bytes | os.PathLike[str]) -> list[str]:
        """The entry names of a directory, sorted."""
        ...

    def stat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        """``stat`` that follows symlinks (for a directory the environment names, such as
        ``$HOME``)."""
        ...

    def read_file(self, path: str | bytes | os.PathLike[str], limit: int) -> FileContent:
        """Read at most ``limit`` bytes of a regular file without following a final symlink
        (``OSError``/ELOOP) or blocking on a FIFO. Other failures are the open's ``OSError``."""
        ...

    def uid(self) -> int:
        """The effective user id: whose files count as "owned by the user"."""
        ...

    def machine_time_zone(self) -> str | None:
        """The IANA name of the machine's zone (``TZ``, ``/etc/localtime``, ``/etc/timezone``),
        or None when it cannot be told. The caller validates the name."""
        ...

    def crontab_read(self) -> CallResult:
        """``crontab -l``. Interpreting a non-zero exit (no crontab yet) is the caller's job."""
        ...

    def crontab_write(self, text: str) -> CallResult:
        """``crontab -`` with ``text`` on stdin."""
        ...


class SystemGateway:
    """The real thing."""

    def __init__(self, *, kill_grace: float = KILL_GRACE_SECONDS) -> None:
        self._kill_grace = kill_grace

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    ) -> CallResult:
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise ValueError("argv must be a non-empty list of strings without NUL")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if stdout_limit <= 0:
            raise ValueError("stdout_limit must be positive")
        if env is not None:
            for name, value in env.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(value, str)
                    or not name
                    or "=" in name
                    or "\0" in name
                    or "\0" in value
                ):
                    raise ValueError(
                        "env must map non-empty names (no '=' or NUL) to strings without NUL"
                    )
        # Strict: a lone surrogate is a bug in the caller, refused before anything is started.
        data = stdin.encode("utf-8", "strict") if stdin is not None else None
        try:
            popen = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=None if env is None else dict(env),
                cwd=cwd,
                process_group=0,  # the child leads its own process group (pgid == pid)
                close_fds=True,
            )
        except OSError as exc:
            return CallResult(CallStatus.SPAWN_FAILED, detail=f"{type(exc).__name__}: {exc}")
        return self._supervise(popen, data, timeout, stdout_limit)

    def _supervise(
        self,
        popen: subprocess.Popen[bytes],
        stdin: bytes | None,
        timeout: float,
        stdout_limit: int,
    ) -> CallResult:
        deadline = time.monotonic() + timeout
        out = bytearray()
        err = bytearray()
        pending = stdin if stdin is not None else b""
        selector = selectors.DefaultSelector()
        try:
            assert popen.stdout is not None and popen.stderr is not None
            for stream, buffer in ((popen.stdout, out), (popen.stderr, err)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, (stream, buffer))
            if stdin is not None:
                assert popen.stdin is not None
                os.set_blocking(popen.stdin.fileno(), False)
                if pending:
                    selector.register(popen.stdin, selectors.EVENT_WRITE)
                else:
                    popen.stdin.close()
            failure: CallStatus | None = None
            while True:
                if time.monotonic() >= deadline:
                    failure = CallStatus.TIMEOUT
                    break
                for key, _ in selector.select(_POLL_SECONDS):
                    if key.fileobj is popen.stdin:
                        pending = self._feed(popen, selector, pending)
                    else:
                        stream, buffer = key.data
                        self._drain(stream, selector, buffer, stdout_limit)
                if len(out) > stdout_limit:
                    failure = CallStatus.OUTPUT_LIMIT
                    break
                # todo(#25): when the leader exits normally the rest of its group is left running
                # (only a failed call ends the group). The hooks (``sh -c``) must kill the
                # remaining group members after a normal leader exit; add that with #25.
                if popen.poll() is not None:
                    # The leader is gone: take what is already buffered, then stop reading, so a
                    # grandchild holding a pipe open cannot keep this call waiting.
                    for stream in (popen.stdout, popen.stderr):
                        self._drain(
                            stream, None, out if stream is popen.stdout else err, stdout_limit
                        )
                    break
            if failure is not None:
                self._end_group(popen)
                return CallResult(
                    failure,
                    stdout=_text(out[:stdout_limit]),
                    stderr=_text(err[:stdout_limit]),
                    detail=_detail(failure, timeout, stdout_limit),
                )
            code = popen.returncode
            text = _text(out[:stdout_limit]), _text(err[:stdout_limit])
            if len(out) > stdout_limit:
                return CallResult(
                    CallStatus.OUTPUT_LIMIT,
                    stdout=text[0],
                    stderr=text[1],
                    detail=_detail(CallStatus.OUTPUT_LIMIT, timeout, stdout_limit),
                )
            if code < 0:
                return CallResult(CallStatus.SIGNALED, stdout=text[0], stderr=text[1], signal=-code)
            return CallResult(CallStatus.EXITED, code, text[0], text[1])
        finally:
            selector.close()
            for stream in (popen.stdin, popen.stdout, popen.stderr):
                if stream is not None:
                    stream.close()
            if popen.poll() is None:  # never leave a child behind on an unexpected error
                self._end_group(popen)

    @staticmethod
    def _feed(
        popen: subprocess.Popen[bytes], selector: selectors.BaseSelector, pending: bytes
    ) -> bytes:
        stream = popen.stdin
        assert stream is not None
        try:
            written = os.write(stream.fileno(), pending[:_READ_CHUNK])
        except BlockingIOError:
            return pending
        except BrokenPipeError:
            written = len(pending)  # the child stopped reading; its exit status tells the rest
        pending = pending[written:]
        if not pending:
            selector.unregister(stream)
            stream.close()
        return pending

    @staticmethod
    def _drain(
        stream: IO[bytes],
        selector: selectors.BaseSelector | None,
        buffer: bytearray,
        limit: int,
    ) -> None:
        """Read what is available; keep at most ``limit`` + 1 bytes (one over marks a breach).

        At most ``_DRAIN_CHUNKS`` reads per call, so a program that writes without pause cannot
        keep the caller here and starve its timeout and output-cap checks."""
        fileno = stream.fileno()
        for _ in range(_DRAIN_CHUNKS):
            try:
                chunk = os.read(fileno, _READ_CHUNK)
            except BlockingIOError:
                return
            except OSError:
                chunk = b""
            if not chunk:
                if selector is not None:
                    selector.unregister(stream)
                return
            room = limit + 1 - len(buffer)
            if room > 0:
                buffer.extend(chunk[:room])

    def _end_group(self, popen: subprocess.Popen[bytes]) -> None:
        """SIGTERM the child's process group, wait ``kill_grace`` for the leader, SIGKILL the
        group. The group id is the leader's pid, which is ours until the leader is reaped."""
        pgid = popen.pid
        deadline = time.monotonic() + self._kill_grace
        _signal_group(pgid, signal.SIGTERM)
        try:
            popen.wait(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit on TERM at once while other members still clean up: they get what
        # is left of the grace too. No member left (the common case) means no sleep at all. A
        # group id cannot be reused while a member lives, so polling it is safe.
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(_GROUP_POLL_SECONDS)
        _signal_group(pgid, signal.SIGKILL)
        popen.wait()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def open_read(self, path: str | bytes | os.PathLike[str]) -> BinaryIO:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        return os.fdopen(os.open(path, flags), "rb")

    def lstat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        return os.lstat(path)

    def list_dir(self, path: str | bytes | os.PathLike[str]) -> list[str]:
        return sorted(os.listdir(Path(os.fsdecode(path))))

    def stat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        return os.stat(path)

    def read_file(self, path: str | bytes | os.PathLike[str], limit: int) -> FileContent:
        if limit < 0:
            raise ValueError("limit must not be negative")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not statmod.S_ISREG(info.st_mode):
                return FileContent(b"", info)
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            data = b"".join(chunks)
            return FileContent(data[:limit], info, truncated=len(data) > limit)
        finally:
            os.close(fd)

    def uid(self) -> int:
        return os.geteuid()

    def machine_time_zone(self) -> str | None:
        name = os.environ.get("TZ", "").lstrip(":")
        if name:
            return name
        try:
            target = os.readlink("/etc/localtime")
        except OSError:
            target = ""
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1] or None
        try:
            with open("/etc/timezone", encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
        except OSError:
            return None
        return first or None

    def crontab_read(self) -> CallResult:
        return self.run(["crontab", "-l"], timeout=CRONTAB_TIMEOUT_SECONDS)

    def crontab_write(self, text: str) -> CallResult:
        return self.run(["crontab", "-"], timeout=CRONTAB_TIMEOUT_SECONDS, stdin=text)


def _detail(failure: CallStatus, timeout: float, stdout_limit: int) -> str:
    if failure is CallStatus.OUTPUT_LIMIT:
        return f"stdout cap exceeded (cap {stdout_limit} bytes)"
    return f"{failure.value} after {timeout:g}s"


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    members = _group_member_states(pgid)
    # killpg(pgid, 0) succeeds for a group of unreaped zombies (a container without an init);
    # nothing is left to wait for then. Without /proc, or on any failure, the group is alive.
    return not members or any(state != "Z" for state in members)


def _parse_proc_stat(text: str) -> tuple[str, int] | None:
    """``(state, pgrp)`` from the text of ``/proc/<pid>/stat``, or None when it does not parse.
    The command name is in parentheses and may itself hold spaces and parentheses, so the
    fields are counted from the last ``)``."""
    rest = text[text.rfind(")") + 1 :].split()
    if len(rest) < 3 or not rest[2].lstrip("-").isdigit():
        return None
    return rest[0], int(rest[2])


def _group_member_states(pgid: int, proc: str = "/proc") -> list[str] | None:
    """The states of the processes in group ``pgid`` from ``proc``, or None when ``proc`` cannot
    be read as expected."""
    try:
        names = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return None
    states: list[str] = []
    for name in names:
        try:
            with open(f"{proc}/{name}/stat", encoding="utf-8", errors="replace") as handle:
                parsed = _parse_proc_stat(handle.read())
        except OSError:
            continue  # it exited meanwhile
        if parsed is None:
            return None
        if parsed[1] == pgid:
            states.append(parsed[0])
    return states


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _text(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", "replace")


def discard_stream(stream: IO[str] | None) -> None:
    """Point a standard stream of the process at ``/dev/null`` after a failed write to it, so the
    bytes still buffered in ``stream`` cannot fail again when the interpreter flushes at exit
    (that would turn the exit status into 120 and print a traceback). Does nothing unless
    ``stream`` is the interpreter's own ``sys.__stdout__`` or ``sys.__stderr__`` (a test's capture
    object is left alone). Safe after any write failure: a descriptor that is open and failing is
    only being replaced for a process that is ending, and one that is closed is simply reopened."""
    if stream is None or stream not in (sys.__stdout__, sys.__stderr__):
        return
    try:
        fd = stream.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
    except (OSError, ValueError):
        pass
