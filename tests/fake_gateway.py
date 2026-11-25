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
import os
import posixpath
import stat as statmod
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from session_audit.gateway import DEFAULT_STDOUT_LIMIT, CallResult, CallStatus, FileContent

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
    kind: str  # "file", "dir", "symlink" or "socket"
    data: bytes = b""
    uid: int = 1000
    mode: int = 0o644
    target: str = ""  # a symlink's target


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
        self.calls: list[dict[str, object]] = []
        self.crontab = ""
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
    ) -> CallResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout, "cwd": cwd})
        for prefix, results in self._rules:
            if tuple(argv[: len(prefix)]) == prefix:
                return results.pop(0) if len(results) > 1 else results[0]
        raise AssertionError(f"unscripted call: {list(argv)}")

    def now(self) -> datetime:
        return self.clock.now()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    # --- the file tree -------------------------------------------------------------------------
    def add_dir(self, path: str, *, uid: int | None = None, mode: int = 0o700) -> None:
        self.files[path] = FakeEntry("dir", uid=self.user_id if uid is None else uid, mode=mode)

    def add_file(
        self, path: str, data: bytes | str = b"", *, uid: int | None = None, mode: int = 0o600
    ) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.files[path] = FakeEntry(
            "file", raw, uid=self.user_id if uid is None else uid, mode=mode
        )

    def add_socket(self, path: str, *, uid: int | None = None, mode: int = 0o600) -> None:
        self.files[path] = FakeEntry("socket", uid=self.user_id if uid is None else uid, mode=mode)

    def add_symlink(self, path: str, target: str, *, uid: int | None = None) -> None:
        self.files[path] = FakeEntry(
            "symlink", uid=self.user_id if uid is None else uid, mode=0o777, target=target
        )

    @staticmethod
    def _result(entry: FakeEntry) -> os.stat_result:
        kind = {
            "file": statmod.S_IFREG,
            "dir": statmod.S_IFDIR,
            "symlink": statmod.S_IFLNK,
            "socket": statmod.S_IFSOCK,
        }
        mode = kind[entry.kind] | entry.mode
        return os.stat_result((mode, 1, 1, 1, entry.uid, entry.uid, len(entry.data), 0, 0, 0))

    def _entry(self, path: str | bytes | os.PathLike[str]) -> FakeEntry:
        name = os.fsdecode(path)
        found = self.files.get(posixpath.normpath(name))
        if found is None:
            raise FileNotFoundError(errno.ENOENT, "no such file", name)
        return found

    def open_read(self, path: str | bytes | os.PathLike[str]) -> BinaryIO:
        raise NotImplementedError(
            "transcript reads are scripted by the issue that first needs them"
        )

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

    def open_private(
        self, path: str | os.PathLike[str], *, create: bool, append: bool = False, mode: int = 0o600
    ) -> BinaryIO:
        raise NotImplementedError("state directory access is tested against real directories")

    def lock_exclusive(self, handle: BinaryIO, timeout: float) -> bool:
        raise NotImplementedError("state directory access is tested against real directories")
