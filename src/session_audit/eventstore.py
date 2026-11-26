"""Storing events: dedupe keys, the deterministic owner of each key, chunked writes and the
reread deletion (issue #13; Counting rules 1 and Ingest rules 6 of ``docs/interface.md``).

The pieces ``ingest`` (#15) composes, from small to large:

- ``event_key(kind, id)`` and ``event_ref(tool_use_id)``: the hashes (``sha256`` of the kind, a NUL
  byte and the id, first 16 hex characters). The kind is inside the key, so kinds never collide.
- ``place_events(conn, file, events)``: the within-file rule and ``place_copy`` for a list of
  ``(Event, line offset)`` pairs, inside a transaction the caller owns.
- ``delete_old_events(...)``: the batched deletion of a file's events that repairs the owner of
  every key it deleted from.
- ``read_file(...)``: one file, end to end: reset on a reread, finish a pending deletion, then
  read lines with no transaction open, parse them, and commit each chunk (events, owners, offset
  and per-file facts) in one transaction under the ingest lease.

Rules kept here:

- No transaction is open while lines are read or parsed. A chunk is parsed first and then written
  in one ``Store.transaction()``; every transaction starts with ``lease.check_still_holder`` and
  only after ``ensure_margin`` made sure the lease has at least ``LEASE_MARGIN_SECONDS`` left,
  so no transaction starts near the expiry. A busy store is the store's retryable exit 12 and is
  never caught here.
- A key keeps at most ``MAX_COPIES`` (8) stored copies, the best by the owner order (largest
  output tokens, earliest timestamp with null last, native copy, smaller path, smaller offset);
  only the best is counted. Placing one event reads at most 8 rows of its key, so a chunk and a
  deletion batch touch a bounded number of rows and ingest is linear in the lines read.
- Hash only: no text is stored; the sessions and agents rows carry ids and names from the path.
- All values reach SQL as parameters; nothing is built from an event value.

``events.session_id`` and ``agent_id`` come from the file (``Candidate``), never from what a line
claims (``Event.agent_id``, ``Event.line_session_id``); a line is *native* when it names no
session or its file's session.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol

from .incremental import (
    IncrementalRead,
    Progress,
    Reason,
    Stop,
    save_progress,
)
from .parser import MAX_BLOCKS, Event, FileFacts, Kind, ParseContext, parse_line
from .reader import AgentMeta, Candidate, Opened
from .store import Store
from .textsafe import fix_surrogates

MAX_COPIES = 8  # stored copies per key
CHUNK_LINES = 10_000
CHUNK_EVENTS = 2_000
DELETE_BATCH = 2_000
LEASE_MARGIN_SECONDS = 60.0  # a transaction never starts with less lease than this left
MAX_LINE_EVENTS = (
    MAX_BLOCKS + 2
)  # the most events one line yields: every block plus a denial and an interruption
NAME_CAP = 128
PATH_CAP = 4096

_FALLBACK_BLOCK_KINDS = frozenset({Kind.TOOL_CALL, Kind.TOOL_ERROR, Kind.MESSAGE})


class WriteGuard(Protocol):
    """What the writer needs of the ingest lease (``lease.Lease`` is one)."""

    expires_ms: int

    def check_still_holder(self, conn: sqlite3.Connection) -> None: ...
    def raise_if_lost(self) -> None: ...
    def renew(self, store: Store | None = None) -> bool: ...


# --- hashes -----------------------------------------------------------------------------------


def _hash(kind: str, ident: str) -> str:
    # surrogatepass: ids are already fixed by the parser, and this can never raise or collide.
    data = kind.encode() + b"\x00" + ident.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()[:16]


def event_key(kind: Kind, ident: str) -> str:
    """The dedupe key: ``sha256(kind + NUL + id)[:16]`` (Counting rules 1)."""
    return _hash(kind.value, ident)


def event_ref(tool_use_id: str) -> str:
    """The ref of a tool_error or denial: the key its tool call has."""
    return _hash(Kind.TOOL_CALL.value, tool_use_id)


def dedupe_id(event: Event, file_id: int, line_offset: int) -> str:
    """The id of an event: the parser's id, else the line position of this file (with the block
    index for the kinds that repeat on one line)."""
    if event.dedupe_id is not None:
        return event.dedupe_id
    base = f"{file_id}:{line_offset}"
    return f"{base}:{event.block}" if event.kind in _FALLBACK_BLOCK_KINDS else base


# --- the owner order --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRef:
    """The file a chunk of events belongs to (``events.session_id`` and ``agent_id`` come from
    here)."""

    file_id: int
    path: bytes
    session_id: str
    agent_id: str | None


@dataclass(slots=True)
class _Copy:
    """One stored (or about to be stored) copy of a key: the columns the owner order reads."""

    rowid: int | None
    file_id: int
    path: bytes
    offset: int
    output: int
    ts: int | None
    native: bool
    counted: bool = False


def _order(c: _Copy) -> tuple[int, bool, int, bool, bytes, int]:
    """Smaller is better: largest output, earliest timestamp (null last), native, smaller path,
    smaller offset."""
    return (-c.output, c.ts is None, c.ts or 0, not c.native, c.path, c.offset)


_COPY_COLUMNS = "id, file_id, file_path, line_offset, output_tokens, ts, native, counted"


def _copies(conn: sqlite3.Connection, key: str) -> list[_Copy]:
    rows = conn.execute(f"SELECT {_COPY_COLUMNS} FROM events WHERE key = ?", (key,)).fetchall()
    return [
        _Copy(r[0], r[1], bytes(r[2] or b""), r[3], r[4], r[5], bool(r[6]), bool(r[7]))
        for r in rows
    ]


def _settle(conn: sqlite3.Connection, copies: Sequence[_Copy], best: _Copy) -> None:
    """Make ``best`` the only counted copy among the stored ``copies``."""
    for c in copies:
        want = c is best
        if c.rowid is not None and c.counted != want:
            conn.execute("UPDATE events SET counted = ? WHERE id = ?", (int(want), c.rowid))
            c.counted = want


# --- placing events ---------------------------------------------------------------------------


@dataclass(slots=True)
class _New:
    event: Event
    offset: int  # the line offset of the message's first line
    native: bool


def _merge(file: FileRef, pending: Sequence[tuple[Event, int]]) -> dict[str, _New]:
    """The within-file rule over one chunk: one entry per key, in order of first appearance; a
    message keeps its largest output (a tie keeps the earliest line) and the offset of its first
    line, every other kind keeps its first line (equal output keeps the first)."""
    merged: dict[str, _New] = {}
    for event, offset in pending:
        key = event_key(event.kind, dedupe_id(event, file.file_id, offset))
        native = event.line_session_id is None or event.line_session_id == file.session_id
        kept = merged.get(key)
        if kept is None:
            merged[key] = _New(event, offset, native)
        elif event.output_tokens > kept.event.output_tokens:
            merged[key] = _New(event, kept.offset, native)
    return merged


_INSERT = (
    "INSERT INTO events (key, ref, file_id, file_path, line_offset, kind, ts, slot, session_id, "
    "agent_id, model, tool, input_tokens, output_tokens, cache_read_tokens, "
    "cache_write_5m_tokens, cache_write_1h_tokens, seconds, native, counted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_UPDATE = (
    "UPDATE events SET ref = ?, ts = ?, slot = ?, model = ?, tool = ?, input_tokens = ?, "
    "output_tokens = ?, cache_read_tokens = ?, cache_write_5m_tokens = ?, "
    "cache_write_1h_tokens = ?, seconds = ?, native = ? WHERE id = ?"
)


def _ref_of(event: Event) -> str | None:
    if event.ref_id is None or event.kind not in (Kind.TOOL_ERROR, Kind.DENIAL):
        return None
    return event_ref(event.ref_id)


def _cap(value: str | None) -> str | None:
    return None if value is None else fix_surrogates(value[:NAME_CAP])


def _values(event: Event) -> tuple[object, ...]:
    """The value columns shared by the insert and the update, from ``ts`` on."""
    return (
        event.ts,
        event.slot,
        _cap(event.model),
        _cap(event.tool),
        event.input_tokens,
        event.output_tokens,
        event.cache_read_tokens,
        event.cache_write_5m_tokens,
        event.cache_write_1h_tokens,
        event.seconds,
    )


def place_copy(
    conn: sqlite3.Connection,
    file: FileRef,
    key: str,
    new: _New,
    dropped: dict[int, int],
) -> None:
    """Store ``new`` as this file's copy of ``key`` and keep the 8 best copies, the best one
    counted. Reads at most 8 rows of the key; ``dropped`` collects the ``copies_dropped`` to add
    per file id (a copy not stored, or one evicted by a better copy)."""
    event = new.event
    copies = _copies(conn, key)
    mine = next((c for c in copies if c.file_id == file.file_id), None)
    if mine is not None:
        if event.output_tokens <= mine.output:
            return  # the first line (or the larger output) stays: nothing to change
        conn.execute(_UPDATE, (_ref_of(event), *_values(event), int(new.native), mine.rowid))
        mine.output, mine.ts, mine.native = event.output_tokens, event.ts, new.native
        candidate = mine
        pool = copies
    else:
        candidate = _Copy(
            None, file.file_id, file.path, new.offset, event.output_tokens, event.ts, new.native
        )
        pool = [*copies, candidate]
    pool.sort(key=_order)
    kept, evicted = pool[:MAX_COPIES], pool[MAX_COPIES:]
    for c in evicted:
        if c.rowid is not None:
            conn.execute("DELETE FROM events WHERE id = ?", (c.rowid,))
        dropped[c.file_id] = dropped.get(c.file_id, 0) + 1
    best = kept[0]
    if candidate.rowid is None and candidate in kept:
        conn.execute(_INSERT, _insert_row(key, event, file, new, candidate is best))
        candidate.counted = candidate is best
    _settle(conn, kept, best)


def _insert_row(
    key: str, event: Event, file: FileRef, new: _New, counted: bool
) -> tuple[object, ...]:
    ts, slot, model, tool, i, o, cr, w5, w1, seconds = _values(event)
    return (
        key,
        _ref_of(event),
        file.file_id,
        file.path,
        new.offset,
        event.kind.value,
        ts,
        slot,
        file.session_id,
        file.agent_id,
        model,
        tool,
        i,
        o,
        cr,
        w5,
        w1,
        seconds,
        int(new.native),
        int(counted),
    )


def place_events(
    conn: sqlite3.Connection, file: FileRef, pending: Sequence[tuple[Event, int]]
) -> dict[int, int]:
    """The within-file rule and ``place_copy`` for one chunk, inside the caller's transaction.
    Returns the ``copies_dropped`` to add per file id."""
    dropped: dict[int, int] = {}
    for key, new in _merge(file, pending).items():
        place_copy(conn, file, key, new, dropped)
    return dropped


# --- the files row ----------------------------------------------------------------------------

_FACT_COLUMNS = (
    "parse_errors, unknown_lines, min_ts, max_ts, last_ts, first_cwd, first_entrypoint, "
    "last_version, cost_state_usd, cost_invalid"
)


def load_facts(conn: sqlite3.Connection, path: bytes) -> FileFacts:
    """The stored per-file facts (``FileFacts()`` for a file with no row)."""
    row = conn.execute(f"SELECT {_FACT_COLUMNS} FROM files WHERE path = ?", (path,)).fetchone()
    if row is None:
        return FileFacts()
    return FileFacts(
        parse_errors=row[0],
        unknown_lines=row[1],
        min_ts=row[2],
        max_ts=row[3],
        last_ts=row[4],
        first_cwd=row[5],
        first_entrypoint=row[6],
        last_version=row[7],
        cost_state_usd=row[8],
        cost_invalid=bool(row[9]),
    )


def _write_facts(
    conn: sqlite3.Connection, path: bytes, facts: FileFacts, now_ms: int, *, reset: bool = False
) -> None:
    """Write the per-file facts; a reset (a full reread) also clears ``copies_dropped``."""
    conn.execute(
        "UPDATE files SET parse_errors = ?, unknown_lines = ?, min_ts = ?, max_ts = ?, "
        "last_ts = ?, first_cwd = ?, first_entrypoint = ?, last_version = ?, "
        "cost_state_usd = ?, cost_invalid = ?, last_read = ?"
        + (", copies_dropped = 0, rereading = 1" if reset else "")
        + " WHERE path = ?",
        (
            facts.parse_errors,
            facts.unknown_lines,
            facts.min_ts,
            facts.max_ts,
            facts.last_ts,
            _cap_path(facts.first_cwd),
            _cap(facts.first_entrypoint),
            _cap(facts.last_version),
            facts.cost_state_usd,
            int(facts.cost_invalid),
            now_ms,
            path,
        ),
    )


def _cap_path(value: str | None) -> str | None:
    return None if value is None else fix_surrogates(value[:PATH_CAP])


def ensure_identity(
    conn: sqlite3.Connection,
    candidate: Candidate,
    file_id: int,
    now_ms: int,
    meta: AgentMeta | None = None,
) -> None:
    """Create or touch the ``sessions`` and ``agents`` rows of a file from its ``Candidate``
    (identity only: no session value is derived here). The session id comes from the path, so a
    subagent file read before its parent still names its session."""
    project = fix_surrogates(candidate.project_dir[:PATH_CAP])
    conn.execute(
        "INSERT INTO sessions (session_id, project_dir, main_file_id, first_ingest_at, "
        "last_ingest_at) VALUES (?, ?, NULL, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET last_ingest_at = excluded.last_ingest_at",
        (candidate.session_id, project, now_ms, now_ms),
    )
    if candidate.kind == "main":
        conn.execute(
            "UPDATE sessions SET main_file_id = ? WHERE session_id = ?",
            (file_id, candidate.session_id),
        )
    elif candidate.agent_id is not None:
        conn.execute(
            "INSERT INTO agents (session_id, agent_id, parent_tool_call_id, agent_type, file_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, agent_id) DO UPDATE SET "
            "file_id = excluded.file_id, "
            "parent_tool_call_id = COALESCE(excluded.parent_tool_call_id, parent_tool_call_id), "
            "agent_type = COALESCE(excluded.agent_type, agent_type)",
            (
                candidate.session_id,
                candidate.agent_id,
                _cap(meta.tool_use_id) if meta else None,
                _cap(meta.agent_type) if meta else None,
                file_id,
            ),
        )


def _file_ref(conn: sqlite3.Connection, candidate: Candidate) -> FileRef:
    row = conn.execute("SELECT id FROM files WHERE path = ?", (candidate.path,)).fetchone()
    return FileRef(row[0], candidate.path, candidate.session_id, candidate.agent_id)


# --- deleting a file's events -----------------------------------------------------------------


def _delete_rows(conn: sqlite3.Connection, rows: Sequence[Sequence[object]]) -> None:
    """Delete the ``(id, key, counted)`` rows and repair the owner of every key a counted row was
    deleted from: the best of its remaining copies (at most 8 rows) becomes the counted one."""
    conn.executemany("DELETE FROM events WHERE id = ?", [(r[0],) for r in rows])
    for key in dict.fromkeys(str(r[1]) for r in rows if r[2]):
        remaining = _copies(conn, key)
        if remaining:
            _settle(conn, remaining, min(remaining, key=_order))


def delete_batch(
    conn: sqlite3.Connection, file_id: int, limit: int = DELETE_BATCH
) -> tuple[int, bool]:
    """Delete up to ``limit`` events of a file and repair the owner of every key a counted row was
    deleted from (the best of its remaining copies, at most 8 rows, becomes counted). Returns the
    number of rows deleted and whether the file has no events left. One transaction's worth of
    work, at most ``limit`` rows."""
    rows = conn.execute(
        "SELECT id, key, counted FROM events WHERE file_id = ? LIMIT ?", (file_id, limit)
    ).fetchall()
    _delete_rows(conn, rows)
    done = len(rows) < limit or (
        conn.execute("SELECT 1 FROM events WHERE file_id = ? LIMIT 1", (file_id,)).fetchone()
        is None
    )
    return len(rows), done


def delete_session_batch(
    conn: sqlite3.Connection, session_id: str, limit: int = DELETE_BATCH
) -> tuple[int, bool]:
    """``delete_batch`` for every event of a session (the main file's and its agents'): up to
    ``limit`` rows, owners repaired the same way, so a key counted in this session and stored in
    another session's file stays counted exactly once (the other copy is promoted). Returns the
    rows deleted and whether the session has no events left."""
    rows = conn.execute(
        "SELECT id, key, counted FROM events WHERE session_id = ? LIMIT ?", (session_id, limit)
    ).fetchall()
    _delete_rows(conn, rows)
    done = len(rows) < limit or (
        conn.execute("SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()
        is None
    )
    return len(rows), done


@dataclass
class Budget:
    """The budgets of one file: the run deadline and this file's own, on the gateway's
    monotonic clock."""

    store: Store
    run_deadline: float | None
    file_deadline: float

    def hit(self) -> Stop | None:
        now = self.store.gateway.monotonic()
        if self.run_deadline is not None and now >= self.run_deadline:
            return Stop.RUN_BUDGET
        if now >= self.file_deadline:
            return Stop.FILE_BUDGET
        return None


def _now_ms(store: Store) -> int:
    return int(store.gateway.now().timestamp() * 1000)


def ensure_margin(store: Store, lease: WriteGuard) -> None:
    """Renew the lease (its own short transaction) when less than the margin is left, and stop
    when it was lost. A write transaction therefore never starts near the expiry."""
    lease.raise_if_lost()
    if lease.expires_ms - _now_ms(store) < LEASE_MARGIN_SECONDS * 1000:
        lease.renew()
        lease.raise_if_lost()


def delete_old_events(
    store: Store,
    lease: WriteGuard,
    path: bytes,
    clock: Budget,
    *,
    batch: int = DELETE_BATCH,
) -> tuple[bool, int]:
    """Finish a pending deletion for the file at ``path`` (its ``rereading`` is set): batches of at
    most ``batch`` rows, each its own short transaction with the lease checked, the lease margin
    ensured before it and the budget checked after it. The last batch clears ``rereading``.
    Returns ``(finished, deleted rows)``; not finished means a budget ended the run, and the next
    run resumes (``rereading`` is still set). A crash anywhere between batches is the same."""
    deleted = 0
    while True:
        ensure_margin(store, lease)
        with store.transaction() as conn:
            lease.check_still_holder(conn)
            row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if row is None:
                return True, deleted
            count, done = delete_batch(conn, row[0], batch)
            deleted += count
            if done:
                conn.execute("UPDATE files SET rereading = 0 WHERE id = ?", (row[0],))
        if done:
            return True, deleted
        if clock.hit() is not None:
            return False, deleted


# --- reading a file ---------------------------------------------------------------------------


@dataclass
class FileResult:
    """What one ``read_file`` did. ``stop`` is why the read ended (``Stop.EOF`` when complete);
    a deletion stopped by a budget leaves ``reading`` False."""

    reason: Reason
    stop: Stop | None
    reading: bool = False  # lines were read (False: only the deletion ran, a budget ended it)
    lines: int = 0
    oversized: int = 0
    errors: int = 0  # error lines read in this run (the per-file ``parse_errors`` delta)
    unknown: int = 0  # unknown-type lines read in this run (the ``unknown_lines`` delta)
    bytes_read: int = 0
    chunks: int = 0
    events: int = 0  # events offered to the owner rule (before the within-file merge)
    progress: Progress | None = None

    @property
    def complete(self) -> bool:
        return self.stop is Stop.EOF


def _write_chunk(
    store: Store,
    lease: WriteGuard,
    candidate: Candidate,
    meta: AgentMeta | None,
    progress: Progress,
    facts: FileFacts,
    pending: Sequence[tuple[Event, int]],
) -> None:
    """One chunk in one transaction: the file row with the offset, the identity rows, the events
    with their owners and the per-file facts. Parsing is done: no line is read in here."""
    ensure_margin(store, lease)
    now = _now_ms(store)
    with store.transaction() as conn:
        lease.check_still_holder(conn)
        save_progress(conn, candidate.path, progress)
        file = _file_ref(conn, candidate)
        ensure_identity(conn, candidate, file.file_id, now, meta)
        dropped = place_events(conn, file, pending)
        for file_id, n in dropped.items():
            conn.execute(
                "UPDATE files SET copies_dropped = copies_dropped + ? WHERE id = ?", (n, file_id)
            )
        _write_facts(conn, candidate.path, facts, now)


def _reset(
    store: Store,
    lease: WriteGuard,
    candidate: Candidate,
    meta: AgentMeta | None,
    progress: Progress,
) -> None:
    """The start of a full reread: offset 0, per-file facts cleared, ``rereading`` set."""
    ensure_margin(store, lease)
    now = _now_ms(store)
    with store.transaction() as conn:
        lease.check_still_holder(conn)
        save_progress(conn, candidate.path, progress)
        file = _file_ref(conn, candidate)
        ensure_identity(conn, candidate, file.file_id, now, meta)
        _write_facts(conn, candidate.path, FileFacts(), now, reset=True)


def _pending_deletion(store: Store, path: bytes) -> bool:
    row = store.conn.execute("SELECT rereading FROM files WHERE path = ?", (path,)).fetchone()
    return row is not None and bool(row[0])


def read_file(
    store: Store,
    lease: WriteGuard,
    opened: Opened,
    previous: Progress | None,
    *,
    rules_version: int,
    file_budget: timedelta,
    run_deadline: float | None,
    force: bool = False,
    meta: AgentMeta | None = None,
    chunk_lines: int = CHUNK_LINES,
    chunk_events: int = CHUNK_EVENTS,
    delete_batch_size: int = DELETE_BATCH,
) -> FileResult:
    """Read one opened transcript into the store, under the ingest ``lease``.

    ``previous`` is the stored progress of the path (``incremental.load_progress``) or None for a
    file never stored. A reread (a changed file, a stale rules version, ``force``) resets the row
    first; a run that finds ``rereading`` set (a crashed or budget-stopped reread) finishes the
    deletion before it reads. Lines are read and parsed with no transaction open; a chunk of at
    most ``chunk_lines`` lines or ``chunk_events`` events (a single line's events are never split)
    is then committed with the offset. A budget stops the run at a line boundary and the next run
    resumes. The ``chunk_*`` and ``delete_batch_size`` parameters exist so tests can scale the
    limits down; production uses the defaults."""
    gateway = store.gateway
    clock = Budget(store, run_deadline, gateway.monotonic() + file_budget.total_seconds())
    run = IncrementalRead(
        opened,
        previous,
        gateway=gateway,
        rules_version=rules_version,
        file_budget=file_budget,
        run_deadline=run_deadline,
        force=force,
    )
    candidate = opened.candidate
    result = FileResult(run.reason, None)
    if run.reason.reread:
        _reset(store, lease, candidate, meta, run.checkpoint())
    if _pending_deletion(store, candidate.path):
        finished, _ = delete_old_events(
            store, lease, candidate.path, clock, batch=delete_batch_size
        )
        if not finished:
            result.stop = clock.hit() or Stop.FILE_BUDGET
            return result
    facts = load_facts(store.conn, candidate.path)
    errors_before, unknown_before = facts.parse_errors, facts.unknown_lines
    context = ParseContext(mtime_ns=opened.stat.st_mtime_ns, main=candidate.kind == "main")
    pending: list[tuple[Event, int]] = []
    chunk_line_count = 0
    before: tuple[Progress, FileFacts] | None = None  # the state before the line in hand
    result.reading = True

    def flush(progress: Progress, snapshot: FileFacts) -> None:
        nonlocal pending, chunk_line_count
        _write_chunk(store, lease, candidate, meta, progress, snapshot, pending)
        result.chunks += 1
        result.events += len(pending)
        pending = []
        chunk_line_count = 0

    for line in run:
        outcome = facts.add(parse_line(line.data, context))
        if pending and before is not None and len(pending) + len(outcome) > chunk_events:
            flush(*before)  # this line would push the chunk over its event cap: commit up to it
        chunk_line_count += 1
        pending.extend((e, line.offset) for e in outcome)
        before = None
        if chunk_line_count >= chunk_lines or len(pending) >= chunk_events:
            flush(run.checkpoint(), replace(facts))
        elif len(pending) + MAX_LINE_EVENTS > chunk_events:
            before = (run.checkpoint(), replace(facts))
    flush(run.checkpoint(), facts)  # the final chunk always: the offset, hashes and facts move
    result.stop = run.stop
    result.lines = run.counts.lines
    result.oversized = run.counts.oversized
    result.errors = facts.parse_errors - errors_before
    result.unknown = facts.unknown_lines - unknown_before
    result.bytes_read = run.counts.bytes_read
    result.progress = run.checkpoint()
    return result


__all__ = [
    "CHUNK_EVENTS",
    "CHUNK_LINES",
    "DELETE_BATCH",
    "LEASE_MARGIN_SECONDS",
    "MAX_COPIES",
    "FileRef",
    "FileResult",
    "WriteGuard",
    "dedupe_id",
    "delete_batch",
    "delete_old_events",
    "ensure_identity",
    "event_key",
    "event_ref",
    "load_facts",
    "place_copy",
    "place_events",
    "read_file",
]
