"""``ingest``: read new transcript bytes into the store (interface, Commands, ``ingest``).

The flow of a run (``docs/design.md``, Ingest run):

1. Discovery opens the roots and lists the transcripts (``reader``); ``--since`` keeps the sessions
   with a file modified at or after the time; files with a lower rules version come first
   (``incremental.stale_first``); ``--rebuild`` marks every listed file's row as lower-version so
   a run stopped by its budget is finished by the next plain run.
2. The ingest lease is taken (``lease``): a second caller reports ``already_running`` and writes
   nothing. Files are opened one by one through ``Discovery.open``; a file whose stored progress,
   size and hashes say nothing changed is counted and never written; any other file goes to
   ``eventstore.read_file`` (its own chunked transactions, under the lease).
3. A completed run appends the ``ingest`` log entry and sets the ``meta`` freshness and failure
   counters; the lease is released on every exit path.

``--dry-run`` takes the same steps over a read-only view of the store and writes nothing: no lease,
no log line, no ``meta`` change, no store when there is none yet.

4. Unless ``--dry-run``, ``--no-harness`` or ``harness.enabled: off``, the claude-session version
   is checked before anything is read (``harness.enabled: on`` and no usable claude-session is exit
   12) and, after the file reads of the same run and still under the lease, ``harness.capture``
   writes the snapshots (short transactions that never need the lease). ``--session CSESSION_ID``
   writes one snapshot first, without the lease, then reads that snapshot's transcripts when the
   lease is free (``files_deferred`` otherwise).

TODO(#25): fire the ``post-attention`` hook with ``ingest_failing_due``'s result.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import args as a
from .. import errors
from ..command import Command, Context
from .. import harness
from ..csversion import Presence, check_version
from ..eventstore import FileResult, read_file
from ..grammar import (
    INGEST_BUDGET_MAX,
    GrammarError,
    format_timestamp,
    parse_csession_id,
    parse_duration,
    parse_when,
)
from ..incremental import (
    IncrementalRead,
    Progress,
    Reason,
    Stop,
    current_rules_version,
    load_progress,
    run_deadline,
    signed_id,
    stale_first,
    stale_rules_paths,
)
from ..lease import IngestTarget, Lease, acquire
from ..parser import FileFacts, ParseContext, parse_line
from ..reader import (
    AgentMeta,
    Candidate,
    Discovery,
    Opened,
    Skip,
    discover,
    open_discovery,
    store_excluded,
    store_pruned_at,
)
from ..store import Store, open_read_only, state_dir
from ..textsafe import fix_surrogates, sanitize

# The ``meta.rules_version`` a fresh store is seeded with (migration 800); a dry run with no
# store yet reads with it.
FRESH_RULES_VERSION = 1
WARNING_CAP = 512
WARNINGS_MAX = 200
ATTENTION_QUIET = timedelta(hours=24)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_time_filters(parser, until=False)
    parser.add_argument(
        "--path", action="append", metavar="PATH", help="only files under this root"
    )
    parser.add_argument("--session", metavar="CSESSION_ID", help="capture one claude-session run")
    parser.add_argument("--no-harness", action="store_true", help="skip claude-session")
    parser.add_argument(
        "--rebuild", action="store_true", help="re-read every transcript from the start"
    )
    parser.add_argument("--budget", metavar="DURATION", help="overall time budget")
    a.add_dry_run(parser)
    a.add_yes(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.since is not None:
        a.check_when(ctx, args.since, "--since")
    for path in args.path or []:
        a.check_path(path, "--path")
    try:
        if args.session is not None:
            parse_csession_id(args.session, field="--session")
        if args.budget is not None:
            args.budget = parse_duration(args.budget, field="--budget", maximum=INGEST_BUDGET_MAX)
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    if args.rebuild:
        for flag, given in (
            ("--path", args.path),
            ("--since", args.since),
            ("--session", args.session),
        ):
            if given:
                raise errors.usage(
                    "--rebuild cannot be combined with this flag",
                    field="--rebuild",
                    hint=f"Drop {flag} or --rebuild.",
                )
    if args.dry_run and args.session is not None:
        raise errors.usage(
            "--dry-run cannot be combined with --session",
            field="--dry-run",
            hint="Drop --dry-run or --session.",
        )
    if args.no_harness and args.session is not None:
        raise errors.usage(
            "--no-harness cannot be combined with --session",
            field="--no-harness",
            hint="Drop --no-harness or --session.",
        )
    if args.rebuild and not args.dry_run:
        a.require_confirmation(ctx)


# --- the report ------------------------------------------------------------------------------


@dataclass
class Tally:
    """The counts of one run (``schemas/defs.json`` ``ingest_files``, ``ingest_lines``,
    ``ingest_sessions``)."""

    seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    unsafe: int = 0
    excluded: int = 0
    vanished: int = 0
    rewritten: int = 0
    remaining: int = 0
    parsed: int = 0
    errors: int = 0
    oversized: int = 0
    unknown: int = 0
    sessions_new: int = 0
    sessions_updated: int = 0
    complete: bool = True
    warnings: list[str] = field(default_factory=list)

    def files(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "unsafe": self.unsafe,
            "excluded": self.excluded,
            "vanished": self.vanished,
            "rewritten": self.rewritten,
            "remaining": self.remaining,
        }

    def lines(self) -> dict[str, int]:
        return {
            "parsed": self.parsed,
            "errors": self.errors,
            "oversized": self.oversized,
            "unknown": self.unknown,
        }

    def sessions(self) -> dict[str, int]:
        return {"new": self.sessions_new, "updated": self.sessions_updated}


def build_report(
    tally: Tally,
    *,
    dry_run: bool,
    already_running: bool = False,
    harness_counts: harness.Counts | None = None,
    files_deferred: bool = False,
) -> dict[str, Any]:
    """The ``--json`` document (``schemas/cli/ingest.json``). Warnings are sanitized and bounded
    here, whatever put them in. ``harness_counts`` is None when claude-session was not used."""
    extra = harness_counts.warnings if harness_counts is not None else []
    warnings = [sanitize(w, WARNING_CAP) for w in [*tally.warnings, *extra] if w][:WARNINGS_MAX]
    return {
        "complete": tally.complete and not already_running and not files_deferred,
        "already_running": already_running,
        "files_deferred": files_deferred,
        "dry_run": dry_run,
        "files": tally.files(),
        "lines": tally.lines(),
        "sessions": tally.sessions(),
        "harness": (
            harness_counts.to_json()
            if harness_counts is not None
            else {"enabled": False, "matched": 0, "captured": 0, "errors": 0}
        ),
        "warnings": warnings,
    }


def print_report(ctx: Context, report: dict[str, Any]) -> None:
    if ctx.json:
        ctx.emit_json(report)
        return
    if ctx.quiet:
        return
    if report["already_running"]:
        state = "already running"
    else:
        state = "complete" if report["complete"] else "partial (budget reached)"
    prefix = "ingest (dry run)" if report["dry_run"] else "ingest"
    f, ln, s = report["files"], report["lines"], report["sessions"]
    ctx.say(f"{prefix}: {state}")
    ctx.say(" ".join(f"files.{k}={v}" for k, v in f.items()))
    ctx.say(" ".join(f"lines.{k}={v}" for k, v in ln.items()))
    ctx.say(f"sessions.new={s['new']} sessions.updated={s['updated']}")
    for text in report["warnings"]:
        ctx.say(f"warning: {text}")


# --- scanning --------------------------------------------------------------------------------


@dataclass
class _Read:
    """What reading one file produced, whoever read it."""

    lines: int
    errors: int
    unknown: int
    oversized: int
    stop: Stop | None
    reading: bool = True


class _Reader:
    """How a run reads one opened file: the real one writes through ``read_file``, a dry run only
    iterates and parses."""

    def read(
        self,
        opened: Opened,
        previous: Progress | None,
        probe: IncrementalRead,
        meta: AgentMeta | None,
    ) -> _Read:
        raise NotImplementedError

    def agent_meta(self, discovery: Discovery, candidate: Candidate) -> AgentMeta | None:
        # Read-only, so a dry run counts an unsafe meta file exactly as a live run does.
        return discovery.read_meta(candidate)


class _Live(_Reader):
    def __init__(
        self, store: Store, lease: Lease, rules: int, file_budget: timedelta, deadline: float
    ) -> None:
        self.store, self.lease, self.rules = store, lease, rules
        self.file_budget, self.deadline = file_budget, deadline
        self.force = False

    def read(
        self,
        opened: Opened,
        previous: Progress | None,
        probe: IncrementalRead,
        meta: AgentMeta | None,
    ) -> _Read:
        result: FileResult = read_file(
            self.store,
            self.lease,
            opened,
            previous,
            rules_version=self.rules,
            file_budget=self.file_budget,
            run_deadline=self.deadline,
            force=self.force,
            meta=meta,
        )
        return _Read(
            result.lines,
            result.errors,
            result.unknown,
            result.oversized,
            result.stop,
            result.reading,
        )


class _Dry(_Reader):
    def read(
        self,
        opened: Opened,
        previous: Progress | None,
        probe: IncrementalRead,
        meta: AgentMeta | None,
    ) -> _Read:
        facts = FileFacts()
        context = ParseContext(
            mtime_ns=opened.stat.st_mtime_ns, main=opened.candidate.kind == "main"
        )
        for line in probe:
            facts.add(parse_line(line.data, context))
        return _Read(
            probe.counts.lines,
            facts.parse_errors,
            facts.unknown_lines,
            probe.counts.oversized,
            probe.stop,
        )


def _since_ns(ctx: Context, args: argparse.Namespace) -> int | None:
    if args.since is None:
        return None
    moment = parse_when(
        args.since, field="--since", now=ctx.gateway.now(), time_zone=ctx.settings.zone()
    )
    return int((moment - _EPOCH) / timedelta(microseconds=1)) * 1000


def plan(
    discovery: Discovery,
    conn: sqlite3.Connection | None,
    rules: int,
    since_ns: int | None,
    only: frozenset[str] | None = None,
) -> tuple[list[Candidate], int]:
    """The candidates to visit, in order, and how many listed files ``--since`` left out. A
    session is kept when any file of it (main or agent) was modified at or after the time.
    ``only`` (``--session``) keeps just the files of those Claude Code session ids; the files it
    leaves out are not "left out" in the count (they were never asked for)."""
    listed = list(discovery.candidates())
    wanted = listed if only is None else [c for c in listed if c.session_id in only]
    if since_ns is not None:
        recent = {c.session_id for c in wanted if c.mtime_ns >= since_ns}
        kept = [c for c in wanted if c.session_id in recent]
    else:
        kept = wanted
    stale = stale_rules_paths(conn, rules) if conn is not None else set()
    return stale_first(kept, stale), len(listed) - len(kept)


def _rereading(conn: sqlite3.Connection | None, path: bytes) -> bool:
    if conn is None:
        return False
    row = conn.execute("SELECT rereading FROM files WHERE path = ?", (path,)).fetchone()
    return row is not None and bool(row[0])


def _probably_unchanged(
    conn: sqlite3.Connection | None, candidate: Candidate, rules: int, rebuild: bool
) -> bool:
    """A listed file the store says nothing is left to read of, judged by the listing alone (ids
    as stored, size equal to the offset, current rules). Used only to keep ``remaining`` from
    counting files an interrupted run never needed to open; it never decides ``unchanged``."""
    if conn is None or rebuild:
        return False
    row = conn.execute(
        "SELECT dev, inode, offset, rules_version, rereading FROM files WHERE path = ?",
        (candidate.path,),
    ).fetchone()
    return (
        row is not None
        and row[0] == signed_id(candidate.dev)
        and row[1] == signed_id(candidate.ino)
        and row[2] == candidate.size
        and row[3] >= rules
        and not row[4]
    )


def _session_known(conn: sqlite3.Connection | None, session_id: str) -> bool:
    if conn is None:
        return False
    row = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return row is not None


def scan(
    ctx: Context,
    args: argparse.Namespace,
    discovery: Discovery,
    conn: sqlite3.Connection | None,
    reader: _Reader,
    *,
    rules: int,
    deadline: float,
    file_budget: timedelta,
    ordered: list[Candidate],
    left_out: int,
) -> Tally:
    """Visit the planned candidates until they are done or the run budget ends."""
    tally = Tally()
    gateway = ctx.gateway
    known: dict[str, bool] = {}
    touched: set[str] = set()
    for index, candidate in enumerate(ordered):
        if gateway.monotonic() >= deadline:
            for rest in ordered[index:]:
                if discovery.settled(rest) or _probably_unchanged(conn, rest, rules, args.rebuild):
                    continue  # excluded, pruned or nothing left to read: not remaining
                tally.remaining += 1
            if tally.remaining:
                tally.complete = False
            break
        opened = discovery.open(candidate)
        if isinstance(opened, Skip):
            continue
        try:
            previous = load_progress(conn, candidate.path) if conn is not None else None
            probe = IncrementalRead(
                opened,
                previous,
                gateway=gateway,
                rules_version=rules,
                file_budget=file_budget,
                run_deadline=deadline,
                force=args.rebuild,
            )
            reason = probe.reason
            if (
                reason is Reason.CONTINUE
                and previous is not None
                and opened.stat.st_size == previous.offset
                and not _rereading(conn, candidate.path)
            ):
                tally.unchanged += 1  # size equals the offset and the hashes matched
                continue
            session = candidate.session_id
            known.setdefault(session, _session_known(conn, session))
            meta = reader.agent_meta(discovery, candidate) if candidate.kind != "main" else None
            outcome = reader.read(opened, previous, probe, meta)
        finally:
            opened.close()
        touched.add(session)
        if reason is Reason.NEW:
            tally.new += 1
        elif reason.reread:
            tally.rewritten += 1
        else:
            tally.changed += 1
        tally.parsed += outcome.lines
        tally.errors += outcome.errors
        tally.unknown += outcome.unknown
        tally.oversized += outcome.oversized
        if not outcome.reading or outcome.stop is not Stop.EOF:
            tally.complete = False
            tally.remaining += 1
    stats = discovery.stats
    tally.seen = stats.seen - left_out
    tally.unsafe, tally.excluded, tally.vanished = stats.unsafe, stats.excluded, stats.vanished
    tally.unchanged += stats.pruned  # a pruned session's unchanged file: unchanged by definition
    tally.sessions_new = sum(1 for s in touched if not known[s])
    tally.sessions_updated = sum(1 for s in touched if known[s])
    tally.warnings.extend(_discovery_warnings(discovery))
    return tally


def _discovery_warnings(discovery: Discovery) -> list[str]:
    found: list[str] = []
    for root, why in discovery.unusable_roots:
        # Sanitized once, by build_report; display_path would escape the markers a second time.
        found.append(f"root_unusable: {fix_surrogates(os.fsdecode(root))} ({why})")
    found.extend(w for w in discovery.warnings if w != "root_unusable")
    return found


# --- meta ------------------------------------------------------------------------------------


def _now_ms(ctx: Context) -> int:
    return int(ctx.gateway.now().timestamp() * 1000)


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    """An untyped ``meta`` value as an int (None when missing or not a whole number)."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError, OverflowError):
        return None


def record_success(
    conn: sqlite3.Connection, now_ms: int, *, complete_scope: bool, complete: bool, full: bool
) -> None:
    """``last_ingest_at`` for every finished run; ``last_complete_ingest_at`` for a complete run
    over every file under the roots (no ``--path``, no ``--since``); a full ingest that exits 0,
    complete or not, resets the failure counters (Hooks, ``ingest_failing``)."""
    _set_meta(conn, "last_ingest_at", now_ms)
    if complete and complete_scope:
        _set_meta(conn, "last_complete_ingest_at", now_ms)
    if full:
        _set_meta(conn, "fail_count", 0)
        conn.execute("DELETE FROM meta WHERE key = 'first_failed_at'")


def record_failure(store: Store, now_ms: int) -> None:
    """A full ingest that ended with exit 1 or 12: ``fail_count`` + 1, ``last_failed_at``, and
    ``first_failed_at`` when it is not set. Best effort: a store that cannot be written is the
    very failure being recorded."""
    try:
        with store.transaction() as conn:
            count = _meta_int(conn, "fail_count") or 0
            _set_meta(conn, "fail_count", max(count, 0) + 1)
            _set_meta(conn, "last_failed_at", now_ms)
            if _meta_int(conn, "first_failed_at") is None:
                _set_meta(conn, "first_failed_at", now_ms)
    except Exception:  # noqa: BLE001 - never hides the error being reported
        return


def ingest_failing_due(
    conn: sqlite3.Connection, threshold: int, now_ms: int
) -> dict[str, Any] | None:
    """The ``post-attention`` details of ``ingest_failing`` when it is due (``fail_count`` has
    reached ``ingest.fail_threshold`` and the last firing is over 24 h ago), else None. It does not
    set ``meta.last_fired_at``: the caller that fires the hook does (TODO(#25))."""
    count = _meta_int(conn, "fail_count") or 0
    if count < threshold or count < 1:
        return None
    fired = _meta_int(conn, "last_fired_at")
    if fired is not None and now_ms - fired < ATTENTION_QUIET / timedelta(milliseconds=1):
        return None
    first = _meta_int(conn, "first_failed_at")
    last = _meta_int(conn, "last_failed_at")

    def stamp(ms: int | None) -> str:
        moment = _EPOCH + timedelta(milliseconds=ms if ms is not None else now_ms)
        return format_timestamp(moment)

    return {
        "consecutive_failures": count,
        "first_failed_at": stamp(first),
        "last_failed_at": stamp(last),
    }


# --- the run ---------------------------------------------------------------------------------


def is_full(args: argparse.Namespace) -> bool:
    """A "full ingest" (Hooks): no ``--path``, ``--session``, ``--since``, ``--rebuild`` and no
    ``--dry-run``."""
    return not (args.path or args.session or args.since or args.rebuild or args.dry_run)


def _exit_code(exc: Exception) -> int:
    return exc.exit_code if isinstance(exc, errors.CliError) else 1


def run(ctx: Context, args: argparse.Namespace) -> int:
    if ctx.dry_run:
        report = run_dry(ctx, args)
    else:
        store = ctx.store()
        try:
            if args.session is not None:
                report = run_session(ctx, args, store)
            else:
                report = run_live(ctx, args, store)
        except Exception as exc:
            if is_full(args) and _exit_code(exc) in (1, 12):
                record_failure(store, _now_ms(ctx))
            raise
    print_report(ctx, report)
    return 0


def _budget(ctx: Context, args: argparse.Namespace) -> timedelta:
    return args.budget if args.budget is not None else ctx.settings.ingest.budget


def run_dry(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    """Read and parse, write nothing: a read-only view of the store (none when there is none),
    no lease, no log line, no ``meta``, no claude-session."""
    from ..config import home_dir  # lazy: config imports args, which imports command

    directory = ctx.state_directory or state_dir(os.environ, home_dir(os.environ))
    deadline = run_deadline(ctx.gateway, _budget(ctx, args))
    conn = _snapshot(open_read_only(ctx.gateway, directory))
    try:
        view = _View(conn)
        discovery = open_discovery(
            ctx.gateway,
            ctx.settings.transcript_roots,
            excluded=store_excluded(view) if conn is not None else (lambda _name: False),
            pruned_at=store_pruned_at(view) if conn is not None else (lambda _id: None),
            paths=args.path or (),
        )
        with discovery:
            rules = current_rules_version(conn) if conn is not None else FRESH_RULES_VERSION
            since = _since_ns(ctx, args)
            ordered, left_out = plan(discovery, conn, rules, since)
            tally = scan(
                ctx,
                args,
                discovery,
                conn,
                _Dry(),
                rules=rules,
                deadline=deadline,
                file_budget=ctx.settings.ingest.file_budget,
                ordered=ordered,
                left_out=left_out,
            )
    finally:
        if conn is not None:
            conn.close()
    return build_report(tally, dry_run=True)


@dataclass
class _View:
    conn: Any


_SNAPSHOT_TABLES = ("meta", "files", "retro_sessions", "pruned_sessions")


def _snapshot(source: sqlite3.Connection | None) -> sqlite3.Connection | None:
    """Everything a dry run reads from the store (``meta``, ``files``, the retro and prune tables
    and the ids of ``sessions``), copied in one short read transaction into memory, and the file
    connection closed. The scan is long and a live ingest may write and checkpoint meanwhile; an
    ``immutable`` connection kept open across it could read torn pages. None when no store."""
    if source is None:
        return None
    mem = sqlite3.connect(":memory:", isolation_level=None)
    try:
        source.execute("BEGIN")
        for table in _SNAPSHOT_TABLES:
            row = source.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            mem.execute(row[0])
            cursor = source.execute(f"SELECT * FROM {table}")
            marks = ",".join("?" * len(cursor.description))
            mem.executemany(f"INSERT INTO {table} VALUES ({marks})", cursor)
        mem.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        mem.executemany(
            "INSERT INTO sessions VALUES (?)", source.execute("SELECT session_id FROM sessions")
        )
        source.execute("COMMIT")
    except BaseException:
        mem.close()
        raise
    finally:
        source.close()
    return mem


def mark_for_rebuild(store: Store, lease: Lease, ordered: list[Candidate]) -> None:
    """``--rebuild``: every listed file that has a row gets a rules version of 0, so the files
    are reread (lower rules first) and a run stopped by its budget is finished by the next plain
    run. Rows of files that are gone are not touched and are kept."""
    batch = 1000
    for start in range(0, len(ordered), batch):
        with store.transaction() as conn:
            lease.check_still_holder(conn)
            conn.executemany(
                "UPDATE files SET rules_version = 0 WHERE path = ?",
                [(c.path,) for c in ordered[start : start + batch]],
            )


@dataclass
class HarnessUse:
    """Whether this run uses claude-session (``command`` is None when it does not) and the
    warnings the decision produced."""

    command: str | None = None
    warnings: list[str] = field(default_factory=list)


def decide_harness(ctx: Context, args: argparse.Namespace) -> HarnessUse:
    """Whether a real ingest captures harness snapshots. ``--no-harness`` and ``harness.enabled:
    off`` skip claude-session entirely (no call). Otherwise ``claude-session --version`` decides:
    with ``on`` anything but a usable claude-session is exit 12; with ``auto`` a missing one is
    silent (``harness.enabled: false`` in the report) and an unusable one adds a warning."""
    mode = ctx.settings.harness.enabled
    if args.no_harness or mode == "off":
        return HarnessUse()
    command = ctx.settings.harness.claude_session_cmd
    check = check_version(ctx.gateway, command)
    if check.ok:
        return HarnessUse(command)
    if mode == "on":
        raise harness.unavailable(check)
    if check.presence is Presence.MISSING:
        return HarnessUse()
    return HarnessUse(warnings=[f"claude_session_unusable: {check.presence.value}"])


def run_session(ctx: Context, args: argparse.Namespace, store: Store) -> dict[str, Any]:
    """``--session CSESSION_ID``: the snapshot of one claude-session run, written without the
    ingest lease, then (lease free) the transcripts of its Claude session ids."""
    if ctx.settings.harness.enabled == "off":
        raise errors.usage(
            "harness.enabled is off",
            field="harness.enabled",
            hint="Set harness.enabled to auto or on to capture a claude-session run.",
        )
    command = ctx.settings.harness.claude_session_cmd
    check = check_version(ctx.gateway, command)
    if not check.ok:
        raise harness.unavailable(check)
    fetched = harness.fetch_status(ctx.gateway, command, args.session)
    if fetched.kind is harness.Fetch.FORGOTTEN:
        raise errors.CliError(
            "not_found",
            "claude-session does not know this session",
            details={"id": args.session},
            hint="It may have been forgotten.",
        )
    if fetched.row is None:
        raise harness.status_failed()
    try:
        written = harness.store_row(store, fetched.row)
    except harness.Invalid:
        raise harness.status_failed() from None
    counts = harness.Counts(matched=1, captured=1 if written else 0)
    ids = frozenset(fetched.row.claude_session_ids)
    if not ids:
        return build_report(Tally(), dry_run=False, harness_counts=counts)
    lease = acquire(store, IngestTarget())
    if lease is None:
        return build_report(Tally(), dry_run=False, harness_counts=counts, files_deferred=True)
    deadline = run_deadline(ctx.gateway, _budget(ctx, args))
    try:
        with discover(ctx, args.path or ()) as discovery, lease:
            return _locked(
                ctx,
                args,
                store,
                lease,
                discovery,
                deadline,
                _since_ns(ctx, args),
                only=ids,
                harness_counts=counts,
            )
    finally:
        with contextlib.suppress(errors.CliError):
            lease.release()


def run_live(ctx: Context, args: argparse.Namespace, store: Store) -> dict[str, Any]:
    deadline = run_deadline(ctx.gateway, _budget(ctx, args))
    since = _since_ns(ctx, args)
    use = decide_harness(ctx, args)
    with discover(ctx, args.path or ()) as discovery:
        lease = acquire(store, IngestTarget())
        if lease is None:
            tally = Tally(complete=False)
            if args.rebuild:
                tally.warnings.append(
                    "--rebuild did not run: another ingest holds the lease; run it again later"
                )
            return build_report(tally, dry_run=False, already_running=True)
        try:
            with lease:
                return _locked(ctx, args, store, lease, discovery, deadline, since, use=use)
        finally:
            with contextlib.suppress(errors.CliError):
                lease.release()


def _locked(
    ctx: Context,
    args: argparse.Namespace,
    store: Store,
    lease: Lease,
    discovery: Discovery,
    deadline: float,
    since: int | None,
    *,
    use: HarnessUse | None = None,
    only: frozenset[str] | None = None,
    harness_counts: harness.Counts | None = None,
) -> dict[str, Any]:
    conn = store.conn
    rules = current_rules_version(conn)
    ordered, left_out = plan(discovery, conn, rules, since, only)
    if args.rebuild:
        mark_for_rebuild(store, lease, ordered)
        ordered, left_out = stale_first(ordered, stale_rules_paths(conn, rules)), left_out
    file_budget = ctx.settings.ingest.file_budget
    live = _Live(store, lease, rules, file_budget, deadline)
    live.force = args.rebuild
    tally = scan(
        ctx,
        args,
        discovery,
        conn,
        live,
        rules=rules,
        deadline=deadline,
        file_budget=file_budget,
        ordered=ordered,
        left_out=left_out,
    )
    lease.raise_if_lost()
    if use is not None:
        tally.warnings.extend(use.warnings)
        if use.command is not None:
            harness_counts = harness.capture(
                ctx.gateway,
                store,
                command=use.command,
                days=harness.window_days(ctx.settings.retention.sessions),
                deadline=deadline,
            )
    report = build_report(tally, dry_run=False, harness_counts=harness_counts)
    assert ctx.actor is not None
    now = _now_ms(ctx)
    with store.transaction() as tx:
        # The lease first: a lost lease writes neither the log line nor the meta.
        lease.check_still_holder(tx)
        store.log.append(
            ctx.actor,
            "ingest",
            complete=report["complete"],
            files=report["files"],
            lines=report["lines"],
            sessions=report["sessions"],
        )
        record_success(
            tx,
            now,
            complete_scope=not (args.path or args.since or args.session),
            complete=tally.complete,
            full=is_full(args),
        )
    # TODO(#25): fire the post-attention hook when ingest_failing_due(...) returns details
    # (nothing calls it before then).
    return report


COMMANDS = (
    Command(
        ("ingest",),
        "read new transcript bytes into the store",
        add_arguments,
        check,
        run,
        accepts_actor=True,
        refuses_unsafe_config=True,
    ),
)
