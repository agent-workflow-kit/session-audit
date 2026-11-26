"""The artifact check, one test per check and per validation reason, the guards, the text pipeline
and the tool-written fields (issue #29, Retro validation)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from session_audit import redact, retroartifact
from session_audit.digest import DigestResult, KeptItem, Metrics, SessionSection, Skipped
from session_audit.fencing import FINDING_CATEGORIES, LIMITS, TARGET_CLASSES
from session_audit.gateway import SystemGateway
from session_audit.retroartifact import ArtifactContext, validate_and_store_artifact
from session_audit.retrooutcome import Collected, Mapped

from .schemas import validate

RETRO = "ra-abcdefgh"
TOKEN = "t" * 24
DIGEST = "d" * 64
SID1 = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
MARKER = "marker-" + "a" * 12
ITEMS1 = ["s1.i001", "s1.i002", "s1.i003"]
ITEMS2 = ["s2.i001"]


def metrics(n: int) -> Metrics:
    return Metrics(n, n + 1, 0, 0, 0, 2, 1500 * n, 10 * n, 20 * n, 0, 0, Decimal("0.5") * n)


def section(ref: str, session_id: str, items: list[str], n: int) -> SessionSection:
    kept = tuple(KeptItem(i, "prompt", "-", "text") for i in items)
    return SessionSection(ref, session_id, (), kept, len(items), 0, 0, metrics(n))


def digest_result() -> DigestResult:
    sections = (section("s1", SID1, ITEMS1, 1), section("s2", SID2, ITEMS2, 2))
    return DigestResult(
        sections, (Skipped(SID2.replace("2", "3"), "empty"),), 0, 2, metrics(3), (), 100, False
    )


MANIFEST = {
    "sessions": [
        {"ref": "s1", "csession_id": None, "items": ITEMS1},
        {"ref": "s2", "csession_id": None, "items": ITEMS2},
    ],
    "coverage": {
        "digest_bytes": 10,
        "items_total": 4,
        "items_kept": 4,
        "sessions": [
            {
                "ref": "s1",
                "items_total": 3,
                "items_kept": 3,
                "dropped": {"session_cap": 0, "total_cap": 0},
            },
            {
                "ref": "s2",
                "items_total": 1,
                "items_kept": 1,
                "dropped": {"session_cap": 0, "total_cap": 0},
            },
        ],
    },
}


def finding(n: int = 1, **over: Any) -> dict[str, Any]:
    base = {
        "id": f"f{n}",
        "category": "friction",
        "severity": "medium",
        "title": "A title",
        "detail": "Some detail",
        "evidence": [
            {"kind": "item", "ref": "s1.i001"},
            {"kind": "metric", "ref": "s2.tool_calls"},
        ],
    }
    return {**base, **over}


def candidate(n: int = 1, **over: Any) -> dict[str, Any]:
    base = {
        "id": f"c{n}",
        "target_class": "skill",
        "target_hint": "a hint",
        "proposal": "a proposal",
        "findings": ["f1"],
    }
    return {**base, **over}


def artifact(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "retro_id": RETRO,
        "run_token": TOKEN,
        "digest_sha256": DIGEST,
        "marker": MARKER,
        "summary": "A summary",
        "findings": [finding()],
        "candidates": [candidate()],
    }
    return {**base, **over}


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.gw = SystemGateway()
        self.marker: str | None = MARKER
        self.masks: list[str] = []

    def context(self) -> ArtifactContext:
        return ArtifactContext(
            self.gw,
            RETRO,
            TOKEN,
            DIGEST,
            MANIFEST,
            digest_result(),
            lambda: self.marker,
            lambda: redact.Redactor(self.masks),
        )

    def report(self, data: bytes, **over: Any) -> dict[str, Any]:
        path = self.dir / "audit.json"
        path.write_bytes(data)
        info = {
            "name": "audit.json",
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        return {"status": "done", "implicit": False, "artifact": {**info, **over}}

    def run(self, document: Any = None, *, raw: bytes | None = None, **over: Any) -> Mapped:
        data = (
            raw
            if raw is not None
            else json.dumps(document if document is not None else artifact()).encode()
        )
        return self.check(self.report(data, **over))

    def check(self, report: dict[str, Any] | None) -> Mapped:
        collected = Collected(0.5, 3, True, "done", report)
        return validate_and_store_artifact(collected, self.context())


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def fails(result: Mapped, reason: str) -> None:
    assert (result.state, result.reason, result.exit_code) == ("failed", reason, 22)
    assert result.artifact is None


def stored(result: Mapped) -> dict[str, Any]:
    assert (result.state, result.reason, result.exit_code) == ("done", None, 0), result
    assert result.artifact is not None
    return json.loads(result.artifact.envelope)  # type: ignore[no-any-return]


# --- the happy path and the tool-written fields -------------------------------------------------


def test_a_good_artifact_is_stored_with_tool_written_fields(h: Harness) -> None:
    result = h.run()
    envelope = stored(result)
    assert result.artifact is not None
    assert (result.artifact.findings, result.artifact.candidates) == (1, 1)
    assert (result.artifact.dropped_findings, result.artifact.dropped_candidates) == (0, 0)
    assert result.artifact.redacted == 0 and result.artifact.summary == "A summary"
    assert set(envelope) == {"artifact", "skipped", "coverage", "metrics"}
    [stored_candidate] = envelope["artifact"]["candidates"]
    assert stored_candidate["risk"] == "medium" and stored_candidate["sessions"] == ["s1", "s2"]
    assert envelope["coverage"] == MANIFEST["coverage"]
    assert envelope["skipped"] == [{"session_id": SID2.replace("2", "3"), "reason": "empty"}]
    assert (
        envelope["metrics"]["s2.tool_calls"] == 2 and envelope["metrics"]["total.tool_calls"] == 3
    )
    assert (
        envelope["metrics"]["s1.active_seconds"] == 1.5
        and envelope["metrics"]["s2.active_seconds"] == 3
    )
    assert len(envelope["metrics"]) == 36


@pytest.mark.parametrize(
    ("target_class", "risk"),
    [
        ("permission_profile", "high"),
        ("config", "high"),
        ("instructions_file", "high"),
        ("skill", "medium"),
        ("workflow", "medium"),
    ],
)
def test_candidate_risk_is_written_by_the_tool_by_target_class(
    h: Harness, target_class: str, risk: str
) -> None:
    document = artifact(candidates=[candidate(target_class=target_class)])
    [written] = stored(h.run(document))["artifact"]["candidates"]
    assert written["risk"] == risk


def test_every_target_class_has_a_risk() -> None:
    assert set(retroartifact.RISK) == set(TARGET_CLASSES)


@pytest.mark.parametrize("key", ["risk", "sessions", "counts", "metrics", "redacted", "dropped"])
def test_a_number_or_tool_field_written_by_the_model_is_refused(h: Harness, key: str) -> None:
    fails(
        h.run(artifact(candidates=[candidate(1, **{key: "low" if key == "risk" else 1})])),
        "artifact_invalid",
    )
    fails(h.run(artifact(**{key: 1})), "artifact_invalid")


def test_the_model_cannot_set_the_risk_even_with_a_valid_value(h: Harness) -> None:
    fails(
        h.run(artifact(candidates=[candidate(risk="medium", target_class="config")])),
        "artifact_invalid",
    )


def test_a_candidate_session_list_holds_the_cited_sessions_only_in_order(h: Harness) -> None:
    findings = [
        finding(1, evidence=[{"kind": "item", "ref": "s2.i001"}]),
        finding(2, evidence=[{"kind": "metric", "ref": "total.tool_calls"}]),
    ]
    document = artifact(findings=findings, candidates=[candidate(findings=["f2", "f1"])])
    [written] = stored(h.run(document))["artifact"]["candidates"]
    assert written["sessions"] == ["s2"] and written["findings"] == ["f2", "f1"]


def test_zero_findings_written_by_the_model_is_a_valid_done_retro(h: Harness) -> None:
    result = h.run(artifact(findings=[], candidates=[]))
    stored(result)
    assert result.artifact is not None and result.artifact.findings == 0


# --- check 1 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"implicit": True, "artifact": {"name": "audit.json"}},
        {"implicit": False, "artifact": None},
        {"implicit": False},
        {"artifact": {"name": "audit.json"}},
    ],
    ids=["no-report", "implicit", "artifact-null", "artifact-absent", "implicit-absent"],
)
def test_check_1_no_artifact(h: Harness, report: dict[str, Any] | None) -> None:
    fails(h.check(report), "no_artifact")


# --- check 2 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"name": "other.json"},
        {"name": None},
        {"size": 262_145},
        {"size": "5"},
        {"size": True},
        {"size": -1},
        {"sha256": "ZZ"},
        {"sha256": None},
        {"path": "relative/audit.json"},
        {"path": None},
        {"path": "/x\x00y"},
    ],
)
def test_check_2_name_size_and_shape(h: Harness, over: dict[str, Any]) -> None:
    fails(h.run(**over), "artifact_invalid")


def test_check_2_an_artifact_that_is_not_an_object(h: Harness) -> None:
    fails(h.check({"implicit": False, "artifact": "audit.json"}), "artifact_invalid")


# --- check 3 -----------------------------------------------------------------------------------


def test_check_3_a_missing_file(h: Harness) -> None:
    report = h.report(b"{}")
    os.unlink(report["artifact"]["path"])
    fails(h.check(report), "artifact_invalid")


def test_check_3_a_symlink_is_never_followed(h: Harness, tmp_path: Path) -> None:
    data = json.dumps(artifact()).encode()
    real = tmp_path / "real.json"
    real.write_bytes(data)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    report = h.report(data, path=str(link))
    fails(h.check(report), "artifact_invalid")


def test_check_3_a_directory_is_not_a_file(h: Harness, tmp_path: Path) -> None:
    report = h.report(b"", path=str(tmp_path))
    fails(h.check(report), "artifact_invalid")


def test_check_3_the_size_must_match(h: Harness) -> None:
    data = json.dumps(artifact()).encode()
    fails(h.run(size=len(data) + 1), "artifact_invalid")
    fails(h.run(size=len(data) - 1), "artifact_invalid")


def test_check_3_the_hash_must_match(h: Harness) -> None:
    fails(h.run(sha256="0" * 64), "artifact_invalid")
    fails(h.run(sha256=hashlib.sha256(b"x").hexdigest().upper()), "artifact_invalid")


def test_check_3_a_file_larger_than_it_was_reported_is_refused_unread(h: Harness) -> None:
    data = json.dumps(artifact(summary="s" * 100)).encode() + b" " * 300_000
    fails(h.run(raw=data, size=100), "artifact_invalid")


def test_check_3_a_file_of_another_owner_is_refused(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(h.gw, "uid", lambda: os.geteuid() + 1)
    fails(h.run(), "artifact_invalid")


# --- check 4: strict JSON, the guards, the closed schema ---------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'"x"',
        b"\xef\xbb\xbf{}",
        b"\xff\xfe",
        b'{"summary": "a", "summary": "b"}',
        b'{"summary": NaN}',
        b'{"summary": Infinity}',
        b'{"summary": 1.5}',
        b'{"summary": 1e999}',
        b'{"summary": 12345678901234567}',
        b'{"summary": 9007199254740993}',
        b'{"summary": -9007199254740992}',
        b'{"summary": ' + b"9" * 5000 + b"}",
    ],
)
def test_check_4_strict_json(h: Harness, raw: bytes) -> None:
    fails(h.run(raw=raw), "artifact_invalid")


def test_check_4_a_duplicate_key_in_a_good_document(h: Harness) -> None:
    good = json.dumps(artifact())
    fails(h.run(raw=(good[:-1] + ', "summary": "again"}').encode()), "artifact_invalid")


def test_check_4_the_depth_guard_runs_before_the_parser(h: Harness) -> None:
    assert retroartifact._depth_ok(b"[" * 16 + b"]" * 16)
    assert not retroartifact._depth_ok(b"[" * 17 + b"]" * 17)
    assert retroartifact._depth_ok(b'{"a": "' + b"[" * 100 + b'"}')  # brackets in strings
    assert retroartifact._depth_ok(b'{"a": "\\"' + b"[" * 100 + b'"}')  # an escaped quote
    fails(h.run(raw=b"[" * 100_000 + b"]" * 100_000), "artifact_invalid")
    fails(h.run(raw=b'{"summary": ' + b"[" * 17 + b"]" * 17 + b"}"), "artifact_invalid")


def test_check_4_the_big_integer_guard_names_the_digit_limit() -> None:
    assert retroartifact._parse_int("1234567890123456") == 1234567890123456
    with pytest.raises(ValueError):
        retroartifact._parse_int("1" * 17)
    with pytest.raises(ValueError):
        retroartifact._parse_int("-" + "1" * 17)
    with pytest.raises(ValueError):
        retroartifact._parse_int("9007199254740992")
    assert retroartifact._parse_int("9007199254740991") == 9007199254740991


def test_check_4_a_lone_surrogate_is_stored_as_the_replacement_character(h: Harness) -> None:
    document = artifact(summary="a\ud800b", findings=[finding(title="t\udfffu")], candidates=[])
    data = json.dumps(document).encode()  # ensure_ascii: the file holds the \ud800 escapes
    assert b"\\ud800" in data
    written = stored(h.run(raw=data))["artifact"]
    assert written["summary"] == "a�b" and written["findings"][0]["title"] == "t�u"


def test_check_4_a_surrogate_pair_in_escapes_is_one_character(h: Harness) -> None:
    data = json.dumps(artifact(summary="x\U0001f600y")).encode()
    assert b"\\ud83d\\ude00" in data
    assert stored(h.run(raw=data))["artifact"]["summary"] == "x\U0001f600y"


def test_check_4_a_lone_surrogate_in_a_key_or_identity_is_refused(h: Harness) -> None:
    fails(h.run(raw=b'{"\\ud800": 1}'), "artifact_invalid")
    fails(h.run(artifact(retro_id="ra-abcdefg\ud800")), "artifact_invalid")


def mutate(**changes: Any) -> dict[str, Any]:
    return artifact(**changes)


BAD_SCHEMA = {
    "missing summary": {k: v for k, v in artifact().items() if k != "summary"},
    "missing findings": {k: v for k, v in artifact().items() if k != "findings"},
    "extra top key": mutate(extra=1),
    "retro id shape": mutate(retro_id="ra-ABCDEFGH"),
    "retro id type": mutate(retro_id=5),
    "token short": mutate(run_token="x" * 15),
    "token chars": mutate(run_token="x" * 15 + "!"),
    "token newline": mutate(run_token=TOKEN + "\n"),
    "digest upper": mutate(digest_sha256="D" * 64),
    "marker empty": mutate(marker=""),
    "marker long": mutate(marker="m" * 257),
    "marker type": mutate(marker=3),
    "summary empty": mutate(summary=""),
    "summary over cap": mutate(summary="s" * (LIMITS["text_limits"]["summary"] + 1)),
    "summary type": mutate(summary=["x"]),
    "findings type": mutate(findings={}),
    "too many findings": mutate(
        findings=[finding(n) for n in range(1, 51)] + [finding(1)], candidates=[]
    ),
    "too many candidates": mutate(candidates=[candidate(n) for n in range(1, 22)]),
    "finding id": mutate(findings=[finding(id="f51")]),
    "finding id zero": mutate(findings=[finding(id="f0")]),
    "finding id leading zero": mutate(findings=[finding(id="f01")]),
    "finding category": mutate(findings=[finding(category="other")]),
    "finding severity": mutate(findings=[finding(severity="critical")]),
    "finding extra key": mutate(findings=[finding(extra=1)]),
    "finding missing key": mutate(findings=[{k: v for k, v in finding().items() if k != "detail"}]),
    "finding title empty": mutate(findings=[finding(title="")]),
    "finding title over": mutate(findings=[finding(title="t" * 121)]),
    "finding detail over": mutate(findings=[finding(detail="d" * 1501)]),
    "finding no evidence": mutate(findings=[finding(evidence=[])]),
    "finding 9 evidence": mutate(
        findings=[finding(evidence=[{"kind": "item", "ref": "s1.i001"}] * 9)]
    ),
    "evidence kind": mutate(findings=[finding(evidence=[{"kind": "x", "ref": "s1.i001"}])]),
    "evidence extra": mutate(
        findings=[finding(evidence=[{"kind": "item", "ref": "s1.i001", "n": 1}])]
    ),
    "evidence item shape": mutate(findings=[finding(evidence=[{"kind": "item", "ref": "s1"}])]),
    "evidence metric shape": mutate(
        findings=[finding(evidence=[{"kind": "metric", "ref": "s1.i001"}])]
    ),
    "evidence kind swapped": mutate(
        findings=[finding(evidence=[{"kind": "metric", "ref": "total.tool_calls "}])]
    ),
    "evidence ref type": mutate(findings=[finding(evidence=[{"kind": "item", "ref": 1}])]),
    "duplicate finding ids": mutate(findings=[finding(1), finding(1)]),
    "candidate id": mutate(candidates=[candidate(id="c21")]),
    "duplicate candidate ids": mutate(candidates=[candidate(1), candidate(1)]),
    "candidate class": mutate(candidates=[candidate(target_class="other")]),
    "candidate hint empty": mutate(candidates=[candidate(target_hint="")]),
    "candidate hint over": mutate(candidates=[candidate(target_hint="h" * 201)]),
    "candidate proposal over": mutate(candidates=[candidate(proposal="p" * 1501)]),
    "candidate no findings": mutate(candidates=[candidate(findings=[])]),
    "candidate repeated finding": mutate(candidates=[candidate(findings=["f1", "f1"])]),
    "candidate finding id shape": mutate(candidates=[candidate(findings=["x"])]),
}


@pytest.mark.parametrize("name", sorted(BAD_SCHEMA))
def test_check_4_the_closed_schema(h: Harness, name: str) -> None:
    fails(h.run(BAD_SCHEMA[name]), "artifact_invalid")


@pytest.mark.parametrize("name", sorted(BAD_SCHEMA))
def test_the_hand_check_agrees_with_the_schema_file(name: str) -> None:
    """Every refused document is refused by schemas/slot/artifact.json too, except the rules the
    schema cannot say (id uniqueness, the 256 KB cap)."""
    cannot = {"duplicate finding ids", "duplicate candidate ids", "token newline"}
    document = BAD_SCHEMA[name]
    try:
        validate(document, "session-audit/slot/artifact.json")
    except Exception:
        return
    assert name in cannot


def test_the_good_artifact_is_valid_for_the_schema_file() -> None:
    validate(artifact(), "session-audit/slot/artifact.json")


def test_the_schema_limits_are_the_fenced_limits() -> None:
    assert len(FINDING_CATEGORIES) == 4
    document = artifact(findings=[finding(n) for n in range(1, 51)], candidates=[])
    validate(document, "session-audit/slot/artifact.json")


# --- check 5 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [{"retro_id": "ra-zzzzzzzz"}, {"run_token": "u" * 24}, {"digest_sha256": "e" * 64}],
)
def test_check_5_identity_echoes(h: Harness, change: dict[str, Any]) -> None:
    fails(h.run(artifact(**change)), "identity_mismatch")


# --- check 6 -----------------------------------------------------------------------------------


def test_check_6_a_wrong_marker(h: Harness) -> None:
    fails(h.run(artifact(marker="marker-" + "b" * 12)), "marker_mismatch")


def test_check_6_a_missing_marker_when_one_exists(h: Harness) -> None:
    document = artifact()
    del document["marker"]
    fails(h.run(document), "marker_mismatch")


def test_check_6_a_marker_when_none_exists(h: Harness) -> None:
    h.marker = None
    fails(h.run(), "marker_mismatch")


def test_check_6_no_marker_and_none_expected_is_valid(h: Harness) -> None:
    h.marker = None
    document = artifact()
    del document["marker"]
    stored(h.run(document))


def test_check_6_an_unreadable_bundled_marker_fails_closed(h: Harness, tmp_path: Path) -> None:
    assert retroartifact.bundled_marker(h.gw, str(tmp_path / "nope")) == ""
    h.marker = retroartifact.bundled_marker(h.gw, str(tmp_path / "nope"))
    fails(h.run(), "marker_mismatch")


def test_the_bundled_marker_is_the_single_line_of_its_file(h: Harness, tmp_path: Path) -> None:
    path = tmp_path / "skills" / "audit" / "references"
    path.mkdir(parents=True)
    (path / "marker.txt").write_text(MARKER + "\n")
    assert retroartifact.bundled_marker(h.gw, str(tmp_path)) == MARKER
    for bad in ["SHORT\n", MARKER + "\nmore\n", MARKER.upper() + "\n", "", "x" * 65 + "\n"]:
        (path / "marker.txt").write_text(bad)
        assert retroartifact.bundled_marker(h.gw, str(tmp_path)) == ""


# --- check 7 -----------------------------------------------------------------------------------


def evidence(*refs: str) -> list[dict[str, str]]:
    return [{"kind": "item" if ".i" in r else "metric", "ref": r} for r in refs]


def test_check_7_findings_with_an_unknown_reference_are_dropped_and_counted(h: Harness) -> None:
    findings = [
        finding(1),
        finding(2, evidence=evidence("s1.i001", "s1.i999")),
        finding(3, evidence=evidence("s3.i001")),
        finding(4, evidence=evidence("s9.tool_calls")),
        finding(5, evidence=evidence("s1.nope")),
        finding(6, evidence=evidence("total.tool_calls")),
    ]
    result = h.run(artifact(findings=findings, candidates=[]))
    written = stored(result)["artifact"]["findings"]
    assert [f["id"] for f in written] == ["f1", "f6"]  # ids kept as written
    assert result.artifact is not None
    assert (result.artifact.findings, result.artifact.dropped_findings) == (2, 4)


def test_check_7_candidates_lose_dropped_findings_and_unknown_ids_drop_them(h: Harness) -> None:
    findings = [finding(1), finding(2, evidence=evidence("s7.i001")), finding(3)]
    candidates = [
        candidate(1, findings=["f1", "f2"]),  # keeps f1
        candidate(2, findings=["f2"]),  # left with none: dropped
        candidate(3, findings=["f1", "f9"]),  # f9 never existed: dropped
        candidate(4, findings=["f3"]),
    ]
    result = h.run(artifact(findings=findings, candidates=candidates))
    written = stored(result)["artifact"]["candidates"]
    assert [(c["id"], c["findings"]) for c in written] == [("c1", ["f1"]), ("c4", ["f3"])]
    assert result.artifact is not None
    assert (result.artifact.candidates, result.artifact.dropped_candidates) == (2, 2)


def test_check_7_no_valid_findings(h: Harness) -> None:
    findings = [finding(1, evidence=evidence("s5.i001")), finding(2, evidence=evidence("s1.i009"))]
    fails(
        h.run(artifact(findings=findings, candidates=[candidate(findings=["f1"])])),
        "no_valid_findings",
    )


def test_check_7_evidence_is_checked_against_the_manifest_not_the_pattern(h: Harness) -> None:
    ok = finding(1, evidence=evidence("s2.i001", "s1.estimated_usd", "total.active_seconds"))
    assert stored(h.run(artifact(findings=[ok], candidates=[])))["artifact"]["findings"]


# --- check 8 -----------------------------------------------------------------------------------


def test_check_8_a_secret_in_model_text_is_redacted_and_counted(h: Harness) -> None:
    secret = "gh" + "p_" + "A1" * 20
    document = artifact(
        summary=f"leaked {secret} here",
        findings=[finding(detail=f"{secret} and {secret}")],
        candidates=[candidate(proposal=f"use {secret}")],
    )
    result = h.run(document)
    text = result.artifact.envelope if result.artifact else ""
    assert secret not in text and "[REDACTED:token]" in text
    assert result.artifact is not None and result.artifact.redacted == 4
    assert "[REDACTED:token]" in result.artifact.summary


def test_check_8_the_encoded_forms_of_a_mask_value_are_redacted(h: Harness) -> None:
    value = "hunter" + "2-" + "value"
    h.masks = [value]
    encoded = base64.b64encode(value.encode()).decode()
    document = artifact(
        summary=f"plain {value}",
        findings=[finding(detail=f"b64 {encoded} hex {value.encode().hex()}")],
        candidates=[],
    )
    result = h.run(document)
    assert result.artifact is not None
    text = result.artifact.envelope
    assert value not in text and encoded not in text and value.encode().hex() not in text
    assert result.artifact.redacted == 3
    assert "[REDACTED:encoded]" in text


def test_check_8_text_expanded_by_nfc_is_cut_to_its_schema_cap(h: Harness) -> None:
    expanding = "\u0344"  # NFC: U+0308 U+0301, two code points each
    caps = LIMITS["text_limits"]
    document = artifact(
        summary=expanding * caps["summary"],
        findings=[finding(title=expanding * caps["title"], detail=expanding * caps["detail"])],
        candidates=[
            candidate(
                target_hint=expanding * caps["target_hint"], proposal=expanding * caps["proposal"]
            )
        ],
    )
    written = stored(h.run(document))["artifact"]
    assert len(written["summary"]) == caps["summary"]
    [f] = written["findings"]
    assert (len(f["title"]), len(f["detail"])) == (caps["title"], caps["detail"])
    [c] = written["candidates"]
    assert (len(c["target_hint"]), len(c["proposal"])) == (caps["target_hint"], caps["proposal"])
    assert "\u0344" not in f["title"]  # normalized


def test_check_8_text_expanded_by_a_marker_is_cut_never_inside_the_marker(h: Harness) -> None:
    value = "secr" + "et99"  # 8 characters become [REDACTED:env_value], 19
    h.masks = [value]
    cap = LIMITS["text_limits"]["title"]
    title = (value + " ") * (cap // 9)  # exactly within the cap as written
    assert len(title) <= cap
    document = artifact(findings=[finding(title=title)], candidates=[])
    written = stored(h.run(document))["artifact"]["findings"][0]["title"]
    assert len(written) <= cap and value not in written
    assert not written.endswith("[REDACTED:env_valu") and "[REDACTED:" in written
    for piece in written.split("[REDACTED:")[1:]:
        assert piece.startswith("env_value]")


def test_check_8_text_over_its_cap_as_written_is_still_refused(h: Harness) -> None:
    fails(h.run(artifact(findings=[finding(title="\u0344" * 121)])), "artifact_invalid")


def test_check_8_control_and_bidi_characters_are_kept_as_stored_text(h: Harness) -> None:
    text = "a\u202eb\x1b[31mc"
    assert stored(h.run(artifact(summary=text)))["artifact"]["summary"] == text


# --- the guard ---------------------------------------------------------------------------------


def test_any_exception_in_the_check_is_artifact_invalid(h: Harness) -> None:
    def boom() -> Any:
        raise RuntimeError("boom")

    context = h.context()
    for name in ("expected_marker", "redactor"):
        broken = ArtifactContext(**{**context.__dict__, name: boom})
        collected = Collected(0.5, 3, True, "done", h.report(json.dumps(artifact()).encode()))
        fails(validate_and_store_artifact(collected, broken), "artifact_invalid")


def test_an_exception_while_reading_the_file_is_artifact_invalid(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise MemoryError

    monkeypatch.setattr(h.gw, "read_file", boom)
    fails(h.run(), "artifact_invalid")


def test_a_report_of_the_wrong_shape_is_artifact_invalid(h: Harness) -> None:
    fails(h.check({"implicit": False, "artifact": {"name": "audit.json"}}), "artifact_invalid")


def test_the_input_is_not_modified(h: Harness) -> None:
    report = h.report(json.dumps(artifact()).encode())
    before = copy.deepcopy(report)
    h.check(report)
    assert report == before


# --- write_artifact ----------------------------------------------------------------------------


def test_write_artifact_stores_the_envelope_and_the_figures() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE retro_artifacts (retro_id TEXT PRIMARY KEY, artifact TEXT, summary TEXT)"
    )
    conn.execute("CREATE TABLE retros (id TEXT, counts TEXT, dropped TEXT, redacted INTEGER)")
    conn.execute(
        "INSERT INTO retros VALUES (?, ?, '{}', 0)", (RETRO, '{"sessions":2,"findings":0}')
    )
    stored_artifact = retroartifact.StoredArtifact("{}", "s", 3, 2, 1, 4, 5)
    retroartifact.write_artifact(conn, RETRO, stored_artifact)
    assert conn.execute("SELECT artifact, summary FROM retro_artifacts").fetchone() == ("{}", "s")
    counts, dropped, redacted = conn.execute(
        "SELECT counts, dropped, redacted FROM retros"
    ).fetchone()
    assert json.loads(counts) == {"sessions": 2, "findings": 3, "candidates": 2}
    assert json.loads(dropped) == {"findings": 1, "candidates": 4} and redacted == 5
    with pytest.raises(sqlite3.IntegrityError):
        retroartifact.write_artifact(conn, RETRO, stored_artifact)
