"""Migration 800 (issue #8): the tables of the data model (design.md, Data model).

The store holds counts, ids, hashes, timestamps, names, paths and harness facts, never
conversation text. The one place for model-written text is ``retro_artifacts`` (validated and
redacted before it is stored); ``tests/test_store_schema.py`` pins every column.

Conventions: a timestamp is an integer of epoch milliseconds (``ts``) and ``slot`` is the epoch
second divided by 900; a hash is lowercase hex text; a path read from a transcript is a BLOB of
raw bytes (``files.path``, ``events.file_path``). Every TEXT column except the two untrusted ones
of ``retro_artifacts`` has a length CHECK (names 128, paths 4096, hashes 64 or 128, lists and maps
a bound sized for their content, with ``json_valid``); ``docs/design.md`` (Data model) states the
bounds. Migrations are additive, so a bound cannot be loosened later: they are generous on purpose.
"""

from __future__ import annotations

from .base import Migration

NAME = 128  # ids, names, kinds, hashes (a hash is 64 hex characters; 128 leaves room)
PATH = 4096  # a path or a project directory name
LIST_SMALL = 4096  # a short JSON list or map (counts, dropped, the 64 Claude session ids)
LIST_MEDIUM = 16_384  # labels
LIST_LARGE = 65_536  # up to 500 sessions (retro.max_sessions) with their ids


def _at_most(column: str, size: int) -> str:
    return f"CHECK (length({column}) <= {size})"


def _short(column: str) -> str:
    return _at_most(column, NAME)


def _path(column: str) -> str:
    return _at_most(column, PATH)


def _json(column: str, size: int) -> str:
    return f"CHECK (length({column}) <= {size} AND json_valid({column}))"


MIGRATION = Migration(
    800,
    "initial_schema",
    (
        f"""CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            path BLOB NOT NULL UNIQUE,
            dev INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            offset INTEGER NOT NULL DEFAULT 0,
            head_len INTEGER NOT NULL DEFAULT 0,
            head_sha TEXT {_short("head_sha")},
            tail_sha TEXT {_short("tail_sha")},
            rules_version INTEGER NOT NULL DEFAULT 0,
            skip_to_lf INTEGER NOT NULL DEFAULT 0,
            parse_errors INTEGER NOT NULL DEFAULT 0,
            unknown_lines INTEGER NOT NULL DEFAULT 0,
            min_ts INTEGER,
            max_ts INTEGER,
            last_ts INTEGER,
            first_cwd TEXT {_path("first_cwd")},
            first_entrypoint TEXT {_short("first_entrypoint")},
            last_version TEXT {_short("last_version")},
            cost_state_usd REAL,
            cost_invalid INTEGER NOT NULL DEFAULT 0,
            copies_dropped INTEGER NOT NULL DEFAULT 0,
            rereading INTEGER NOT NULL DEFAULT 0,
            last_read INTEGER,
            status TEXT {_at_most("status", 64)}
        )""",
        f"""CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY {_short("session_id")},
            project_dir TEXT NOT NULL {_path("project_dir")},
            main_file_id INTEGER,
            first_ingest_at INTEGER NOT NULL,
            last_ingest_at INTEGER NOT NULL
        )""",
        f"""CREATE TABLE agents (
            session_id TEXT NOT NULL {_short("session_id")},
            agent_id TEXT NOT NULL {_short("agent_id")},
            parent_tool_call_id TEXT {_short("parent_tool_call_id")},
            agent_type TEXT {_short("agent_type")},
            file_id INTEGER,
            PRIMARY KEY (session_id, agent_id)
        )""",
        f"""CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL CHECK (length(key) BETWEEN 16 AND 64),
            ref TEXT CHECK (length(ref) BETWEEN 16 AND 64),
            file_id INTEGER NOT NULL,
            file_path BLOB,
            line_offset INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('message', 'tool_call', 'tool_error', 'denial',
                'interruption', 'turn', 'compaction')),
            ts INTEGER,
            slot INTEGER,
            session_id TEXT {_short("session_id")},
            agent_id TEXT {_short("agent_id")},
            model TEXT {_short("model")},
            tool TEXT {_short("tool")},
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
            seconds REAL,
            native INTEGER NOT NULL DEFAULT 0,
            counted INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE UNIQUE INDEX events_key_file ON events (key, file_id)",
        "CREATE INDEX events_file ON events (file_id)",
        "CREATE INDEX events_session_counted ON events (session_id, counted)",
        f"""CREATE TABLE pruned_sessions (
            session_id TEXT PRIMARY KEY {_short("session_id")},
            pruned_at INTEGER NOT NULL
        )""",
        f"""CREATE TABLE harness_runs (
            csession_id TEXT PRIMARY KEY {_short("csession_id")},
            updated_at TEXT NOT NULL {_at_most("updated_at", 64)},
            claude_session_ids TEXT NOT NULL DEFAULT '[]' {_json("claude_session_ids", LIST_SMALL)},
            kind TEXT {_short("kind")},
            labels TEXT NOT NULL DEFAULT '[]' {_json("labels", LIST_MEDIUM)},
            run INTEGER,
            outcome TEXT {_short("outcome")},
            end_reason TEXT {_short("end_reason")},
            cost_usd REAL,
            turns INTEGER,
            skill TEXT {_short("skill")},
            repo TEXT {_path("repo")},
            final INTEGER NOT NULL DEFAULT 0
        )""",
        f"""CREATE TABLE retros (
            id TEXT PRIMARY KEY CHECK (length(id) = 11),
            key TEXT NOT NULL CHECK (length(key) = 64),
            state TEXT NOT NULL CHECK (state IN ('digesting', 'running', 'validating', 'done',
                'failed', 'escalated', 'canceled')),
            reason TEXT {_short("reason")},
            scope TEXT NOT NULL DEFAULT '' {_path("scope")},
            resolved_scope TEXT NOT NULL DEFAULT '[]' {_json("resolved_scope", LIST_LARGE)},
            skill TEXT {_short("skill")},
            model TEXT {_short("model")},
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            csession_id TEXT {_short("csession_id")},
            session_map TEXT NOT NULL DEFAULT '{{}}' {_json("session_map", LIST_LARGE)},
            cost_usd REAL,
            turns INTEGER,
            outcome TEXT {_short("outcome")},
            sandbox INTEGER,
            again_of TEXT {_short("again_of")},
            lease_pid INTEGER,
            lease_start TEXT {_short("lease_start")},
            lease_expires_at INTEGER,
            counts TEXT NOT NULL DEFAULT '{{}}' {_json("counts", LIST_SMALL)},
            dropped TEXT NOT NULL DEFAULT '{{}}' {_json("dropped", LIST_SMALL)},
            redacted INTEGER NOT NULL DEFAULT 0,
            warnings TEXT NOT NULL DEFAULT '[]' {_json("warnings", LIST_SMALL)},
            run_token TEXT {_short("run_token")},
            digest_sha256 TEXT CHECK (length(digest_sha256) = 64)
        )""",
        "CREATE INDEX retros_key ON retros (key)",
        "CREATE INDEX retros_created ON retros (created_at)",
        f"""CREATE TABLE retro_artifacts (
            retro_id TEXT PRIMARY KEY {_short("retro_id")},
            artifact TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        )""",
        f"""CREATE TABLE retro_starts (
            day TEXT NOT NULL CHECK (length(day) = 10),
            retro_id TEXT NOT NULL {_short("retro_id")},
            PRIMARY KEY (day, retro_id)
        )""",
        f"""CREATE TABLE retro_sessions (
            csession_id TEXT PRIMARY KEY {_short("csession_id")},
            retro_id TEXT NOT NULL {_short("retro_id")},
            csession_dir TEXT NOT NULL {_path("csession_dir")},
            private_dir TEXT {_path("private_dir")},
            project_dir TEXT NOT NULL {_path("project_dir")},
            cleanup_state TEXT NOT NULL DEFAULT 'pending' CHECK (cleanup_state IN ('pending',
                'done', 'incomplete', 'skipped')),
            forgotten_at INTEGER,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX retro_sessions_retro ON retro_sessions (retro_id)",
        "CREATE INDEX retro_sessions_project ON retro_sessions (project_dir)",
        """CREATE TABLE meta (
            key TEXT PRIMARY KEY CHECK (length(key) <= 64),
            value CHECK (typeof(value) <> 'text' OR length(value) <= 4096)
        )""",
        "INSERT INTO meta (key, value) VALUES ('rules_version', 1)",
    ),
)
