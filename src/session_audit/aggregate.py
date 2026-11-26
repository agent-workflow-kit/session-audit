"""Aggregation for ``usage`` and the streams of ``export`` (issue #17).

``usage``. The filter set is the sessions that pass the Session filters (``--since`` and
``--until`` narrow *events* here, not sessions: Session filters). Their counted events are read
in chunks of at most ``CHUNK`` session ids (``events_session_counted`` serves every statement; an
unconstrained scan of the events is ~3 s per million events) and grouped in SQL by
(session, slot, model, tool, kind), then folded in Python into one accumulator per dimension
tuple. Memory is bounded by the distinct groups and the number of sessions, never by events.

- A session's events are all in one chunk, so a (row, session) pair is seen once per chunk and
  ``sessions``, the ``flags`` union and ``reported_usd`` are folded once per pair.
- The estimate is additive: per-model token sums go into the row's ``CostAccumulator`` (exact
  ``Decimal``), the total is rounded once at the end. ``totals`` is its own accumulator fed by
  the same groups, never the sum of the rows, so overlapping label rows do not inflate it.
- Turn milliseconds are summed as integers and divided once (``derive.seconds_of``).
- ``reported_usd`` goes through ``derive.reported_sum`` with ``time_filtered`` and
  ``splits_session`` (day, week, month, model and tool dimensions split a session).

``export``. ``sessions`` derives the Session objects in batches in the order of a small sort key
list (start time, id); ``usage`` reads one grouped, slot-ordered statement and emits a day at a
time. Both are generators: a line is written as soon as it exists.
"""

from __future__ import annotations

import heapq
import itertools
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from decimal import Decimal
from typing import Any

from . import derive
from .command import Context
from .derive import TokenSums
from .sessionquery import (
    Filters,
    Snapshot,
    _us,
    selected,
    session_json,
    snapshot_index,
)

CHUNK = 500  # session ids per statement (SQLite builds before 3.32 allow 999 variables)
KEY_MAX = 4096  # the schema's cap on a key text
TIME_DIMENSIONS = ("day", "week", "month")
TOOL_KINDS = frozenset({"tool_call", "tool_error", "denial"})
SPLITTING = frozenset({"day", "week", "month", "model", "tool"})
_FLAG_ORDER = {name: i for i, name in enumerate(derive.FLAGS)}
_SLOTS_PER_DAY = 96


# --- the statement -----------------------------------------------------------------------------


def usage_sql(*, model: str, tool: bool, marks: int, window: bool) -> str:
    """The grouped counted events of ``marks`` sessions. ``model`` is ``"dimension"`` (the
    resolved model of every kind), ``"messages"`` (the model of message events, for cost) or
    ``"none"``; ``tool`` selects the resolved tool; ``window`` adds ``ts`` bounds (two
    parameters after the ids). The group has the kind, so a row can drop kinds it does not own."""
    model_expr = {
        "dimension": "e.model",
        "messages": "CASE WHEN e.kind = 'message' THEN e.model END",
        "none": "NULL",
    }[model]
    tool_expr = "e.tool" if tool else "NULL"
    where = f"e.session_id IN ({','.join('?' * marks)})"
    if window:
        where += " AND e.ts >= ? AND e.ts < ?"
    return (
        f"SELECT e.session_id, e.slot, {model_expr}, {tool_expr}, e.kind, "
        "SUM(e.input_tokens), SUM(e.output_tokens), SUM(e.cache_read_tokens), "
        "SUM(e.cache_write_5m_tokens), SUM(e.cache_write_1h_tokens), COUNT(*), SUM(e.seconds_ms) "
        f"FROM {derive.COUNTED_EVENTS_SQL} e WHERE {where} "
        "GROUP BY e.session_id, e.slot, 3, 4, e.kind"
    )


# --- the filter set -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Meta:
    """What a session contributes to a key: compact, so a chunk of them is cheap."""

    session_id: str
    project: str | None
    entrypoint: str
    kind: str | None
    flags: tuple[str, ...]
    reported: Decimal | None
    labels: tuple[str, ...]


def clip(text: str | None) -> str | None:
    return None if text is None else text[:KEY_MAX]


def label_values(labels: Sequence[str], key: str) -> tuple[str, ...]:
    """The distinct values the snapshot's labels hold for ``key`` (a label is ``key=value``;
    compared as plain text, so no character of a key means anything to a pattern)."""
    found = {text.partition("=")[2] for text in labels if text.partition("=")[0] == key}
    return tuple(sorted(clip(v) or "" for v in found))


def _meta(values: derive.SessionValues, snapshot: Snapshot | None) -> Meta:
    return Meta(
        session_id=values.session_id,
        project=clip(values.project),
        entrypoint=values.entrypoint,
        kind=None if snapshot is None else snapshot.shown_kind,
        flags=values.flags,
        reported=values.cost.reported,
        labels=() if snapshot is None else snapshot.labels,
    )


# --- accumulators -------------------------------------------------------------------------------


@dataclass
class Acc:
    """One output row (or the totals)."""

    cost: derive.CostAccumulator
    sessions: int = 0
    tokens: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    tool_calls: int = 0
    tool_errors: int = 0
    denials: int = 0
    interruptions: int = 0
    active_ms: int | None = None
    flags: set[str] = field(default_factory=set)
    reported: Decimal = field(default_factory=lambda: Decimal(0))
    reported_missing: bool = False

    def count(self, kind: str, n: int, seconds_ms: int | None) -> None:
        if kind == "tool_call":
            self.tool_calls += n
        elif kind == "tool_error":
            self.tool_errors += n
        elif kind == "denial":
            self.denials += n
        elif kind == "interruption":
            self.interruptions += n
        elif kind == "turn":
            self.active_ms = (self.active_ms or 0) + (seconds_ms or 0)

    def token_sums(self, sums: TokenSums) -> None:
        self.tokens[0] += sums.input
        self.tokens[1] += sums.output
        self.tokens[2] += sums.cache_read
        self.tokens[3] += sums.cache_write

    def contribute(self, meta: Meta) -> None:
        self.sessions += 1
        self.flags.update(meta.flags)
        if meta.reported is None:
            self.reported_missing = True
        else:
            with derive.exact():
                self.reported += meta.reported

    def reported_usd(self, *, time_filtered: bool, splits_session: bool) -> Decimal | None:
        if self.sessions == 0:
            return None
        values: list[Decimal | None] = [None if self.reported_missing else self.reported]
        return derive.reported_sum(
            values, time_filtered=time_filtered, splits_session=splits_session
        )

    def to_json(
        self, *, tool_view: bool, time_filtered: bool, splits_session: bool
    ) -> dict[str, object]:
        """The row fields without ``key``. A tool view (the ``tool`` dimension) owns no tokens,
        cost, active time or interruptions."""
        cost = derive.Cost(
            self.reported_usd(time_filtered=time_filtered, splits_session=splits_session),
            self.cost.estimated,
            self.cost.unknown_tokens,
        )
        return {
            "sessions": self.sessions,
            "tokens": None
            if tool_view
            else {
                "input": self.tokens[0],
                "output": self.tokens[1],
                "cache_read": self.tokens[2],
                "cache_write": self.tokens[3],
            },
            "cost": None if tool_view else cost.to_json(),
            "active_seconds": None if tool_view else derive.seconds_of(self.active_ms),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "denials": self.denials,
            "interruptions": None if tool_view else self.interruptions,
            "flags": sorted(self.flags, key=_FLAG_ORDER.__getitem__),
        }

    @property
    def token_total(self) -> int:
        return sum(self.tokens)


# --- usage --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageQuery:
    dims: tuple[str, ...]
    zone: tzinfo
    since_ms: int | None
    until_ms: int | None
    limit: int

    @property
    def time_filtered(self) -> bool:
        return self.since_ms is not None or self.until_ms is not None

    @property
    def tool_view(self) -> bool:
        return "tool" in self.dims

    @property
    def splits_session(self) -> bool:
        return any(d in SPLITTING for d in self.dims)


@dataclass
class UsageResult:
    rows: list[dict[str, object]]
    totals: dict[str, object]
    truncated: bool


def window_bounds(
    since: datetime | None, until: datetime | None
) -> tuple[int | None, int | None]:
    """Event ``ts`` bounds in epoch milliseconds for ``S <= ts < U``, exactly: an instant with
    microseconds rounds up, so ``ts * 1000 >= us(S)`` and ``ts * 1000 < us(U)`` hold as integers."""
    def ms(moment: datetime | None) -> int | None:
        return None if moment is None else -(-_us(moment) // 1000)

    return ms(since), ms(until)


def _parts(
    dim: str, meta: Meta, slot: int | None, model: str | None, tool: str | None, kind: str,
    bucketer: derive.Bucketer,
) -> Sequence[str | None]:
    if dim in TIME_DIMENSIONS:
        return (bucketer.key(slot, dim),)  # type: ignore[arg-type]
    if dim == "project":
        return (meta.project,)
    if dim == "entrypoint":
        return (meta.entrypoint,)
    if dim == "kind":
        return (meta.kind,)
    if dim == "session":
        return (meta.session_id,)
    if dim == "model":
        return (clip("unknown" if model is None and kind == "message" else model),)
    if dim == "tool":
        return (clip(tool),)
    return label_values(meta.labels, dim[len("label=") :]) or (None,)


def _order(key: tuple[str | None, ...]) -> tuple[tuple[bool, str], ...]:
    return tuple((part is None, part or "") for part in key)


def usage(conn: sqlite3.Connection | None, ctx: Context, filters: Filters, q: UsageQuery) -> UsageResult:
    """Rows, totals and ``truncated`` of one ``usage`` run (``filters`` has no time bounds)."""
    prices = ctx.settings.prices
    bucketer = derive.Bucketer(q.zone)
    rows: dict[tuple[str | None, ...], Acc] = {}
    totals = Acc(derive.CostAccumulator(prices))
    if conn is not None:
        sql_model = "dimension" if "model" in q.dims else ("none" if q.tool_view else "messages")
        window = q.time_filtered
        bounds = (
            q.since_ms if q.since_ms is not None else -(2**62),
            q.until_ms if q.until_ms is not None else 2**62,
        )
        buffer: list[Meta] = []

        def flush() -> None:
            if buffer:
                _chunk(conn, buffer, q, rows, totals, bucketer, sql_model, window, bounds)
                buffer.clear()

        for values, snapshot in selected(conn, ctx, filters):
            buffer.append(_meta(values, snapshot))
            if len(buffer) >= CHUNK:
                flush()
        flush()
    flat = (q.time_filtered, q.splits_session)

    def row_json(key: tuple[str | None, ...], acc: Acc) -> dict[str, object]:
        doc: dict[str, object] = {"key": list(key)}
        doc.update(acc.to_json(tool_view=q.tool_view, time_filtered=flat[0], splits_session=flat[1]))
        return doc

    if q.dims[0] in TIME_DIMENSIONS:

        def sort_key(item: tuple[tuple[str | None, ...], Acc]) -> tuple[Any, ...]:
            return (_order(item[0]),)
    else:

        def sort_key(item: tuple[tuple[str | None, ...], Acc]) -> tuple[Any, ...]:
            acc = item[1]
            return (q.tool_view, -acc.token_total, _order(item[0]))

    best = heapq.nsmallest(q.limit + 1, rows.items(), key=sort_key)
    truncated = len(best) > q.limit
    out_rows = [row_json(key, acc) for key, acc in best[: q.limit]]
    totals_doc = totals.to_json(tool_view=q.tool_view, time_filtered=q.time_filtered, splits_session=False)
    return UsageResult(out_rows, totals_doc, truncated)


def _chunk(
    conn: sqlite3.Connection,
    metas: list[Meta],
    q: UsageQuery,
    rows: dict[tuple[str | None, ...], Acc],
    totals: Acc,
    bucketer: derive.Bucketer,
    sql_model: str,
    window: bool,
    bounds: tuple[int, int],
) -> None:
    by_id = {m.session_id: m for m in metas}
    prices = totals.cost._prices  # noqa: SLF001 - one price table for every accumulator
    sql = usage_sql(model=sql_model, tool=q.tool_view, marks=len(by_id), window=window)
    params: list[Any] = list(by_id)
    if window:
        params.extend(bounds)
    touched: dict[str, set[tuple[str | None, ...]]] = {}
    model_sums: dict[tuple[tuple[str | None, ...], str | None], TokenSums] = {}
    total_sums: dict[str | None, TokenSums] = {}
    tool_view = q.tool_view
    for row in conn.execute(sql, params):
        sid, slot, model, tool, kind = row[:5]
        meta = by_id[sid]
        n, seconds = row[10], row[11]
        sums = TokenSums(*(int(v or 0) for v in row[5:10])) if kind == "message" else None
        touched.setdefault(sid, set())
        if not tool_view:
            totals.count(kind, n, seconds)
            if sums is not None:
                totals.token_sums(sums)
                total_sums[model] = total_sums.get(model, TokenSums()) + sums
        elif kind in TOOL_KINDS:
            totals.count(kind, n, seconds)
        if tool_view and kind not in TOOL_KINDS:
            continue
        options = [_parts(d, meta, slot, model, tool, kind, bucketer) for d in q.dims]
        for key in itertools.product(*options):
            acc = rows.get(key)
            if acc is None:
                acc = rows[key] = Acc(derive.CostAccumulator(prices))
            acc.count(kind, n, seconds)
            if sums is not None and not tool_view:
                acc.token_sums(sums)
                slot_key = (key, model)
                model_sums[slot_key] = model_sums.get(slot_key, TokenSums()) + sums
            touched[sid].add(key)
    for (key, model), sums in model_sums.items():
        rows[key].cost.add(model, sums)
    for model, sums in total_sums.items():
        totals.cost.add(model, sums)
    for sid, keys in touched.items():
        meta = by_id[sid]
        totals.contribute(meta)
        for key in keys:
            rows[key].contribute(meta)


# --- export -------------------------------------------------------------------------------------


def session_lines(conn: sqlite3.Connection | None, ctx: Context, filters: Filters) -> Iterator[dict[str, object]]:
    """``export sessions``: the Session objects, oldest first (``started_at`` ascending, no start
    last, then id). Pass one keeps only (start, id) of each selected session; pass two derives
    the objects in batches in that order, so no list of objects is ever built."""
    if conn is None:
        return
    order = sorted(
        (v.started_ms is None, v.started_ms or 0, v.session_id) for v, _s in selected(conn, ctx, filters)
    )
    index = snapshot_index(conn)
    for start in range(0, len(order), derive.BATCH):
        ids = [item[2] for item in order[start : start + derive.BATCH]]
        for values in derive.iter_session_values(conn, ctx.settings, ids):
            yield session_json(values, index.get(values.session_id))


_EXPORT_SQL = (
    "SELECT e.slot, e.session_id, e.agent_id, e.model, "
    "SUM(CASE WHEN e.kind = 'message' THEN e.input_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' THEN e.output_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' THEN e.cache_read_tokens ELSE 0 END), "
    "SUM(CASE WHEN e.kind = 'message' "
    "THEN e.cache_write_5m_tokens + e.cache_write_1h_tokens ELSE 0 END), "
    "SUM(e.kind = 'tool_call'), SUM(e.kind = 'tool_error'), SUM(e.kind = 'denial'), "
    "SUM(e.kind = 'interruption'), SUM(CASE WHEN e.kind = 'turn' THEN e.seconds_ms END) "
    f"FROM {derive.COUNTED_EVENTS_SQL} e WHERE e.session_id IS NOT NULL{{bound}} "
    "GROUP BY e.slot, e.session_id, e.agent_id, 4 ORDER BY e.slot IS NULL, e.slot"
)


@dataclass
class _Line:
    tokens: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    counts: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    active_ms: int | None = None


def usage_line(
    day: str | None, session_id: str, agent_id: str | None, model: str | None, acc: _Line
) -> dict[str, object]:
    """One line of ``export usage``: the usage row of ``show``."""
    return {
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


_LineKey = tuple[str, str | None, str | None]


def _line_order(item: tuple[_LineKey, _Line]) -> tuple[Any, ...]:
    session_id, agent_id, model = item[0]
    return (session_id, agent_id is not None, agent_id or "", model is None, model or "")


def usage_lines(
    conn: sqlite3.Connection | None,
    ctx: Context,
    filters: Filters,
    zone: tzinfo,
    since_day: str | None,
    min_slot: int | None,
) -> Iterator[dict[str, object]]:
    """``export usage``: counted events per (day in ``zone``, session, agent, model), oldest day
    first (a null day last), then session, agent (the main agent first) and model (none last).

    One grouped statement ordered by slot is read as a stream. The local date never goes more
    than a day back as slots advance, so a day is complete once the slot is more than 48 hours
    past it; only the days still open are held (bounded by their groups). ``since_day`` drops
    earlier days and the lines with no day; ``min_slot`` lets SQLite skip older events."""
    if conn is None:
        return
    index = snapshot_index(conn)
    skipped = {sid for sid, snap in index.items() if not filters.snapshot_matches(snap)}
    bucketer = derive.Bucketer(zone)
    pending: dict[str, dict[_LineKey, _Line]] = {}
    undated: dict[_LineKey, _Line] = {}

    def emit(day: str | None, groups: dict[_LineKey, _Line]) -> Iterator[dict[str, object]]:
        for (sid, agent, model), acc in sorted(groups.items(), key=_line_order):
            yield usage_line(day, sid, agent, model, acc)

    bound = "" if min_slot is None else " AND e.slot >= ?"
    cursor = conn.execute(_EXPORT_SQL.format(bound=bound), () if min_slot is None else (min_slot,))
    for row in cursor:
        slot, sid = row[0], row[1]
        if sid in skipped:
            continue
        day = bucketer.key(slot, "day")
        if day is None:
            groups = undated
        else:
            behind = bucketer.key(slot - 2 * _SLOTS_PER_DAY, "day")
            for done in sorted(d for d in pending if behind is not None and d < behind):
                if since_day is None or done >= since_day:
                    yield from emit(done, pending[done])
                del pending[done]
            groups = pending.setdefault(day, {})
        acc = groups.setdefault((sid, row[2], row[3]), _Line())
        for i in range(4):
            acc.tokens[i] += int(row[4 + i] or 0)
            acc.counts[i] += int(row[8 + i] or 0)
        if row[12] is not None:
            acc.active_ms = (acc.active_ms or 0) + int(row[12])
    for done in sorted(pending):
        if since_day is None or done >= since_day:
            yield from emit(done, pending[done])
    if since_day is None:
        yield from emit(None, undated)


__all__ = [
    "CHUNK",
    "UsageQuery",
    "UsageResult",
    "session_lines",
    "usage",
    "usage_lines",
    "usage_sql",
    "window_bounds",
]
