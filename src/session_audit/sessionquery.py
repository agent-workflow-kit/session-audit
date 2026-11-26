"""The read side shared by ``sessions`` and ``show`` (issue #16; ``usage`` and ``export`` follow).

- ``Filters``: the Session filters of ``docs/interface.md`` as one predicate over a derived
  session and its harness snapshot (AND semantics; every comparison is a plain Python
  comparison, so no filter text ever reaches SQL and a ``%``, ``_`` or backslash in ``--project``
  is an ordinary character).
- ``Snapshot`` and the query-time join of a session to its harness snapshot (Harness
  enrichment). The snapshot rows are written by ``ingest`` (TODO(#19)); this module only reads
  ``harness_runs``, so a store with no row there (everything until #19 lands) has no snapshot
  for any session.
- The default exclusion of self-improve sessions: a snapshot whose labels contain
  ``owner=self-improve`` is left out unless ``--include-self-improve`` is given. Labels are
  unauthenticated hints, so this is a convenience and not a boundary.
- ``open_reader``: a read-only, snapshot-consistent view of the store (never creates, migrates
  or writes it), ``freshness``, ``resolve_session`` (a bounded prefix lookup) and ``usage_rows``
  (the usage rows of one session, always constrained by its id).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from . import derive
from .command import Context
from .derive import SessionValues
from .grammar import (
    MAX_PREFIX_MATCHES,
    AmbiguousPrefixError,
    PrefixNotFoundError,
    parse_label,
    parse_when,
)
from .store import open_read_only, state_dir, storage_error
from .textsafe import show

SELF_IMPROVE_LABEL = "owner=self-improve"
USAGE_ROW_MAX = 10_000
PREFIX_PROBE = MAX_PREFIX_MATCHES + 1  # one more than the matches an error lists
EPOCH_US_PER_MS = 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INSTANT = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z")


def one_line(value: object) -> str:
    """An untrusted value as text for one output line, sanitized exactly once. The sanitizer
    turns every line break (and every control character) into a visible marker such as
    ``<U+000A>``, so the result has no line break and cannot start a forged line. Its output is
    final: print it with ``put``, never through ``Context.say``, which would sanitize the
    markers again."""
    return show(value)


def put(ctx: Context, text: str) -> None:
    """Print one line whose every untrusted part already went through ``one_line``."""
    ctx.out.write(text + "\n")


# --- the store ---------------------------------------------------------------------------------


@contextmanager
def open_reader(ctx: Context) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection inside one read transaction (the command sees one consistent
    state while an ``ingest`` writes), or None when there is no store yet. A store written by a
    newer version raises ``store_newer`` (exit 20)."""
    from .config import home_dir  # lazy: config imports args, which imports command

    directory = ctx.state_directory or state_dir(os.environ, home_dir(os.environ))
    conn = open_read_only(ctx.gateway, directory)
    if conn is None:
        yield None
        return
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def read_store[T](ctx: Context, work: Callable[[sqlite3.Connection | None], T]) -> T:
    """Run the read phase of a command in one read transaction (``work`` gets the connection, or
    None without a store) and return its result. A read-only view opened ``immutable`` can meet a
    writer that started after the ``-wal`` check and read a torn page (``sqlite3.DatabaseError``):
    the view is then reopened, checking for ``-wal`` again, and the work retried once. A second
    failure, or any other ``sqlite3.Error`` (an empty or unmigrated ``state.db``), is the store
    error (exit 12 ``store_unwritable``). ``work`` must have no side effect outside its result."""
    for attempt in (1, 2):
        try:
            with open_reader(ctx) as conn:
                return work(conn)
        except sqlite3.DatabaseError as exc:
            if attempt == 2:
                raise storage_error(exc) from None
        except sqlite3.Error as exc:
            raise storage_error(exc) from None
    raise AssertionError("unreachable")  # pragma: no cover


def freshness(conn: sqlite3.Connection | None) -> dict[str, str | None]:
    """The ``store`` object: ``meta.last_ingest_at`` and ``meta.last_complete_ingest_at`` (epoch
    milliseconds, read untyped) as timestamps, null when never set or not a whole number."""
    found: dict[str, Any] = {}
    if conn is not None:
        found = dict(
            conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('last_ingest_at', "
                "'last_complete_ingest_at')"
            )
        )
    return {
        "last_ingest_at": _stamp(found.get("last_ingest_at")),
        "last_complete_ingest_at": _stamp(found.get("last_complete_ingest_at")),
    }


def _stamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        return None
    try:
        return derive.format_ms(value)
    except (ValueError, OverflowError, OSError):
        return None


# --- harness snapshots ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One ``harness_runs`` row (``defs.json`` ``harness``); every text is untrusted."""

    csession_id: str
    updated_at: str
    kind: str | None
    labels: tuple[str, ...]
    run: int | None
    outcome: str | None
    end_reason: str | None
    cost_usd: float | None
    turns: int | None
    skill: str | None
    repo: str | None

    @property
    def shown_kind(self) -> str:
        """The kind as output and as ``--kind`` compares it (a null kind is ``unknown``)."""
        return self.kind if self.kind is not None else "unknown"

    @property
    def order(self) -> tuple[str, str]:
        """``updated_at`` compared as an instant would be (the fraction padded to nine digits,
        so ``...05Z`` and ``...05.5Z`` order right). Text that is not a timestamp ranks below every
        real one."""
        return _instant_key(self.updated_at)

    def to_json(self) -> dict[str, object]:
        return {
            "csession_id": self.csession_id,
            "kind": self.shown_kind,
            "run": self.run if self.run is not None else 0,
            "labels": list(self.labels),
            "updated_at": self.updated_at,
            "outcome": self.outcome,
            "end_reason": self.end_reason,
            "cost_usd": self.cost_usd,
            "num_turns": self.turns,
            "skill": self.skill,
            "repo": self.repo,
            "source": "harness",
        }


def _instant_key(text: str) -> tuple[str, str]:
    found = _INSTANT.fullmatch(text)
    if found is None:
        return "", ""
    try:
        datetime.strptime(found.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:  # the shape of a timestamp, not a date
        return "", ""
    return found.group(1), (found.group(2) or "").ljust(9, "0")


def _labels(text: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(text) if isinstance(text, str) else []
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, str))


_SNAPSHOT_SELECT = (
    "SELECT j.value, h.csession_id, h.updated_at, h.kind, h.labels, h.run, h.outcome, "
    "h.end_reason, h.cost_usd, h.turns, h.skill, h.repo "
    "FROM harness_runs h, json_each(h.claude_session_ids) j "
    "WHERE json_valid(h.claude_session_ids)"
)


def _snapshot(row: tuple[Any, ...]) -> Snapshot:
    return Snapshot(
        csession_id=row[1],
        updated_at=row[2],
        kind=row[3],
        labels=_labels(row[4]),
        run=row[5],
        outcome=row[6],
        end_reason=row[7],
        cost_usd=row[8],
        turns=row[9],
        skill=row[10],
        repo=row[11],
    )


def _newer(candidate: Snapshot, current: Snapshot | None) -> bool:
    """Newest ``updated_at`` wins, then the lowest csession id (Harness enrichment)."""
    if current is None:
        return True
    if candidate.order != current.order:
        return candidate.order > current.order
    return candidate.csession_id < current.csession_id


def snapshot_index(conn: sqlite3.Connection) -> dict[str, Snapshot]:
    """The snapshot of every Claude Code session id that has one (the join, for a whole listing).
    Only the winning row per session id is kept; the table holds the capture window's runs."""
    best: dict[str, Snapshot] = {}
    for row in conn.execute(_SNAPSHOT_SELECT):
        if not isinstance(row[0], str):
            continue
        snap = _snapshot(row)
        if _newer(snap, best.get(row[0])):
            best[row[0]] = snap
    return best


def snapshot_for(conn: sqlite3.Connection, session_id: str) -> Snapshot | None:
    """The snapshot joined to one session, or None."""
    best: Snapshot | None = None
    for row in conn.execute(_SNAPSHOT_SELECT + " AND j.value = ?", (session_id,)):
        snap = _snapshot(row)
        if _newer(snap, best):
            best = snap
    return best


def is_self_improve(snapshot: Snapshot | None) -> bool:
    """TODO(#19): the labels come from the snapshots ``ingest`` captures. A label is a hint."""
    return snapshot is not None and SELF_IMPROVE_LABEL in snapshot.labels


def session_json(values: SessionValues, snapshot: Snapshot | None) -> dict[str, object]:
    """The Session object: the derived values plus ``harness`` (null without a snapshot)."""
    doc = values.to_json()
    doc["harness"] = None if snapshot is None else snapshot.to_json()
    return doc


# --- filters -------------------------------------------------------------------------------------


def project_matches(project: str | None, wanted: str) -> bool:
    """``project = P or project starts with P + '/'`` after stripping one trailing ``/`` from
    ``P``: a path boundary, no wildcard, no symlink resolution. A null project never matches."""
    if project is None:
        return False
    if wanted.endswith("/"):
        wanted = wanted[:-1]
    return project == wanted or project.startswith(wanted + "/")


@dataclass(frozen=True)
class Filters:
    """The Session filters of one command run (instants are aware datetimes)."""

    since: datetime | None = None
    until: datetime | None = None
    project: str | None = None
    labels: tuple[tuple[str, str], ...] = ()
    kind: str | None = None
    outcome: str | None = None
    entrypoint: str | None = None
    model: str | None = None
    include_self_improve: bool = False
    _label_texts: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        object.__setattr__(self, "_label_texts", frozenset(f"{k}={v}" for k, v in self.labels))

    @property
    def needs_snapshot(self) -> bool:
        """True when a filter can only be answered from a harness snapshot."""
        return bool(self.labels) or self.kind is not None or self.outcome is not None

    def snapshot_matches(self, snapshot: Snapshot | None) -> bool:
        if not self.include_self_improve and is_self_improve(snapshot):
            return False
        if not self.needs_snapshot:
            return True
        if snapshot is None:
            return False
        if self.kind is not None and snapshot.shown_kind != self.kind:
            return False
        if self.outcome is not None and snapshot.outcome != self.outcome:
            return False
        return self._label_texts <= set(snapshot.labels)

    def matches(self, values: SessionValues, snapshot: Snapshot | None) -> bool:
        if not self.snapshot_matches(snapshot):
            return False
        if self.since is not None and not _at_or_after(values.ended_ms, self.since):
            return False
        if self.until is not None and not _before(values.started_ms, self.until):
            return False
        if self.project is not None and not project_matches(values.project, self.project):
            return False
        if self.entrypoint is not None and values.entrypoint != self.entrypoint:
            return False
        return self.model is None or self.model in values.models


def _us(moment: datetime) -> int:
    """Epoch microseconds of an aware instant, exactly (no float)."""
    delta = moment - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _at_or_after(ms: int | None, instant: datetime) -> bool:
    return ms is not None and ms * EPOCH_US_PER_MS >= _us(instant)


def _before(ms: int | None, instant: datetime) -> bool:
    return ms is not None and ms * EPOCH_US_PER_MS < _us(instant)


def build_filters(ctx: Context, args: Any) -> Filters:
    """The filters of a parsed command line (``args.check_filters`` already validated them).
    Times resolve against the gateway clock and the configured ``time_zone``."""
    zone = ctx.settings.zone()
    now = ctx.gateway.now()

    def when(name: str) -> datetime | None:
        value = getattr(args, name, None)
        if value is None:
            return None
        return parse_when(value, field=f"--{name}", now=now, time_zone=zone)

    return Filters(
        since=when("since"),
        until=when("until"),
        project=getattr(args, "project", None),
        labels=tuple(parse_label(v, field="--label") for v in (getattr(args, "label", None) or [])),
        kind=getattr(args, "kind", None),
        outcome=getattr(args, "outcome", None),
        entrypoint=getattr(args, "entrypoint", None),
        model=getattr(args, "model", None),
        include_self_improve=bool(getattr(args, "include_self_improve", False)),
    )


def selected(
    conn: sqlite3.Connection, ctx: Context, filters: Filters
) -> Iterator[tuple[SessionValues, Snapshot | None]]:
    """Every stored session that passes the filters, with its snapshot, in no particular order.
    Derivation streams in batches of 200 sessions. A harness filter narrows the ids first from
    the snapshots, so a store with few harness runs derives few sessions."""
    index = snapshot_index(conn)
    ids: list[str] | None = None
    if filters.needs_snapshot:
        ids = sorted(sid for sid, snap in index.items() if filters.snapshot_matches(snap))
    for values in derive.iter_session_values(conn, ctx.settings, ids):
        snapshot = index.get(values.session_id)
        if filters.matches(values, snapshot):
            yield values, snapshot


# --- show ----------------------------------------------------------------------------------------


def _prefix_range(prefix: str) -> tuple[str, str]:
    """``[low, high)`` holding exactly the ids that start with ``prefix`` (hex digits and dashes
    only, so the last character can be bumped without leaving the byte order)."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def resolve_session(conn: sqlite3.Connection | None, prefix: str) -> str:
    """The stored session id that ``prefix`` names. Raises ``PrefixNotFoundError`` (exit 10) or
    ``AmbiguousPrefixError`` (exit 21, at most 20 sorted matches and the real total). Reads at
    most 21 ids through the primary-key index, whatever the corpus size."""
    if conn is None:
        raise PrefixNotFoundError("not found", "SESSION_ID", details={"id": prefix})
    low, high = _prefix_range(prefix)
    rows = [
        r[0]
        for r in conn.execute(
            "SELECT session_id FROM sessions WHERE session_id >= ? AND session_id < ? "
            "ORDER BY session_id LIMIT ?",
            (low, high, PREFIX_PROBE),
        )
    ]
    if prefix in rows:
        return prefix
    if not rows:
        raise PrefixNotFoundError("not found", "SESSION_ID", details={"id": prefix})
    if len(rows) == 1:
        return rows[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id >= ? AND session_id < ?", (low, high)
    ).fetchone()[0]
    error = AmbiguousPrefixError(
        "ambiguous prefix",
        "SESSION_ID",
        details={"matches": rows[:MAX_PREFIX_MATCHES], "matched": total},
    )
    raise error


# One group per (slot, agent, resolved model; column 3, because a name in GROUP BY would be the
# raw ``e.model`` column): the day needs the time zone, so it is applied in Python.
# The WHERE names the session and ``counted`` so ``events_session_counted`` serves it.
USAGE_SQL = (
    "SELECT e.slot, e.agent_id, "
    f"{derive.RESOLVED_MODEL_SQL} AS model, "
    "SUM(CASE WHEN e.kind = 'message' THEN e.input_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' THEN e.output_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' THEN e.cache_read_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' "
    "THEN e.cache_write_5m_tokens + e.cache_write_1h_tokens ELSE 0 END), "
    "SUM(e.kind = 'tool_call'), SUM(e.kind = 'tool_error'), SUM(e.kind = 'denial'), "
    "SUM(e.kind = 'interruption'), "
    f"SUM(CASE WHEN e.kind = 'turn' THEN {derive.SECONDS_MS_SQL.replace('seconds', 'e.seconds')} "
    "END) "
    "FROM events e WHERE e.session_id = ? AND e.counted = 1 "
    "GROUP BY e.slot, e.agent_id, 3"
)


@dataclass
class _Acc:
    tokens: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    counts: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    active_ms: int | None = None


def usage_rows(
    conn: sqlite3.Connection, session_id: str, zone: tzinfo, *, limit: int | None = None
) -> tuple[list[dict[str, object]], bool]:
    """The usage rows of one session: counted events per (day in ``zone``, agent or main, model)
    and whether more than ``limit`` rows existed. Ordered by day (no slot last), then agent (the
    main agent first), then model (none last). Memory is bounded by the session's distinct
    (slot, agent, model) groups, never by its events."""
    limit = USAGE_ROW_MAX if limit is None else limit
    bucketer = derive.Bucketer(zone)
    merged: dict[tuple[str | None, str | None, str | None], _Acc] = {}
    for row in conn.execute(USAGE_SQL, (session_id,)):
        slot, agent_id, model = row[0], row[1], row[2]
        key = (bucketer.key(slot, "day"), agent_id, model)
        acc = merged.setdefault(key, _Acc())
        for i in range(4):
            acc.tokens[i] += row[3 + i]
        for i in range(4):
            acc.counts[i] += row[7 + i]
        if row[11] is not None:
            acc.active_ms = (acc.active_ms or 0) + row[11]
    ordered = sorted(
        merged.items(),
        key=lambda item: (
            item[0][0] is None,
            item[0][0] or "",
            item[0][1] is not None,
            item[0][1] or "",
            item[0][2] is None,
            item[0][2] or "",
        ),
    )
    rows: list[dict[str, object]] = []
    for (day, agent_id, model), acc in ordered[:limit]:
        rows.append(
            {
                "day": day,
                "session_id": session_id,
                "agent_id": agent_id,
                "model": model,
                "tokens": {
                    "input": acc.tokens[0],
                    "output": acc.tokens[1],
                    "cache_read": acc.tokens[2],
                    "cache_write": acc.tokens[3],
                },
                "tool_calls": acc.counts[0],
                "tool_errors": acc.counts[1],
                "denials": acc.counts[2],
                "interruptions": acc.counts[3],
                "active_seconds": derive.seconds_of(acc.active_ms),
            }
        )
    return rows, len(ordered) > limit


def read_stream[T](
    ctx: Context, work: Callable[[sqlite3.Connection | None], Iterator[T]]
) -> Iterator[T]:
    """``read_store`` for a command that streams: the items of ``work`` are yielded as they are
    produced inside one read transaction. A torn read before the first item reopens the store
    and retries once (as ``read_store``); after an item was yielded nothing can be retried
    without repeating output, so any ``sqlite3.Error`` is the store error (exit 12)."""
    produced = False
    for attempt in (1, 2):
        try:
            with open_reader(ctx) as conn:
                for item in work(conn):
                    produced = True
                    yield item
                return
        except sqlite3.DatabaseError as exc:
            if attempt == 2 or produced:
                raise storage_error(exc) from None
        except sqlite3.Error as exc:
            raise storage_error(exc) from None


def derive_day(since: datetime | None, zone: tzinfo) -> tuple[str | None, int | None]:
    """``export usage --since``: the local day of the instant in ``zone`` (lines on or after it
    are kept) and a slot bound two days before the instant that lets SQLite skip older events."""
    if since is None:
        return None, None
    local = since.astimezone(zone)
    day = f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    return day, int(since.timestamp()) // derive.SLOT_SECONDS - 2 * 96
