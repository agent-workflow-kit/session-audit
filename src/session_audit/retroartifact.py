"""The artifact check and the stored artifact (issue #29, Retro validation).

``validate_and_store_artifact(collected, context)`` is called for a run that ended ``done`` with
the lease held, the sandbox rule satisfied and the retro in state ``validating``. It runs checks 1
to 8 in the order of the interface inside ONE guard: any exception is ``artifact_invalid`` (exit
22), never exit 1. It writes nothing: it returns the ``Mapped`` row the run ends with, ``failed``
with a validation reason, or ``done`` carrying a ``StoredArtifact``. ``write_artifact`` stores it
inside the transaction of the final ``done`` write (``Retro.write(also=...)``), so a crash between
``validating`` and the end leaves no stored artifact.

The frozen file is read into memory and never written anywhere by the tool; the only stored copy
is the redacted ``retro_artifacts`` envelope (``retroview.encode_artifact``). Every number in it
(``counts``, ``metrics``, ``dropped``, ``redacted``, candidate ``risk`` and ``sessions``) is written
by the tool; a number the model writes is never used (the closed schema has no number at all, and
the parser refuses one that is large, fractional or not finite).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat as statmod
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import errors, redact
from .digest import METRIC_NAMES, DigestResult, Metrics
from .fencing import FINDING_CATEGORIES, LIMITS, TARGET_CLASSES
from .gateway import Gateway
from .harness import Invalid, strict_loads
from .retrooutcome import VALIDATION_REASONS, Collected, Mapped, StoredArtifact
from .retroview import MAX_ARTIFACT_BYTES, encode_artifact
from .textsafe import fix_surrogates

__all__ = [
    "ARTIFACT_NAME",
    "MAX_ARTIFACT_FILE",
    "MAX_DEPTH",
    "MAX_DIGITS",
    "RISK",
    "VALIDATION_REASONS",
    "ArtifactContext",
    "bundled_marker",
    "validate_and_store_artifact",
    "write_artifact",
]

ARTIFACT_NAME = "audit.json"
MAX_ARTIFACT_FILE = 262_144  # 256 KB
MAX_DEPTH = 16
MAX_DIGITS = 16
MAX_INTEGER = 2**53
# the bundled skill's marker (plugin/skills/audit/references/marker.txt, issue #30)
MARKER_FILE = "skills/audit/references/marker.txt"
_MARKER_LINE = re.compile(r"[a-z0-9-]{16,64}", re.ASCII)
_PATH_MAX = 4096
_USD4 = Decimal("0.0001")

# Tool-written candidate risk by target class (Retro artifact). Checked against the fixed classes.
RISK = {
    "permission_profile": "high",
    "config": "high",
    "instructions_file": "high",
    "skill": "medium",
    "workflow": "medium",
}
if set(RISK) != set(TARGET_CLASSES):  # pragma: no cover - a programming error
    raise RuntimeError("RISK must name exactly the target classes")

_TEXT_LIMITS: Mapping[str, int] = LIMITS["text_limits"]
_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}", re.ASCII)
_RUN_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_FINDING_ID = re.compile(r"f([1-9]|[1-4][0-9]|50)", re.ASCII)
_CANDIDATE_ID = re.compile(r"c([1-9]|1[0-9]|20)", re.ASCII)
_ITEM_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)\.i[0-9]{3,6}", re.ASCII)
_METRIC_REF = re.compile(
    r"(s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)|total)\.(?!i[0-9]{3,6}\Z)[a-z][a-z0-9_]{0,63}",
    re.ASCII,
)
_SESSION_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)", re.ASCII)


class _Reject(Exception):
    """A check failed with a validation reason (never shown: the envelope has a fixed text)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _invalid() -> _Reject:
    return _Reject("artifact_invalid")


@dataclass(frozen=True)
class ArtifactContext:
    """What the check compares the artifact with. ``expected_marker`` (None: no marker exists) and
    ``redactor`` are called inside the guard."""

    gateway: Gateway
    retro_id: str
    run_token: str
    digest_sha256: str
    manifest: Mapping[str, Any]
    digest: DigestResult
    expected_marker: Callable[[], str | None]
    redactor: Callable[[], redact.Redactor]


def bundled_marker(gateway: Gateway, plugin_dir: str) -> str:
    """The marker line of the bundled skill, or ``""`` when the file is unusable. ``""`` is a
    marker that exists and that no artifact can equal (the schema needs 1 character), so a bundled
    skill whose marker cannot be read fails closed with ``marker_mismatch``."""
    try:
        content = gateway.read_file(f"{plugin_dir}/{MARKER_FILE}", 4096)
        if content.truncated or not statmod.S_ISREG(content.stat.st_mode):
            return ""
        text = content.data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    line = text[:-1] if text.endswith("\n") else text
    return line if _MARKER_LINE.fullmatch(line) else ""


# --- checks 1 to 3: the report and the frozen file --------------------------------------------


def _frozen_bytes(collected: Collected, gateway: Gateway) -> bytes:
    report = collected.report
    if report is None or report.get("implicit") is not False or report.get("artifact") is None:
        raise _Reject("no_artifact")  # check 1
    info = report["artifact"]
    if not isinstance(info, dict):
        raise _invalid()
    size, sha, path = info.get("size"), info.get("sha256"), info.get("path")
    if info.get("name") != ARTIFACT_NAME or type(size) is not int or not 0 <= size:  # check 2
        raise _invalid()
    if size > MAX_ARTIFACT_FILE or not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise _invalid()
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > _PATH_MAX
        or "\x00" in path
    ):
        raise _invalid()
    content = gateway.read_file(path, MAX_ARTIFACT_FILE + 1)  # check 3: O_NOFOLLOW inside
    info_stat = content.stat
    if (
        not statmod.S_ISREG(info_stat.st_mode)
        or info_stat.st_uid != gateway.uid()
        or content.truncated
        or len(content.data) != size
        or hashlib.sha256(content.data).hexdigest() != sha
    ):
        raise _invalid()
    return content.data


# --- check 4: strict JSON and the closed schema -----------------------------------------------


def _depth_ok(data: bytes) -> bool:
    """The byte scan: nesting (outside strings) deeper than ``MAX_DEPTH`` is refused before any
    parse, so the parser's recursion is never reached."""
    depth = 0
    in_string = escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return True


def _parse_int(text: str) -> int:
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > MAX_DIGITS:
        raise Invalid("integer with too many digits")
    value = int(text)
    if abs(value) >= MAX_INTEGER:
        raise Invalid("integer out of range")
    return value


def _parse_float(_text: str) -> Any:
    raise Invalid("the artifact holds no fractional number")


def _parse(data: bytes) -> dict[str, Any]:
    if not _depth_ok(data):
        raise _invalid()
    document = strict_loads(data.decode("utf-8"), parse_int=_parse_int, parse_float=_parse_float)
    if not isinstance(document, dict):
        raise _invalid()
    return document


def _object(
    value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(required) <= set(value):
        raise _invalid()
    if not set(value) <= {*required, *optional}:
        raise _invalid()
    return value


def _match(pattern: re.Pattern[str], value: Any) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid()
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _TEXT_LIMITS[name]:
        raise _invalid()
    return value


def _list(value: Any, low: int, high: int) -> list[Any]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise _invalid()
    return value


def _evidence(value: Any) -> tuple[str, str]:
    found = _object(value, ("kind", "ref"))
    kind, ref = found["kind"], found["ref"]
    if not isinstance(ref, str) or not 3 <= len(ref) <= 80:
        raise _invalid()
    if kind == "item":
        _match(_ITEM_REF, ref)
    elif kind == "metric":
        _match(_METRIC_REF, ref)
    else:
        raise _invalid()
    return kind, ref


def _check_schema(document: dict[str, Any]) -> None:
    """``schemas/slot/artifact.json`` by hand (the runtime has no JSON Schema library; a test
    compares this check with the schema file)."""
    _object(
        document,
        ("retro_id", "run_token", "digest_sha256", "summary", "findings", "candidates"),
        ("marker",),
    )
    _match(_RETRO_ID, document["retro_id"])
    _match(_RUN_TOKEN, document["run_token"])
    _match(_SHA, document["digest_sha256"])
    if "marker" in document and (
        not isinstance(document["marker"], str) or not 1 <= len(document["marker"]) <= 256
    ):
        raise _invalid()
    _text(document["summary"], "summary")
    finding_ids: set[str] = set()
    for finding in _list(document["findings"], 0, LIMITS["max_findings"]):
        found = _object(finding, ("id", "category", "severity", "title", "detail", "evidence"))
        if _match(_FINDING_ID, found["id"]) in finding_ids:
            raise _invalid()
        finding_ids.add(found["id"])
        if found["category"] not in FINDING_CATEGORIES or found["severity"] not in (
            "low",
            "medium",
            "high",
        ):
            raise _invalid()
        _text(found["title"], "title")
        _text(found["detail"], "detail")
        for item in _list(found["evidence"], 1, 8):
            _evidence(item)
    candidate_ids: set[str] = set()
    for candidate in _list(document["candidates"], 0, LIMITS["max_candidates"]):
        found = _object(candidate, ("id", "target_class", "target_hint", "proposal", "findings"))
        if _match(_CANDIDATE_ID, found["id"]) in candidate_ids:
            raise _invalid()
        candidate_ids.add(found["id"])
        if found["target_class"] not in TARGET_CLASSES:
            raise _invalid()
        _text(found["target_hint"], "target_hint")
        _text(found["proposal"], "proposal")
        cited = [_match(_FINDING_ID, f) for f in _list(found["findings"], 1, 50)]
        if len(set(cited)) != len(cited):
            raise _invalid()


# --- check 7: references -----------------------------------------------------------------------


def _valid_refs(manifest: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    sessions = {s["ref"] for s in manifest["sessions"]}
    items = {i for s in manifest["sessions"] for i in s["items"]}
    return sessions, items


def _evidence_known(kind: str, ref: str, sessions: set[str], items: set[str]) -> bool:
    if kind == "item":
        return ref in items
    head, _, name = ref.partition(".")
    return name in METRIC_NAMES and (head == "total" or head in sessions)


def _session_of(ref: str) -> str | None:
    head = ref.partition(".")[0]
    return head if _SESSION_REF.fullmatch(head) else None


def _references(
    document: dict[str, Any], manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """The surviving findings and candidates (candidates with ``risk`` and ``sessions``) and the
    dropped counts. Ids are kept as written."""
    sessions, items = _valid_refs(manifest)
    written = document["findings"]
    existed = {f["id"] for f in written}
    kept = [
        f
        for f in written
        if all(_evidence_known(e["kind"], e["ref"], sessions, items) for e in f["evidence"])
    ]
    alive = {f["id"]: f for f in kept}
    if written and not kept:
        raise _Reject("no_valid_findings")
    candidates: list[dict[str, Any]] = []
    for candidate in document["candidates"]:
        if not set(candidate["findings"]) <= existed:
            continue  # cites an id that never existed
        cited = [f for f in candidate["findings"] if f in alive]
        if not cited:
            continue
        refs = {
            s
            for f in cited
            for e in alive[f]["evidence"]
            if (s := _session_of(e["ref"])) is not None
        }
        candidates.append(
            {
                **candidate,
                "findings": cited,
                "risk": RISK[candidate["target_class"]],
                "sessions": sorted(refs, key=lambda r: int(r[1:])),
            }
        )
    return kept, candidates, len(written) - len(kept), len(document["candidates"]) - len(candidates)


# --- check 8: text -----------------------------------------------------------------------------


class _Text:
    def __init__(self, redactor: redact.Redactor) -> None:
        self.redactor = redactor
        self.redacted = 0

    def __call__(self, value: str, cap: int) -> str:
        """``fix_surrogates``, NFC, redaction (and the encoded forms of the mask values), then the
        cut to the cap on a code point and never inside a marker: NFC or a marker may have made
        the text longer than the schema allows."""
        text = unicodedata.normalize("NFC", fix_surrogates(value))
        text, counts = self.redactor.redact_counted(text)
        text, encoded = self.redactor.redact_encoded(text)
        self.redacted += sum(counts.values()) + encoded
        return redact.cut(text, cap)


# --- the tool-written parts --------------------------------------------------------------------


def _metrics(digest: DigestResult) -> dict[str, int | float]:
    found: dict[str, int | float] = {}

    def add(ref: str, metrics: Metrics) -> None:
        for name, value in zip(METRIC_NAMES, _numbers(metrics), strict=True):
            if value is not None:
                found[f"{ref}.{name}"] = value

    for section in digest.sections:
        add(section.ref, section.metrics)
    add("total", digest.totals)
    return found


def _numbers(metrics: Metrics) -> tuple[int | float | None, ...]:
    ms = metrics.active_ms
    seconds: int | float | None = (
        None if ms is None else (ms // 1000 if ms % 1000 == 0 else ms / 1000)
    )
    return (
        metrics.tool_calls,
        metrics.tool_errors,
        metrics.denials,
        metrics.interruptions,
        metrics.compactions,
        metrics.agents,
        seconds,
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.cache_read_tokens,
        metrics.cache_write_tokens,
        float(metrics.estimated.quantize(_USD4, rounding=ROUND_HALF_UP)),  # as the digest shows
    )


def _validate(collected: Collected, context: ArtifactContext) -> Mapped:
    data = _frozen_bytes(collected, context.gateway)  # checks 1 to 3
    document = _parse(data)  # check 4
    _check_schema(document)
    if (
        document["retro_id"] != context.retro_id  # check 5
        or document["run_token"] != context.run_token
        or document["digest_sha256"] != context.digest_sha256
    ):
        raise _Reject("identity_mismatch")
    expected = context.expected_marker()  # check 6
    if document.get("marker") != expected:
        raise _Reject("marker_mismatch")
    findings, candidates, dropped_f, dropped_c = _references(document, context.manifest)  # 7
    clean = _Text(context.redactor())  # check 8
    stored: dict[str, Any] = {
        key: document[key] for key in ("retro_id", "run_token", "digest_sha256") if key in document
    }
    if "marker" in document:  # equal to the expected marker; a masked secret is not stored in clear
        stored["marker"] = clean(document["marker"], 256)
    summary = clean(document["summary"], _TEXT_LIMITS["summary"])
    stored["summary"] = summary
    stored["findings"] = [
        {
            **f,
            "title": clean(f["title"], _TEXT_LIMITS["title"]),
            "detail": clean(f["detail"], _TEXT_LIMITS["detail"]),
        }
        for f in findings
    ]
    stored["candidates"] = [
        {
            **c,
            "target_hint": clean(c["target_hint"], _TEXT_LIMITS["target_hint"]),
            "proposal": clean(c["proposal"], _TEXT_LIMITS["proposal"]),
        }
        for c in candidates
    ]
    skipped = [{"session_id": s.session_id, "reason": s.reason} for s in context.digest.skipped]
    envelope = encode_artifact(
        stored, skipped, dict(context.manifest["coverage"]), _metrics(context.digest)
    )
    if len(envelope.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise _invalid()
    return Mapped(
        "done",
        None,
        0,
        outcome="done",
        artifact=StoredArtifact(
            envelope, summary, len(findings), len(candidates), dropped_f, dropped_c, clean.redacted
        ),
    )


def validate_and_store_artifact(collected: Collected, context: ArtifactContext) -> Mapped:
    """Checks 1 to 8. Never raises an ``Exception``: any failure is ``artifact_invalid``."""
    try:
        return _validate(collected, context)
    except _Reject as rejected:
        return Mapped("failed", rejected.reason, 22, outcome="done")
    except Exception:
        return Mapped("failed", "artifact_invalid", 22, outcome="done")


def write_artifact(conn: sqlite3.Connection, retro_id: str, stored: StoredArtifact) -> None:
    """Inside the transaction of the final ``done`` write: the ``retro_artifacts`` row (the only
    stored copy), the ``dropped`` and ``redacted`` figures and the finding and candidate counts."""
    conn.execute(
        "INSERT INTO retro_artifacts (retro_id, artifact, summary) VALUES (?, ?, ?)",
        (retro_id, stored.envelope, stored.summary),
    )
    cursor = conn.execute(
        "UPDATE retros SET counts = json_set(counts, '$.findings', ?, '$.candidates', ?), "
        "dropped = ?, redacted = ? WHERE id = ?",
        (
            stored.findings,
            stored.candidates,
            json.dumps(
                {"findings": stored.dropped_findings, "candidates": stored.dropped_candidates},
                separators=(",", ":"),
            ),
            stored.redacted,
            retro_id,
        ),
    )
    if cursor.rowcount != 1:
        raise errors.CliError("internal", "the retro row is gone")
