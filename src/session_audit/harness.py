"""Harness enrichment: claude-session snapshots (interface, Harness enrichment).

``ingest`` lists claude-session sessions over a fixed window (``list --json --since <n>d``),
fetches ``status --json`` for every one whose ``updated_at`` differs from the stored snapshot
(newest first, at most ``CAP_PER_RUN`` a run) and stores one ``harness_runs`` row per claude-session
id. Sessions join to snapshots only through the Claude Code session ids claude-session recorded
(``sessionquery.snapshot_index``); this module never guesses from paths or names.

Everything claude-session prints is untrusted. Documents are parsed strictly (no duplicate keys, no
``NaN``, nesting and sizes bounded by the gateway's output caps and the parser), every field is
checked against its grammar, and a document that fails is counted in ``harness.errors`` and never
stored. Nothing here can end an ingest except a store that cannot be written (``CliError``).

``upsert_harness_run`` is one short write transaction and never needs the ingest lease, so
``ingest --session`` (a ``post-state-change`` hook) can write while a cron ingest runs.
"""

from __future__ import annotations

import enum
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import errors
from .csversion import Presence, VersionCheck
from .gateway import Gateway
from .grammar import GrammarError, format_timestamp, is_csession_id, is_session_id, parse_label
from .store import Store
from .textsafe import fix_surrogates

CAP_PER_RUN = 500
LIST_TIMEOUT = 60.0
LIST_OUTPUT_LIMIT = 16 * 1024 * 1024
STATUS_TIMEOUT = 60.0
STATUS_OUTPUT_LIMIT = 1024 * 1024
MAX_CLAUDE_IDS = 64
MAX_LABELS = 64
# Column bounds (migration 800): names 128, repo 4096; the interface caps strings at 256, so the
# smaller of the two applies. Labels are a JSON text of at most 16384 characters.
NAME_CAP = 128
REPO_CAP = 256
LABELS_COLUMN_MAX = 16_384
INT64_MAX = (1 << 63) - 1

_KIND = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,127}", re.ASCII)
_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)


class Invalid(ValueError):
    """A claude-session document that does not satisfy the grammar (its text is never shown)."""


# --- strict parsing ---------------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Invalid("duplicate key")
        result[key] = value
    return result


def _no_constants(_name: str) -> Any:
    raise Invalid("NaN or Infinity")


def strict_loads(text: str) -> Any:
    """``json.loads`` that refuses duplicate keys, ``NaN`` and ``Infinity`` and turns every parse
    failure (including a document nested too deeply) into ``Invalid``."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except Invalid:
        raise
    except (ValueError, RecursionError, MemoryError):
        raise Invalid("not a JSON document") from None


def parse_instant(text: object) -> datetime | None:
    """An ISO 8601 timestamp with ``Z`` or an offset as an aware UTC datetime (the fraction
    truncated to microseconds), or None when it is not exactly that shape."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    found = _INSTANT.fullmatch(text)
    if found is None:
        return None
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    micro = int((found.group(7) or "").ljust(6, "0")[:6])
    zone = found.group(8)
    try:
        moment = datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)
        if zone != "Z":
            hours, minutes = int(zone[1:3]), int(zone[4:6])
            if hours > 23 or minutes > 59:
                return None
            delta = timedelta(hours=hours, minutes=minutes)
            moment = moment - delta if zone[0] == "+" else moment + delta
    except (ValueError, OverflowError):
        return None
    return moment


# --- documents --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListEntry:
    csession_id: str
    updated_at: datetime


@dataclass(frozen=True)
class HarnessRun:
    """What ``upsert_harness_run`` stores: a validated, capped snapshot."""

    csession_id: str
    updated_at: datetime
    claude_session_ids: tuple[str, ...]
    kind: str | None
    labels: tuple[str, ...]
    run: int | None
    outcome: str | None
    end_reason: str | None
    cost_usd: float | None
    turns: int | None
    skill: str | None
    repo: str | None
    final: bool

    @property
    def updated_text(self) -> str:
        return format_timestamp(self.updated_at)


def parse_list(text: str) -> tuple[list[ListEntry], int]:
    """The valid entries of a ``list --json`` document (one per claude-session id, the newest
    ``updated_at``) and how many entries were malformed. A document that is not an object with a
    ``sessions`` array is ``Invalid``."""
    doc = strict_loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("sessions"), list):
        raise Invalid("not a list document")
    found: dict[str, ListEntry] = {}
    bad = 0
    for item in doc["sessions"]:
        cid = item.get("csession_id") if isinstance(item, dict) else None
        moment = parse_instant(item.get("updated_at")) if isinstance(item, dict) else None
        if not is_csession_id(cid) or moment is None:
            bad += 1
            continue
        assert isinstance(cid, str)
        old = found.get(cid)
        if old is None or moment > old.updated_at:
            found[cid] = ListEntry(cid, moment)
    return list(found.values()), bad


def _optional_text(value: object, cap: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("not text")
    text = fix_surrogates(value)[:cap]
    return text or None


def _optional_token(value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Invalid("not a token")
    return value


def _optional_int(value: object, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= INT64_MAX:
        raise Invalid("not an integer in range")
    return value


def _optional_cost(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid("not a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise Invalid("not a finite non-negative number")
    return number


def _claude_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise Invalid("claude_session_ids is not a list")
    found: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise Invalid("claude_session_ids entry is not an object")
        cid = item.get("claude_session_id")
        if is_session_id(cid):
            assert isinstance(cid, str)
            found.add(cid)
    return tuple(sorted(found)[:MAX_CLAUDE_IDS])


def _labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise Invalid("labels is not a list")
    texts: list[str] = []
    for item in value:
        try:
            key, val = parse_label(item, field="labels")
        except GrammarError:
            raise Invalid("a label is out of grammar") from None
        texts.append(f"{key}={val}")
    kept = sorted(texts)[:MAX_LABELS]
    # The stored JSON text must fit its column; labels are ASCII, so the length is exact.
    while kept and len(dump_list(kept)) > LABELS_COLUMN_MAX:
        kept.pop()
    return tuple(kept)


def dump_list(items: list[str] | tuple[str, ...]) -> str:
    return json.dumps(list(items), separators=(",", ":"))


def parse_status(text: str, expected: str) -> HarnessRun:
    """A ``status --json`` document as a ``HarnessRun``; ``Invalid`` when it is not an object, is
    about another session, or any field breaks its grammar."""
    doc = strict_loads(text)
    if not isinstance(doc, dict) or doc.get("csession_id") != expected:
        raise Invalid("not the requested session")
    moment = parse_instant(doc.get("updated_at"))
    if moment is None:
        raise Invalid("updated_at is not a timestamp")
    run = doc.get("run")
    if run is None:
        run = {}
    if not isinstance(run, dict):
        raise Invalid("run is not an object")
    outcome = _optional_token(run.get("outcome"), _TOKEN)
    return HarnessRun(
        csession_id=expected,
        updated_at=moment,
        claude_session_ids=_claude_ids(doc.get("claude_session_ids")),
        kind=_optional_token(doc.get("launch_kind"), _KIND),
        labels=_labels(doc.get("labels")),
        run=_optional_int(run.get("run"), 0),
        outcome=outcome,
        end_reason=_optional_token(run.get("end_reason"), _TOKEN),
        cost_usd=_optional_cost(run.get("cost_usd")),
        turns=_optional_int(run.get("num_turns"), 0),
        skill=_optional_text(run.get("skill"), NAME_CAP),
        repo=_optional_text(doc.get("repo"), REPO_CAP),
        final=outcome is not None,
    )


# --- the store --------------------------------------------------------------------------------


def upsert_harness_run(conn: sqlite3.Connection, row: HarnessRun) -> bool:
    """Write ``row`` inside the caller's write transaction; the stored row is replaced only when
    ``row.updated_at`` is newer as an instant (a stored value that is not a timestamp ranks
    lowest). True when a row was written."""
    current = conn.execute(
        "SELECT updated_at FROM harness_runs WHERE csession_id = ?", (row.csession_id,)
    ).fetchone()
    if current is not None:
        stored = parse_instant(current[0])
        if stored is not None and row.updated_at <= stored:
            return False
    conn.execute(
        "INSERT INTO harness_runs (csession_id, updated_at, claude_session_ids, kind, labels, "
        "run, outcome, end_reason, cost_usd, turns, skill, repo, final) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (csession_id) DO UPDATE SET updated_at = excluded.updated_at, "
        "claude_session_ids = excluded.claude_session_ids, kind = excluded.kind, "
        "labels = excluded.labels, run = excluded.run, outcome = excluded.outcome, "
        "end_reason = excluded.end_reason, cost_usd = excluded.cost_usd, turns = excluded.turns, "
        "skill = excluded.skill, repo = excluded.repo, final = excluded.final",
        (
            row.csession_id,
            row.updated_text,
            dump_list(row.claude_session_ids),
            row.kind,
            dump_list(row.labels),
            row.run,
            row.outcome,
            row.end_reason,
            row.cost_usd,
            row.turns,
            row.skill,
            row.repo,
            1 if row.final else 0,
        ),
    )
    return True


def store_row(store: Store, row: HarnessRun) -> bool:
    """One short transaction. A store that cannot be written is the ``store_unwritable`` error
    (retryable); a row the table's own checks refuse is ``Invalid`` (never stored, counted)."""
    try:
        with store.transaction() as conn:
            return upsert_harness_run(conn, row)
    except sqlite3.IntegrityError:
        raise Invalid("the store refused the row") from None


# --- claude-session calls ---------------------------------------------------------------------


class Fetch(enum.Enum):
    ROW = "row"
    FORGOTTEN = "forgotten"
    ERROR = "error"


@dataclass(frozen=True)
class Fetched:
    kind: Fetch
    row: HarnessRun | None = None


def fetch_status(gateway: Gateway, command: str, csession_id: str) -> Fetched:
    """``status <id> --json``: exit 10 is a session forgotten since the list; a failed call, any
    other exit or a document that does not parse is an error. ``csession_id`` must already match
    the claude-session id grammar (it goes into argv)."""
    if not is_csession_id(csession_id):
        raise ValueError("csession_id must be a claude-session id")
    result = gateway.run(
        [command, "status", csession_id, "--json"],
        timeout=STATUS_TIMEOUT,
        stdout_limit=STATUS_OUTPUT_LIMIT,
    )
    if result.exit_is(10):
        return Fetched(Fetch.FORGOTTEN)
    if not result.ok:
        return Fetched(Fetch.ERROR)
    try:
        return Fetched(Fetch.ROW, parse_status(result.stdout, csession_id))
    except Invalid:
        return Fetched(Fetch.ERROR)


@dataclass
class Counts:
    matched: int = 0
    captured: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "matched": self.matched,
            "captured": self.captured,
            "errors": self.errors,
        }


def window_days(retention: timedelta) -> int:
    """``retention.sessions`` in whole days, at least one (a partial day rounds up)."""
    return max(1, math.ceil(retention.total_seconds() / 86400))


def _stored_updated(store: Store) -> dict[str, datetime | None]:
    return {
        row[0]: parse_instant(row[1])
        for row in store.conn.execute("SELECT csession_id, updated_at FROM harness_runs")
    }


def capture(
    gateway: Gateway,
    store: Store,
    *,
    command: str,
    days: int,
    deadline: float,
) -> Counts:
    """The capture of one ingest (after its file reads). Never raises for anything claude-session
    does; a store that cannot be written raises its ``CliError``."""
    counts = Counts()
    if gateway.monotonic() >= deadline:
        counts.warnings.append("harness_capture_skipped: the budget was used by the file reads")
        return counts
    listed = gateway.run(
        [command, "list", "--json", "--since", f"{days}d"],
        timeout=LIST_TIMEOUT,
        stdout_limit=LIST_OUTPUT_LIMIT,
    )
    if not listed.ok:
        counts.errors += 1
        return counts
    try:
        entries, bad = parse_list(listed.stdout)
    except Invalid:
        counts.errors += 1
        return counts
    counts.errors += bad
    stored = _stored_updated(store)
    todo = [e for e in entries if e.csession_id not in stored or stored[e.csession_id] != e.updated_at]
    counts.matched = len(todo)
    todo.sort(key=lambda e: (e.updated_at, e.csession_id), reverse=True)
    for entry in todo[:CAP_PER_RUN]:
        if gateway.monotonic() >= deadline:
            counts.warnings.append("harness_capture_partial: the budget ended the capture")
            break
        fetched = fetch_status(gateway, command, entry.csession_id)
        if fetched.kind is Fetch.FORGOTTEN:
            continue
        if fetched.row is None:
            counts.errors += 1
            continue
        try:
            if store_row(store, fetched.row):
                counts.captured += 1
        except Invalid:
            counts.errors += 1
    return counts


# --- availability -----------------------------------------------------------------------------

INSTALL_HINT = "Install it with: uv tool install --link-mode=copy claude-session"


def unavailable(check: VersionCheck) -> errors.CliError:
    """The exit 12 error of a claude-session that is required and not usable. ``missing`` and
    ``too_old`` are the reasons of the interface; a program that ran but is no claude-session
    (an unparsable version) counts as too old, and a failed call as ``claude_session_error``."""
    if check.presence is Presence.OK:
        raise ValueError("claude-session is usable; there is nothing to map")
    if check.presence is Presence.FAILED:
        assert check.result is not None
        return errors.claude_session_failed(check.result)
    if check.presence is Presence.MISSING:
        return errors.CliError(
            "failed",
            "claude-session is not installed",
            details={"reason": "claude_session_missing", "retryable": False},
            hint=INSTALL_HINT,
        )
    return errors.CliError(
        "failed",
        "claude-session is older than 0.45.2",
        details={"reason": "claude_session_too_old", "retryable": False},
        hint=INSTALL_HINT,
    )


def status_failed() -> errors.CliError:
    """``status`` for one session did not give a usable document (a failed call, an error exit or
    a document that breaks the grammar): exit 12 ``claude_session_error``."""
    return errors.CliError(
        "failed",
        "a claude-session call failed",
        details={"reason": "claude_session_error", "retryable": False},
    )
