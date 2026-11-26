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

import contextlib
import enum
import fcntl
import math
import os
import re
import selectors
import signal
import stat as statmod
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, BinaryIO, Protocol
from zoneinfo import ZoneInfo

DEFAULT_STDOUT_LIMIT = 8 * 1024 * 1024
KILL_GRACE_SECONDS = 5.0
CRONTAB_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.05
_READ_CHUNK = 65536
_DRAIN_CHUNKS = 16
_GROUP_POLL_SECONDS = 0.02
_LOCK_POLL_SECONDS = 0.01
_PS_TIMEOUT_SECONDS = 5.0


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

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` (a bounded poll between retries)."""
        ...

    def pid(self) -> int:
        """This process's id."""
        ...

    def process_start(self, pid: int) -> str | None:
        """An opaque token for when process ``pid`` started (at most 64 characters, equal for
        the same process at any time, different for a later process that reuses the pid), or
        None when no such process is running (a zombie is not running). Linux reads
        ``/proc/<pid>/stat``; without ``/proc`` (macOS) ``ps`` answers. ``OSError`` when neither
        can."""
        ...

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

    def machine_time_zone(self, env: Mapping[str, str] | None = None) -> str | None:
        """The IANA name of the machine's zone: ``TZ`` from ``env`` (the process environment when
        None) when it names a zone, else ``/etc/localtime``, else ``/etc/timezone``; None when no
        candidate is a valid zone name (a POSIX ``TZ`` such as ``EST5EDT`` is not one)."""
        ...

    def crontab_read(self) -> CallResult:
        """``crontab -l``. Interpreting a non-zero exit (no crontab yet) is the caller's job."""
        ...

    def crontab_write(self, text: str) -> CallResult:
        """``crontab -`` with ``text`` on stdin."""
        ...

    # --- the state directory (the store's own files) ------------------------------------------
    # sqlite3 opens ``state.db`` itself (the boundary test allows it); everything else the store
    # needs from the file system (directories, ``log.jsonl``, ``log.lock``) goes through these.

    def umask(self, mask: int) -> AbstractContextManager[None]:
        """Set the process umask to ``mask`` for the duration of the block."""
        ...

    def mkdir(self, path: str | os.PathLike[str], mode: int) -> None:
        """Create one directory with exactly ``mode`` whatever the umask (``FileExistsError`` when
        it exists, even as a symlink). A parent that is missing is an error."""
        ...

    def open_private(
        self,
        path: str | os.PathLike[str],
        *,
        create: bool,
        append: bool = False,
        mode: int = 0o600,
        read_only: bool = False,
    ) -> BinaryIO:
        """Open a regular file read-write (read-only with ``read_only``, which excludes ``create``
        and ``append``), unbuffered, never following a final symlink and never blocking on a FIFO.
        With ``create`` a missing file is created with exactly ``mode`` whatever the umask; an
        existing one is opened as it is. With ``append`` every write goes to the end. The caller
        checks what it opened (``os.fstat`` on the handle)."""
        ...

    def lock_exclusive(self, handle: BinaryIO, timeout: float) -> bool:
        """Take an exclusive ``flock`` on ``handle``, waiting at most ``timeout`` seconds; False
        when it was not obtained. The lock ends when ``handle`` is closed."""
        ...


class SystemGateway:
    """The real thing."""

    def __init__(self, *, kill_grace: float = KILL_GRACE_SECONDS, proc: str = "/proc") -> None:
        self._kill_grace = kill_grace
        self._proc = proc  # tests point it at a missing directory to exercise the ps fallback

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

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def pid(self) -> int:
        return os.getpid()

    def process_start(self, pid: int) -> str | None:
        if pid <= 0:
            return None
        found = _proc_start(self._proc, pid)
        if found is not _NO_PROC:
            return found
        return self._ps_start(pid)

    def _ps_start(self, pid: int) -> str | None:
        result = self.run(
            ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
            timeout=_PS_TIMEOUT_SECONDS,
            env={"LC_ALL": "C", "PATH": os.environ.get("PATH", os.defpath)},
        )
        if result.exit_is(1) and not result.stdout.strip() and not result.stderr.strip():
            return None  # ps: no such process (exit 1 and nothing said)
        if not result.ok:
            raise OSError(f"cannot read the start time of process {pid} ({result.status.value})")
        return _parse_ps_start(result.stdout)

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

    LOCALTIME = "/etc/localtime"
    TIMEZONE_FILE = "/etc/timezone"

    def machine_time_zone(self, env: Mapping[str, str] | None = None) -> str | None:
        source = os.environ if env is None else env
        candidates = [source.get("TZ", "").lstrip(":")]
        try:
            target = os.readlink(self.LOCALTIME)
        except OSError:
            target = ""
        marker = "zoneinfo/"
        if marker in target:
            candidates.append(target.split(marker, 1)[1])
        try:
            with open(self.TIMEZONE_FILE, encoding="utf-8", errors="replace") as handle:
                candidates.append(handle.readline().strip())
        except OSError:
            pass
        for name in candidates:
            if name and _is_zone(name):
                return name
        return None

    def crontab_read(self) -> CallResult:
        return self.run(["crontab", "-l"], timeout=CRONTAB_TIMEOUT_SECONDS)

    def crontab_write(self, text: str) -> CallResult:
        return self.run(["crontab", "-"], timeout=CRONTAB_TIMEOUT_SECONDS, stdin=text)

    @contextlib.contextmanager
    def umask(self, mask: int) -> Iterator[None]:
        # The umask is process-wide: one thread at a time may change and restore it, or a second
        # thread would save the first one's temporary value and restore that.
        with _UMASK_LOCK:
            previous = os.umask(mask)
            try:
                yield
            finally:
                os.umask(previous)

    def mkdir(self, path: str | os.PathLike[str], mode: int) -> None:
        # umask 0 for the call: a umask such as 0777 would create the directory with no permission
        # bits, and it could then not be opened to be fixed. The mode is then set exactly below.
        with _UMASK_LOCK:
            previous = os.umask(0)
            try:
                os.mkdir(path, mode)
            finally:
                os.umask(previous)
        # The mode asked for, not the mode the umask left: set through a descriptor that does not
        # follow a symlink, so a path swapped after the mkdir is never changed.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def open_private(
        self,
        path: str | os.PathLike[str],
        *,
        create: bool,
        append: bool = False,
        mode: int = 0o600,
        read_only: bool = False,
    ) -> BinaryIO:
        if read_only and (create or append):
            raise ValueError("a read-only open cannot create or append")
        flags = (os.O_RDONLY if read_only else os.O_RDWR) | os.O_NOFOLLOW
        flags |= os.O_NONBLOCK | os.O_CLOEXEC
        if append:
            flags |= os.O_APPEND
        fd = -1
        created = False
        if create:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, mode)
                created = True
            except FileExistsError:
                pass
        try:
            if fd < 0:
                fd = os.open(path, flags)
            if created:
                os.fchmod(fd, mode)
            return os.fdopen(fd, "rb" if read_only else "r+b", buffering=0)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise

    def lock_exclusive(self, handle: BinaryIO, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL_SECONDS)


_UMASK_LOCK = threading.RLock()
_SIGNED_INT = re.compile(r"-?[0-9]{1,19}")
_ASCII_TICKS = re.compile(r"[0-9]{1,19}")


def _is_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:  # not found, a malformed key, an unreadable file: not a usable name
        return False
    return True


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
    if len(rest) < 3 or not _SIGNED_INT.fullmatch(rest[2]):
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


_NO_PROC: Any = object()  # sentinel: /proc cannot answer, ask ps


def _parse_proc_start(text: str) -> tuple[str, str] | None:
    """``(state, starttime)`` from the text of ``/proc/<pid>/stat`` (field 22, clock ticks since
    boot), or None when it does not parse. Fields are counted from the last ``)``."""
    rest = text[text.rfind(")") + 1 :].split()
    if len(rest) < 20 or not _ASCII_TICKS.fullmatch(rest[19]):
        return None
    return rest[0], rest[19]


def _proc_start(proc: str, pid: int) -> str | None:
    """The start-time token of a running process read from ``proc``, None when it is not running
    (missing or a zombie), ``_NO_PROC`` when ``proc`` cannot answer. The token carries the boot id
    when readable, so a pid and tick count repeated after a reboot do not match."""
    try:
        with open(f"{proc}/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            parsed = _parse_proc_start(handle.read())
    except (FileNotFoundError, ProcessLookupError):
        # A missing entry means no such process only when /proc itself is there.
        return None if os.path.isdir(proc) and os.path.exists(f"{proc}/self") else _NO_PROC
    except OSError:
        return _NO_PROC
    if parsed is None:
        return _NO_PROC
    state, ticks = parsed
    if state in ("Z", "X"):
        return None
    try:
        with open(f"{proc}/sys/kernel/random/boot_id", encoding="ascii") as handle:
            boot = handle.read().strip().replace("-", "")[:8]
    except (OSError, ValueError) as exc:
        # Never a token of another shape: a holder that recorded "<boot>-<ticks>" would look dead.
        raise OSError(f"cannot read the boot id to identify process {pid}") from exc
    if not boot:
        raise OSError(f"the boot id is empty, cannot identify process {pid}")
    return f"{boot}-{ticks}"


def _parse_ps_start(text: str) -> str | None:
    """The start time from ``ps -o stat= -o lstart=`` output (``Ss   Tue Nov 25 10:00:00 2026``);
    None for a zombie. Output that holds no state and start time raises ``OSError``: ps failing
    with output is an error, not proof that the process is dead."""
    parts = text.strip().split(None, 1)
    if len(parts) != 2:
        raise OSError(f"cannot parse the ps output {text.strip()[:80]!r}")
    if parts[0][:1] in ("Z", "X"):
        return None
    return " ".join(parts[1].split())


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
