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
    with pytest.raises(sqlite3.IntegrityError):  # a hash is exactly 16 characters, not a sentence
        conn.execute(
            "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 3, 0, 'message')",
            ("a whole sentence of user text",),
        )
    with pytest.raises(sqlite3.IntegrityError):  # a kind is one of seven words
        conn.execute(
            "INSERT INTO events (key, file_id, line_offset, kind) VALUES (?, 4, 0, 'hello')",
            ("c" * 16,),
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
