"""The retention rules of ``prune`` (interface.md, Retention), one function per rule.

Every rule has a real form (it deletes, in bounded transactions) and a planning form (it counts
what the real form would delete and writes nothing: ``prune --dry-run`` runs it over a read-only
view of the store). Both return the row counts the report names (``PRUNE_TABLES``).

Session retention is the one rule that needs the ingest lease (``session_retention``): it takes
the lease the caller holds and checks it in every transaction, as ingest does. One session is
pruned in this order (interface.md, Retention), in transactions of at most ``batch`` rows:

1. events (``eventstore.delete_session_batch``: a counted owner that is deleted promotes the best
   remaining stored copy of its key, so every key keeps one counted row); a files row that only
   those events referred to goes in the same transaction as their last event,
2. agents, with their files rows,
3. the main file's row, the session row, then ``pruned_sessions(session_id, pruned_at)``, in one
   transaction that also clears the marker.

``meta.prune_session`` (``<session_id>|<start ms>``) names the session being pruned from the first
transaction to the last, and the time its prune started: ``pruned_at`` is that time, kept across a
resume, so a line appended to the session's file after the start is ingested again (a bare legacy
value without ``|`` has no start: the resume time is used). A
crash between two transactions leaves the session with fewer events (the session row, which the
rest of the tool needs for every event, is still there) and the marker set; the next prune
finishes that session first, whatever its age then (the age comes from the events and files, which
the partial deletion changed). Nothing is lost that was not already past retention.

Sizes: a transaction deletes at most ``batch`` events (each with at most 8 sibling copies read to
settle the owner) or ``batch`` agents, and the lease margin is ensured before each one, so none
starts near the lease expiry.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .eventstore import DELETE_BATCH, delete_session_batch, ensure_margin
from .gateway import DirHandle, Gateway
from .lease import IngestTarget, Lease, is_live
from .log import LogWriter
from .removal import remove_tree
from .store import Store

PRUNE_TABLES = (
    "sessions",
    "agents",
    "events",
    "files",
    "harness_runs",
    "retros",
    "digests",
    "log_entries",
    "retro_starts",
    "retro_sessions",
    "pruned_sessions",
)
FINAL_RETRO_STATES = ("done", "failed", "escalated", "canceled")
CLEANUP_SETTLED = ("done", "incomplete", "skipped")
RETRO_SESSION_MAX_AGE = timedelta(days=400)
RETRO_START_KEEP_DAYS = 2
MARKER = "prune_session"
DIGESTS_DIR = b"digests"
_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}")
_PAGE = 500
_MS = timedelta(milliseconds=1)

# The age of a session (interface.md, Retention): the end of its counted events (agents included),
# else of its files, else when it was last ingested.
_AGE_SQL = """
SELECT s.session_id, COALESCE(
  (SELECT MAX(e.ts) FROM events e WHERE e.session_id = s.session_id AND e.counted = 1),
  NULLIF(MAX(
    COALESCE((SELECT f.max_ts FROM files f WHERE f.id = s.main_file_id), -1),
    COALESCE((SELECT MAX(f.max_ts) FROM agents a JOIN files f ON f.id = a.file_id
              WHERE a.session_id = s.session_id), -1)), -1),
  s.last_ingest_at)
FROM sessions s WHERE s.session_id > ? ORDER BY s.session_id LIMIT ?
"""


def ms(delta: timedelta) -> int:
    return delta // _MS


def epoch_ms(moment: datetime) -> int:
    return ms(moment - datetime(1970, 1, 1, tzinfo=UTC))


@dataclass
class Report:
    tables: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PRUNE_TABLES, 0))
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # skipped items, for stderr


@dataclass
class Settings:
    """What the rules read from the config."""

    sessions: timedelta
    retros: timedelta
    digests: timedelta
    log: timedelta
    roots: tuple[str, ...]
    zone: ZoneInfo


# --- sessions ---------------------------------------------------------------------------------


def expired_sessions(conn: sqlite3.Connection, cutoff_ms: int) -> Iterator[str]:
    """The ids of the sessions older than the cutoff, in id order, read a page at a time."""
    after = ""
    while True:
        rows = conn.execute(_AGE_SQL, (after, _PAGE)).fetchall()
        if not rows:
            return
        for session_id, age in rows:
            if age is not None and age < cutoff_ms:
                yield session_id
        after = rows[-1][0]


def _set_marker(conn: sqlite3.Connection, session_id: str, start_ms: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (MARKER, f"{session_id}|{start_ms}"),
    )


def parse_marker(value: object) -> tuple[str, int | None]:
    """``(session id, start ms)`` of a marker; the start is None for a bare legacy value."""
    text = str(value)
    session_id, bar, start = text.rpartition("|")
    if bar and start.isdigit():
        return session_id, int(start)
    return text, None


def session_retention(
    store: Store,
    lease: Lease,
    now_ms: Callable[[], int],
    settings: Settings,
    report: Report,
    *,
    batch: int = DELETE_BATCH,
) -> None:
    """Prune the expired sessions and the old ``pruned_sessions`` rows, holding the ingest lease."""
    tables = report.tables
    gone_before = now_ms() - 2 * ms(settings.sessions)
    while True:
        ensure_margin(store, lease)
        with store.transaction() as conn:
            lease.check_still_holder(conn)
            cur = conn.execute(
                "DELETE FROM pruned_sessions WHERE rowid IN "
                "(SELECT rowid FROM pruned_sessions WHERE pruned_at < ? LIMIT ?)",
                (gone_before, batch),
            )
            tables["pruned_sessions"] += cur.rowcount
        if cur.rowcount < batch:
            break
    pending = store.conn.execute("SELECT value FROM meta WHERE key = ?", (MARKER,)).fetchone()
    if pending is not None:
        resumed, started = parse_marker(pending[0])
        _resume(store, lease, resumed, started, now_ms, report, batch)
    cutoff = now_ms() - ms(settings.sessions)
    for session_id in expired_sessions(store.conn, cutoff):
        _prune_session(store, lease, session_id, now_ms, report, batch)


def _resume(
    store: Store,
    lease: Lease,
    session_id: str,
    started_ms: int | None,
    now_ms: Callable[[], int],
    report: Report,
    batch: int,
) -> None:
    known = store.conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if known is not None:
        _prune_session(store, lease, session_id, now_ms, report, batch, started_ms=started_ms)
        return
    ensure_margin(store, lease)
    with store.transaction() as conn:  # the last transaction committed, the marker survived
        lease.check_still_holder(conn)
        conn.execute("DELETE FROM meta WHERE key = ?", (MARKER,))


def _prune_session(
    store: Store,
    lease: Lease,
    session_id: str,
    now_ms: Callable[[], int],
    report: Report,
    batch: int,
    *,
    started_ms: int | None = None,
) -> None:
    tables = report.tables
    start = now_ms() if started_ms is None else started_ms
    while True:  # 1. events
        ensure_margin(store, lease)
        with store.transaction() as conn:
            lease.check_still_holder(conn)
            _set_marker(conn, session_id, start)
            event_files = _event_files(conn, session_id, batch)
            count, done = delete_session_batch(conn, session_id, batch)
            tables["files"] += _drop_unreferenced_files(conn, event_files)
        tables["events"] += count
        if done:
            break
    while True:  # 2. agents, each with its files row
        ensure_margin(store, lease)
        with store.transaction() as conn:
            lease.check_still_holder(conn)
            rows = conn.execute(
                "SELECT agent_id, file_id FROM agents WHERE session_id = ? LIMIT ?",
                (session_id, batch),
            ).fetchall()
            for agent_id, file_id in rows:
                if file_id is not None:
                    tables["files"] += conn.execute(
                        "DELETE FROM files WHERE id = ?", (file_id,)
                    ).rowcount
                conn.execute(
                    "DELETE FROM agents WHERE session_id = ? AND agent_id = ?",
                    (session_id, agent_id),
                )
            tables["agents"] += len(rows)
        if len(rows) < batch:
            break
    ensure_margin(store, lease)  # 3. the main file, the session, the pruned mark
    with store.transaction() as conn:
        lease.check_still_holder(conn)
        row = conn.execute(
            "SELECT main_file_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None and row[0] is not None:
            tables["files"] += conn.execute("DELETE FROM files WHERE id = ?", (row[0],)).rowcount
        tables["sessions"] += conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        ).rowcount
        conn.execute(
            "INSERT INTO pruned_sessions (session_id, pruned_at) VALUES (?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET pruned_at = excluded.pruned_at",
            (session_id, start),
        )
        conn.execute("DELETE FROM meta WHERE key = ?", (MARKER,))


def _event_files(conn: sqlite3.Connection, session_id: str, limit: int) -> list[int]:
    """The files the next ``limit`` events of the session (in ``delete_session_batch`` order)
    were read from."""
    rows = conn.execute(
        "SELECT DISTINCT file_id FROM events WHERE id IN "
        "(SELECT id FROM events WHERE session_id = ? ORDER BY id LIMIT ?)",
        (session_id, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _drop_unreferenced_files(conn: sqlite3.Connection, file_ids: list[int]) -> int:
    """Delete the files rows that no event, agent or session refers to any more. A row an agent
    or a main file still names is the business of the later steps (or of another session)."""
    count = 0
    for file_id in file_ids:
        count += conn.execute(
            "DELETE FROM files WHERE id = ? "
            "AND NOT EXISTS (SELECT 1 FROM events WHERE file_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM agents WHERE file_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM sessions WHERE main_file_id = ?)",
            (file_id, file_id, file_id, file_id),
        ).rowcount
    return count


def plan_sessions(
    conn: sqlite3.Connection, now_ms: int, settings: Settings, report: Report
) -> None:
    tables = report.tables
    tables["pruned_sessions"] += conn.execute(
        "SELECT COUNT(*) FROM pruned_sessions WHERE pruned_at < ?",
        (now_ms - 2 * ms(settings.sessions),),
    ).fetchone()[0]
    ids = list(expired_sessions(conn, now_ms - ms(settings.sessions)))
    pending = conn.execute("SELECT value FROM meta WHERE key = ?", (MARKER,)).fetchone()
    if pending is not None:
        marked = parse_marker(pending[0])[0]
        known = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (marked,)).fetchone()
        if known is not None and marked not in ids:
            ids.append(marked)
    planned = set(ids)
    file_ids: set[int] = set()
    event_files: set[int] = set()
    for session_id in ids:
        tables["sessions"] += 1
        tables["events"] += conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        tables["agents"] += conn.execute(
            "SELECT COUNT(*) FROM agents WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        file_ids.update(
            r[0]
            for r in conn.execute(
                "SELECT file_id FROM agents WHERE session_id = ? AND file_id IS NOT NULL "
                "UNION SELECT main_file_id FROM sessions WHERE session_id = ? "
                "AND main_file_id IS NOT NULL",
                (session_id, session_id),
            )
        )
        event_files.update(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT file_id FROM events WHERE session_id = ?", (session_id,)
            )
        )
    for file_id in event_files - file_ids:
        if _only_planned_sessions_use(conn, file_id, planned):
            file_ids.add(file_id)
    tables["files"] += len(file_ids)


def _only_planned_sessions_use(conn: sqlite3.Connection, file_id: int, planned: set[str]) -> bool:
    for sql in (
        "SELECT DISTINCT session_id FROM events WHERE file_id = ?",
        "SELECT DISTINCT session_id FROM agents WHERE file_id = ?",
        "SELECT DISTINCT session_id FROM sessions WHERE main_file_id = ?",
    ):
        if any(r[0] not in planned for r in conn.execute(sql, (file_id,))):
            return False
    return True


def ingest_is_running(conn: sqlite3.Connection, gateway: Gateway) -> bool:
    state = IngestTarget().read(conn)
    return state is not None and is_live(gateway, state)


# --- harness runs -----------------------------------------------------------------------------


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


Transaction = Callable[[], contextlib.AbstractContextManager[sqlite3.Connection]]


def harness_runs(
    conn: sqlite3.Connection,
    transaction: Transaction | None,
    now: datetime,
    settings: Settings,
    report: Report,
) -> None:
    """Remove the runs last updated before the cutoff, a page of rows per transaction
    (``transaction`` is None for the planning form). ``updated_at`` is claude-session's text; a
    value that is not an ISO 8601 time is kept (its age is unknown)."""
    cutoff = now - settings.sessions
    after = ""
    while True:
        rows = conn.execute(
            "SELECT csession_id, updated_at FROM harness_runs WHERE csession_id > ? "
            "ORDER BY csession_id LIMIT ?",
            (after, _PAGE),
        ).fetchall()
        if not rows:
            return
        after = rows[-1][0]
        old = [(r[0], r[1]) for r in rows if (t := _parse_time(r[1])) is not None and t < cutoff]
        if transaction is None or not old:
            report.tables["harness_runs"] += len(old)
            continue
        with transaction() as tx:  # a row updated since it was read is another run's: it stays
            for csession_id, updated_at in old:
                report.tables["harness_runs"] += tx.execute(
                    "DELETE FROM harness_runs WHERE csession_id = ? AND updated_at = ?",
                    (csession_id, updated_at),
                ).rowcount


# --- retros and digests -----------------------------------------------------------------------


@contextlib.contextmanager
def digest_dir(gateway: Gateway, directory: str) -> Iterator[DirHandle | None]:
    """The ``digests`` directory of the state directory (None when there is none)."""
    handle: DirHandle | None = None
    try:
        root = gateway.open_root(directory)
    except OSError:
        root = None
    if root is not None:
        try:
            handle = root.open_dir(DIGESTS_DIR)  # O_NOFOLLOW: a symlink is not entered
        except OSError:
            handle = None  # none, or not a directory: nothing there is trusted
        finally:
            root.close()
    try:
        yield handle
    finally:
        if handle is not None:
            handle.close()


def _digest_exists(handle: DirHandle | None, retro_id: str) -> bool:
    if handle is None or not _RETRO_ID.fullmatch(retro_id):
        return False  # a row's id is data: only this tool's own names are looked up
    try:
        handle.lstat(retro_id.encode())
    except OSError:
        return False
    return True


def retros(
    store_conn: sqlite3.Connection,
    transaction: Transaction | None,
    gateway: Gateway,
    directory: str,
    now_ms: int,
    settings: Settings,
    report: Report,
) -> None:
    """Remove final retros older than ``retention.retros`` with their artifact and digest
    directory, then the digests older than ``retention.digests``. ``transaction`` is None for the
    planning form. A digest directory that cannot be removed completely keeps its retro for the
    next prune (and is noted once). The rows of a page of retros go in one transaction, after
    their digests, so no row is deleted on its own and left in a freed page."""
    tables = report.tables
    retro_cutoff = now_ms - ms(settings.retros)
    digest_cutoff = now_ms - ms(settings.digests)
    marks = ",".join("?" * len(FINAL_RETRO_STATES))
    apply = transaction is not None
    skip: set[str] = set()  # retros removed (or planned) and digests that could not be removed
    with digest_dir(gateway, directory) as handle:
        after = ""
        while True:
            rows = store_conn.execute(
                f"SELECT id FROM retros WHERE id > ? AND created_at < ? AND state IN ({marks}) "
                "ORDER BY id LIMIT ?",
                (after, retro_cutoff, *FINAL_RETRO_STATES, _PAGE),
            ).fetchall()
            if not rows:
                break
            after = rows[-1][0]
            page: list[str] = []
            for (retro_id,) in rows:
                had = _digest_exists(handle, retro_id)
                if handle is not None and had:
                    if not remove_tree(gateway, handle, retro_id.encode(), apply=apply):
                        report.notes.append(f"digest of {retro_id} could not be removed completely")
                        skip.add(retro_id)
                        continue
                tables["digests"] += int(had)
                tables["retros"] += 1
                skip.add(retro_id)
                page.append(retro_id)
            if transaction is not None and page:
                slots = ",".join("?" * len(page))
                with transaction() as conn:
                    conn.execute(f"DELETE FROM retro_artifacts WHERE retro_id IN ({slots})", page)
                    conn.execute(f"DELETE FROM retros WHERE id IN ({slots})", page)
        if handle is None:
            return
        _expired_digests(store_conn, gateway, handle, digest_cutoff, apply, skip, report)


def _expired_digests(
    conn: sqlite3.Connection,
    gateway: Gateway,
    handle: DirHandle,
    cutoff_ms: int,
    apply: bool,
    skip: set[str],
    report: Report,
) -> None:
    names, _more = handle.entries(100_000)
    for raw in sorted(names):
        try:
            name = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        if name in skip or not _RETRO_ID.fullmatch(name):
            continue  # never touch what this tool did not write
        row = conn.execute("SELECT state, created_at FROM retros WHERE id = ?", (name,)).fetchone()
        if row is not None:
            if row[0] not in FINAL_RETRO_STATES:
                continue  # a live retro's digest is never expired under it
            created = row[1]
        else:
            try:
                created = handle.lstat(raw).st_mtime_ns // 1_000_000
            except OSError:
                continue
        if created >= cutoff_ms:
            continue
        if not remove_tree(gateway, handle, raw, apply=apply):
            report.notes.append(f"digest of {name} could not be removed completely")
            continue
        report.tables["digests"] += 1


# --- small tables -----------------------------------------------------------------------------


def retro_starts(
    conn: sqlite3.Connection, now: datetime, settings: Settings, report: Report, *, apply: bool
) -> None:
    """Rows of a local day before ``today - 2 days`` (the two days before today are kept)."""
    today: date = now.astimezone(settings.zone).date()
    cutoff = (today - timedelta(days=RETRO_START_KEEP_DAYS)).isoformat()
    if apply:
        count = conn.execute("DELETE FROM retro_starts WHERE day < ?", (cutoff,)).rowcount
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM retro_starts WHERE day < ?", (cutoff,)
        ).fetchone()[0]
    report.tables["retro_starts"] += count


class TranscriptRoots:
    """Does a transcript root hold a project directory? Looked up by name below each root through
    its directory descriptor (no path is built, so no prefix test and no symlink is followed to
    answer). A root that cannot be examined for a reason other than not existing answers yes:
    the row stays."""

    def __init__(self, gateway: Gateway, roots: tuple[str, ...]) -> None:
        self._gateway = gateway
        self._roots = roots
        self._handles: list[DirHandle | None] | None = None
        self._unknown = False
        self._cache: dict[str, bool] = {}

    def _open(self) -> list[DirHandle | None]:
        if self._handles is None:
            self._handles = []
            for root in self._roots:
                try:
                    self._handles.append(self._gateway.open_root(root))
                except (FileNotFoundError, NotADirectoryError):
                    continue  # nothing there to hold anything
                except OSError:
                    self._unknown = True
        return self._handles

    def holds(self, name: str) -> bool:
        if name not in self._cache:
            self._cache[name] = self._lookup(name)
        return self._cache[name]

    def _lookup(self, name: str) -> bool:
        try:
            raw = name.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if not raw or b"/" in raw or b"\x00" in raw or raw in (b".", b".."):
            return False  # not one path component: no directory can have this name
        handles = [h for h in self._open() if h is not None]
        for handle in handles:
            try:
                handle.lstat(raw)
            except FileNotFoundError:
                continue
            except NotADirectoryError:
                continue
            except OSError:
                return True
            return True
        return self._unknown

    def close(self) -> None:
        for handle in self._handles or []:
            if handle is not None:
                handle.close()
        self._handles = None


def retro_sessions(
    conn: sqlite3.Connection,
    transaction: Transaction | None,
    roots: TranscriptRoots,
    now_ms: int,
    report: Report,
) -> None:
    """Remove a row whose cleanup is settled and whose project directory is gone from every
    root, or that is older than 400 days. A row with cleanup pending, or whose directory exists,
    stays (the directory is what excludes the retro's transcripts from ingest). The directory
    lookups happen outside any transaction; a page of rows is then deleted in one short one,
    each row checked again to be settled (``transaction`` is None for the planning form)."""
    cutoff = now_ms - ms(RETRO_SESSION_MAX_AGE)
    marks = ",".join("?" * len(CLEANUP_SETTLED))
    after = ""
    while True:
        rows = conn.execute(
            "SELECT csession_id, project_dir, created_at FROM retro_sessions "
            f"WHERE csession_id > ? AND cleanup_state IN ({marks}) ORDER BY csession_id LIMIT ?",
            (after, *CLEANUP_SETTLED, _PAGE),
        ).fetchall()
        if not rows:
            return
        after = rows[-1][0]
        old = [r[0] for r in rows if r[2] < cutoff or not roots.holds(r[1])]
        if transaction is None or not old:
            report.tables["retro_sessions"] += len(old)
            continue
        with transaction() as tx:
            for csession_id in old:
                report.tables["retro_sessions"] += tx.execute(
                    f"DELETE FROM retro_sessions WHERE csession_id = ? "
                    f"AND cleanup_state IN ({marks})",
                    (csession_id, *CLEANUP_SETTLED),
                ).rowcount


# --- the log ----------------------------------------------------------------------------------


def log_entries(
    log: LogWriter, now: datetime, settings: Settings, report: Report, *, apply: bool
) -> None:
    cutoff = now - settings.log
    removed = log.prune_before(cutoff) if apply else log.count_before(cutoff)
    report.tables["log_entries"] += removed
