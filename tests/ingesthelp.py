"""Helpers for the ``ingest`` command tests: a synthetic transcript corpus under the private home,
a gateway with a scripted monotonic clock, and a driver that runs the real CLI in process."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from session_audit.cli import main
from session_audit.gateway import CallResult, CallStatus

from .eventhelp import message, raw
from .storehelp import ClockedGateway

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"


class TickGateway(ClockedGateway):
    """The real file system and process table, a scripted wall clock, and a monotonic clock that
    moves ``tick`` seconds at every reading (0 leaves it still)."""

    def __init__(self) -> None:
        super().__init__()
        self.tick = 0.0
        self.runs: list[object] = []

    def monotonic(self) -> float:
        now = self.clock.monotonic()
        if self.tick:
            self.clock.advance(self.tick)
        return now

    def run(self, argv: Any, **kwargs: Any) -> Any:
        """claude-session is missing here: a real ingest finds no harness and goes on; a test
        asserts ``runs`` to see what was asked. Any other program is a bug of the test or of
        ingest, so it fails loudly instead of looking like a missing tool."""
        self.runs.append(list(argv))
        if Path(str(argv[0])).name != "claude-session":
            raise AssertionError(f"ingest must not start a process: {list(argv)!r}")
        return CallResult(CallStatus.SPAWN_FAILED, detail="FileNotFoundError: scripted missing")


class Corpus:
    """``<home>/.claude/projects`` with transcripts written into it."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home / ".claude" / "projects"
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)

    def main_path(self, session: str, project: str = "proj-a") -> Path:
        directory = self.root / project
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory / f"{session}.jsonl"

    def write_main(
        self, session: str, lines: list[bytes], project: str = "proj-a", mtime: int | None = None
    ) -> Path:
        path = self.main_path(session, project)
        path.write_bytes(b"".join(lines))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def append_main(self, session: str, lines: list[bytes], project: str = "proj-a") -> None:
        with open(self.main_path(session, project), "ab") as handle:
            handle.write(b"".join(lines))

    def write_agent(
        self,
        session: str,
        agent: str,
        lines: list[bytes],
        project: str = "proj-a",
        meta: dict[str, str] | None = None,
        mtime: int | None = None,
    ) -> Path:
        directory = self.root / project / session / "subagents"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / f"agent-{agent}.jsonl"
        path.write_bytes(b"".join(lines))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        if meta is not None:
            (directory / f"agent-{agent}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return path


def messages(prefix: str, count: int, *, out: int = 10) -> list[bytes]:
    """``count`` assistant lines with distinct message ids."""
    return [message(f"{prefix}-{i}", out, t=i % 3000) for i in range(count)]


class Run:
    """The result of one CLI run."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.out)  # type: ignore[no-any-return]


def ingest(
    capsys: pytest.CaptureFixture[str],
    gateway: ClockedGateway,
    *argv: str,
    json_output: bool = True,
) -> Run:
    capsys.readouterr()
    flags = ["--json"] if json_output else []
    code = main(["ingest", *flags, *argv], gateway=gateway, stdin_is_tty=False)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def state_dir_of(home: Path) -> Path:
    return home / ".local" / "state" / "session-audit"


def connect(home: Path) -> sqlite3.Connection:
    return sqlite3.connect(state_dir_of(home) / "state.db")


def meta(home: Path) -> dict[str, Any]:
    conn = connect(home)
    try:
        return dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()


def scalar(home: Path, sql: str, *params: object) -> Any:
    conn = connect(home)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def counted_tokens(home: Path) -> int:
    return int(scalar(home, "SELECT COALESCE(SUM(output_tokens), 0) FROM events WHERE counted = 1"))


def bad_line() -> bytes:
    return b"this is not json\n"


def unknown_line() -> bytes:
    return raw({"type": "bogus-kind", "timestamp": "2021-05-06T07:00:00.000Z"})
