"""Session values, time buckets and cost with its basis (issue #14), derived at query time.

Nothing here is stored: every value comes from counted events (``events.counted = 1``) and the
per-file facts of ``files`` (Counting rules 8 and Cost in ``docs/interface.md``), so a reread or a
changed ``time_zone`` or price table changes the answer with no migration. The query commands
(#16 ``sessions`` and ``show``, #17 ``usage``) call this module; it prints nothing and holds no
conversation text. Strings it returns (project, version, model names, agent types) are untrusted
and bounded; the output layer shows them through ``textsafe``.

Arithmetic. Tokens and milliseconds are integers (SQL ``SUM`` over integers is exact). A dollar
figure is computed in ``Decimal`` from the price table (each price read through its shortest
``repr``, so ``3.75`` is 3.75), summed exactly over every event and model, and rounded once,
half up, to whole micro-dollars (six decimals) when it is shown. A total is rounded from the
exact sum of its events, never from rounded rows; a reported figure is rounded to micro-dollars
when it is read and its sums are exact. ``active_seconds`` is the exact integer sum of turn
milliseconds divided by 1000 (correctly rounded), so it is the same whatever order the rows were
added. No float is ever summed.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from .config import ModelPrice, PriceTable, Settings
from .grammar import format_timestamp
from .incremental import current_rules_version

SLOT_SECONDS = 900  # a slot is the epoch second divided by this (D22)
MAX_MODELS = 64  # Session `models`
BATCH = 200  # session ids per SQL statement; bounds memory and parameters
USD_STEP = Decimal("0.000001")  # dollars are shown in whole micro-dollars
UNKNOWN_MODEL = "unknown"  # a message with no model name
TOKENS_PER_PRICE = Decimal(1_000_000)  # prices are USD per million tokens
EXACT_PRECISION = 80  # digits: a sum of capped counts times prices never rounds at this width
MAX_AGENTS = 1000  # agent_values cap; keeps its bound parameters below SQLite's limit

# The closed set of quality flags, in output order (schemas/defs.json ``flag``).
FLAGS = (
    "cost_mismatch",
    "no_usage",
    "no_timestamps",
    "old_rules",
    "project_from_dir",
    "cost_invalid",
)

Unit = Literal["day", "week", "month"]

# Turn seconds are stored as ``ms / 1000`` floats; the integer milliseconds are exact, so every
# aggregate of ``seconds`` must sum this expression and divide by 1000 once (``seconds_of``).
SECONDS_MS_SQL = "CAST(ROUND(seconds * 1000) AS INTEGER)"

# Model and tool of an event (Counting rules 6): a tool_error or denial takes both from the call
# event whose key is its ``ref`` (the counted copy, else any copy), else NULL. ``e`` is the
# events alias. A message and a call keep their own values.
RESOLVED_MODEL_SQL = (
    "COALESCE(e.model, (SELECT c.model FROM events c WHERE c.key = e.ref "
    "ORDER BY c.counted DESC, c.id LIMIT 1))"
)
RESOLVED_TOOL_SQL = (
    "COALESCE(e.tool, (SELECT c.tool FROM events c WHERE c.key = e.ref "
    "ORDER BY c.counted DESC, c.id LIMIT 1))"
)
# The counted events with their resolved model and tool, for ``FROM`` in a larger query. Only
# tool_error and denial events have a ``ref``, so a NULL model of any other kind stays NULL.
COUNTED_EVENTS_SQL = (
    "(SELECT e.id, e.key, e.ref, e.kind, e.ts, e.slot, e.session_id, e.agent_id, "
    f"{RESOLVED_MODEL_SQL} AS model, {RESOLVED_TOOL_SQL} AS tool, "
    "e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_5m_tokens, "
    f"e.cache_write_1h_tokens, e.seconds, {SECONDS_MS_SQL.replace('seconds', 'e.seconds')} "
    "AS seconds_ms FROM events e WHERE e.counted = 1)"
)

# The statements of ``_derive_batch``; ``{marks}`` is the placeholder list of the session ids.
# Kept as constants so a test can run ``EXPLAIN QUERY PLAN`` on the real text.
COUNTS_SQL = (
    "SELECT session_id, SUM(kind = 'tool_call'), SUM(kind = 'tool_error'), "
    "SUM(kind = 'denial'), SUM(kind = 'interruption'), SUM(kind = 'compaction'), "
    f"SUM(kind = 'turn'), SUM(CASE WHEN kind = 'turn' THEN {SECONDS_MS_SQL} END), "
    "MIN(ts), MAX(ts), SUM(ts IS NULL), SUM(kind = 'message') "
    "FROM events WHERE counted = 1 AND session_id IN ({marks}) GROUP BY session_id"
)
# One row per (session, model name): all-agent sums, then the main agent's sums. A message
# with no model is the model ``unknown`` (so it merges with one named that).
TOKENS_SQL = (
    "SELECT session_id, COALESCE(model, 'unknown'), "
    "SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
    "SUM(cache_write_5m_tokens), SUM(cache_write_1h_tokens), "
    "SUM(CASE WHEN agent_id IS NULL THEN input_tokens ELSE 0 END), "
    "SUM(CASE WHEN agent_id IS NULL THEN output_tokens ELSE 0 END), "
    "SUM(CASE WHEN agent_id IS NULL THEN cache_read_tokens ELSE 0 END), "
    "SUM(CASE WHEN agent_id IS NULL THEN cache_write_5m_tokens ELSE 0 END), "
    "SUM(CASE WHEN agent_id IS NULL THEN cache_write_1h_tokens ELSE 0 END) "
    "FROM events WHERE counted = 1 AND kind = 'message' AND session_id IN ({marks}) "
    "GROUP BY session_id, COALESCE(model, 'unknown')"
)
# Sessions whose main file holds a copy (counted 0) owned by another session.
COPIED_SQL = (
    "SELECT DISTINCT s.session_id FROM sessions s "
    "JOIN events c ON c.file_id = s.main_file_id AND c.counted = 0 "
    "WHERE s.session_id IN ({marks}) AND EXISTS (SELECT 1 FROM events o "
    "WHERE o.key = c.key AND o.counted = 1 AND o.session_id IS NOT s.session_id)"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --- time -------------------------------------------------------------------------------------


def slot_start(slot: int) -> datetime:
    """The UTC start of a slot. Raises ``ValueError`` outside the supported range."""
    try:
        return _EPOCH + timedelta(seconds=slot * SLOT_SECONDS)
    except OverflowError:
        raise ValueError("slot out of range") from None


def bucket_key(slot: int | None, unit: Unit, zone: tzinfo) -> str | None:
    """The key of a slot's ``day`` (``2026-11-20``), ISO ``week`` (``2026-W47``) or ``month``
    (``2026-11``) in ``zone``, from the local date of the slot's start. Every real zone offset
    and every transition is a multiple of 15 minutes, so a slot lies inside one local day and
    any zone, and a changed ``time_zone``, re-bucket exactly. None for no slot (or one outside
    the calendar), which usage puts in the row whose key is null."""
    if slot is None:
        return None
    try:
        local = slot_start(slot).astimezone(zone)
    except (ValueError, OverflowError, OSError):
        return None
    if unit == "day":
        return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    if unit == "month":
        return f"{local.year:04d}-{local.month:02d}"
    iso = local.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


class Bucketer:
    """``bucket_key`` for one zone with a cache; a corpus has few distinct slots per query."""

    def __init__(self, zone: tzinfo) -> None:
        self.zone = zone
        self._cache: dict[tuple[int, str], str | None] = {}

    def key(self, slot: int | None, unit: Unit) -> str | None:
        if slot is None:
            return None
        found = self._cache.get((slot, unit), _MISSING)
        if found is _MISSING:
            found = bucket_key(slot, unit, self.zone)
            if len(self._cache) < 1_000_000:
                self._cache[(slot, unit)] = found
        return found  # type: ignore[return-value]


_MISSING: object = object()


def format_ms(ms: int | None) -> str | None:
    """Epoch milliseconds as a UTC ``timestamp`` of the schemas (None stays None)."""
    if ms is None:
        return None
    try:
        return format_timestamp(_EPOCH + timedelta(milliseconds=ms))
    except (OverflowError, ValueError):
        return None


def seconds_of(milliseconds: int | None) -> float | None:
    """``active_seconds`` from an exact millisecond sum (None when there was no turn)."""
    return None if milliseconds is None else milliseconds / 1000


# --- cost -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenSums:
    """The counters of message events (cache writes kept apart for the two price tiers)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: TokenSums) -> TokenSums:
        return TokenSums(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write_5m + other.cache_write_5m,
            self.cache_write_1h + other.cache_write_1h,
        )

    def to_json(self) -> dict[str, int]:
        """The four counters (``tokens4``)."""
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@contextmanager
def exact() -> Iterator[None]:
    """A ``Decimal`` context wide enough that no dollar sum, rounding or compare loses a digit
    (the default 28 digits can round a large sum the wrong way)."""
    with localcontext() as ctx:
        ctx.prec = EXACT_PRECISION
        yield


def _dec(price: float) -> Decimal:
    return Decimal(repr(float(price)))


def tokens_cost(price: ModelPrice, sums: TokenSums) -> Decimal:
    """The exact (unrounded) USD of ``sums`` at ``price`` (USD per million tokens)."""
    with exact():
        total = (
            sums.input * _dec(price.input)
            + sums.output * _dec(price.output)
            + sums.cache_read * _dec(price.cache_read)
            + sums.cache_write_5m * _dec(price.cache_write)
            + sums.cache_write_1h * _dec(price.cache_write_1h_or_default)
        )
        return total / TOKENS_PER_PRICE


def usd(value: Decimal) -> Decimal:
    """Round to whole micro-dollars, half up."""
    with exact():
        return value.quantize(USD_STEP, rounding=ROUND_HALF_UP)


def usd_json(value: Decimal) -> float:
    """The JSON number of a dollar figure: finite, rounded to micro-dollars, and the shortest
    float that reads back as the same decimal (so it prints stably)."""
    return float(usd(value))


class CostAccumulator:
    """The estimate of a set of message events: ``add`` the token sums of each model, then read
    ``estimated`` (exact, unrounded) and ``unknown_tokens``. A model with no price adds its four
    counters to ``unknown_tokens`` and nothing to the estimate (price lookup is
    ``PriceTable.lookup``: exact name, else the longest key followed by a ``-`` boundary)."""

    def __init__(self, prices: PriceTable) -> None:
        self._prices = prices
        self._lookups: dict[str, ModelPrice | None] = {}
        self.estimated = Decimal(0)
        self.unknown_tokens = 0

    def add(self, model: str | None, sums: TokenSums) -> None:
        name = UNKNOWN_MODEL if model is None else model
        if name in self._lookups:
            price = self._lookups[name]
        else:
            price = self._prices.lookup(name)
            if len(self._lookups) < 1024:  # a session with many names must not grow without bound
                self._lookups[name] = price
        if price is None:
            self.unknown_tokens += sums.total
        else:
            with exact():
                self.estimated += tokens_cost(price, sums)


def reported_from_file(value: float | None) -> Decimal | None:
    """``files.cost_state_usd`` as micro-dollars; None when missing or not a finite non-negative
    number (a damaged store never prints NaN or a negative figure)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < 0:
        return None
    return usd(Decimal(repr(x)))


def is_mismatch(reported: Decimal | None, estimated: Decimal, margin: float) -> bool:
    """``cost_mismatch``: a reported figure that differs from the estimate by more than
    ``margin`` times the estimate, both as printed (the estimate rounded to micro-dollars; both
    figures stay unchanged). With a printed estimate of 0 any positive reported figure is a
    mismatch. Compared exactly in ``Decimal``; ``unknown_tokens`` plays no part."""
    if reported is None:
        return False
    with exact():
        printed = usd(estimated)
        return abs(reported - printed) > Decimal(repr(float(margin))) * printed


def reported_sum(
    reported: Iterable[Decimal | None], *, time_filtered: bool = False, splits_session: bool = False
) -> Decimal | None:
    """The ``reported_usd`` of a usage row or totals (Counting rules 2): the exact sum over the
    contributing sessions when every one has a reported figure, no ``--since``/``--until`` is
    given and no dimension of the row splits a session (day, week, month, model, tool); else
    None. No contributing session is None too, so a missing figure is never 0."""
    if time_filtered or splits_session:
        return None
    total = Decimal(0)
    seen = False
    for value in reported:
        if value is None:
            return None
        with exact():
            total += value
        seen = True
    return total if seen else None


@dataclass(frozen=True, slots=True)
class Cost:
    """The three figures of the ``cost`` object. ``estimated`` is exact and unrounded; the JSON
    form rounds once. ``reported`` is already in micro-dollars."""

    reported: Decimal | None
    estimated: Decimal
    unknown_tokens: int

    def to_json(self) -> dict[str, object]:
        return {
            "reported_usd": None if self.reported is None else usd_json(self.reported),
            "estimated_usd": usd_json(self.estimated),
            "unknown_tokens": self.unknown_tokens,
        }


# --- session values ---------------------------------------------------------------------------


def map_entrypoint(value: str | None) -> str:
    """``cli`` is interactive, a value starting ``sdk-`` is headless, anything else unknown."""
    if value == "cli":
        return "interactive"
    if value is not None and value.startswith("sdk-"):
        return "headless"
    return "unknown"


@dataclass(frozen=True, slots=True)
class SessionValues:
    """One session (``defs.json`` ``session``) without ``harness``, which #16 joins at query
    time. Times are epoch milliseconds (``format_ms`` for the output)."""

    session_id: str
    project: str | None
    entrypoint: str
    version: str | None
    started_ms: int | None
    ended_ms: int | None
    active_ms: int | None
    models: tuple[str, ...]
    tokens: TokenSums  # the main agent's
    subagent_tokens: int  # all four counters over the agents
    cost: Cost
    tool_calls: int
    tool_errors: int
    denials: int
    interruptions: int
    agents: int
    compactions: int
    parse_errors: int
    unknown_line_types: int
    flags: tuple[str, ...]

    @property
    def active_seconds(self) -> float | None:
        return seconds_of(self.active_ms)

    def to_json(self) -> dict[str, object]:
        """The Session object minus ``harness``."""
        return {
            "session_id": self.session_id,
            "project": self.project,
            "entrypoint": self.entrypoint,
            "version": self.version,
            "started_at": format_ms(self.started_ms),
            "ended_at": format_ms(self.ended_ms),
            "active_seconds": self.active_seconds,
            "models": list(self.models),
            "tokens": {**self.tokens.to_json(), "subagents": self.subagent_tokens},
            "cost": self.cost.to_json(),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "denials": self.denials,
            "interruptions": self.interruptions,
            "agents": self.agents,
            "compactions": self.compactions,
            "parse_errors": self.parse_errors,
            "unknown_line_types": self.unknown_line_types,
            "flags": list(self.flags),
        }


def _marks(n: int) -> str:
    return ",".join("?" * n)


def session_ids(conn: sqlite3.Connection, *, after: str | None = None) -> Iterator[str]:
    """Every stored session id in ascending order, read in keyset pages (bounded memory)."""
    last = after
    while True:
        rows = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id > ? ORDER BY session_id LIMIT ?",
            ("" if last is None else last, BATCH * 5),
        ).fetchall()
        if not rows:
            return
        for (sid,) in rows:
            yield sid
        last = rows[-1][0]


def iter_session_values(
    conn: sqlite3.Connection, settings: Settings, ids: Iterable[str] | None = None
) -> Iterator[SessionValues]:
    """The values of the given sessions (every stored session when ``ids`` is None), in the order
    of ``ids`` (ascending id otherwise); an unknown id is skipped. Reads at most ``BATCH``
    sessions' aggregates at a time, in SQL, so memory never grows with the number of events."""
    source = session_ids(conn) if ids is None else iter(ids)
    batch: list[str] = []
    for sid in source:
        batch.append(sid)
        if len(batch) >= BATCH:
            yield from _derive_batch(conn, settings, batch)
            batch = []
    if batch:
        yield from _derive_batch(conn, settings, batch)


def session_values(
    conn: sqlite3.Connection, settings: Settings, session_id: str
) -> SessionValues | None:
    """One session's values, or None when it is not stored."""
    return next(iter_session_values(conn, settings, [session_id]), None)


def _derive_batch(
    conn: sqlite3.Connection, settings: Settings, batch: list[str]
) -> Iterator[SessionValues]:
    ids = list(dict.fromkeys(batch))
    marks = _marks(len(ids))
    rules = current_rules_version(conn)

    identity = {
        row[0]: row
        for row in conn.execute(
            "SELECT s.session_id, s.project_dir, s.main_file_id, f.first_cwd, f.first_entrypoint, "
            "f.last_version, f.cost_state_usd, f.cost_invalid, f.parse_errors, f.unknown_lines, "
            "f.min_ts, f.max_ts, f.rules_version, "
            "(SELECT COUNT(*) FROM agents a WHERE a.session_id = s.session_id) "
            "FROM sessions s LEFT JOIN files f ON f.id = s.main_file_id "
            f"WHERE s.session_id IN ({marks})",
            ids,
        )
    }
    agent_files = {
        row[0]: row
        for row in conn.execute(
            "SELECT a.session_id, SUM(f.parse_errors), SUM(f.unknown_lines), MIN(f.min_ts), "
            "MAX(f.max_ts), MIN(f.rules_version) FROM agents a JOIN files f ON f.id = a.file_id "
            f"WHERE a.session_id IN ({marks}) GROUP BY a.session_id",
            ids,
        )
    }
    counts = {row[0]: row for row in conn.execute(COUNTS_SQL.format(marks=marks), ids)}
    # One pass over (session, model) rows: cost is added per row and only the top MAX_MODELS
    # names are kept, so memory is bounded however many distinct model names a session has.
    accumulators: dict[str, CostAccumulator] = {}
    shown: dict[str, list[tuple[int, str]]] = {}  # (-tokens, name) candidates
    subagent_total: dict[str, int] = {}
    main_total: dict[str, TokenSums] = {}
    for sid, name, *sums in conn.execute(TOKENS_SQL.format(marks=marks), ids):
        everyone, main = TokenSums(*sums[:5]), TokenSums(*sums[5:])
        accumulators.setdefault(sid, CostAccumulator(settings.prices)).add(name, everyone)
        main_total[sid] = main_total.get(sid, TokenSums()) + main
        subagent_total[sid] = subagent_total.get(sid, 0) + everyone.total - main.total
        top = shown.setdefault(sid, [])
        top.append((-everyone.total, name))
        if len(top) >= 2 * MAX_MODELS:
            top.sort()
            del top[MAX_MODELS:]
    copied = {row[0] for row in conn.execute(COPIED_SQL.format(marks=marks), ids)}

    for sid in ids:
        row = identity.get(sid)
        if row is None:
            continue
        (
            _,
            project_dir,
            main_file_id,
            first_cwd,
            first_entrypoint,
            last_version,
            cost_state,
            cost_invalid,
            parse_errors,
            unknown_lines,
            min_ts,
            max_ts,
            main_rules,
            agent_count,
        ) = row
        agent = agent_files.get(sid)
        c = counts.get(sid)

        # started_at, ended_at: min and max of counted events' ts, else of the files' range
        file_mins = [v for v in (min_ts, agent[3] if agent else None) if v is not None]
        file_maxs = [v for v in (max_ts, agent[4] if agent else None) if v is not None]
        if c is not None and c[8] is not None:
            started, ended = c[8], c[9]
        else:
            started = min(file_mins) if file_mins else None
            ended = max(file_maxs) if file_maxs else None

        accumulator = accumulators.get(sid) or CostAccumulator(settings.prices)
        ordered = [name for _, name in sorted(shown.get(sid, []))[:MAX_MODELS]]

        reported = None
        if main_file_id is not None and sid not in copied:
            reported = reported_from_file(cost_state)
        cost = Cost(reported, accumulator.estimated, accumulator.unknown_tokens)

        project = first_cwd or None
        from_dir = project is None and bool(project_dir)
        if project is None:
            project = project_dir or None

        has_message = c is not None and c[11] > 0
        flags = {
            "no_usage": not has_message,
            "no_timestamps": c is not None and c[10] > 0,
            "cost_mismatch": is_mismatch(reported, cost.estimated, settings.cost.mismatch_margin),
            "old_rules": any(
                v is not None and v < rules for v in (main_rules, agent[5] if agent else None)
            ),
            "project_from_dir": from_dir,
            "cost_invalid": bool(cost_invalid),
        }
        yield SessionValues(
            session_id=sid,
            project=project,
            entrypoint=map_entrypoint(first_entrypoint),
            version=last_version or None,
            started_ms=started,
            ended_ms=ended,
            active_ms=int(c[7] or 0) if c is not None and c[6] else None,
            models=tuple(ordered),
            tokens=main_total.get(sid, TokenSums()),
            subagent_tokens=subagent_total.get(sid, 0),
            cost=cost,
            tool_calls=int(c[1]) if c else 0,
            tool_errors=int(c[2]) if c else 0,
            denials=int(c[3]) if c else 0,
            interruptions=int(c[4]) if c else 0,
            agents=int(agent_count),
            compactions=int(c[5]) if c else 0,
            parse_errors=int(parse_errors or 0) + int(agent[1] or 0 if agent else 0),
            unknown_line_types=int(unknown_lines or 0) + int(agent[2] or 0 if agent else 0),
            flags=tuple(f for f in FLAGS if flags[f]),
        )


# --- agent values -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentValues:
    """One agent (``defs.json`` ``agent``): its own counted events only."""

    agent_id: str
    parent_tool_call_id: str | None
    agent_type: str | None
    models: tuple[str, ...]
    tokens: TokenSums
    tool_calls: int
    tool_errors: int
    denials: int
    interruptions: int

    def to_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "parent_tool_call_id": self.parent_tool_call_id,
            "agent_type": self.agent_type,
            "models": list(self.models),
            "tokens": self.tokens.to_json(),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "denials": self.denials,
            "interruptions": self.interruptions,
        }


def agent_values(
    conn: sqlite3.Connection, session_id: str, *, limit: int = MAX_AGENTS
) -> tuple[list[AgentValues], bool]:
    """The agents of a session by tokens descending, then agent id, at most ``limit``, and
    whether more existed. An agent with no counted events has zero counters. ``limit`` is
    clamped to 0..``MAX_AGENTS`` (the documented cap)."""
    limit = max(0, min(limit, MAX_AGENTS))
    total = conn.execute(
        "SELECT COUNT(*) FROM agents WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    head = conn.execute(
        "SELECT a.agent_id, a.parent_tool_call_id, a.agent_type FROM agents a "
        "LEFT JOIN (SELECT agent_id, SUM(input_tokens + output_tokens + cache_read_tokens "
        "+ cache_write_5m_tokens + cache_write_1h_tokens) AS tok FROM events "
        "WHERE counted = 1 AND kind = 'message' AND session_id = ? AND agent_id IS NOT NULL "
        "GROUP BY agent_id) t ON t.agent_id = a.agent_id WHERE a.session_id = ? "
        "ORDER BY COALESCE(t.tok, 0) DESC, a.agent_id LIMIT ?",
        (session_id, session_id, limit),
    ).fetchall()
    if not head:
        return [], False
    ids = [r[0] for r in head]
    marks = _marks(len(ids))
    counters: dict[str, tuple[int, int, int, int]] = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in conn.execute(
            "SELECT agent_id, SUM(kind = 'tool_call'), SUM(kind = 'tool_error'), "
            "SUM(kind = 'denial'), SUM(kind = 'interruption') FROM events "
            f"WHERE counted = 1 AND session_id = ? AND agent_id IN ({marks}) GROUP BY agent_id",
            (session_id, *ids),
        )
    }
    models: dict[str, dict[str, TokenSums]] = {}
    for row in conn.execute(
        "SELECT agent_id, model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
        "SUM(cache_write_5m_tokens), SUM(cache_write_1h_tokens) FROM events "
        f"WHERE counted = 1 AND kind = 'message' AND session_id = ? AND agent_id IN ({marks}) "
        "GROUP BY agent_id, model",
        (session_id, *ids),
    ):
        name = UNKNOWN_MODEL if row[1] is None else row[1]
        per = models.setdefault(row[0], {})
        per[name] = per.get(name, TokenSums()) + TokenSums(*row[2:])
    out: list[AgentValues] = []
    for agent_id, parent, agent_type in head:
        per = models.get(agent_id, {})
        calls, errors, denials, interruptions = counters.get(agent_id, (0, 0, 0, 0))
        tokens = TokenSums()
        for sums in per.values():
            tokens += sums
        out.append(
            AgentValues(
                agent_id=agent_id,
                parent_tool_call_id=parent,
                agent_type=agent_type,
                models=tuple(sorted(per, key=lambda m: (-per[m].total, m))[:MAX_MODELS]),
                tokens=tokens,
                tool_calls=int(calls),
                tool_errors=int(errors),
                denials=int(denials),
                interruptions=int(interruptions),
            )
        )
    return out, total > len(head)


__all__ = [
    "COPIED_SQL",
    "COUNTED_EVENTS_SQL",
    "COUNTS_SQL",
    "FLAGS",
    "MAX_MODELS",
    "RESOLVED_MODEL_SQL",
    "RESOLVED_TOOL_SQL",
    "SECONDS_MS_SQL",
    "SLOT_SECONDS",
    "TOKENS_SQL",
    "UNKNOWN_MODEL",
    "AgentValues",
    "Bucketer",
    "Cost",
    "CostAccumulator",
    "SessionValues",
    "TokenSums",
    "agent_values",
    "bucket_key",
    "format_ms",
    "is_mismatch",
    "iter_session_values",
    "map_entrypoint",
    "reported_from_file",
    "reported_sum",
    "seconds_of",
    "session_ids",
    "session_values",
    "slot_start",
    "tokens_cost",
    "usd",
    "usd_json",
]
