"""Session retention (``retention.session_retention``): the age rule and its boundary, the order and
size of the transactions, owners of duplicated keys, crash and resume, the lease."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from session_audit import retention
from session_audit.errors import CliError
from session_audit.retention import Report, Settings, epoch_ms, ms

from .eventhelp import SESSION_A, SESSION_B, SESSION_C, Env, make_env, message, tool_use, turn

DAY = timedelta(days=1)


def settings(sessions: timedelta = 400 * DAY) -> Settings:
    return Settings(sessions, 180 * DAY, 7 * DAY, 90 * DAY, (), ZoneInfo("UTC"))


@pytest.fixture
def env(tmp_path: Path):  # noqa: ANN201
    e = make_env(tmp_path)
    yield e
    e.lease.release()
    e.close()


def now_ms(env: Env) -> int:
    return epoch_ms(env.gateway.now())


def prune(env: Env, cfg: Settings | None = None, *, batch: int = 2000) -> Report:
    report = Report()
    retention.session_retention(
        env.store, env.lease, lambda: now_ms(env), cfg or settings(), report, batch=batch
    )
    return report


def rows(env: Env, sql: str, *params: object) -> list[tuple]:
    return [tuple(r) for r in env.conn.execute(sql, params).fetchall()]


def add_bare_session(env: Env, session: str, last_ingest_at: int) -> None:
    """A session row with no files and no events: its age is ``last_ingest_at``."""
    with env.store.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_dir, main_file_id, first_ingest_at, "
            "last_ingest_at) VALUES (?, 'p', NULL, 0, ?)",
            (session, last_ingest_at),
        )


def assert_consistent(env: Env) -> None:
    """The invariants usage relies on: every event has a session row, one counted row per key."""
    orphans = rows(
        env,
        "SELECT DISTINCT session_id FROM events WHERE session_id NOT IN "
        "(SELECT session_id FROM sessions)",
    )
    assert orphans == []
    bad = rows(env, "SELECT key FROM events GROUP BY key HAVING SUM(counted) <> 1")
    assert bad == []


# --- the age rule and its boundary ------------------------------------------------------------


def test_a_session_exactly_at_the_cutoff_stays_and_one_ms_older_goes(env: Env) -> None:
    cutoff = now_ms(env) - ms(400 * DAY)
    add_bare_session(env, SESSION_A, cutoff)
    add_bare_session(env, SESSION_B, cutoff - 1)
    report = prune(env)
    assert rows(env, "SELECT session_id FROM sessions") == [(SESSION_A,)]
    assert report.tables["sessions"] == 1
    assert rows(env, "SELECT session_id FROM pruned_sessions") == [(SESSION_B,)]


def test_the_age_is_the_end_of_the_counted_events_else_the_files_else_the_ingest_time(
    env: Env,
) -> None:
    env.write("a", [message("m1", 5, t=0), message("m2", 5, t=10)])
    env.ingest("a", session=SESSION_A)
    # counted events end at 2021-05-06 07:00:10 UTC, however recently the session was ingested
    ended = 1620284410000
    assert rows(env, "SELECT MAX(ts) FROM events") == [(ended,)]
    exactly = timedelta(milliseconds=now_ms(env) - ended)  # the cutoff lands on the last event
    assert prune(env, settings(exactly)).tables["sessions"] == 0
    assert prune(env, settings(exactly - timedelta(milliseconds=1))).tables["sessions"] == 1


def test_a_session_with_no_event_time_ages_by_its_files_range(env: Env) -> None:
    env.write("a", [turn(5, t=0)])  # a turn has no ts-less trick: use the files range instead
    env.ingest("a", session=SESSION_A)
    with env.store.transaction() as conn:
        conn.execute("UPDATE events SET ts = NULL")
        conn.execute("UPDATE files SET max_ts = ?", (now_ms(env),))
    assert prune(env).tables["sessions"] == 0
    with env.store.transaction() as conn:
        conn.execute("UPDATE files SET max_ts = ?", (now_ms(env) - ms(400 * DAY) - 1,))
    assert prune(env).tables["sessions"] == 1


def test_a_young_session_is_untouched_whatever_its_neighbours(env: Env) -> None:
    env.write("a", [message("m1", 5, t=0)])
    env.ingest("a", session=SESSION_A)
    assert prune(env, settings(3650 * DAY)).tables == dict.fromkeys(retention.PRUNE_TABLES, 0)
    assert env.stored() == 1


# --- what a pruned session leaves ---------------------------------------------------------------


def seed_with_agent(env: Env) -> None:
    env.write("a", [message("m1", 5, t=0), message("m2", 7, t=1), tool_use("m3", "t1", t=2)])
    env.ingest("a", session=SESSION_A)
    env.write("ag", [message("g1", 3, t=3)])
    env.ingest("ag", session=SESSION_A, agent="x1")
    env.write("b", [message("n1", 5, t=0)])
    env.ingest("b", session=SESSION_B)


def test_everything_of_the_session_goes_and_the_mark_is_written(env: Env) -> None:
    seed_with_agent(env)
    env.gateway.clock.advance(500 * 86400)
    report = prune(env)
    assert report.tables["sessions"] == 2
    assert report.tables["agents"] == 1
    assert report.tables["events"] == 6
    assert report.tables["files"] == 3
    assert report.tables["pruned_sessions"] == 0
    for table in ("sessions", "agents", "events", "files"):
        assert rows(env, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    marks = rows(env, "SELECT session_id, pruned_at FROM pruned_sessions ORDER BY 1")
    assert marks == [(SESSION_A, now_ms(env)), (SESSION_B, now_ms(env))]
    assert rows(env, "SELECT 1 FROM meta WHERE key = 'prune_session'") == []


def test_only_the_expired_session_is_deleted_and_the_other_keeps_every_row(env: Env) -> None:
    seed_with_agent(env)
    with env.store.transaction() as conn:  # B is young
        conn.execute("UPDATE events SET ts = ? WHERE session_id = ?", (now_ms(env), SESSION_B))
    counted_b = rows(env, "SELECT * FROM events WHERE session_id = ?", SESSION_B)
    report = prune(env)
    assert report.tables["sessions"] == 1
    assert rows(env, "SELECT session_id FROM sessions") == [(SESSION_B,)]
    assert rows(env, "SELECT * FROM events WHERE session_id = ?", SESSION_B) == counted_b
    assert_consistent(env)


# --- duplicated keys: the owner is promoted -----------------------------------------------------


def test_deleting_the_counted_owner_promotes_the_copy_in_another_session(env: Env) -> None:
    lines = [message("m1", 5, t=0), message("m2", 7, t=0), message("m3", 9, t=0)]
    env.write("a", lines)
    env.ingest("a", session=SESSION_A)
    env.write("b", [message(f"m{i}", o, t=50) for i, o in ((1, 5), (2, 7), (3, 9))])
    env.ingest("b", session=SESSION_B)  # later copies of the same keys: A owns them
    assert (
        rows(env, "SELECT session_id, counted FROM events ORDER BY id")
        == [(SESSION_A, 1)] * 3 + [(SESSION_B, 0)] * 3
    )
    before = env.counted()
    retention._prune_session(env.store, env.lease, SESSION_A, lambda: 1, Report(), 2)
    assert rows(env, "SELECT session_id, counted FROM events") == [(SESSION_B, 1)] * 3
    assert_consistent(env)
    # the same facts are counted, now through B's copy (its own timestamp and session)
    assert [(r[0], r[1], r[10]) for r in env.counted()] == [(r[0], r[1], r[10]) for r in before]


def test_a_copy_kept_in_a_third_session_is_the_best_of_the_remaining(env: Env) -> None:
    for name, session, t in (("a", SESSION_A, 0), ("b", SESSION_B, 10), ("c", SESSION_C, 20)):
        env.write(name, [message("m1", 5, t=t)])
        env.ingest(name, session=session)
    assert rows(env, "SELECT session_id FROM events WHERE counted = 1") == [(SESSION_A,)]
    retention._prune_session(env.store, env.lease, SESSION_A, lambda: 1, Report(), 5)
    assert rows(env, "SELECT session_id FROM events WHERE counted = 1") == [(SESSION_B,)]
    retention._prune_session(env.store, env.lease, SESSION_B, lambda: 1, Report(), 5)
    assert rows(env, "SELECT session_id FROM events WHERE counted = 1") == [(SESSION_C,)]


# --- bounded transactions, crash and resume -------------------------------------------------


def test_no_transaction_deletes_more_than_the_batch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.write("a", [message(f"m{i}", 1, t=i % 3000) for i in range(25)])
    env.ingest("a", session=SESSION_A)
    sizes: list[int] = []
    real = retention.delete_session_batch

    def spy(conn: sqlite3.Connection, session_id: str, limit: int) -> tuple[int, bool]:
        result = real(conn, session_id, limit)
        sizes.append(result[0])
        return result

    monkeypatch.setattr(retention, "delete_session_batch", spy)
    env.gateway.clock.advance(500 * 86400)
    assert prune(env, batch=4).tables["events"] == 25
    assert sizes == [4, 4, 4, 4, 4, 4, 1]
    assert rows(env, "SELECT COUNT(*) FROM events") == [(0,)]


def test_the_lease_is_renewed_before_every_transaction(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.write("a", [message(f"m{i}", 1, t=i) for i in range(10)])
    env.ingest("a", session=SESSION_A)
    env.gateway.clock.advance(500 * 86400)
    renewals: list[int] = []
    real = env.lease.renew
    monkeypatch.setattr(env.lease, "renew", lambda *a, **k: renewals.append(1) or real(*a, **k))
    env.gateway.tick = 0
    # a run that takes longer than the lease margin: every batch finds the lease nearly expired
    original = retention.delete_session_batch

    def slow(conn: sqlite3.Connection, session_id: str, limit: int) -> tuple[int, bool]:
        env.gateway.clock.advance(100)
        return original(conn, session_id, limit)

    monkeypatch.setattr(retention, "delete_session_batch", slow)
    prune(env, batch=3)
    assert len(renewals) >= 3


class Crash(Exception):
    pass


def crashing(monkeypatch: pytest.MonkeyPatch, after: int) -> None:
    real = retention.delete_session_batch
    calls = {"n": 0}

    def maybe(conn: sqlite3.Connection, session_id: str, limit: int) -> tuple[int, bool]:
        calls["n"] += 1
        if calls["n"] > after:
            raise Crash
        return real(conn, session_id, limit)

    monkeypatch.setattr(retention, "delete_session_batch", maybe)


def test_a_crash_between_batches_leaves_a_consistent_store_and_the_next_run_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_with_agent(env)
    env.write("dup", [message("m1", 5, t=40), message("m2", 7, t=40)])
    env.ingest("dup", session=SESSION_C)
    env.gateway.clock.advance(500 * 86400)
    with monkeypatch.context() as patched:
        crashing(patched, after=1)
        with pytest.raises(Crash):
            prune(env, batch=2)
    # the first batch of the first session is committed, nothing else; the session row is there
    assert rows(env, "SELECT value FROM meta WHERE key = 'prune_session'") == [(SESSION_A,)]
    assert rows(env, "SELECT COUNT(*) FROM sessions") == [(3,)]
    assert rows(env, "SELECT COUNT(*) FROM pruned_sessions") == [(0,)]
    assert_consistent(env)
    report = prune(env, batch=2)
    for table in ("sessions", "agents", "events", "files"):
        assert rows(env, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert rows(env, "SELECT COUNT(*) FROM pruned_sessions") == [(3,)]
    assert rows(env, "SELECT 1 FROM meta WHERE key = 'prune_session'") == []
    assert report.tables["sessions"] == 3  # the resumed one counts once, in the run that ended it


def test_a_crash_after_every_possible_batch_always_resumes_to_the_same_end(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.write("a", [message(f"m{i}", 1, t=i) for i in range(7)])
    env.ingest("a", session=SESSION_A)
    env.write("b", [message(f"m{i}", 1, t=30 + i) for i in range(7)])
    env.ingest("b", session=SESSION_B)
    env.gateway.clock.advance(500 * 86400)
    crash_after = 0
    while True:
        with monkeypatch.context() as patched:
            crashing(patched, after=crash_after)
            try:
                prune(env, batch=3)
            except Crash:
                assert_consistent(env)
                crash_after += 1
                continue
        break
    assert crash_after >= 3
    assert rows(env, "SELECT COUNT(*) FROM events") == [(0,)]
    assert rows(env, "SELECT COUNT(*) FROM sessions") == [(0,)]


def test_a_marked_session_is_finished_even_when_it_no_longer_looks_old(env: Env) -> None:
    add_bare_session(env, SESSION_A, now_ms(env))  # young by its own rows
    with env.store.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('prune_session', ?)", (SESSION_A,))
    prune(env)
    assert rows(env, "SELECT COUNT(*) FROM sessions") == [(0,)]
    assert rows(env, "SELECT session_id FROM pruned_sessions") == [(SESSION_A,)]


def test_a_marker_whose_session_is_gone_is_cleared(env: Env) -> None:
    with env.store.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('prune_session', ?)", (SESSION_A,))
    prune(env)
    assert rows(env, "SELECT 1 FROM meta WHERE key = 'prune_session'") == []
    assert rows(env, "SELECT COUNT(*) FROM pruned_sessions") == [(0,)]


def test_a_session_with_many_agents_is_deleted_in_bounded_steps(env: Env) -> None:
    env.write("a", [message("m1", 1, t=0)])
    env.ingest("a", session=SESSION_A)
    for i in range(7):
        env.write(f"ag{i}", [message(f"g{i}", 1, t=1)])
        env.ingest(f"ag{i}", session=SESSION_A, agent=f"x{i}")
    env.gateway.clock.advance(500 * 86400)
    report = prune(env, batch=3)
    assert (report.tables["agents"], report.tables["files"], report.tables["events"]) == (7, 8, 8)
    assert rows(env, "SELECT COUNT(*) FROM files") == [(0,)]


# --- the lease and the old marks -----------------------------------------------------------------


def test_a_lost_lease_stops_the_prune_before_it_writes(env: Env) -> None:
    add_bare_session(env, SESSION_A, 0)
    with env.store.transaction() as conn:
        conn.execute("UPDATE meta SET value = '999|other|1' WHERE key = 'ingest_lease'")
    with pytest.raises(CliError):
        prune(env)
    assert rows(env, "SELECT COUNT(*) FROM sessions") == [(1,)]


def test_pruned_marks_go_after_twice_the_retention_and_not_before(env: Env) -> None:
    cutoff = now_ms(env) - 2 * ms(400 * DAY)
    with env.store.transaction() as conn:
        conn.executemany(
            "INSERT INTO pruned_sessions (session_id, pruned_at) VALUES (?, ?)",
            [(SESSION_A, cutoff), (SESSION_B, cutoff - 1), (SESSION_C, cutoff + 1)],
        )
    report = prune(env)
    assert report.tables["pruned_sessions"] == 1
    assert {r[0] for r in rows(env, "SELECT session_id FROM pruned_sessions")} == {
        SESSION_A,
        SESSION_C,
    }


def test_old_marks_are_removed_in_bounded_batches(env: Env) -> None:
    with env.store.transaction() as conn:
        conn.executemany(
            "INSERT INTO pruned_sessions (session_id, pruned_at) VALUES (?, 0)",
            [(f"s{i}",) for i in range(11)],
        )
    assert prune(env, batch=4).tables["pruned_sessions"] == 11
