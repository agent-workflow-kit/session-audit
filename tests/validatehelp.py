"""Helpers for the ``validate`` tests (issue #33): a gateway with a scripted claude-session and
crontab that fails the test on a hook or a write, a private install, and a snapshot of a directory
that proves a run changed nothing."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from session_audit.cli import main
from session_audit.gateway import CallResult, CallStatus, SystemGateway
from session_audit.store import open_store

from .fake_claude_session import FakeClaudeSession
from .ingesthelp import Corpus, Run, connect, state_dir_of
from .storehelp import ClockedGateway

VERSION_OK = "claude-session 0.45.3\n"
CS = "claude-session"


class ValidateGateway(ClockedGateway):
    """The real file system and clock-less gateway of the store tests, with a scripted
    claude-session (``version``, ``kinds`` or, with ``fake``, the scripted fake program) and a
    scripted crontab. A hook, a spawn or a crontab write fails the test."""

    def __init__(self) -> None:
        super().__init__()
        self.version: CallResult | None = CallResult(CallStatus.EXITED, 0, stdout=VERSION_OK)
        self.kinds: CallResult | None = None
        self.fake: FakeClaudeSession | None = None
        self.crontab: str | None = None
        self.crontab_result: CallResult | None = None
        self.runs: list[list[str]] = []
        self.process_start_error: OSError | None = None

    def run(self, argv: Any, **kwargs: Any) -> CallResult:
        words = [str(a) for a in argv]
        self.runs.append(words)
        if self.fake is not None:
            forwarded = {**kwargs, "env": self.fake.env()}
            return SystemGateway.run(self, [str(self.fake.program), *words[1:]], **forwarded)
        if words[1:] == ["--version"]:
            return self.version or CallResult(CallStatus.SPAWN_FAILED, detail="scripted missing")
        if words[1:] == ["kinds", "--json"] and self.kinds is not None:
            return self.kinds
        raise AssertionError(f"validate must not run {words!r}")

    def spawn(self, argv: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"validate must not spawn {list(argv)!r}")

    def run_hook(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("validate must never run a hook")

    def crontab_read(self) -> CallResult:
        if self.crontab_result is not None:
            return self.crontab_result
        if self.crontab is None:
            return CallResult(CallStatus.EXITED, 1, stderr="no crontab for someone\n")
        return CallResult(CallStatus.EXITED, 0, stdout=self.crontab)

    def crontab_write(self, text: str) -> CallResult:
        raise AssertionError("validate must never write the crontab")

    def process_start(self, pid: int) -> str | None:
        if self.process_start_error is not None:
            raise self.process_start_error
        return super().process_start(pid)


def validate(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, *argv: str, json_output: bool = True
) -> Run:
    capsys.readouterr()
    flags = ["--json"] if json_output else []
    code = main(["validate", *flags, *argv], gateway=gw, stdin_is_tty=False)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def install(home: Path) -> Corpus:
    """A usable transcript root (``~/.claude/projects``, mode 0700)."""
    return Corpus(home)


def write_config(home: Path, text: str, *, mode: int = 0o600) -> Path:
    directory = home / ".config" / "session-audit"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def make_store(home: Path) -> None:
    open_store(SystemGateway(), str(state_dir_of(home))).close()


def set_meta(home: Path, key: str, value: object) -> None:
    make_store(home)
    conn = connect(home)
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def make_pending(home: Path) -> int:
    """Remove the newest ledger row: that migration is now pending. Returns its number."""
    make_store(home)
    conn = connect(home)
    try:
        number = conn.execute("SELECT MAX(number) FROM schema_migrations").fetchone()[0]
        conn.execute("DELETE FROM schema_migrations WHERE number = ?", (number,))
        conn.commit()
        return int(number)
    finally:
        conn.close()


def make_newer(home: Path) -> None:
    make_store(home)
    conn = connect(home)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (number, name, applied_at) VALUES (?, ?, ?)",
            (999_999, "from_the_future", 1),
        )
        conn.commit()
    finally:
        conn.close()


def now_ms(gw: ClockedGateway) -> int:
    return int(gw.now().timestamp() * 1000)


def check_of(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in doc["checks"] if c["name"] == name)


def reasons(doc: dict[str, Any]) -> dict[str, str | None]:
    return {c["name"]: c["reason"] for c in doc["checks"] if not c["ok"]}


Snapshot = dict[str, tuple[bytes | None, int, int, int, int]]


def snapshot(*roots: Path) -> Snapshot:
    """Every entry below ``roots`` (and the roots): bytes (None for a directory), mtime, mode,
    inode and link count. Equal snapshots mean nothing was created, removed, written or touched."""
    found: Snapshot = {}
    for root in roots:
        if not root.exists():
            continue
        paths = [root, *sorted(root.rglob("*"))]
        for path in paths:
            info = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            found[str(path)] = (data, info.st_mtime_ns, info.st_mode, info.st_ino, info.st_nlink)
    return found


def hold_wal(home: Path) -> Callable[[], None]:
    """Leave a live WAL beside ``state.db`` (a second connection keeps it from being checkpointed
    away) and return the function that closes it."""
    make_store(home)
    conn = sqlite3.connect(state_dir_of(home) / "state.db", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('held', 1) ON CONFLICT (key) DO UPDATE SET value = 1"
    )
    return conn.close
