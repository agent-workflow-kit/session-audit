"""Migration 800 (issue #8): the tables of the data model (design.md, Data model).

The store holds counts, ids, hashes, timestamps, names, paths and harness facts, never
conversation text. The one place for model-written text is ``retro_artifacts`` (validated and
redacted before it is stored); ``tests/test_store_schema.py`` pins every column.

Conventions: a timestamp is an integer of epoch milliseconds (``ts``) and ``slot`` is the epoch
second divided by 900; a hash is lowercase hex text; a path read from a transcript is a BLOB of
raw bytes (``files.path``, ``events.file_path``). Free-ish text columns have length CHECKs.
"""

from __future__ import annotations

from .base import Migration

_NAME = "CHECK (length({c}) <= 128)"


def _short(column: str) -> str:
    return _NAME.format(c=column)


MIGRATION = Migration(
    800,
    "initial_schema",
    (
        """CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            path BLOB NOT NULL UNIQUE,
            dev INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            offset INTEGER NOT NULL DEFAULT 0,
            head_len INTEGER NOT NULL DEFAULT 0,
            head_sha TEXT,
            tail_sha TEXT,
            rules_version INTEGER NOT NULL DEFAULT 0,
            skip_to_lf INTEGER NOT NULL DEFAULT 0,
            parse_errors INTEGER NOT NULL DEFAULT 0,
            unknown_lines INTEGER NOT NULL DEFAULT 0,
            min_ts INTEGER,
            max_ts INTEGER,
            last_ts INTEGER,
            first_cwd TEXT,
            first_entrypoint TEXT CHECK (length(first_entrypoint) <= 128),
            last_version TEXT CHECK (length(last_version) <= 128),
            cost_state_usd REAL,
            cost_invalid INTEGER NOT NULL DEFAULT 0,
            copies_dropped INTEGER NOT NULL DEFAULT 0,
            rereading INTEGER NOT NULL DEFAULT 0,
            last_read INTEGER,
            status TEXT CHECK (length(status) <= 64)
        )""",
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY CHECK (length(session_id) = 36),
            project_dir TEXT NOT NULL,
            main_file_id INTEGER,
            first_ingest_at INTEGER NOT NULL,
            last_ingest_at INTEGER NOT NULL
        )""",
        f"""CREATE TABLE agents (
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL {_short("agent_id")},
            parent_tool_call_id TEXT {_short("parent_tool_call_id")},
            agent_type TEXT {_short("agent_type")},
            file_id INTEGER,
            PRIMARY KEY (session_id, agent_id)
        )""",
        f"""CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL CHECK (length(key) = 16),
            ref TEXT CHECK (length(ref) = 16),
            file_id INTEGER NOT NULL,
            file_path BLOB,
            line_offset INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('message', 'tool_call', 'tool_error', 'denial',
                'interruption', 'turn', 'compaction')),
            ts INTEGER,
            slot INTEGER,
            session_id TEXT,
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
        """CREATE TABLE pruned_sessions (
            session_id TEXT PRIMARY KEY,
            pruned_at INTEGER NOT NULL
        )""",
        f"""CREATE TABLE harness_runs (
            csession_id TEXT PRIMARY KEY CHECK (length(csession_id) <= 128),
            updated_at TEXT NOT NULL CHECK (length(updated_at) <= 64),
            claude_session_ids TEXT NOT NULL DEFAULT '[]',
            kind TEXT {_short("kind")},
            labels TEXT NOT NULL DEFAULT '[]',
            run INTEGER,
            outcome TEXT {_short("outcome")},
            end_reason TEXT {_short("end_reason")},
            cost_usd REAL,
            turns INTEGER,
            skill TEXT {_short("skill")},
            repo TEXT,
            final INTEGER NOT NULL DEFAULT 0
        )""",
        f"""CREATE TABLE retros (
            id TEXT PRIMARY KEY CHECK (length(id) = 11),
            key TEXT NOT NULL CHECK (length(key) = 64),
            state TEXT NOT NULL CHECK (state IN ('digesting', 'running', 'validating', 'done',
                'failed', 'escalated', 'canceled')),
            reason TEXT {_short("reason")},
            scope TEXT NOT NULL DEFAULT '',
            resolved_scope TEXT NOT NULL DEFAULT '[]',
            skill TEXT {_short("skill")},
            model TEXT {_short("model")},
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            csession_id TEXT {_short("csession_id")},
            session_map TEXT NOT NULL DEFAULT '{{}}',
            cost_usd REAL,
            turns INTEGER,
            outcome TEXT {_short("outcome")},
            sandbox INTEGER,
            again_of TEXT {_short("again_of")},
            lease_pid INTEGER,
            lease_start TEXT {_short("lease_start")},
            lease_expires_at INTEGER,
            counts TEXT NOT NULL DEFAULT '{{}}',
            dropped TEXT NOT NULL DEFAULT '{{}}',
            redacted INTEGER NOT NULL DEFAULT 0,
            warnings TEXT NOT NULL DEFAULT '[]',
            run_token TEXT {_short("run_token")},
            digest_sha256 TEXT CHECK (length(digest_sha256) = 64)
        )""",
        "CREATE INDEX retros_key ON retros (key)",
        "CREATE INDEX retros_created ON retros (created_at)",
        """CREATE TABLE retro_artifacts (
            retro_id TEXT PRIMARY KEY,
            artifact TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE retro_starts (
            day TEXT NOT NULL CHECK (length(day) = 10),
            retro_id TEXT NOT NULL,
            PRIMARY KEY (day, retro_id)
        )""",
        """CREATE TABLE retro_sessions (
            csession_id TEXT PRIMARY KEY CHECK (length(csession_id) <= 128),
            retro_id TEXT NOT NULL,
            csession_dir TEXT NOT NULL,
            private_dir TEXT,
            project_dir TEXT NOT NULL CHECK (length(project_dir) <= 200),
            cleanup_state TEXT NOT NULL DEFAULT 'pending' CHECK (cleanup_state IN ('pending',
                'done', 'incomplete', 'skipped')),
            forgotten_at INTEGER,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX retro_sessions_retro ON retro_sessions (retro_id)",
        "CREATE INDEX retro_sessions_project ON retro_sessions (project_dir)",
        """CREATE TABLE meta (
            key TEXT PRIMARY KEY CHECK (length(key) <= 64),
            value
        )""",
        "INSERT INTO meta (key, value) VALUES ('rules_version', 1)",
    ),
)
