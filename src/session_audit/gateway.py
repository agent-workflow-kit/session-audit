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
"""

from __future__ import annotations

import enum
import os
import selectors
import signal
import subprocess
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
        if timeout <= 0 or stdout_limit <= 0:
            raise ValueError("timeout and stdout_limit must be positive")
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
        return self._supervise(popen, stdin, timeout, stdout_limit)

    def _supervise(
        self,
        popen: subprocess.Popen[bytes],
        stdin: str | None,
        timeout: float,
        stdout_limit: int,
    ) -> CallResult:
        deadline = time.monotonic() + timeout
        out = bytearray()
        err = bytearray()
        pending = stdin.encode("utf-8", "replace") if stdin is not None else b""
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
                    detail=f"{failure.value} after {timeout:g}s (stdout cap {stdout_limit})",
                )
            code = popen.returncode
            text = _text(out[:stdout_limit]), _text(err[:stdout_limit])
            if len(out) > stdout_limit:
                return CallResult(CallStatus.OUTPUT_LIMIT, stdout=text[0], stderr=text[1])
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
        """Read what is available; keep at most ``limit`` + 1 bytes (one over marks a breach)."""
        fileno = stream.fileno()
        while True:
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
        _signal_group(pgid, signal.SIGTERM)
        try:
            popen.wait(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            pass
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

    def crontab_read(self) -> CallResult:
        return self.run(["crontab", "-l"], timeout=CRONTAB_TIMEOUT_SECONDS)

    def crontab_write(self, text: str) -> CallResult:
        return self.run(["crontab", "-"], timeout=CRONTAB_TIMEOUT_SECONDS, stdin=text)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _text(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", "replace")
