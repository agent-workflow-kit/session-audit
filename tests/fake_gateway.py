"""A scriptable gateway for tests of the modules above it: no process, file or clock is real.

    gw = FakeGateway()
    gw.on(["claude-session", "list"], CallResult(CallStatus.EXITED, 0, stdout="{}"))
    gw.clock.advance(60)

``on`` matches a call by the argv prefix; the first matching rule that still has results left
answers (the last result of a rule repeats). An unmatched call fails the test, so no code under
test runs something nobody scripted.
"""

from __future__ import annotations

import errno
import io
import os
import posixpath
import stat as statmod
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from session_audit.gateway import (
    DEFAULT_STDOUT_LIMIT,
    HOOK_STDOUT_LIMIT,
    CallResult,
    CallStatus,
    DirHandle,
    FileContent,
    ProcessHandle,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = EPOCH) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._now = start.astimezone(UTC)
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


@dataclass
class FakeEntry:
    kind: str  # "file", "dir", "symlink", "socket", "fifo" or "device"
    data: bytes = b""
    uid: int = 1000
    mode: int = 0o644
    target: str = ""  # a symlink's target
    nlink: int = 1
    mtime_ns: int = 0
    ino: int = 1


class FakeHandle:
    """A ``ProcessHandle`` of a call that is already over: nothing runs, nothing can be signaled."""

    pid = None

    def __init__(self, result: CallResult) -> None:
        self._result = result
        self.signals: list[int] = []

    def wait(self, timeout: float) -> bool:
        return True

    def signal(self, signum: int) -> bool:
        return False

    def end_group(self, grace: float | None = None) -> None:
        return None

    def result(self) -> CallResult:
        return self._result

    def close(self) -> None:
        return None

    def abandon(self) -> None:
        return None

    def __enter__(self) -> FakeHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeGateway:
    """Besides the scripted programs and the clock, an in-memory file tree (``add_file``,
    ``add_dir``, ``add_symlink``) for the modules that read files through the gateway. Paths are
    absolute and normalized; a lookup is by exact path (a symlink is never followed on the way,
    so a component that is a symlink must be checked by the caller, as the real gateway's
    ``lstat`` allows)."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.files: dict[str, FakeEntry] = {}
        self.user_id = 1000
        self.time_zone: str | None = "UTC"
        self.time_zone_env: Mapping[str, str] | None = None
        self.reads: list[str] = []
        # Hex strings token_hex hands out in order (the last one repeats); empty: a counter.
        self.nonces: list[str] = []
        self._nonce_count = 0
        # The same for token_urlsafe (run tokens): the last one repeats; empty: a counter.
        self.tokens: list[str] = []
        self._token_count = 0
        # Called with the normalized path of every directory or file opened below a root, before
        # the open looks at the tree: a test swaps the entry here to stage a race.
        self.on_open: Callable[[str], None] | None = None
        self._next_ino = 100
        self.calls: list[dict[str, object]] = []
        self.crontab = ""
        self.pid_value = 4242
        self.process_starts: dict[int, str] = {4242: "boot-4242"}  # pid -> start token (running)
        self._rules: list[tuple[tuple[str, ...], list[CallResult]]] = []

    def on(self, prefix: Sequence[str], *results: CallResult) -> None:
        if not results:
            raise ValueError("a rule needs at least one result")
        self._rules.append((tuple(prefix), list(results)))

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
        kill_grace: float | None = None,
        on_start: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> CallResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout, "cwd": cwd})
        for prefix, results in self._rules:
            if tuple(argv[: len(prefix)]) == prefix:
                return results.pop(0) if len(results) > 1 else results[0]
        raise AssertionError(f"unscripted call: {list(argv)}")

    def spawn(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
        kill_grace: float | None = None,
    ) -> ProcessHandle:
        """Scripted like ``run``: the handle is finished at once with the scripted result."""
        return FakeHandle(self.run(argv, timeout=timeout, stdin=stdin, cwd=cwd))

    def run_hook(
        self,
        command: str,
        *,
        timeout: float,
        stdin: str,
        env_extra: Mapping[str, str],
        stdout_limit: int = HOOK_STDOUT_LIMIT,
        cancel: threading.Event | None = None,
    ) -> CallResult:
        """Scripted like ``run``: match with ``on(["/bin/sh", "-c", command], ...)``. The call is
        recorded with ``hook`` set and the extra environment."""
        argv = ["/bin/sh", "-c", command]
        self.calls.append(
            {
                "argv": argv,
                "stdin": stdin,
                "timeout": timeout,
                "cwd": None,
                "hook": True,
                "env_extra": dict(env_extra),
            }
        )
        for prefix, results in self._rules:
            if tuple(argv[: len(prefix)]) == prefix:
                return results.pop(0) if len(results) > 1 else results[0]
        raise AssertionError(f"unscripted hook: {command!r}")

    def now(self) -> datetime:
        return self.clock.now()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def token_hex(self, nbytes: int) -> str:
        if self.nonces:
            return self.nonces.pop(0) if len(self.nonces) > 1 else self.nonces[0]
        self._nonce_count += 1
        return f"{self._nonce_count:0{2 * nbytes}x}"

    def token_urlsafe(self, nbytes: int) -> str:
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or not 12 <= nbytes <= 96:
            raise ValueError("a run token is 12 to 96 random bytes (16 to 128 characters)")
        if self.tokens:
            return self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]
        self._token_count += 1
        length = (nbytes * 4 + 2) // 3  # the length token_urlsafe(nbytes) has
        return f"t{self._token_count}".ljust(length, "A")

    def pid(self) -> int:
        return self.pid_value

    def process_start(self, pid: int) -> str | None:
        return self.process_starts.get(pid)

    # --- the file tree -------------------------------------------------------------------------
    def _new(self, kind: str, path: str, uid: int | None, mode: int, **more: object) -> None:
        self._next_ino += 1
        self.files[path] = FakeEntry(
            kind,
            uid=self.user_id if uid is None else uid,
            mode=mode,
            ino=self._next_ino,
            **more,  # type: ignore[arg-type]
        )

    def add_dir(
        self, path: str, *, uid: int | None = None, mode: int = 0o700, mtime_ns: int = 0
    ) -> None:
        self._new("dir", path, uid, mode, mtime_ns=mtime_ns)

    def add_file(
        self,
        path: str,
        data: bytes | str = b"",
        *,
        uid: int | None = None,
        mode: int = 0o600,
        nlink: int = 1,
        mtime_ns: int = 0,
    ) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self._new("file", path, uid, mode, data=raw, nlink=nlink, mtime_ns=mtime_ns)

    def add_socket(self, path: str, *, uid: int | None = None, mode: int = 0o600) -> None:
        self._new("socket", path, uid, mode)

    def add_fifo(self, path: str, *, uid: int | None = None, mode: int = 0o600) -> None:
        self._new("fifo", path, uid, mode)

    def add_device(self, path: str, *, uid: int | None = None, mode: int = 0o600) -> None:
        self._new("device", path, uid, mode)

    def add_symlink(self, path: str, target: str, *, uid: int | None = None) -> None:
        self._new("symlink", path, uid, 0o777, target=target)

    @staticmethod
    def _result(entry: FakeEntry) -> os.stat_result:
        kind = {
            "file": statmod.S_IFREG,
            "dir": statmod.S_IFDIR,
            "symlink": statmod.S_IFLNK,
            "socket": statmod.S_IFSOCK,
            "fifo": statmod.S_IFIFO,
            "device": statmod.S_IFCHR,
        }
        mode = kind[entry.kind] | entry.mode
        seconds = entry.mtime_ns // 1_000_000_000
        return os.stat_result(
            (
                mode,
                entry.ino,
                1,
                entry.nlink,
                entry.uid,
                entry.uid,
                len(entry.data),
                0,
                seconds,
                0,
                0.0,
                seconds + (entry.mtime_ns % 1_000_000_000) / 1e9,
                0.0,
                0,
                entry.mtime_ns,
                0,
            )
        )

    def _entry(self, path: str | bytes | os.PathLike[str]) -> FakeEntry:
        name = os.fsdecode(path)
        found = self.files.get(posixpath.normpath(name))
        if found is None:
            raise FileNotFoundError(errno.ENOENT, "no such file", name)
        return found

    def open_read(self, path: str | bytes | os.PathLike[str]) -> BinaryIO:
        raise NotImplementedError("transcript reads go through open_root and its handles")

    # --- directory descriptors -----------------------------------------------------------------
    def open_root(self, path: str | bytes) -> DirHandle:
        name = posixpath.normpath(os.fsdecode(path))
        for _ in range(8):  # realpath of the root: a symlink as the last component is followed
            entry = self._entry(name)
            if entry.kind != "symlink":
                break
            name = posixpath.normpath(entry.target)
        else:
            raise OSError(errno.ELOOP, "too many levels of symbolic links")
        if self._entry(name).kind != "dir":
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", name)
        return FakeDir(self, name)

    def fstat(self, handle: BinaryIO) -> os.stat_result:
        return handle.fake_stat  # type: ignore[attr-defined, no-any-return]

    def set_blocking(self, handle: BinaryIO) -> None:
        handle.fake_blocking = True  # type: ignore[attr-defined]

    def lstat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        return self._result(self._entry(path))

    def stat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        entry = self._entry(path)
        for _ in range(8):
            if entry.kind != "symlink":
                return self._result(entry)
            entry = self._entry(entry.target)
        raise OSError(errno.ELOOP, "too many levels of symbolic links")

    def read_file(self, path: str | bytes | os.PathLike[str], limit: int) -> FileContent:
        entry = self._entry(path)
        self.reads.append(posixpath.normpath(os.fsdecode(path)))
        if entry.kind == "symlink":
            raise OSError(errno.ELOOP, "too many levels of symbolic links", os.fsdecode(path))
        info = self._result(entry)
        if entry.kind != "file":
            return FileContent(b"", info)
        return FileContent(entry.data[:limit], info, truncated=len(entry.data) > limit)

    def list_dir(self, path: str | bytes | os.PathLike[str]) -> list[str]:
        base = posixpath.normpath(os.fsdecode(path))
        if self._entry(base).kind != "dir":
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", base)
        names = {
            posixpath.basename(p) for p in self.files if posixpath.dirname(p) == base and p != base
        }
        return sorted(names)

    def uid(self) -> int:
        return self.user_id

    def machine_time_zone(self, env: Mapping[str, str] | None = None) -> str | None:
        self.time_zone_env = env
        return self.time_zone

    def crontab_read(self) -> CallResult:
        return CallResult(CallStatus.EXITED, 0, stdout=self.crontab)

    def crontab_write(self, text: str) -> CallResult:
        self.crontab = text
        return CallResult(CallStatus.EXITED, 0)

    # --- the state directory: scripted by the tests that need it (they use a real directory) ---
    def umask(self, mask: int) -> AbstractContextManager[None]:
        raise NotImplementedError("state directory access is tested against real directories")

    def mkdir(self, path: str | os.PathLike[str], mode: int) -> None:
        raise NotImplementedError("state directory access is tested against real directories")

    def writable(self, path: str | os.PathLike[str]) -> bool:
        raise NotImplementedError("state directory access is tested against real directories")

    def open_private(
        self,
        path: str | os.PathLike[str],
        *,
        create: bool,
        append: bool = False,
        mode: int = 0o600,
        read_only: bool = False,
        exclusive: bool = False,
    ) -> BinaryIO:
        raise NotImplementedError("state directory access is tested against real directories")

    def lock_exclusive(self, handle: BinaryIO, timeout: float) -> bool:
        raise NotImplementedError("state directory access is tested against real directories")

    def replace(self, source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise NotImplementedError("state directory access is tested against real directories")


class _FakeFile(io.BytesIO):
    """What ``FakeDir.open_file`` returns: the bytes, the ``fstat`` taken at open and whether
    ``set_blocking`` ran."""

    fake_stat: os.stat_result
    fake_blocking = False


class FakeDir:
    """A directory descriptor of the in-memory tree (see ``DirHandle``): names are looked up
    below ``path`` and a symlink is never followed."""

    def __init__(self, gateway: FakeGateway, path: str) -> None:
        self._gateway = gateway
        self._path = path
        self.closed = False

    @property
    def path(self) -> bytes:
        return os.fsencode(self._path)

    def _child(self, name: bytes) -> str:
        text = os.fsdecode(name)
        if not text or "/" in text or text in (".", ".."):
            raise ValueError("a name is one path component")
        return posixpath.join(self._path, text)

    def entries(self, limit: int) -> tuple[list[bytes], bool]:
        names = [os.fsencode(n) for n in self._gateway.list_dir(self._path)]
        return names[:limit], len(names) > limit

    def lstat(self, name: bytes) -> os.stat_result:
        return self._gateway.lstat(self._child(name))

    def open_dir(self, name: bytes) -> DirHandle:
        path = self._child(name)
        if self._gateway.on_open:
            self._gateway.on_open(path)
        entry = self._gateway._entry(path)
        if entry.kind == "symlink":
            raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
        if entry.kind != "dir":
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
        return FakeDir(self._gateway, path)

    def open_file(self, name: bytes) -> BinaryIO:
        path = self._child(name)
        if self._gateway.on_open:
            self._gateway.on_open(path)
        entry = self._gateway._entry(path)
        if entry.kind == "symlink":
            raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
        if entry.kind == "dir":
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        if entry.kind == "socket":
            raise OSError(errno.ENXIO, "no such device or address", path)
        self._gateway.reads.append(path)
        handle = _FakeFile(entry.data)
        handle.fake_stat = self._gateway._result(entry)
        return handle  # type: ignore[return-value]

    def stat(self) -> os.stat_result:
        return self._gateway.lstat(self._path)

    def unlink(self, name: bytes) -> None:
        path = self._child(name)
        if self._gateway._entry(path).kind == "dir":
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        del self._gateway.files[path]

    def rmdir(self, name: bytes) -> None:
        path = self._child(name)
        if self._gateway._entry(path).kind != "dir":
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
        if any(posixpath.dirname(p) == path for p in self._gateway.files):
            raise OSError(errno.ENOTEMPTY, "directory not empty", path)
        del self._gateway.files[path]

    def close(self) -> None:
        self.closed = True
