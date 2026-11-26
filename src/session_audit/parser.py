"""Parsing transcript lines into events (issue #12), by the transcript field map of
``docs/interface.md`` (Ingest rules 4 and 5, Counting rules 1 and 6).

``parse_line(data, context)`` is a pure function: no I/O, no clock, no store. It takes one line as
the incremental reader yields it (bytes, LF included; it may hold NUL, invalid UTF-8 or a cut
multibyte character) and returns a ``LineResult``. It never raises for line content: the whole
parse and validation run inside one guard, and any exception (JSON, recursion, value, overflow,
Unicode, memory) makes the line an error line with the fixed result ``ERROR`` (no text of the line
is ever kept in an error or a message).

Two kinds of output, kept in two types so the second can never be stored by accident:

- ``Event``: one counted fact. It holds only numbers, capped names and ids (never text); #13 turns
  it into an ``events`` row (dedupe key, ref, owner) and #14 derives the session values.
- ``DigestItem``: a fact only the digest uses (prompt, assistant text, a tool call's argument
  summary, a rejection). It carries conversation text, lives in memory, is produced only when the
  context asks for it (``ParseContext.digest``) and has no path to the store: the events table
  has no text column, ``Event`` has no text field, and a test pins both.

``FileFacts`` is the per-file accumulator: line counts and the facts of the ``files`` row. It also
gives each event its slot (Counting rules 3), which needs the file order.

Bounds. A line is at most 8 MiB (the reader). Nesting is limited by the JSON scanner (a
``RecursionError`` is an error line); an integer literal of more than 64 digits is read as a huge
value (never converted, so the integer digit limit cannot raise) and fails every numeric rule; a
line with more than ``MAX_BLOCKS`` content blocks is an error line, so one line yields a bounded
number of events and items. Every string kept is cut after ``fix_surrogates``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from urllib.parse import urlsplit

from .textsafe import fix_surrogates

INT_MAX = 2**32  # every stored numeric field: 0 <= n <= 2**32
COST_MAX = 1_000_000.0
NAME_CAP = 128  # model, tool, agent id, session id, entrypoint, version
PATH_CAP = 4096  # cwd
ID_CAP = 4096  # a dedupe id longer than this is treated as missing (it is hashed, never stored)
TEXT_CAP = 65_536  # one digest text (the digest cuts it further)
ARG_CAP = 4096  # one tool argument summary
MAX_BLOCKS = 1000  # content blocks per line; more is an error line
MAX_DIGITS = 64  # an integer literal longer than this is "huge"
SLOT_SECONDS = 900
SYNTHETIC = "<synthetic>"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)
MIN_TS_MS = (datetime(2020, 1, 1, tzinfo=UTC) - _EPOCH) // _MS
_DAY_MS = 86_400_000

KNOWN_TYPES = frozenset({"assistant", "user", "system", "cost-state"})
_HUGE = float("inf")  # an integer literal too long to be a stored number


class Status(Enum):
    OK = "ok"
    UNKNOWN = "unknown"  # a line type the map does not know (lines.unknown)
    ERROR = "error"  # not an object, NUL, a bad value, any exception (lines.errors)


class Kind(Enum):
    """The event kinds of the ``events`` table."""

    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_ERROR = "tool_error"
    DENIAL = "denial"
    INTERRUPTION = "interruption"
    TURN = "turn"
    COMPACTION = "compaction"


class DigestKind(Enum):
    PROMPT = "prompt"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    REJECTION = "rejection"


@dataclass(frozen=True, slots=True)
class ParseContext:
    """What a line is parsed against: the file's mtime (the upper bound of a usable timestamp is
    mtime + 1 day; the caller passes ``Opened.stat.st_mtime_ns``), whether it is a main file (only
    a main file's cost-state lines count) and whether digest items are wanted."""

    mtime_ns: int
    main: bool = True
    digest: bool = False


@dataclass(frozen=True, slots=True)
class Event:
    """One counted fact, with the fields #13 needs for the dedupe key and the owner rule.

    ``dedupe_id`` is the id of Counting rules 1 (message.id, tool_use.id, tool_result.tool_use_id,
    or the line's uuid for denial, interruption, turn and compaction); None when the line has no
    such id, and then #13 uses ``f"{file_id}:{line_offset}"`` (with ``:{block}`` for the kinds
    that can repeat on one line). ``ref_id`` is the tool-use id a tool_error or denial refers to.
    ``line_session_id`` is the line's ``sessionId`` (None: native). No field holds text."""

    kind: Kind
    dedupe_id: str | None
    ref_id: str | None
    block: int  # index of the content block the event came from (0 for line-level events)
    line_session_id: str | None
    agent_id: str | None
    ts: int | None  # epoch milliseconds
    slot: int | None  # filled by FileFacts.add
    model: str | None
    tool: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    seconds: float | None = None  # turn events


@dataclass(frozen=True, slots=True)
class DigestItem:
    """A digest-only fact. ``text`` is conversation content: never store it, never log it
    (``repr`` hides it). Cut to ``TEXT_CAP`` (``ARG_CAP`` for a tool argument) after
    ``fix_surrogates``."""

    kind: DigestKind
    block: int
    uuid: str | None
    message_id: str | None  # assistant items: the message the text belongs to
    ts: int | None
    tool: str | None  # tool_call items
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LineResult:
    status: Status
    events: tuple[Event, ...] = ()
    digest: tuple[DigestItem, ...] = ()
    ts: int | None = None  # the line's usable timestamp, epoch milliseconds
    cwd: str | None = None
    entrypoint: str | None = None
    version: str | None = None
    cost_usd: float | None = None  # a valid cost-state line of a main file
    cost_invalid: bool = False  # a cost-state line of a main file with an invalid value


ERROR = LineResult(Status.ERROR)


class _Invalid(Exception):
    """A value the rules reject; the line is an error line."""


# --- value helpers -------------------------------------------------------------------------------


def _parse_int(literal: str) -> int | float:
    return int(literal) if len(literal) <= MAX_DIGITS else _HUGE


def _text(v: object, cap: int = NAME_CAP) -> str | None:
    """A capped, surrogate-fixed string, or None when ``v`` is not a non-empty string."""
    if type(v) is not str or not v:
        return None
    return fix_surrogates(v[:cap])


def _ident(v: object) -> str | None:
    """A dedupe id: a non-empty string of at most ``ID_CAP`` characters, else missing."""
    if type(v) is not str or not v or len(v) > ID_CAP:
        return None
    return fix_surrogates(v)


def _count(d: dict[str, object], key: str) -> int:
    """A stored numeric field: absent or null is 0; else an int (not bool) in [0, 2**32]."""
    v = d.get(key)
    if v is None:
        return 0
    if type(v) is not int or not 0 <= v <= INT_MAX:
        raise _Invalid
    return v


def _timestamp(v: object, mtime_ns: int) -> int | None:
    """Epoch milliseconds of an ISO 8601 timestamp with a zone, inside [2020-01-01, mtime + 1 day];
    else None (missing)."""
    if type(v) is not str or not 0 < len(v) <= 64:
        return None
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            return None
        ms = (dt - _EPOCH) // _MS
    except (ValueError, OverflowError):
        return None
    if MIN_TS_MS <= ms <= mtime_ns // 1_000_000 + _DAY_MS:
        return ms
    return None


def _cost(v: object) -> float:
    if type(v) not in (int, float):
        raise _Invalid
    x = float(v)  # type: ignore[arg-type]
    if not math.isfinite(x) or not 0 <= x <= COST_MAX:
        raise _Invalid
    return x + 0.0  # -0.0 becomes 0.0


def _content_text(content: object) -> str | None:
    """The text of a message content: a string, or the text blocks joined by one space."""
    if type(content) is str:
        return fix_surrogates(content[:TEXT_CAP]) if content else None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    size = 0
    for block in content[:MAX_BLOCKS]:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if type(t) is str and t:
                parts.append(t[:TEXT_CAP])
                size += len(parts[-1]) + 1
                if size >= TEXT_CAP:
                    break
    if not parts:
        return None
    return fix_surrogates(" ".join(parts)[:TEXT_CAP])


_ARG_KEYS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "Agent": "subagent_type",
    "Task": "subagent_type",
    "Skill": "skill",
}


def _tool_arg(name: str | None, tool_input: object) -> str | None:
    """The one-line argument summary the digest shows for a tool (Digest, tool table)."""
    key = _ARG_KEYS.get(name) if name is not None else None
    if key is None or not isinstance(tool_input, dict):
        return None
    v = tool_input.get(key)
    if type(v) is not str or not v:
        return None
    if key == "url":
        try:
            host = urlsplit(v[:ARG_CAP]).hostname
        except ValueError:
            return None
        return fix_surrogates(host[:ARG_CAP]) if host else None
    return fix_surrogates(v[:ARG_CAP])


def _blocks(message: dict[str, object]) -> list[object]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    if len(content) > MAX_BLOCKS:
        raise _Invalid
    return content


# --- the line ------------------------------------------------------------------------------------


def parse_line(data: bytes, context: ParseContext) -> LineResult:
    """Parse one transcript line. Never raises for the content of ``data``."""
    try:
        return _parse(data, context)
    except Exception:  # the guard of Ingest rules 5: any exception is an error line
        return ERROR


def _parse(data: bytes, context: ParseContext) -> LineResult:
    if b"\x00" in data:
        return ERROR
    # Explicit UTF-8: json.loads on bytes would sniff UTF-16 and UTF-32.
    obj = json.loads(data.decode("utf-8", errors="replace"), parse_int=_parse_int)
    if not isinstance(obj, dict):
        return ERROR
    ts = _timestamp(obj.get("timestamp"), context.mtime_ns)
    common = {
        "ts": ts,
        "cwd": _text(obj.get("cwd"), PATH_CAP),
        "entrypoint": _text(obj.get("entrypoint")),
        "version": _text(obj.get("version")),
    }
    kind = obj.get("type")
    if type(kind) is not str or kind not in KNOWN_TYPES:
        return LineResult(Status.UNKNOWN, **common)
    if kind == "cost-state":
        return _cost_state(obj, context, common)
    line = _Line(obj, context, ts)
    if kind == "assistant":
        line.assistant()
    elif kind == "user":
        line.user()
    else:
        line.system()
    return LineResult(Status.OK, tuple(line.events), tuple(line.digest), **common)


def _cost_state(
    obj: dict[str, object], context: ParseContext, common: dict[str, object]
) -> LineResult:
    if not context.main:  # only the main file's cost-state lines count (Counting rules 2)
        return LineResult(Status.OK, **common)  # type: ignore[arg-type]
    value = obj.get("totalCostUSD")
    if value is None:
        return LineResult(Status.OK, **common)  # type: ignore[arg-type]
    try:
        cost = _cost(value)
    except _Invalid:
        return LineResult(Status.ERROR, cost_invalid=True)
    return LineResult(Status.OK, cost_usd=cost, **common)  # type: ignore[arg-type]


class _Line:
    """The facts of one object line, collected in field-map order."""

    def __init__(self, obj: dict[str, object], context: ParseContext, ts: int | None) -> None:
        self.obj = obj
        self.want_digest = context.digest
        self.ts = ts
        self.uuid = _ident(obj.get("uuid"))
        msg = obj.get("message")
        self.message: dict[str, object] = msg if isinstance(msg, dict) else {}
        self.session = _text(obj.get("sessionId"))
        self.agent = _text(obj.get("agentId"))
        self.events: list[Event] = []
        self.digest: list[DigestItem] = []

    def event(self, kind: Kind, **fields: object) -> None:
        base: dict[str, object] = {
            "kind": kind,
            "dedupe_id": self.uuid,
            "ref_id": None,
            "block": 0,
            "line_session_id": self.session,
            "agent_id": self.agent,
            "ts": self.ts,
            "slot": None,
            "model": None,
            "tool": None,
        }
        base.update(fields)
        self.events.append(Event(**base))  # type: ignore[arg-type]

    def item(self, kind: DigestKind, text: str | None, **fields: object) -> None:
        if not self.want_digest or text is None:
            return
        base: dict[str, object] = {
            "block": 0,
            "uuid": self.uuid,
            "message_id": None,
            "ts": self.ts,
            "tool": None,
        }
        base.update(fields)
        self.digest.append(DigestItem(kind, text=text, **base))  # type: ignore[arg-type]

    # assistant: message tokens, tool calls, assistant text
    def assistant(self) -> None:
        msg = self.message
        model = _text(msg.get("model"))
        message_id = _ident(msg.get("id"))
        blocks = _blocks(msg)
        usage = msg.get("usage")
        if isinstance(usage, dict):
            counters = self._usage(usage)
            if model == SYNTHETIC and not any(counters.values()):
                return  # a synthetic message with zero usage is ignored
            self.event(Kind.MESSAGE, dedupe_id=message_id, model=model or "unknown", **counters)
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = _text(block.get("name"))
                self.event(
                    Kind.TOOL_CALL,
                    dedupe_id=_ident(block.get("id")),
                    block=index,
                    model=model,
                    tool=name,
                )
                self.item(
                    DigestKind.TOOL_CALL,
                    _tool_arg(name, block.get("input")),
                    block=index,
                    tool=name,
                    message_id=message_id,
                )
            elif block.get("type") == "text" and self.want_digest:
                text = _content_text([block])
                self.item(DigestKind.ASSISTANT, text, block=index, message_id=message_id)
        if type(msg.get("content")) is str:
            self.item(DigestKind.ASSISTANT, _content_text(msg["content"]), message_id=message_id)

    @staticmethod
    def _usage(usage: dict[str, object]) -> dict[str, int]:
        split = usage.get("cache_creation")
        e5, e1 = "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"
        if isinstance(split, dict) and (split.get(e5) is not None or split.get(e1) is not None):
            write_5m, write_1h = _count(split, e5), _count(split, e1)
        else:  # no split: all of cache_creation_input_tokens counts as 5-minute
            write_5m, write_1h = _count(usage, "cache_creation_input_tokens"), 0
        return {
            "input_tokens": _count(usage, "input_tokens"),
            "output_tokens": _count(usage, "output_tokens"),
            "cache_read_tokens": _count(usage, "cache_read_input_tokens"),
            "cache_write_5m_tokens": write_5m,
            "cache_write_1h_tokens": write_1h,
        }

    # user: tool errors, denial, interruption, rejection and prompt
    def user(self) -> None:
        obj = self.obj
        blocks = _blocks(self.message)
        results = [
            (i, b)
            for i, b in enumerate(blocks)
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        for index, block in results:
            if block.get("is_error") is True:
                tool_use_id = _ident(block.get("tool_use_id"))
                self.event(Kind.TOOL_ERROR, dedupe_id=tool_use_id, ref_id=tool_use_id, block=index)
        decision = obj.get("permissionDecision")
        denied = obj.get("toolDenialKind") is not None or (
            isinstance(decision, dict) and decision.get("decision") == "reject"
        )
        if denied:
            first = _ident(results[0][1].get("tool_use_id")) if results else None
            self.event(Kind.DENIAL, ref_id=first)
        if obj.get("interruptedMessageId") is not None:
            self.event(Kind.INTERRUPTION)
        if not self.want_digest:
            return
        if obj.get("toolDenialKind") == "user-rejected":
            source = results[0][1].get("content") if results else self.message.get("content")
            self.item(DigestKind.REJECTION, _content_text(source))
        elif not results and not obj.get("isMeta") and not obj.get("isCompactSummary"):
            self.item(DigestKind.PROMPT, _content_text(self.message.get("content")))

    # system: turn duration and compaction
    def system(self) -> None:
        subtype = self.obj.get("subtype")
        if subtype == "turn_duration":
            ms = _count(self.obj, "durationMs") if self.obj.get("durationMs") is not None else None
            if ms is not None:
                self.event(Kind.TURN, seconds=ms / 1000)
        elif subtype == "compact_boundary":
            self.event(Kind.COMPACTION)


# --- the per-file accumulator ------------------------------------------------------------------


def slot_of(ts_ms: int) -> int:
    return ts_ms // 1000 // SLOT_SECONDS


@dataclass
class FileFacts:
    """The per-file facts of a ``files`` row (Ingest rules 6), fed one result at a time in file
    order. Start from the stored row for an incremental read (counts add, ``first_*`` is set only
    when None, ``last_*`` and ``cost_state_usd`` are replaced when a later line has one, the
    timestamp range widens) and from ``FileFacts()`` after a full reread.

    ``last_ts`` is the usable timestamp of the latest line that had one (file order, so it may step
    backwards); ``min_ts`` and ``max_ts`` are the range."""

    parse_errors: int = 0
    unknown_lines: int = 0
    min_ts: int | None = None
    max_ts: int | None = None
    last_ts: int | None = None
    first_cwd: str | None = None
    first_entrypoint: str | None = None
    last_version: str | None = None
    cost_state_usd: float | None = None
    cost_invalid: bool = False

    def add(self, result: LineResult) -> tuple[Event, ...]:
        """Absorb one line's result; return its events with their slot set (Counting rules 3)."""
        if result.cost_invalid:
            self.cost_invalid = True
        if result.status is Status.ERROR:
            self.parse_errors += 1
            return ()
        if result.status is Status.UNKNOWN:
            self.unknown_lines += 1
        ts = result.ts
        if ts is not None:
            self.min_ts = ts if self.min_ts is None else min(self.min_ts, ts)
            self.max_ts = ts if self.max_ts is None else max(self.max_ts, ts)
            self.last_ts = ts
        if self.first_cwd is None:
            self.first_cwd = result.cwd
        if self.first_entrypoint is None:
            self.first_entrypoint = result.entrypoint
        if result.version is not None:
            self.last_version = result.version
        if result.cost_usd is not None:
            self.cost_state_usd = result.cost_usd
        anchor = self.last_ts
        slot = None if anchor is None else slot_of(anchor)
        return tuple(replace(e, slot=slot) for e in result.events)


__all__ = [
    "ERROR",
    "KNOWN_TYPES",
    "DigestItem",
    "DigestKind",
    "Event",
    "FileFacts",
    "Kind",
    "LineResult",
    "ParseContext",
    "Status",
    "parse_line",
    "slot_of",
]
