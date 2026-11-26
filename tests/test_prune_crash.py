"""A real crash of session retention: a child process pruning a few thousand events in batches of
50 is killed (SIGKILL, the PID it was launched with) once ``meta.prune_session`` shows in the
store; the next prune must finish and leave every invariant usage relies on."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from session_audit.store import open_store

from .storehelp import ClockedGateway, state_path

CHILD = Path(__file__).with_name("crash_child.py")
OLD_SESSIONS = ("old-session-1", "old-session-2")
YOUNG = "young-session"
PER_OLD = 1500
SHARED = 600  # keys of the first old session that the young session also holds (not counted there)


def key(tag: str, i: int) -> str:
    return f"{tag}{i:028x}"[-32:]


def seed(directory: str) -> None:
    now_ms = int(time.time() * 1000)
    store = open_store(ClockedGateway(), directory)
    with store.transaction() as conn:
        for file_id, session, max_ts in (
            (1, OLD_SESSIONS[0], 1000),
            (2, OLD_SESSIONS[1], 1000),
            (3, YOUNG, now_ms),
        ):
            conn.execute(
                "INSERT INTO files (id, path, dev, inode, max_ts) VALUES (?, ?, 1, ?, ?)",
                (file_id, f"/f{file_id}".encode(), file_id, max_ts),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, project_dir, main_file_id, first_ingest_at, "
                "last_ingest_at) VALUES (?, 'p', ?, 0, ?)",
                (session, file_id, max_ts),
            )

        def event(k: str, file_id: int, session: str, counted: int, ts: int, n: int) -> tuple:
            return (k, file_id, n, "message", ts, session, counted)

        sql = (
            "INSERT INTO events (key, file_id, line_offset, kind, ts, session_id, counted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        conn.executemany(
            sql, [event(key("a", i), 1, OLD_SESSIONS[0], 1, 1000, i) for i in range(PER_OLD)]
        )
        conn.executemany(
            sql, [event(key("b", i), 2, OLD_SESSIONS[1], 1, 1000, i) for i in range(PER_OLD)]
        )
        conn.executemany(sql, [event(key("a", i), 3, YOUNG, 0, now_ms, i) for i in range(SHARED)])
    store.close()


def child(directory: str, mode: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - our own script, the interpreter running the tests
        [sys.executable, str(CHILD), directory, mode], env=os.environ.copy()
    )


def marker_visible(database: str) -> bool:
    conn = sqlite3.connect(database, timeout=5)
    try:
        return conn.execute("SELECT 1 FROM meta WHERE key = 'prune_session'").fetchone() is not None
    finally:
        conn.close()


def test_a_killed_prune_is_finished_by_the_next_one(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    seed(directory)
    database = f"{directory}/state.db"
    process = child(directory, "slow")
    pid = process.pid  # captured at launch: the only process this test ever signals
    deadline = time.monotonic() + 60
    while not marker_visible(database):
        assert process.poll() is None, "the child ended before it showed a marker"
        assert time.monotonic() < deadline, "no marker within 60 s"
        time.sleep(0.005)
    os.kill(pid, signal.SIGKILL)
    assert process.wait(timeout=30) == -signal.SIGKILL
    conn = sqlite3.connect(database, timeout=30)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3  # mid-run
    assert conn.execute("SELECT value FROM meta WHERE key = 'prune_session'").fetchone()[0]

    resume = child(directory, "fast")
    assert resume.wait(timeout=120) == 0

    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'prune_session'").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id NOT IN (SELECT session_id FROM sessions)"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT key FROM events GROUP BY key HAVING SUM(counted) <> 1").fetchall()
        == []
    )
    assert {r[0] for r in conn.execute("SELECT session_id FROM pruned_sessions")} == set(
        OLD_SESSIONS
    )
    assert [r[0] for r in conn.execute("SELECT session_id FROM sessions")] == [YOUNG]
    assert conn.execute("SELECT COUNT(*), SUM(counted) FROM events").fetchone() == (SHARED, SHARED)
    assert conn.execute("SELECT id FROM files").fetchall() == [(3,)]
    conn.close()
