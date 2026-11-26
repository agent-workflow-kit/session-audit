"""The stored retro as the Retro object (``schemas/cli/retro-run.json``, issue #23).

``retro run`` prints a reused ``done`` retro with this (``reused: true`` is added by the caller);
``retro show`` (#31) reads the same rows. The layout of the columns the schema has no column for
is one choice of #23; the writers (#26 and #29) store exactly this through the ``encode_*``
functions below, and ``docs/design.md`` (Data model, ``retros``) states it with its size budgets:

* ``retros.scope``: a short descriptive text (``scope_text``), at most 4096 characters, never read
  back as data;
* ``retros.resolved_scope``: JSON object ``{"flags": {labels, project, since, until, kind,
  outcome}, "sessions": [full ids], "since": ts|null, "until": ts|null}``;
* ``retros.session_map``: JSON object ``{"sessions": [{ref, session_id, csession_id}]}``;
* ``retros.counts`` and ``retros.dropped``: the ``counts`` and ``dropped`` objects;
* ``retro_artifacts.artifact``: the envelope ``{"artifact": <the checked artifact>, "skipped":
  [...], "coverage": {...}, "metrics": {...}}`` (no column cap; at most ``MAX_ARTIFACT_BYTES``);
  ``retro_artifacts.summary``: the summary.

The capped columns provably fit their maxima (500 sessions, 64 labels of 400 characters, a
4096-character project; the tests build and insert such a row). The migration defaults of
``scope`` (``''``), ``resolved_scope`` (``'[]'``) and ``session_map`` (``'{}'``) are not valid
retros: a writer always writes every column.

``updated_at`` is the newest of ``created_at``, ``started_at`` and ``finished_at`` (no column).
A row that does not fit this layout and the schema's field rules (wrong types, a bad id or key, a
missing artifact, out-of-schema numbers or names, an oversize artifact) is not a valid retro:
``load_retro`` returns None, so a hostile or damaged row is never reused or printed. No text read
here is trusted: ids, the key, names and the closed sets are checked by pattern and range.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import derive
from .textsafe import fix_surrogates

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "encode_artifact",
    "encode_resolved_scope",
    "encode_session_map",
    "load_retro",
    "scope_text",
]

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024  # a stored artifact envelope over this is never loaded
SCOPE_TEXT_MAX = 4096  # the ``retros.scope`` CHECK

_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}", re.ASCII)
_KEY = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CSESSION = re.compile(r"cs-[a-z2-7]{8}", re.ASCII)
_SESSION_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.ASCII)
_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)", re.ASCII)
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[a-z0-9][a-z0-9-]{0,63}", re.ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}", re.ASCII)
_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}", re.ASCII)
_WHEN = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2}))?"
    r"|[1-9][0-9]{0,8}(s|m|h|d))",
    re.ASCII,
)
_STAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z", re.ASCII
)
_METRIC = re.compile(
    r"(s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)|total)\.(?!i[0-9]{3,6}\Z)[a-z][a-z0-9_]{0,63}",
    re.ASCII,
)
_ITEM_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)\.i[0-9]{3,6}", re.ASCII)
_FINDING_ID = re.compile(r"f([1-9]|[1-4][0-9]|50)", re.ASCII)
_CANDIDATE_ID = re.compile(r"c([1-9]|1[0-9]|20)", re.ASCII)
_SKIP_REASONS = ("live", "transcript_missing", "transcript_changed", "empty")
_WARNINGS = ("transcripts_kept", "recovery_skipped", "ingest_busy")
_CLEANUP = ("pending", "done", "incomplete", "skipped")
_CATEGORIES = ("went_well", "went_badly", "friction", "cost")
_TARGET_CLASSES = ("skill", "instructions_file", "config", "permission_profile", "workflow")
_COUNT_KEYS = ("sessions", "skipped", "over_cap", "items", "findings", "candidates")
_FLAG_KEYS = ("labels", "project", "since", "until", "kind", "outcome")
_COVERAGE_KEYS = {"digest_bytes", "items_total", "items_kept", "sessions"}


class _Bad(Exception):
    """The row does not fit the layout."""


# --- writer side: the layout, in one place -------------------------------------------------------


def _dumps(value: object) -> str:
    """Compact JSON with characters kept (so a column bound counts characters, not escapes of
    non-ASCII text) and lone surrogates replaced (so the text always stores)."""
    return fix_surrogates(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def scope_text(flags: Mapping[str, Any]) -> str:
    """The descriptive ``retros.scope`` text: ``name=value`` words of the flags that were given,
    cut at a character boundary to ``SCOPE_TEXT_MAX`` characters. It is never read back."""
    words = []
    for name in _FLAG_KEYS:
        value = flags.get(name)
        if name == "labels":
            words.extend(f"label={label}" for label in dict.fromkeys(value or []))
        elif value is not None:
            words.append(f"{name}={value}")
    return fix_surrogates(" ".join(words))[:SCOPE_TEXT_MAX]


def encode_resolved_scope(
    flags: Mapping[str, Any], sessions: Sequence[str], since: str | None, until: str | None
) -> str:
    """``retros.resolved_scope``. At the maxima (500 session ids, 64 labels, a 4096-character
    project of characters JSON escapes to six, the other flags at their bounds) it is 65,185
    characters of the 65,536 the column allows. Labels are stored de-duplicated in typed order
    (the reader and the schema require unique labels)."""
    stored = {name: flags[name] for name in _FLAG_KEYS}
    stored["labels"] = list(dict.fromkeys(stored["labels"]))
    return _dumps(
        {
            "flags": stored,
            "sessions": list(sessions),
            "since": since,
            "until": until,
        }
    )


def encode_session_map(sessions: Sequence[Mapping[str, Any]]) -> str:
    """``retros.session_map``: about 47,500 characters at 500 sessions."""
    return _dumps({"sessions": [dict(row) for row in sessions]})


def encode_artifact(
    artifact: Mapping[str, Any],
    skipped: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> str:
    """``retro_artifacts.artifact``: the checked artifact stays intact under ``artifact``; the
    parts the tool writes sit beside it."""
    return _dumps(
        {
            "artifact": dict(artifact),
            "skipped": [dict(row) for row in skipped],
            "coverage": dict(coverage),
            "metrics": dict(metrics),
        }
    )


# --- reader side ---------------------------------------------------------------------------------


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


def _text(value: object, low: int, high: int) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise _Bad
    return value


def _number(value: object) -> int | float:
    """A finite non-negative number (a bool is not one)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Bad
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise _Bad
    return value


def _scope(flags: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    if set(flags) != set(_FLAG_KEYS):
        raise _Bad
    sessions = [_match(_SESSION_ID, s) for s in _list(resolved.get("sessions"), 1000)]
    if len(set(sessions)) != len(sessions):
        raise _Bad
    labels = [_match(_LABEL, s) for s in _list(flags["labels"], 64)]
    if len(set(labels)) != len(labels) or any(len(s) > 400 for s in labels):
        raise _Bad
    project = flags["project"]
    if project is not None:
        _text(project, 1, 4096)
    for name in ("since", "until"):
        if flags[name] is not None:
            _match(_WHEN, flags[name])
            _text(flags[name], 1, 40)
    if flags["kind"] is not None:
        _text(flags["kind"], 1, 64)
    _optional(_TOKEN, flags["outcome"])
    return {"sessions": sessions, **{k: flags[k] for k in _FLAG_KEYS}, "labels": labels}


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


def _coverage(value: object) -> dict[str, Any]:
    found = _object(value)
    if set(found) != _COVERAGE_KEYS:
        raise _Bad
    rows = []
    for row in _list(found["sessions"], 1000):
        entry = _object(row)
        if set(entry) != {"ref", "items_total", "items_kept", "dropped"}:
            raise _Bad
        rows.append(
            {
                "ref": _match(_REF, entry["ref"]),
                "items_total": _count(entry["items_total"]),
                "items_kept": _count(entry["items_kept"]),
                "dropped": _counts(entry["dropped"], ("session_cap", "total_cap")),
            }
        )
    return {
        "digest_bytes": _count(found["digest_bytes"]),
        "items_total": _count(found["items_total"]),
        "items_kept": _count(found["items_kept"]),
        "sessions": rows,
    }


def _metrics(value: object) -> dict[str, int | float]:
    found = _object(value)
    if len(found) > 6012:
        raise _Bad
    return {_match(_METRIC, name): _number(number) for name, number in found.items()}


def _evidence(value: object) -> dict[str, str]:
    found = _object(value)
    if set(found) != {"kind", "ref"}:
        raise _Bad
    kind, ref = found["kind"], found["ref"]
    if kind == "item":
        _match(_ITEM_REF, ref)
    elif kind == "metric":
        _match(_METRIC, ref)
    else:
        raise _Bad
    return {"kind": kind, "ref": ref}


def _finding(value: object) -> dict[str, Any]:
    found = _object(value)
    if set(found) != {"id", "category", "severity", "title", "detail", "evidence"}:
        raise _Bad
    if found["category"] not in _CATEGORIES or found["severity"] not in ("low", "medium", "high"):
        raise _Bad
    evidence = _list(found["evidence"], 8)
    if not evidence:
        raise _Bad
    return {
        "id": _match(_FINDING_ID, found["id"]),
        "category": found["category"],
        "severity": found["severity"],
        "title": _text(found["title"], 1, 120),
        "detail": _text(found["detail"], 1, 1500),
        "evidence": [_evidence(e) for e in evidence],
    }


def _candidate(value: object) -> dict[str, Any]:
    found = _object(value)
    keys = {"id", "target_class", "target_hint", "proposal", "findings", "risk", "sessions"}
    if set(found) != keys or found["target_class"] not in _TARGET_CLASSES:
        raise _Bad
    if found["risk"] not in ("high", "medium"):
        raise _Bad
    findings = [_match(_FINDING_ID, f) for f in _list(found["findings"], 50)]
    refs = [_match(_REF, r) for r in _list(found["sessions"], 500)]
    if not findings or len(set(findings)) != len(findings) or len(set(refs)) != len(refs):
        raise _Bad
    return {
        "id": _match(_CANDIDATE_ID, found["id"]),
        "target_class": found["target_class"],
        "target_hint": _text(found["target_hint"], 1, 200),
        "proposal": _text(found["proposal"], 1, 1500),
        "findings": findings,
        "risk": found["risk"],
        "sessions": refs,
    }


def _document(conn: sqlite3.Connection, retro_id: object) -> tuple[dict[str, Any], str]:
    """The artifact envelope and the summary of a retro, the size checked before the parse."""
    sized = conn.execute(
        "SELECT length(CAST(artifact AS BLOB)), summary FROM retro_artifacts WHERE retro_id = ?",
        (retro_id,),
    ).fetchone()
    if sized is None or not isinstance(sized[0], int) or sized[0] > MAX_ARTIFACT_BYTES:
        raise _Bad
    summary = _text(sized[1], 1, 2000)
    text = conn.execute(
        "SELECT artifact FROM retro_artifacts WHERE retro_id = ?", (retro_id,)
    ).fetchone()[0]
    return _object(_json(text)), summary


def _build(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    if row["state"] != "done" or row["reason"] is not None:
        raise _Bad
    _text(row["scope"], 0, SCOPE_TEXT_MAX)  # descriptive only: its content is never used
    resolved = _object(_json(row["resolved_scope"]))
    flags = _object(resolved.get("flags"))
    mapping = _object(_json(row["session_map"]))
    if set(mapping) != {"sessions"}:
        raise _Bad
    envelope, summary = _document(conn, row["id"])
    if set(envelope) != {"artifact", "skipped", "coverage", "metrics"}:
        raise _Bad
    document = _object(envelope["artifact"])
    findings = [_finding(x) for x in _list(document.get("findings"), 50)]
    candidates = [_candidate(x) for x in _list(document.get("candidates"), 20)]
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
    if model is not None and (
        not isinstance(model, str)
        or not 1 <= len(model) <= 128
        or any(ord(c) < 32 or ord(c) == 127 for c in model)
    ):
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
        "coverage": _coverage(envelope["coverage"]),
        "skipped": _skipped(envelope["skipped"]),
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
        "metrics": _metrics(envelope["metrics"]),
        "dropped": dropped,
        "summary": summary,
        "findings": findings,
        "candidates": candidates,
    }


def _maybe_stamp_text(value: object) -> str | None:
    return None if value is None else _match(_STAMP, value)


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
