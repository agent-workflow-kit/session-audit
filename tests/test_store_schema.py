"""The store holds no conversation text: every column is pinned here, so adding one is a reviewed
change (CLAUDE.md rule 4), and the migration registry follows its own rules."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from session_audit.migrations import MIGRATIONS, Migration
from session_audit.store import column_names, open_store

from .storehelp import ClockedGateway, state_path

N, T, B, R, V = "INTEGER", "TEXT", "BLOB", "REAL", ""

COLUMNS: dict[str, list[tuple[str, str]]] = {
    "files": [("id", N), ("path", B), ("dev", N), ("inode", N), ("size", N), ("offset", N),
        ("head_len", N), ("head_sha", T), ("tail_sha", T), ("rules_version", N), ("skip_to_lf", N),
        ("parse_errors", N), ("unknown_lines", N), ("min_ts", N), ("max_ts", N), ("last_ts", N),
        ("first_cwd", T), ("first_entrypoint", T), ("last_version", T), ("cost_state_usd", R),
        ("cost_invalid", N), ("copies_dropped", N), ("rereading", N), ("last_read", N),
        ("status", T)],
    "sessions": [("session_id", T), ("project_dir", T), ("main_file_id", N), ("first_ingest_at", N),
        ("last_ingest_at", N)],
    "agents": [("session_id", T), ("agent_id", T), ("parent_tool_call_id", T), ("agent_type", T),
        ("file_id", N)],
    "events": [("id", N), ("key", T), ("ref", T), ("file_id", N), ("file_path", B),
        ("line_offset", N), ("kind", T), ("ts", N), ("slot", N), ("session_id", T), ("agent_id", T),
        ("model", T), ("tool", T), ("input_tokens", N), ("output_tokens", N),
        ("cache_read_tokens", N), ("cache_write_5m_tokens", N), ("cache_write_1h_tokens", N),
        ("seconds", R), ("native", N), ("counted", N)],
    "pruned_sessions": [("session_id", T), ("pruned_at", N)],
    "harness_runs": [("csession_id", T), ("updated_at", T), ("claude_session_ids", T), ("kind", T),
        ("labels", T), ("run", N), ("outcome", T), ("end_reason", T), ("cost_usd", R),
        ("turns", N), ("skill", T), ("repo", T), ("final", N)],
    "retros": [("id", T), ("key", T), ("state", T), ("reason", T), ("scope", T),
        ("resolved_scope", T), ("skill", T), ("model", T), ("created_at", N), ("started_at", N),
        ("finished_at", N), ("csession_id", T), ("session_map", T), ("cost_usd", R), ("turns", N),
        ("outcome", T), ("sandbox", N), ("again_of", T), ("lease_pid", N), ("lease_start", T),
        ("lease_expires_at", N), ("counts", T), ("dropped", T), ("redacted", N), ("warnings", T),
        ("run_token", T), ("digest_sha256", T)],
    "retro_artifacts": [("retro_id", T), ("artifact", T), ("summary", T)],
    "retro_starts": [("day", T), ("retro_id", T)],
    "retro_sessions": [("csession_id", T), ("retro_id", T), ("csession_dir", T), ("private_dir", T),
        ("project_dir", T), ("cleanup_state", T), ("forgotten_at", N), ("created_at", N)],
    "meta": [("key", T), ("value", V)],
    "schema_migrations": [("number", N), ("name", T), ("applied_at", N)],
}  # fmt: skip

# The only columns that may hold model-written text: validated and redacted before they are
# stored (interface.md, Retro artifact). Every other TEXT column holds an id, a name, a hash, a
# date, a path or a small JSON list of those.
UNTRUSTED = {("retro_artifacts", "artifact"), ("retro_artifacts", "summary")}
TEXT_NAMES = re.compile(
    r"text|content|prompt|message|body|output(?!_tokens)|input(?!_tokens)|transcript|line$|stdout|stderr"
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    store = open_store(ClockedGateway(), state_path(tmp_path))
    yield store.conn
    store.close()


def test_every_column_is_pinned(conn: sqlite3.Connection) -> None:
    assert column_names(conn) == COLUMNS


def test_no_column_is_named_for_conversation_text() -> None:
    offenders = [
        (t, c)
        for t, cols in COLUMNS.items()
        for c, _ in cols
        if TEXT_NAMES.search(c) and (t, c) not in UNTRUSTED
    ]
    assert offenders == []


def test_the_pin_catches_a_new_text_column(tmp_path: Path) -> None:
    store = open_store(ClockedGateway(), state_path(tmp_path))
    store.conn.execute("ALTER TABLE events ADD COLUMN prompt_text TEXT")
    assert column_names(store.conn) != COLUMNS
    assert TEXT_NAMES.search("prompt_text")
    store.close()


def test_text_columns_that_could_carry_a_message_are_length_limited(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 1, 0, 'message')",
        ("a" * 16,),
    )
    for column in ("model", "tool", "agent_id"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO events (key, file_id, line_offset, kind, {column}) "
                "VALUES (?, 2, 0, 'message', ?)",
                ("b" * 16, "x" * 129),
            )
    for bad in ("short", "a whole paragraph of user text, much longer than any hash is " * 2):
        with pytest.raises(sqlite3.IntegrityError):  # a hash is 16 to 64 characters
            conn.execute(
                "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 3, 0, 'message')",
                (bad,),
            )
    with pytest.raises(sqlite3.IntegrityError):  # a kind is one of seven words
        conn.execute(
            "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 4, 0, 'hello')",
            ("c" * 16,),
        )


# A valid row for each table: the columns without a default that the schema requires.
BASE_ROWS: dict[str, dict[str, object]] = {
    "files": {"path": b"/p", "dev": 1, "inode": 1},
    "sessions": {"session_id": "s", "project_dir": "p", "first_ingest_at": 0, "last_ingest_at": 0},
    "agents": {"session_id": "s", "agent_id": "a"},
    "events": {"key": "k" * 16, "file_id": 1, "line_offset": 0, "kind": "turn"},
    "pruned_sessions": {"session_id": "s", "pruned_at": 0},
    "harness_runs": {"csession_id": "c", "updated_at": "u"},
    "retros": {"id": "ra-aaaaaaaa", "key": "k" * 64, "state": "done", "created_at": 0},
    "retro_artifacts": {"retro_id": "r", "artifact": "{}"},
    "retro_starts": {"day": "2026-01-01", "retro_id": "r"},
    "retro_sessions": {
        "csession_id": "c",
        "retro_id": "r",
        "csession_dir": "d",
        "project_dir": "p",
        "created_at": 0,
    },
    "meta": {"key": "k", "value": "v"},
    "schema_migrations": {"number": 1, "name": "n", "applied_at": 0},
}
JSON_COLUMNS = {
    ("harness_runs", "claude_session_ids"),
    ("harness_runs", "labels"),
    ("retros", "resolved_scope"),
    ("retros", "session_map"),
    ("retros", "counts"),
    ("retros", "dropped"),
    ("retros", "warnings"),
}


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))


def test_the_base_rows_cover_every_table(conn: sqlite3.Connection) -> None:
    assert set(BASE_ROWS) == set(COLUMNS)
    for table, row in BASE_ROWS.items():
        _insert(conn, table, row)  # a valid row is accepted, so a refusal below is the column


def _oversize_cases() -> list[tuple[str, str]]:
    cases = [
        (t, c)
        for t, cols in COLUMNS.items()
        for c, kind in cols
        if (kind == T or (t, c) == ("meta", "value")) and (t, c) not in UNTRUSTED
    ]
    assert len(cases) > 40
    return cases


@pytest.mark.parametrize(("table", "column"), _oversize_cases())
def test_every_text_column_refuses_an_oversize_value(
    conn: sqlite3.Connection, table: str, column: str
) -> None:
    big = "x" * 70_000
    value = f'"{big}"' if (table, column) in JSON_COLUMNS else big  # valid JSON: only size fails
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, table, {**BASE_ROWS[table], column: value})


@pytest.mark.parametrize(("table", "column"), sorted(JSON_COLUMNS))
def test_a_json_column_refuses_text_that_is_not_json(
    conn: sqlite3.Connection, table: str, column: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, table, {**BASE_ROWS[table], column: "not json at all"})


def test_untrusted_columns_are_the_only_unbounded_text(conn: sqlite3.Connection) -> None:
    _insert(
        conn,
        "retro_artifacts",
        {"retro_id": "r", "artifact": "x" * 70_000, "summary": "y" * 70_000},
    )


def test_the_event_key_is_unique_per_file(conn: sqlite3.Connection) -> None:
    sql = (
        "INSERT INTO events (key, file_id, line_offset, kind) "
        "VALUES ('kkkkkkkkkkkkkkkk', ?, 0, 'turn')"
    )
    conn.execute(sql, (1,))
    conn.execute(sql, (2,))  # another file: a copy
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (1,))


def test_the_indexes_of_events_exist(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA index_list(events)").fetchall()
    columns = {tuple(i[2] for i in conn.execute(f"PRAGMA index_info({r[1]})")) for r in rows}
    assert {("key", "file_id"), ("file_id",), ("session_id", "counted")} <= columns


def test_a_retro_state_is_one_of_the_seven(conn: sqlite3.Connection) -> None:
    sql = "INSERT INTO retros (id, key, state, created_at) VALUES ('ra-aaaaaaaa', ?, ?, 0)"
    conn.execute(sql, ("k" * 64, "digesting"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO retros (id, key, state, created_at) VALUES ('ra-bbbbbbbb', ?, 'oops', 0)",
            ("k" * 64,),
        )


# --- the registry ---------------------------------------------------------------------------


def test_registry_numbers_are_unique_ordered_and_start_with_800() -> None:
    numbers = [m.number for m in MIGRATIONS]
    assert numbers == sorted(set(numbers)) and numbers[0] == 800


def test_every_registered_migration_is_additive() -> None:
    for migration in MIGRATIONS:
        for statement in migration.statements:
            assert not re.search(r"\b(DROP|DELETE|UPDATE|RENAME|REPLACE)\b", statement, re.I)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE files",
        "ALTER TABLE files RENAME TO f",
        "ALTER TABLE files DROP COLUMN path",
        "DELETE FROM meta",
        "UPDATE meta SET value = 1",
        "CREATE TABLE a (x); DROP TABLE a",
        "CREATE TABLE a (x); CREATE TABLE b (y)",
        "INSERT INTO files VALUES (1)",
        "CREATE TABLE a AS SELECT 1",
        "PRAGMA user_version = 3",
        "",
    ],
)
def test_a_non_additive_statement_is_refused(statement: str) -> None:
    with pytest.raises(ValueError):
        Migration(900, "bad", (statement,))


@pytest.mark.parametrize(
    ("number", "name"),
    [(0, "x"), (99, "x"), (900, "Bad"), (900, ""), (900, "has space"), (1.5, "x")],
)
def test_a_malformed_migration_identity_is_refused(number: int, name: str) -> None:
    with pytest.raises(ValueError):
        Migration(number, name, ("CREATE TABLE a (x)",))


def test_a_migration_may_add_a_column_an_index_and_a_meta_key() -> None:
    Migration(
        901,
        "fine",
        (
            "ALTER TABLE files ADD COLUMN extra INTEGER",
            "CREATE INDEX files_extra ON files (extra)",
            "INSERT INTO meta (key, value) VALUES ('k', 1)",
        ),
    )


# --- what a migration may write -------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO meta (key, value) VALUES ('k', 1) ON CONFLICT (key) DO UPDATE SET value = 2",
        "INSERT INTO meta (key, value) VALUES ('k', 1) ON CONFLICT DO NOTHING",
        "insert into meta (key, value) select name, 1 from sqlite_master",
        "INSERT INTO meta SELECT 'a', 1",
        "INSERT INTO meta (key, value) SELECT key, value FROM meta",
        "INSERT INTO meta (key, value) VALUES ('k', (SELECT count(*) FROM files))",
        "INSERT INTO meta (key, value) VALUES ('k', 1) RETURNING value",
        "INSERT INTO meta (key, value) VALUES ('a', 1), ('b', 2)",
        "INSERT INTO meta (key, value) WITH x AS (SELECT 1) SELECT 'a', 1 FROM x",
        "WITH x AS (SELECT 1) INSERT INTO meta (key, value) SELECT 'a', 1 FROM x",
        "INSERT INTO meta (value, key) VALUES (1, 'k')",
        "INSERT INTO meta VALUES ('k', 1)",
        "INSERT INTO meta (key) VALUES ('k')",
        "INSERT INTO meta_other (key, value) VALUES ('k', 1)",
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('k', 1)",
        "INSERT INTO meta (key, value) VALUES ('k', 1) -- x",
    ],
)
def test_a_meta_write_other_than_a_literal_seed_is_refused(statement: str) -> None:
    with pytest.raises(ValueError):
        Migration(900, "bad", (statement,))


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO meta (key, value) VALUES ('k', 1)",
        "insert  or  ignore into META ( key , value ) values ( 'a b', 'it''s' )",
        "INSERT INTO meta (key, value)\nVALUES ('k', -1.5);",
        "INSERT INTO meta (key, value) VALUES ('k', NULL)",
    ],
)
def test_a_literal_meta_seed_is_accepted(statement: str) -> None:
    Migration(900, "fine", (statement,))


def test_event_hash_columns_accept_16_to_64_characters(conn: sqlite3.Connection) -> None:
    for i, size in enumerate((16, 40, 64)):
        conn.execute(
            "INSERT INTO events (key, ref, file_id, line_offset, kind) VALUES (?, ?, ?, 0, 'turn')",
            ("a" * size, "b" * size, i),
        )
    for size in (15, 65):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 9, 0, 'turn')",
                ("a" * size,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (key, ref, file_id, line_offset, kind) "
                "VALUES ('a' || hex(randomblob(8)), ?, 9, 0, 'turn')",
                ("b" * size,),
            )


def test_paths_and_names_take_4096_characters(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, project_dir, first_ingest_at, last_ingest_at) "
        "VALUES ('s', ?, 0, 0)",
        ("p" * 4096,),
    )
