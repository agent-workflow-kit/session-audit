"""Storing events (issue #13): dedupe keys, the within-file rule, the owner rule, chunked writes
under the ingest lease, the batched reread deletion and its crash resume."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from session_audit import eventstore
from session_audit.errors import CliError
from session_audit.eventstore import (
    MAX_COPIES,
    FileRef,
    dedupe_id,
    event_key,
    event_ref,
    place_events,
)
from session_audit.incremental import Reason, Stop
from session_audit.lease import IngestTarget
from session_audit.parser import Event, Kind
from session_audit.reader import AgentMeta

from .eventhelp import (
    SESSION_A,
    SESSION_B,
    SESSION_C,
    Env,
    compaction,
    denial_and_interruption,
    make_env,
    message,
    raw,
    tool_error,
    tool_use,
    ts,
    turn,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = make_env(tmp_path)
    yield e
    e.close()


def rows(env: Env, sql: str, *params: object) -> list[tuple]:
    return [tuple(r) for r in env.conn.execute(sql, params)]


# --- hashes -----------------------------------------------------------------------------------


def test_key_is_the_documented_hash_with_the_kind_inside() -> None:
    expected = hashlib.sha256(b"message\x00msg_1").hexdigest()[:16]
    assert event_key(Kind.MESSAGE, "msg_1") == expected
    assert event_key(Kind.TOOL_CALL, "x") != event_key(Kind.TOOL_ERROR, "x")


def test_ref_of_a_tool_error_is_the_key_of_its_call() -> None:
    assert event_ref("toolu_1") == event_key(Kind.TOOL_CALL, "toolu_1")


def test_a_lone_surrogate_id_hashes_without_raising() -> None:
    assert len(event_key(Kind.MESSAGE, "a\ud800b")) == 16


def _event(kind: Kind, ident: str | None, block: int = 0) -> Event:
    return Event(kind, ident, None, block, None, None, None, None, None, None)


def test_fallback_id_is_the_line_position_with_the_block_for_repeating_kinds() -> None:
    assert dedupe_id(_event(Kind.TURN, None), 7, 120) == "7:120"
    assert dedupe_id(_event(Kind.COMPACTION, None), 7, 120) == "7:120"
    assert dedupe_id(_event(Kind.TOOL_CALL, None, 3), 7, 120) == "7:120:3"
    assert dedupe_id(_event(Kind.TOOL_ERROR, None, 2), 7, 120) == "7:120:2"
    assert dedupe_id(_event(Kind.MESSAGE, None), 7, 120) == "7:120:0"
    assert dedupe_id(_event(Kind.MESSAGE, "m1"), 7, 120) == "m1"


# --- the within-file rule and the spike facts -------------------------------------------------


def test_one_message_over_three_block_lines_counts_its_largest_output_once(env: Env) -> None:
    lines = [message("m1", 10, t=1), message("m1", 40, t=2), message("m1", 25, t=3)]
    env.write("a", lines)
    env.ingest("a")
    (row,) = rows(env, "SELECT output_tokens, line_offset, ts, counted FROM events")
    assert row[0] == 40  # the naive sum would be 75
    assert row[1] == 0  # the message's first line
    assert row[3] == 1


def test_equal_output_keeps_the_earliest_line(env: Env) -> None:
    env.write("a", [message("m1", 9, t=5), message("m1", 9, t=6)])
    env.ingest("a")
    (ts_row,) = rows(env, "SELECT ts FROM events")
    assert ts_row[0] == 1620284405000  # t=5


def test_a_message_split_across_chunks_equals_one_chunk(env: Env, tmp_path: Path) -> None:
    lines = [message("m1", 10, t=1), message("m1", 40, t=2), message("m1", 25, t=3)]
    env.write("a", lines)
    env.ingest("a", chunk_lines=1)
    split = rows(env, "SELECT key, output_tokens, line_offset, ts FROM events")
    other = make_env(tmp_path / "x")
    try:
        other.write("a", lines)
        other.ingest("a")
        assert rows(other, "SELECT key, output_tokens, line_offset, ts FROM events") == split
    finally:
        other.close()


def test_a_tool_call_and_its_error_are_two_events(env: Env) -> None:
    env.write("a", [tool_use("m1", "toolu_1", "Bash", t=1), tool_error("toolu_1", t=2)])
    env.ingest("a")
    got = rows(env, "SELECT kind, key, ref, tool FROM events ORDER BY line_offset")
    assert sorted(g[0] for g in got) == ["message", "tool_call", "tool_error"]
    kinds = {g[0]: g for g in got}
    assert kinds["tool_error"][2] == kinds["tool_call"][1]  # ref is the call's key
    assert kinds["tool_call"][3] == "Bash"


def test_a_denial_and_an_interruption_on_one_line_are_two_events(env: Env) -> None:
    env.write("a", [denial_and_interruption()])
    env.ingest("a")
    got = rows(env, "SELECT kind, ref FROM events ORDER BY kind")
    assert [g[0] for g in got] == ["denial", "interruption"]
    assert got[0][1] == event_ref("toolu_1")
    assert got[1][1] is None


def test_turn_and_compaction_without_uuid_use_the_line_position(env: Env) -> None:
    env.write("a", [turn(1500, t=1), compaction(None, t=2), turn(2500, t=3)])
    env.ingest("a")
    got = rows(env, "SELECT kind, seconds, line_offset FROM events ORDER BY line_offset")
    assert [g[0] for g in got] == ["turn", "compaction", "turn"]  # three distinct keys
    (file_id,) = rows(env, "SELECT id FROM files")[0]
    first = event_key(Kind.TURN, f"{file_id}:0")
    assert rows(env, "SELECT 1 FROM events WHERE key = ?", first)


def test_two_tool_calls_without_ids_on_one_line_are_distinct_by_block(env: Env) -> None:
    line = raw(
        {
            "type": "assistant",
            "timestamp": ts(1),
            "message": {
                "model": "m",
                "usage": {"output_tokens": 1},
                "content": [
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "tool_use", "name": "Read"},
                ],
            },
        }
    )
    env.write("a", [line])
    env.ingest("a")
    got = rows(env, "SELECT kind, tool FROM events ORDER BY tool")
    assert ("tool_call", "Bash") in got and ("tool_call", "Read") in got
    assert len(got) == 3  # the two calls and the message


def test_a_repeated_tool_use_id_keeps_the_first_line(env: Env) -> None:
    env.write(
        "a",
        [
            tool_use("m1", "toolu_1", "Bash", out=5, t=1),
            tool_use("m2", "toolu_1", "Read", out=9, t=2),
        ],
    )
    env.ingest("a")
    assert rows(env, "SELECT tool FROM events WHERE kind = 'tool_call'") == [("Bash",)]


# --- identity comes from the file -------------------------------------------------------------


def test_session_and_agent_come_from_the_file_not_the_line(env: Env) -> None:
    env.write("a", [message("m1", 5, session="someone-else", agent="liar")])
    env.ingest("a", session=SESSION_A, agent="agent-1")
    got = rows(env, "SELECT session_id, agent_id, native FROM events")
    assert got == [(SESSION_A, "agent-1", 0)]


def test_native_is_a_missing_or_equal_session_id(env: Env) -> None:
    env.write(
        "a",
        [
            message("m1", 5, session=SESSION_A),
            message("m2", 5),
            message("m3", 5, session=SESSION_B),
        ],
    )
    env.ingest("a", session=SESSION_A)
    got = dict(rows(env, "SELECT key, native FROM events"))
    assert got[event_key(Kind.MESSAGE, "m1")] == 1
    assert got[event_key(Kind.MESSAGE, "m2")] == 1
    assert got[event_key(Kind.MESSAGE, "m3")] == 0


def test_a_subagent_file_read_first_creates_its_session_and_agent_rows(env: Env) -> None:
    env.write("sub", [message("m1", 5)])
    env.ingest(
        "sub",
        session=SESSION_A,
        agent="agent-7",
        meta=AgentMeta(agent_type="Explore", tool_use_id="toolu_9"),
    )
    assert rows(env, "SELECT session_id, project_dir, main_file_id FROM sessions") == [
        (SESSION_A, "proj", None)
    ]
    (agent,) = rows(env, "SELECT session_id, agent_id, parent_tool_call_id, agent_type FROM agents")
    assert agent == (SESSION_A, "agent-7", "toolu_9", "Explore")
    env.write("main", [message("m2", 5)])
    env.ingest("main", session=SESSION_A)
    (main_file,) = rows(env, "SELECT id FROM files WHERE path = ?", bytes(env.path("main")))[0]
    assert rows(env, "SELECT main_file_id FROM sessions") == [(main_file,)]
    assert rows(env, "SELECT count(*) FROM sessions") == [(1,)]


def test_a_project_name_that_is_not_utf8_is_stored_fixed(env: Env) -> None:
    env.write("a", [message("m1", 5)])
    env.ingest("a", project=b"p\xffq")
    (name,) = rows(env, "SELECT project_dir FROM sessions")[0]
    assert name == "p�q"


# --- the owner rule ---------------------------------------------------------------------------


def two_copies(env: Env, a: list[bytes], b: list[bytes], **kw: dict) -> str:
    """Ingest file a (session A) then b (session B); return the session of the counted copy."""
    env.write("a", a)
    env.write("b", b)
    env.ingest("a", session=SESSION_A, **kw.get("a", {}))
    env.ingest("b", session=SESSION_B, **kw.get("b", {}))
    (got,) = rows(env, "SELECT session_id FROM events WHERE counted = 1 AND kind = 'message'")[0]
    return got


def test_owner_prefers_the_larger_output(env: Env) -> None:
    assert two_copies(env, [message("m", 5, t=1)], [message("m", 9, t=2)]) == SESSION_B


def test_owner_then_the_earlier_timestamp(env: Env) -> None:
    assert two_copies(env, [message("m", 5, t=9)], [message("m", 5, t=2)]) == SESSION_B


def test_owner_a_missing_timestamp_is_last(env: Env) -> None:
    undated = raw(
        {"type": "assistant", "message": {"id": "m", "model": "x", "usage": {"output_tokens": 5}}}
    )
    assert two_copies(env, [undated], [message("m", 5, t=2)]) == SESSION_B


def test_owner_then_the_native_copy(env: Env) -> None:
    got = two_copies(
        env, [message("m", 5, t=1, session=SESSION_C)], [message("m", 5, t=1, session=SESSION_B)]
    )
    assert got == SESSION_B  # b's line names its own session; a's line is copied history


def test_owner_then_the_smaller_path_then_it_does_not_depend_on_order(env: Env) -> None:
    got = two_copies(env, [message("m", 5, t=1)], [message("m", 5, t=1)])
    assert got == SESSION_A  # "a.jsonl" sorts before "b.jsonl"


def test_owner_is_the_same_when_read_in_the_other_order(env: Env) -> None:
    env.write("a", [message("m", 5, t=1)])
    env.write("b", [message("m", 5, t=1)])
    env.ingest("b", session=SESSION_B)
    env.ingest("a", session=SESSION_A)
    assert rows(env, "SELECT session_id FROM events WHERE counted = 1") == [(SESSION_A,)]


def test_fork_with_native_and_copied_history(env: Env) -> None:
    """A fork copies the source's lines (still naming the source session) and then adds its own."""
    shared = [message("m1", 10, t=1, session=SESSION_A), message("m2", 20, t=2, session=SESSION_A)]
    env.write("src", shared)
    env.write("fork", [*shared, message("m3", 7, t=3, session=SESSION_B)])
    env.ingest("fork", session=SESSION_B)
    env.ingest("src", session=SESSION_A)
    got = dict(rows(env, "SELECT key, session_id FROM events WHERE counted = 1"))
    assert got[event_key(Kind.MESSAGE, "m1")] == SESSION_A
    assert got[event_key(Kind.MESSAGE, "m2")] == SESSION_A
    assert got[event_key(Kind.MESSAGE, "m3")] == SESSION_B
    assert env.stored() == 5  # both copies of m1 and m2 are stored, one counted each
    assert rows(env, "SELECT count(*) FROM events WHERE counted = 1") == [(3,)]


def test_a_partial_copy_of_a_streaming_message_never_wins(env: Env) -> None:
    full = [message("m", 10, t=1), message("m", 90, t=2)]
    env.write("a", full)
    env.write("b", full[:1])  # the fork copied only the first block line
    env.ingest("b", session=SESSION_B)
    env.ingest("a", session=SESSION_A)
    assert rows(env, "SELECT session_id, output_tokens FROM events WHERE counted = 1") == [
        (SESSION_A, 90)
    ]


def test_growing_a_copy_in_a_later_run_moves_the_owner(env: Env) -> None:
    env.write("a", [message("m", 10, t=1)])
    env.write("b", [message("m", 5, t=1)])
    env.ingest("a", session=SESSION_A)
    env.ingest("b", session=SESSION_B)
    assert rows(env, "SELECT session_id FROM events WHERE counted = 1") == [(SESSION_A,)]
    env.append("b", [message("m", 50, t=2)])
    result = env.ingest("b", session=SESSION_B)
    assert result.reason is Reason.CONTINUE
    assert rows(env, "SELECT session_id, output_tokens FROM events WHERE counted = 1") == [
        (SESSION_B, 50)
    ]
    assert rows(env, "SELECT count(*) FROM events") == [(2,)]


# --- eight copies, linear work ----------------------------------------------------------------


def msg_event(out: int, t: int | None = 1000) -> Event:
    return Event(Kind.MESSAGE, "m", None, 0, None, None, t, None, "model", None, output_tokens=out)


def copies_into(conn: sqlite3.Connection, n: int, quality: str) -> tuple[int, int]:
    """Place n copies of one key, each from its own file; return (statements, copies_dropped)."""
    statements = 0

    def count(_sql: str) -> None:
        nonlocal statements
        statements += 1

    conn.set_trace_callback(count)
    conn.execute("BEGIN IMMEDIATE")
    for i in range(n):
        conn.execute(
            "INSERT INTO files (path, dev, inode) VALUES (?, 1, ?)", (f"/f/{i:05d}".encode(), i)
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE path = ?", (f"/f/{i:05d}".encode(),)
        ).fetchone()[0]
        out = {"rising": i, "falling": n - i, "flat": 7}[quality]
        ref = FileRef(file_id, f"/f/{i:05d}".encode(), "s", None)
        dropped = place_events(conn, ref, [(msg_event(out), 0)])
        for fid, k in dropped.items():
            conn.execute(
                "UPDATE files SET copies_dropped = copies_dropped + ? WHERE id = ?", (k, fid)
            )
    conn.execute("COMMIT")
    conn.set_trace_callback(None)
    total = conn.execute("SELECT sum(copies_dropped) FROM files").fetchone()[0]
    return statements, total


@pytest.mark.parametrize("n", [9, 300])
@pytest.mark.parametrize("quality", ["rising", "falling", "flat"])
def test_many_copies_keep_eight_and_count_the_dropped(env: Env, n: int, quality: str) -> None:
    statements, dropped = copies_into(env.conn, n, quality)
    assert env.stored() == MAX_COPIES
    assert dropped == n - MAX_COPIES
    assert rows(env, "SELECT count(*) FROM events WHERE counted = 1") == [(1,)]
    # The kept copies are the 8 best: with equal timestamps and natives, larger output then path.
    if quality == "rising":
        assert rows(env, "SELECT min(output_tokens) FROM events") == [(n - MAX_COPIES,)]
        assert rows(env, "SELECT output_tokens FROM events WHERE counted = 1") == [(n - 1,)]
    assert statements <= 12 * n  # a bounded number of statements per event, whatever n is


@pytest.mark.parametrize("quality", ["rising", "falling", "flat"])
def test_work_is_linear_in_the_number_of_copies(env: Env, tmp_path: Path, quality: str) -> None:
    small, _ = copies_into(env.conn, 100, quality)
    other = make_env(tmp_path / "second")
    try:
        large, _ = copies_into(other.conn, 300, quality)
    finally:
        other.close()
    assert large <= small * 3 * 1.2  # three times the copies, about three times the statements


def test_the_counted_copy_is_the_best_whatever_the_arrival_order(env: Env) -> None:
    import random

    outs = list(range(1, 21))
    random.Random(5).shuffle(outs)
    conn = env.conn
    conn.execute("BEGIN IMMEDIATE")
    for i, out in enumerate(outs):
        conn.execute(
            "INSERT INTO files (path, dev, inode) VALUES (?, 1, ?)", (f"/f/{i:02d}".encode(), i)
        )
        fid = conn.execute(
            "SELECT id FROM files WHERE path = ?", (f"/f/{i:02d}".encode(),)
        ).fetchone()[0]
        place_events(conn, FileRef(fid, f"/f/{i:02d}".encode(), "s", None), [(msg_event(out), 0)])
    conn.execute("COMMIT")
    assert sorted(r[0] for r in conn.execute("SELECT output_tokens FROM events")) == list(
        range(13, 21)
    )
    assert rows(env, "SELECT output_tokens FROM events WHERE counted = 1") == [(20,)]


# --- chunked writes under the lease -----------------------------------------------------------


def test_lines_are_parsed_with_no_transaction_open(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = eventstore.parse_line
    seen: list[bool] = []

    def watching(data: bytes, context):  # type: ignore[no-untyped-def]
        seen.append(env.conn.in_transaction)
        return real(data, context)

    monkeypatch.setattr(eventstore, "parse_line", watching)
    env.write("a", [message(f"m{i}", i + 1, t=i) for i in range(12)])
    env.ingest("a", chunk_lines=3)
    assert len(seen) == 12 and not any(seen)


def test_a_chunk_commits_events_offset_and_facts_together(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the write of the second chunk: the first chunk's events, offset and facts stay, and
    nothing of the second does (no lost and no double-counted line on the resume)."""
    lines = [message(f"m{i}", i + 1, t=i) for i in range(6)]
    env.write("a", lines)
    real = env.lease.check_still_holder
    calls = {"n": 0}

    def flaky(conn: sqlite3.Connection) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash in the second chunk")
        real(conn)

    monkeypatch.setattr(env.lease, "check_still_holder", flaky)
    with pytest.raises(RuntimeError):
        env.ingest("a", chunk_lines=2)
    monkeypatch.undo()
    assert env.stored() == 2
    (offset, last_ts, errs) = rows(env, "SELECT offset, last_ts, parse_errors FROM files")[0]
    assert offset == len(b"".join(lines[:2]))
    assert last_ts is not None
    result = env.ingest("a", chunk_lines=2)
    assert result.reason is Reason.CONTINUE and result.lines == 4
    assert env.stored() == 6
    assert rows(env, "SELECT min_ts, max_ts FROM files") == [(1620284400000, 1620284405000)]


def test_chunks_are_capped_by_lines_and_by_events(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    real = eventstore.place_events

    def spy(conn, file, pending):  # type: ignore[no-untyped-def]
        sizes.append(len(pending))
        return real(conn, file, pending)

    monkeypatch.setattr(eventstore, "place_events", spy)
    lines = [
        message(f"m{i}", 1, t=i, tools=((f"t{i}a", "Bash"), (f"t{i}b", "Read"))) for i in range(10)
    ]  # 3 events a line
    env.write("a", lines)
    env.ingest("a", chunk_events=7, chunk_lines=100)
    assert sum(sizes) == 30
    assert max(sizes) <= 7  # a chunk closes before a line that would pass the cap
    sizes.clear()
    env.write("b", [message(f"n{i}", 1, t=i) for i in range(10)])
    env.ingest("b", chunk_lines=4, chunk_events=100)
    assert sizes == [4, 4, 2]


def test_one_line_with_more_events_than_the_cap_is_never_split(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    real = eventstore.place_events
    monkeypatch.setattr(
        eventstore, "place_events", lambda c, f, p: (sizes.append(len(p)), real(c, f, p))[1]
    )
    tools = tuple((f"t{i}", "Bash") for i in range(9))
    env.write("a", [message("m", 1, tools=tools), message("n", 1)])
    env.ingest("a", chunk_events=5)
    assert sizes == [10, 1]


def test_an_empty_file_and_a_file_of_corrupt_lines_still_save_progress(env: Env) -> None:
    env.write("empty", [])
    result = env.ingest("empty")
    assert result.complete and env.stored() == 0
    assert rows(env, "SELECT offset FROM files") == [(0,)]
    env.write("junk", [b"not json\n", b"[1]\n", b'{"type":"zzz"}\n', b"\x00\x00\n"])
    env.ingest("junk")
    assert rows(env, "SELECT parse_errors, unknown_lines, offset FROM files WHERE offset > 0")[0][
        :2
    ] == (3, 1)
    assert env.stored() == 0


def test_hostile_values_never_reach_sql_text(env: Env) -> None:
    evil = "x'); DROP TABLE events; --"
    env.write(
        "a",
        [
            message(evil, 5, model=evil[:20], tools=((evil, evil[:30]),)),
            tool_error(evil, uuid=evil),
            denial_and_interruption(uuid=evil, tool_id=evil),
            raw({"type": "assistant", "message": {"id": "z", "usage": {"output_tokens": 10**30}}}),
        ],
    )
    env.ingest("a")
    assert (
        env.stored() == 5
    )  # message, call, error, denial, interruption; the huge count is an error line
    dump = "\n".join(env.conn.iterdump())
    assert evil not in dump  # ids are only ever stored as hashes
    assert rows(env, "SELECT count(*) FROM sqlite_master WHERE name = 'events'") == [(1,)]


def test_a_huge_tool_use_id_is_a_fallback_and_unicode_names_are_stored(env: Env) -> None:
    env.write("a", [message("m1", 3, tools=(("x" * 5000, "Bäsh\U0001f600"),))])
    env.ingest("a")
    assert rows(env, "SELECT tool FROM events WHERE kind = 'tool_call'") == [("Bäsh\U0001f600",)]


def test_no_event_text_is_stored(env: Env) -> None:
    secret = "sk-ant-api03-SECRETSECRETSECRET"
    obj = {
        "type": "assistant",
        "timestamp": ts(1),
        "message": {
            "id": "m",
            "model": "m",
            "usage": {"output_tokens": 2},
            "content": [
                {"type": "text", "text": secret},
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": secret}},
            ],
        },
    }
    env.write("a", [raw(obj)])
    env.ingest("a")
    assert secret not in "\n".join(env.conn.iterdump())


# --- the lease and a busy store ---------------------------------------------------------------


def test_a_lost_lease_stops_the_write_and_stores_nothing(env: Env) -> None:
    env.conn.execute("UPDATE meta SET value = '999|other|1' WHERE key = 'ingest_lease'")
    env.write("a", [message("m", 5)])
    with pytest.raises(CliError) as err:
        env.ingest("a")
    assert err.value.code == "internal"
    assert env.stored() == 0
    assert rows(env, "SELECT count(*) FROM files") == [(0,)]


def test_a_busy_store_is_a_retryable_error_never_swallowed(env: Env, tmp_path: Path) -> None:
    env.write("a", [message("m", 5)])
    env.conn.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(f"{env.store.directory}/state.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(CliError) as err:
            env.ingest("a")
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert err.value.exit_code == 12
    assert err.value.details == {"reason": "store_unwritable", "retryable": True}
    assert env.stored() == 0


def test_no_transaction_starts_near_the_lease_expiry(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock moves 7 s at every budget reading; the writer renews before a transaction whenever
    less than the margin is left, so no write transaction starts with less than that."""
    env.gateway.tick = 7.0
    margins: list[float] = []
    renewals: list[int] = []
    real = env.store.transaction
    real_renew = env.lease.renew
    inside = {"renew": False}

    def renew(store=None):  # type: ignore[no-untyped-def]
        inside["renew"] = True
        renewals.append(1)
        try:
            return real_renew(store)
        finally:
            inside["renew"] = False

    def watched():  # type: ignore[no-untyped-def]
        if not inside["renew"]:
            now_ms = int(env.gateway.now().timestamp() * 1000)
            margins.append((env.lease.expires_ms - now_ms) / 1000)
        return real()

    monkeypatch.setattr(env.store, "transaction", watched)
    monkeypatch.setattr(env.lease, "renew", renew)
    env.write("a", [message(f"m{i}", i + 1, t=i) for i in range(60)])
    env.ingest("a", chunk_lines=2, file_budget=timedelta(hours=1))
    assert len(margins) >= 30
    assert renewals  # the 60 s margin was reached several times
    assert min(margins) >= 60.0  # the literal, so a changed constant is caught


# --- reread, deletion in batches, crash resume ------------------------------------------------


def many(prefix: str, n: int, out: int = 5) -> list[bytes]:
    return [message(f"{prefix}{i}", out, t=i % 3000) for i in range(n)]


def test_a_rewritten_file_is_reread_and_the_owner_is_repaired(env: Env) -> None:
    env.write("a", [message("m", 90, t=1), message("only-a", 4, t=2)])
    env.write("b", [message("m", 10, t=1)])
    env.ingest("a", session=SESSION_A)
    env.ingest("b", session=SESSION_B)
    assert rows(
        env,
        "SELECT session_id FROM events WHERE key = ? AND counted = 1",
        event_key(Kind.MESSAGE, "m"),
    ) == [(SESSION_A,)]
    env.write("a", [message("different", 3, t=9)])  # the owner file was rewritten
    result = env.ingest("a", session=SESSION_A)
    assert result.reason in (Reason.HEAD, Reason.TAIL, Reason.SHRUNK)
    got = dict(rows(env, "SELECT key, session_id FROM events WHERE counted = 1"))
    assert got[event_key(Kind.MESSAGE, "m")] == SESSION_B
    assert event_key(Kind.MESSAGE, "only-a") not in got
    assert got[event_key(Kind.MESSAGE, "different")] == SESSION_A
    assert rows(env, "SELECT rereading FROM files") == [(0,), (0,)]


def test_a_reread_resets_the_per_file_facts(env: Env) -> None:
    for i in range(7):  # seven better copies, so a's copy is the eighth and a later one evicts it
        env.write(f"c{i}", [message("m", 50, t=1)])
        env.ingest(f"c{i}", session=SESSION_B)
    env.write("a", [raw({"type": "zzz"}), b"junk\n", message("m", 5, t=1)])
    env.ingest("a")
    env.write("b", [message("m", 90, t=1)])
    env.ingest("b", session=SESSION_C)  # the ninth copy evicts a's
    facts = "SELECT parse_errors, unknown_lines, copies_dropped FROM files WHERE path = ?"
    a_path = bytes(env.path("a"))
    assert rows(env, facts, a_path) == [(1, 1, 1)]
    env.write("a", [message("other", 5, t=1)])
    env.ingest("a")
    assert rows(env, facts, a_path) == [(0, 0, 0)]


def test_an_evicted_copy_counts_in_its_own_file_not_the_evictors(env: Env) -> None:
    for i in range(7):
        env.write(f"c{i}", [message("m", 50, t=1)])
        env.ingest(f"c{i}", session=SESSION_B)
    env.write("a", [message("m", 5, t=1)])
    env.ingest("a")  # the eighth copy, stored
    env.write("b", [message("m", 90, t=1)])
    env.ingest("b", session=SESSION_C)  # better than all: evicts a's copy
    dropped = "SELECT copies_dropped FROM files WHERE path = ?"
    assert rows(env, dropped, bytes(env.path("a"))) == [(1,)]
    assert rows(env, dropped, bytes(env.path("b"))) == [(0,)]


def test_the_deletion_runs_in_batches_that_repair_every_owner(env: Env) -> None:
    n = 25
    env.write("a", many("k", n, out=50))
    env.write("b", many("k", n, out=5))
    env.ingest("a", session=SESSION_A)
    env.ingest("b", session=SESSION_B)
    assert rows(
        env, "SELECT count(*) FROM events WHERE counted = 1 AND session_id = ?", SESSION_A
    ) == [(n,)]
    env.write("a", [message("z", 1, t=1)])
    transactions: list[int] = []
    real = env.store.transaction
    import contextlib

    @contextlib.contextmanager
    def counting():  # type: ignore[no-untyped-def]
        with real() as conn:
            transactions.append(conn.execute("SELECT count(*) FROM events").fetchone()[0])
            yield conn

    env.store.transaction = counting  # type: ignore[method-assign]
    env.ingest("a", session=SESSION_A, delete_batch_size=7)
    env.store.transaction = real  # type: ignore[method-assign]
    # reset, batches 7+7+7+4, then the one chunk: every batch deleted at most 7 rows
    deltas = [x - y for x, y in zip(transactions[1:], transactions[2:], strict=False)]
    assert all(d <= 7 for d in deltas)
    assert rows(
        env, "SELECT count(*) FROM events WHERE counted = 1 AND session_id = ?", SESSION_B
    ) == [(n,)]
    assert rows(env, "SELECT count(*) FROM events WHERE counted = 1") == [(n + 1,)]
    assert rows(env, "SELECT sum(rereading) FROM files") == [(0,)]


def test_a_crash_between_batches_resumes_with_rereading_set(env: Env) -> None:
    n = 30
    env.write("a", many("k", n, out=50))
    env.write("b", many("k", n, out=5))
    env.ingest("a", session=SESSION_A)
    env.ingest("b", session=SESSION_B)
    env.write("a", many("q", 4, out=7))
    env.gateway.readings = 0
    env.gateway.fail_monotonic_after = 3  # the read's own clock reads, then the batch budget check
    with pytest.raises(RuntimeError, match="simulated crash"):
        env.ingest("a", session=SESSION_A, delete_batch_size=4)
    env.gateway.fail_monotonic_after = None
    state = rows(env, "SELECT rereading, offset FROM files WHERE path = ?", bytes(env.path("a")))
    assert state == [(1, 0)]  # the reread was started and the deletion is not finished
    left = rows(
        env,
        "SELECT count(*) FROM events WHERE file_id = (SELECT id FROM files WHERE path = ?)",
        bytes(env.path("a")),
    )[0][0]
    assert 0 < left < n
    # Every key is still counted exactly once, owner repaired in the batches already committed.
    assert rows(
        env, "SELECT count(*) FROM (SELECT key FROM events GROUP BY key HAVING sum(counted) <> 1)"
    ) == [(0,)]
    result = env.ingest("a", session=SESSION_A, delete_batch_size=4)
    assert result.complete
    assert rows(env, "SELECT rereading FROM files") == [(0,), (0,)]
    got = rows(
        env,
        "SELECT session_id, count(*) FROM events WHERE counted = 1 GROUP BY session_id ORDER BY 1",
    )
    assert got == [(SESSION_A, 4), (SESSION_B, n)]


def test_a_budget_ends_the_deletion_and_the_next_run_finishes_it(env: Env) -> None:
    env.write("a", many("k", 20, out=50))
    env.ingest("a")
    env.write("a", many("q", 3, out=7))
    env.gateway.tick = 10.0
    first = env.gateway.monotonic()  # a deadline already passed for the second reading on
    result = env.ingest("a", delete_batch_size=5, run_deadline=first)
    assert result.stop is Stop.RUN_BUDGET and not result.reading
    assert rows(env, "SELECT rereading FROM files") == [(1,)]
    assert 0 < env.stored() < 20
    env.gateway.tick = 0.0
    result = env.ingest("a", delete_batch_size=5)
    assert result.complete and env.stored() == 3
    assert rows(env, "SELECT rereading FROM files") == [(0,)]


def test_force_rebuilds_and_gives_the_same_counted_events(env: Env) -> None:
    env.write(
        "a",
        [message("m", 9, t=1), tool_use("m2", "t1", t=2), tool_error("t1", t=3), turn(900, t=4)],
    )
    env.write("b", [message("m", 9, t=1, session=SESSION_A)])
    env.ingest("a", session=SESSION_A)
    env.ingest("b", session=SESSION_B)
    before = env.counted()
    env.ingest("b", session=SESSION_B, force=True)
    env.ingest("a", session=SESSION_A, force=True)
    assert env.counted() == before


def test_the_ingest_lease_guard_is_the_real_lease_target(env: Env) -> None:
    assert isinstance(env.lease.target, IngestTarget)


def test_a_big_file_is_written_and_rebuilt_in_bounded_transactions(env: Env) -> None:
    """Scaled down (6,000 events, chunks of 500 and batches of 400 instead of 2,000 and 2,000, so
    the test is quick): every write transaction changes a number of rows bounded by its batch, and
    a rebuild never holds one over the whole file."""
    n, chunk, batch = 6000, 500, 400
    env.write("a", [message(f"k{i}", 5, t=i % 3000) for i in range(n)])
    env.write("b", [message(f"k{i}", 4, t=i % 3000) for i in range(0, n, 3)])
    changes: list[int] = []
    real = env.store.transaction

    import contextlib

    @contextlib.contextmanager
    def measuring():  # type: ignore[no-untyped-def]
        with real() as conn:
            start = conn.total_changes
            yield conn
            changes.append(conn.total_changes - start)

    env.store.transaction = measuring  # type: ignore[method-assign]
    env.ingest("b", session=SESSION_B, chunk_events=chunk)
    env.ingest("a", session=SESSION_A, chunk_events=chunk)
    assert max(changes) <= 6 * chunk  # events, owner flips, identity and file rows of one chunk
    changes.clear()
    env.ingest("a", session=SESSION_A, force=True, chunk_events=chunk, delete_batch_size=batch)
    assert max(changes) <= 6 * max(chunk, batch)
    assert len(changes) >= n // batch + n // chunk  # many short transactions, not one
    env.store.transaction = real  # type: ignore[method-assign]
    assert rows(env, "SELECT count(*) FROM events WHERE counted = 1") == [(n,)]
    assert rows(
        env, "SELECT count(*) FROM events WHERE counted = 1 AND session_id = ?", SESSION_A
    ) == [(n,)]


# --- review round 1 ---------------------------------------------------------------------------


def test_the_largest_possible_line_is_never_split_at_the_production_caps(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1,000 error results plus a denial and an interruption are 1,002 events: with 999 pending
    the look-ahead must close the chunk first (a cap of 2,001 events would otherwise commit)."""
    sizes: list[int] = []
    real = eventstore.place_events
    monkeypatch.setattr(
        eventstore, "place_events", lambda c, f, p: (sizes.append(len(p)), real(c, f, p))[1]
    )
    widest = raw(
        {
            "type": "user",
            "uuid": "u-wide",
            "timestamp": ts(1),
            "toolDenialKind": "user-rejected",
            "interruptedMessageId": "m-x",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": True}
                    for i in range(1000)
                ]
            },
        }
    )
    env.write("a", [message(f"m{i}", 1, t=i % 3000) for i in range(999)] + [widest])
    env.ingest("a")
    assert sizes == [999, 1002]


def test_a_failed_second_chunk_leaves_the_offset_and_facts_of_the_lines_stored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event-cap path commits the state before the line that would pass the cap."""
    lines = [
        message("m1", 1, t=1, tools=(("t1", "Bash"),)),  # 2 events
        b"junk\n",  # a parse error with no events
        message("m2", 1, t=2, tools=(("t2", "Bash"),)),  # 4 events
        message("m3", 1, t=50, tools=(("t3", "Bash"),)),  # would make 6 > 5
    ]
    env.write("a", lines)
    real = env.lease.check_still_holder
    calls = {"n": 0}

    def flaky(conn: sqlite3.Connection) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash in the second chunk")
        real(conn)

    monkeypatch.setattr(env.lease, "check_still_holder", flaky)
    with pytest.raises(RuntimeError):
        env.ingest("a", chunk_events=5)
    monkeypatch.undo()
    assert env.stored() == 4
    (offset, errs, max_ts) = rows(env, "SELECT offset, parse_errors, max_ts FROM files")[0]
    assert offset == len(b"".join(lines[:3]))
    assert errs == 1
    assert max_ts == 1620284402000  # t=2, not the t=50 of the line left for the next chunk


def test_the_owner_with_more_than_eight_copies_can_depend_on_chunk_boundaries(
    env: Env, tmp_path: Path
) -> None:
    """Pins a documented residual risk (Residual risks, more than 8 copies of a key): a file's
    first line of a message is dropped when 8 copies are better, and a later line of the same
    message that is as good then enters as a new copy when it is read in a later chunk."""
    first, second = message("m", 5, t=30), message("m", 5, t=10)

    def owner(e: Env, chunk_lines: int) -> tuple[int, int, int]:
        for i in range(8):
            e.write(f"f{i}", [message("m", 5, t=20)])
            e.ingest(f"f{i}")
        e.write("z", [first, second])
        e.ingest("z", chunk_lines=chunk_lines)
        (file_path, offset, when) = rows(
            e, "SELECT file_path, line_offset, ts FROM events WHERE counted = 1"
        )[0]
        assert e.stored() == 8 and len(rows(e, "SELECT 1 FROM events WHERE counted = 1")) == 1
        return (file_path == bytes(e.path("z")), offset, when)

    assert owner(env, chunk_lines=100) == (False, 0, 1620284420000)  # f0 owns; z's copy dropped
    other = make_env(tmp_path / "y")
    try:
        assert owner(other, chunk_lines=1) == (True, len(first), 1620284410000)
    finally:
        other.close()
