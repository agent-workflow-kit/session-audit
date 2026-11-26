"""The stored retro layout (``retroview``): maximum-size rows against the real CHECK constraints,
hostile rows, the artifact size bound (issue #23, design.md Data model)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from session_audit.retroview import (
    MAX_ARTIFACT_BYTES,
    encode_artifact,
    encode_resolved_scope,
    encode_session_map,
    load_retro,
    scope_text,
)
from session_audit.store import open_store

from .ingesthelp import S1, Corpus, connect, state_dir_of
from .retrohelp import KEY_A, RETRO, RetroGateway, add_retro
from .schemas import validate


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def conn(corpus: Corpus) -> Iterator[sqlite3.Connection]:
    open_store(RetroGateway(), str(state_dir_of(corpus.home))).close()
    connection = connect(corpus.home)
    yield connection
    connection.close()


def session_id(n: int) -> str:
    return str(uuid.UUID(int=n + 1, version=4))


def flags_at_maxima() -> dict[str, Any]:
    labels = [f"{'l' * 60}{n:04d}={'v' * 256}" for n in range(64)]
    return {
        "labels": labels,
        "project": "\x01" * 4096,  # JSON escapes each of these to six characters
        "since": "2026-01-01T00:00:00.123456789+00:00",
        "until": "2026-01-01T00:00:00.123456789+00:00",
        "kind": "k" * 64,
        "outcome": "o" * 64,
    }


def maximum_envelope() -> tuple[dict[str, Any], list[Any], dict[str, Any], dict[str, Any]]:
    finding = {
        "id": "f50",
        "category": "cost",
        "severity": "high",
        "title": "t" * 120,
        "detail": "d" * 1500,
        "evidence": [{"kind": "item", "ref": "s500.i999999"}] * 8,
    }
    candidate = {
        "id": "c20",
        "target_class": "skill",
        "target_hint": "h" * 200,
        "proposal": "p" * 1500,
        "findings": [f"f{n}" for n in range(1, 51)],
        "risk": "high",
        "sessions": [f"s{n}" for n in range(1, 501)],
    }
    artifact: dict[str, Any] = {"findings": [finding] * 50, "candidates": [candidate] * 20}
    artifact["padding"] = "x" * (262_144 - len(json.dumps(artifact)))  # the checked maximum
    skipped = [{"session_id": session_id(n), "reason": "transcript_changed"} for n in range(1000)]
    coverage = {
        "digest_bytes": 2**40,
        "items_total": 10**9,
        "items_kept": 10**9,
        "sessions": [
            {
                "ref": f"s{n % 500 + 1}",
                "items_total": 10**9,
                "items_kept": 10**9,
                "dropped": {"session_cap": 10**9, "total_cap": 10**9},
            }
            for n in range(1000)
        ],
    }
    metrics = {f"s{n}.metric_{k:02d}": 123456789.123456 for n in range(1, 501) for k in range(12)}
    metrics.update({f"total.metric_{k:02d}": 1.5e300 for k in range(12)})
    assert len(metrics) == 6012
    return artifact, skipped, coverage, metrics


def insert_row(
    conn: sqlite3.Connection,
    *,
    scope: str,
    resolved: str,
    session_map: str,
    artifact: str,
    retro_id: str = RETRO,
    summary: str = "s" * 2000,
) -> None:
    conn.execute(
        "INSERT INTO retros (id, key, state, scope, resolved_scope, skill, created_at, "
        "session_map, counts, dropped) VALUES (?, ?, 'done', ?, ?, 'session-audit:audit', 1000, "
        "?, ?, ?)",
        (
            retro_id,
            KEY_A,
            scope,
            resolved,
            session_map,
            json.dumps(
                {
                    "sessions": 500,
                    "skipped": 1000,
                    "over_cap": 0,
                    "items": 9,
                    "findings": 50,
                    "candidates": 20,
                }
            ),
            json.dumps({"findings": 0, "candidates": 0}),
        ),
    )
    conn.execute(
        "INSERT INTO retro_artifacts (retro_id, artifact, summary) VALUES (?, ?, ?)",
        (retro_id, artifact, summary),
    )
    conn.commit()


def test_a_maximum_size_retro_fits_every_check_and_reads_back(conn: sqlite3.Connection) -> None:
    flags = flags_at_maxima()
    sessions = [session_id(n) for n in range(500)]
    resolved = encode_resolved_scope(
        flags, sessions, "2026-01-01T00:00:00.123456789Z", "2026-01-01T00:00:00.123456789Z"
    )
    mapping = encode_session_map(
        [
            {"ref": f"s{n + 1}", "session_id": sessions[n], "csession_id": "cs-abcdefgh"}
            for n in range(500)
        ]
    )
    artifact, skipped, coverage, metrics = maximum_envelope()
    envelope = encode_artifact(artifact, skipped, coverage, metrics)
    assert len(resolved) <= 65_400 and len(mapping) <= 55_000  # the budgets design.md states
    assert len(scope_text(flags)) <= 4096
    assert len(envelope.encode()) < MAX_ARTIFACT_BYTES
    insert_row(  # the real CHECK constraints of migration 800
        conn,
        scope=scope_text(flags),
        resolved=resolved,
        session_map=mapping,
        artifact=envelope,
    )
    loaded = load_retro(conn, RETRO)
    assert loaded is not None
    validate({**loaded, "reused": True}, "session-audit/cli/retro-run.json")
    assert loaded["scope"]["labels"] == flags["labels"]
    assert loaded["scope"]["project"] == flags["project"]
    assert len(loaded["sessions"]) == 500 and len(loaded["skipped"]) == 1000
    assert len(loaded["metrics"]) == 6012 and len(loaded["findings"]) == 50


def test_a_long_scope_text_is_cut_at_a_character_boundary() -> None:
    text = scope_text({"project": "é" * 5000, "labels": [], "since": None})
    assert len(text) == 4096 and set(text) == {"é"} | set("project=")
    text.encode("utf-8")  # never half a character
    assert scope_text({"project": "a\ud800b"}).encode("utf-8")  # a lone surrogate is replaced


def test_the_scope_text_is_never_read_back(conn: sqlite3.Connection, corpus: Corpus) -> None:
    add_retro(corpus.home, KEY_A, scope="anything\x1b[31m at all")
    stored = connect(corpus.home)
    try:
        assert load_retro(stored, RETRO) is not None
    finally:
        stored.close()


def stored(corpus: Corpus, **overrides: Any) -> dict[str, Any] | None:
    open_store(RetroGateway(), str(state_dir_of(corpus.home))).close()
    add_retro(corpus.home, KEY_A, **overrides)
    reader = connect(corpus.home)
    try:
        return load_retro(reader, RETRO)
    finally:
        reader.close()


def envelope_with(**changes: Any) -> str:
    coverage: dict[str, Any] = {
        "digest_bytes": 10,
        "items_total": 2,
        "items_kept": 2,
        "sessions": [
            {
                "ref": "s1",
                "items_total": 2,
                "items_kept": 2,
                "dropped": {"session_cap": 0, "total_cap": 0},
            }
        ],
    }
    parts: dict[str, Any] = {
        "artifact": {"findings": [], "candidates": []},
        "skipped": [],
        "coverage": coverage,
        "metrics": {"total.tool_calls": 0, "s1.denials": 2.5},
    }
    parts.update(changes)
    return json.dumps(parts)


GOOD_FINDING = {
    "id": "f1",
    "category": "friction",
    "severity": "low",
    "title": "t",
    "detail": "d",
    "evidence": [{"kind": "item", "ref": "s1.i001"}, {"kind": "metric", "ref": "total.denials"}],
}
GOOD_CANDIDATE = {
    "id": "c1",
    "target_class": "config",
    "target_hint": "h",
    "proposal": "p",
    "findings": ["f1"],
    "risk": "medium",
    "sessions": ["s1"],
}


def test_a_well_formed_retro_with_findings_validates_against_the_schema(corpus: Corpus) -> None:
    artifact = envelope_with(
        artifact={"findings": [GOOD_FINDING], "candidates": [GOOD_CANDIDATE], "version": 1}
    )
    loaded = stored(corpus, artifact=artifact)
    assert loaded is not None
    validate({**loaded, "reused": True}, "session-audit/cli/retro-run.json")
    assert loaded["metrics"] == {"total.tool_calls": 0, "s1.denials": 2.5}


def bad_coverage(**changes: Any) -> dict[str, Any]:
    good = json.loads(envelope_with())["coverage"]
    good.update(changes)
    return good


HOSTILE = {
    "negative coverage": {"coverage": bad_coverage(digest_bytes=-1)},
    "string coverage": {"coverage": bad_coverage(items_kept="5")},
    "float coverage": {"coverage": bad_coverage(items_total=1.5)},
    "bool coverage": {"coverage": bad_coverage(digest_bytes=True)},
    "extra coverage key": {"coverage": bad_coverage(extra=1)},
    "bad coverage ref": {"coverage": bad_coverage(sessions=[{"ref": "s0"}])},
    "negative metric": {"metrics": {"total.x": -1}},
    "string metric": {"metrics": {"total.x": "5"}},
    "bool metric": {"metrics": {"total.x": True}},
    "evil metric name": {"metrics": {"<evil>\x1b[31m": 1}},
    "bare metric name": {"metrics": {"tool_calls": 1}},
    "item-shaped metric name": {"metrics": {"s1.i001": 1}},
    "session ref out of range": {"metrics": {"s501.x": 1}},
    "huge metric": {"metrics": {"total.x": 1e999}},
    "nan metric": {"metrics": "NaN"},
    "extra envelope key": {"extra": 1},
    "bad skip reason": {"skipped": [{"session_id": S1, "reason": "x"}]},
    "bad skip id": {"skipped": [{"session_id": "nope", "reason": "live"}]},
    "finding with extra key": {"artifact": {"findings": [{**GOOD_FINDING, "x": 1}]}},
    "finding with bad id": {"artifact": {"findings": [{**GOOD_FINDING, "id": "f51"}]}},
    "finding with no evidence": {"artifact": {"findings": [{**GOOD_FINDING, "evidence": []}]}},
    "finding with bad category": {"artifact": {"findings": [{**GOOD_FINDING, "category": "x"}]}},
    "finding with long title": {"artifact": {"findings": [{**GOOD_FINDING, "title": "t" * 121}]}},
    "candidate without risk": {
        "artifact": {"candidates": [{k: v for k, v in GOOD_CANDIDATE.items() if k != "risk"}]}
    },
    "candidate with bad finding": {
        "artifact": {"candidates": [{**GOOD_CANDIDATE, "findings": ["x"]}]}
    },
    "artifact is a list": {"artifact": []},
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_row_outside_the_schema_is_not_a_valid_retro(name: str, corpus: Corpus) -> None:
    changes = dict(HOSTILE[name])
    if "artifact" in changes and isinstance(changes["artifact"], dict):
        changes["artifact"] = {"findings": [], "candidates": [], **changes["artifact"]}
    text = envelope_with(**changes)
    if name == "nan metric":
        text = text.replace('"metrics": "NaN"', '"metrics": {"total.x": NaN}')
    assert stored(corpus, artifact=text) is None


FLAGS = {
    "labels": [],
    "project": None,
    "since": None,
    "until": None,
    "kind": None,
    "outcome": None,
}


@pytest.mark.parametrize(
    "flags",
    [
        {**FLAGS, "labels": ["no-equals"]},
        {**FLAGS, "labels": ["a=b", "a=b"]},
        {**FLAGS, "labels": ["a=" + "b" * 257]},
        {**FLAGS, "since": "yesterday"},
        {**FLAGS, "until": "7d" + "\n"},
        {**FLAGS, "kind": "k" * 65},
        {**FLAGS, "outcome": "Done"},
        {**FLAGS, "project": ""},
        {**FLAGS, "project": 5},
        {**FLAGS, "extra": 1},
    ],
)
def test_scope_flags_outside_the_schema_are_not_a_valid_retro(
    flags: dict[str, Any], corpus: Corpus
) -> None:
    resolved = json.dumps({"flags": flags, "sessions": [S1], "since": None, "until": None})
    assert stored(corpus, resolved_scope=resolved) is None


def test_a_model_name_with_a_control_character_is_not_a_valid_retro(corpus: Corpus) -> None:
    assert stored(corpus, model="m\x1b[31m") is None


def test_an_artifact_over_the_bound_is_refused_before_it_is_parsed(corpus: Corpus) -> None:
    assert MAX_ARTIFACT_BYTES == 8 * 1024 * 1024
    valid = envelope_with()
    pad = " " * (MAX_ARTIFACT_BYTES - len(valid) + 1)  # whitespace is valid JSON: only size fails
    assert stored(corpus, artifact=valid + pad) is None
    exact = valid + " " * (MAX_ARTIFACT_BYTES - len(valid))
    reader = connect(corpus.home)
    reader.execute("UPDATE retro_artifacts SET artifact = ?", (exact,))
    reader.commit()
    assert load_retro(reader, RETRO) is not None
    reader.close()


def test_the_writer_stores_repeated_labels_once_in_typed_order(
    corpus: Corpus, conn: sqlite3.Connection
) -> None:
    flags = {
        "labels": ["a=b", "c=d", "a=b"],
        "project": None,
        "since": None,
        "until": None,
        "kind": None,
        "outcome": None,
    }
    assert scope_text(flags) == "label=a=b label=c=d"
    add_retro(
        corpus.home,
        KEY_A,
        scope=scope_text(flags),
        resolved_scope=encode_resolved_scope(flags, [S1], None, None),
    )
    reader = connect(corpus.home)
    try:
        loaded = load_retro(reader, RETRO)
    finally:
        reader.close()
    assert loaded is not None and loaded["scope"]["labels"] == ["a=b", "c=d"]
