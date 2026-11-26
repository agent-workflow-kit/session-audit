"""The stored retro as the Retro object (``schemas/cli/retro-run.json``, issue #23).

``retro run`` prints a reused ``done`` retro with this (``reused: true`` is added by the caller);
``retro show`` (#31) reads the same rows. The layout of the columns the schema has no column for
(a choice of #23; the writers, #26 and #29, store exactly this and #31 reads it):

* ``retros.scope``: JSON object of the scope flags ``{labels, project, since, until, kind,
  outcome}`` as typed (at most 4096 characters, so the session list is not in it);
* ``retros.resolved_scope``: JSON object ``{"sessions": [full ids], "since": ts|null,
  "until": ts|null}``;
* ``retros.session_map``: JSON object ``{"sessions": [{ref, session_id, csession_id}],
  "skipped": [{session_id, reason}], "coverage": <coverage>, "metrics": <metrics>}``;
* ``retros.counts`` and ``retros.dropped``: the ``counts`` and ``dropped`` objects;
* ``retro_artifacts.artifact``: the checked artifact JSON, whose ``findings`` and ``candidates``
  arrays are copied; ``retro_artifacts.summary``: the summary.

``updated_at`` is the newest of ``created_at``, ``started_at`` and ``finished_at`` (no column).
A row that does not fit this shape (wrong types, a bad id or key, a missing artifact) is not a
valid retro: ``load_retro`` returns None, so a hostile or damaged row is never reused or printed.
No text read here is trusted: ids, the key and the closed sets are checked by pattern.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import derive

__all__ = ["load_retro"]

_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}", re.ASCII)
_KEY = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CSESSION = re.compile(r"cs-[a-z2-7]{8}", re.ASCII)
_SESSION_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.ASCII)
_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)", re.ASCII)
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[a-z0-9][a-z0-9-]{0,63}", re.ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}", re.ASCII)
_SKIP_REASONS = ("live", "transcript_missing", "transcript_changed", "empty")
_WARNINGS = ("transcripts_kept", "recovery_skipped", "ingest_busy")
_CLEANUP = ("pending", "done", "incomplete", "skipped")
_COUNT_KEYS = ("sessions", "skipped", "over_cap", "items", "findings", "candidates")
_FLAG_KEYS = ("labels", "project", "since", "until", "kind", "outcome")


class _Bad(Exception):
    """The row does not fit the layout."""


def _stamp(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Bad
    text = derive.format_ms(value)
    if text is None:
        raise _Bad
    return text


def _maybe_stamp(value: object) -> str | None:
    return None if value is None else _stamp(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Bad
    return value


def _json(text: object) -> Any:
    if not isinstance(text, str):
        raise _Bad
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise _Bad from None


def _match(pattern: re.Pattern[str], value: object) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _Bad
    return value


def _optional(pattern: re.Pattern[str], value: object) -> str | None:
    return None if value is None else _match(pattern, value)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Bad
    return value


def _list(value: object, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise _Bad
    return value


def _counts(value: object, keys: tuple[str, ...]) -> dict[str, int]:
    found = _object(value)
    if set(found) != set(keys):
        raise _Bad
    return {k: _count(found[k]) for k in keys}


def _scope(flags: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    if set(flags) != set(_FLAG_KEYS):
        raise _Bad
    sessions = [_match(_SESSION_ID, s) for s in _list(resolved.get("sessions"), 1000)]
    if len(set(sessions)) != len(sessions):
        raise _Bad
    labels = [s for s in _list(flags["labels"], 64) if isinstance(s, str) and len(s) <= 400]
    if len(labels) != len(flags["labels"]):
        raise _Bad
    for name in ("project", "since", "until", "kind", "outcome"):
        value = flags[name]
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 4096):
            raise _Bad
    return {"sessions": sessions, **{k: flags[k] for k in _FLAG_KEYS}}


def _sessions(value: object) -> list[dict[str, Any]]:
    rows = []
    for row in _list(value, 500):
        found = _object(row)
        if set(found) != {"ref", "session_id", "csession_id"}:
            raise _Bad
        rows.append(
            {
                "ref": _match(_REF, found["ref"]),
                "session_id": _match(_SESSION_ID, found["session_id"]),
                "csession_id": _optional(_CSESSION, found["csession_id"]),
            }
        )
    if not rows:
        raise _Bad
    return rows


def _skipped(value: object) -> list[dict[str, str]]:
    rows = []
    for row in _list(value, 1000):
        found = _object(row)
        if set(found) != {"session_id", "reason"} or found["reason"] not in _SKIP_REASONS:
            raise _Bad
        rows.append(
            {"session_id": _match(_SESSION_ID, found["session_id"]), "reason": found["reason"]}
        )
    return rows


def _build(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    if row["state"] != "done" or row["reason"] is not None:
        raise _Bad
    flags = _object(_json(row["scope"]))
    resolved = _object(_json(row["resolved_scope"]))
    mapping = _object(_json(row["session_map"]))
    if not {"sessions", "skipped", "coverage", "metrics"} <= set(mapping):
        raise _Bad
    metrics = _object(mapping["metrics"])
    for name, number in metrics.items():
        if (
            not isinstance(name, str)
            or isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not number >= 0
            or number == float("inf")
        ):
            raise _Bad
    coverage = _object(mapping["coverage"])
    if set(coverage) != {"digest_bytes", "items_total", "items_kept", "sessions"}:
        raise _Bad
    artifact = conn.execute(
        "SELECT artifact, summary FROM retro_artifacts WHERE retro_id = ?", (row["id"],)
    ).fetchone()
    if artifact is None:
        raise _Bad
    summary = artifact[1]
    if not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        raise _Bad
    document = _object(_json(artifact[0]))
    findings = _list(document.get("findings"), 50)
    candidates = _list(document.get("candidates"), 20)
    if not all(isinstance(x, dict) for x in (*findings, *candidates)):
        raise _Bad
    warnings = _list(_json(row["warnings"]), 8)
    if any(w not in _WARNINGS for w in warnings) or len(set(warnings)) != len(warnings):
        raise _Bad
    dropped = _counts(_json(row["dropped"]), ("findings", "candidates"))
    cost = row["cost_usd"]
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost < 1e12
    ):
        raise _Bad
    turns = row["turns"]
    sandbox = row["sandbox"]
    if sandbox is not None and sandbox not in (0, 1):
        raise _Bad
    cleanup = None
    csession = _optional(_CSESSION, row["csession_id"])
    if csession is not None:
        found = conn.execute(
            "SELECT cleanup_state FROM retro_sessions WHERE csession_id = ?", (csession,)
        ).fetchone()
        if found is not None:
            if found[0] not in _CLEANUP:
                raise _Bad
            cleanup = found[0]
    stamps = [_stamp(row["created_at"])]
    started, finished = _maybe_stamp(row["started_at"]), _maybe_stamp(row["finished_at"])
    model = row["model"]
    if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 128):
        raise _Bad
    stamps += [s for s in (started, finished) if s is not None]
    return {
        "id": _match(_RETRO_ID, row["id"]),
        "state": "done",
        "reason": None,
        "key": _match(_KEY, row["key"]),
        "scope": _scope(flags, resolved),
        "scope_resolved": {
            "since": _maybe_stamp_text(resolved.get("since")),
            "until": _maybe_stamp_text(resolved.get("until")),
        },
        "coverage": coverage,
        "skipped": _skipped(mapping["skipped"]),
        "sessions": _sessions(mapping["sessions"]),
        "skill": _match(_SKILL, row["skill"]),
        "model": model,
        "created_at": stamps[0],
        "updated_at": max(stamps),
        "finished_at": finished,
        "csession_id": csession,
        "cost_usd": None if cost is None else float(cost),
        "num_turns": None if turns is None else _count(turns),
        "outcome": _optional(_TOKEN, row["outcome"]),
        "sandbox_active": None if sandbox is None else bool(sandbox),
        "cleanup": cleanup,
        "warnings": warnings,
        "redacted": _count(row["redacted"]),
        "again_of": _optional(_RETRO_ID, row["again_of"]),
        "counts": _counts(_json(row["counts"]), _COUNT_KEYS),
        "metrics": metrics,
        "dropped": dropped,
        "summary": summary,
        "findings": findings,
        "candidates": candidates,
    }


def _maybe_stamp_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z", value, re.ASCII
    ):
        raise _Bad
    return value


def load_retro(conn: sqlite3.Connection, retro_id: str) -> dict[str, Any] | None:
    """The Retro object of a stored ``done`` retro, or None when there is no such retro, it is not
    ``done`` or its row does not fit the layout above."""
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM retros WHERE id = ?", (retro_id,)).fetchone()
        if row is None:
            return None
        try:
            return _build(conn, row)
        except _Bad:
            return None
    finally:
        conn.row_factory = previous
