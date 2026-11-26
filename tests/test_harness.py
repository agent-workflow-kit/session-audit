"""Harness enrichment: strict parsing, the snapshot upsert, the capture of one ingest, ``--session``
and the query-time join, against the fake claude-session (every document it prints is validated
against the pinned schemas unless a test says it is hostile)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from session_audit import harness
from session_audit.cli import main
from session_audit.errors import CliError
from session_audit.gateway import CallResult
from session_audit.lease import IngestTarget, acquire
from session_audit.store import open_store

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    list_doc,
    list_item,
    status_doc,
    step,
    version_step,
)
from .golden import assert_golden
from .ingesthelp import (
    S1,
    S2,
    S3,
    Corpus,
    connect,
    ingest,
    messages,
    meta,
    scalar,
    state_dir_of,
)
from .schemas import validate
from .storehelp import ClockedGateway

CS1, CS2, CS3 = "cs-aaaaaaab", "cs-aaaaaaac", "cs-aaaaaaad"
UNKNOWN_UUID = "99999999-9999-4999-8999-999999999999"


# --- strict parsing ---------------------------------------------------------------------------


def dumps(doc: object) -> str:
    return json.dumps(doc)


def parse(doc: object, expected: str = CS1) -> harness.HarnessRun:
    return harness.parse_status(dumps(doc), expected)


def test_a_valid_status_document_becomes_a_run() -> None:
    row = parse(
        status_doc(
            CS1,
            updated_at="2026-01-02T03:04:05.123456789+02:00",
            launch_kind="audit",
            labels=["team=x", "a=b", "team=x"],
            claude_session_ids=[S2, S1, S1],
            outcome="failed",
            cost_usd=1.25,
            num_turns=7,
            skill="plug:skill",
            repo="host/org/repo",
        )
    )
    assert row.csession_id == CS1 and row.kind == "audit" and row.run == 1
    assert row.labels == ("a=b", "team=x", "team=x")  # a multiset, sorted
    assert row.claude_session_ids == (S1, S2)  # sorted, distinct
    assert (row.outcome, row.end_reason, row.cost_usd, row.turns) == ("failed", "normal", 1.25, 7)
    assert (row.skill, row.repo, row.final) == ("plug:skill", "host/org/repo", True)
    assert row.updated_text == "2026-01-02T01:04:05.123456Z"  # UTC, microseconds, Z


def test_a_live_run_is_not_final() -> None:
    row = parse(status_doc(outcome=None, cost_usd=None, num_turns=None))
    assert row.final is False and row.outcome is None and row.cost_usd is None


def test_claude_session_ids_keep_only_uuids_and_at_most_64() -> None:
    many = [f"{i:08x}-1111-4111-8111-111111111111" for i in range(100)]
    doc = status_doc(CS1, claude_session_ids=[*many, "not-a-uuid", S1.upper()])
    row = parse(doc)
    assert row.claude_session_ids == tuple(sorted({*many, S1.upper()} - {S1.upper()}))[:64]
    assert len(row.claude_session_ids) == 64 and "not-a-uuid" not in row.claude_session_ids


def test_at_most_64_labels_sorted_before_the_cut() -> None:
    labels = [f"k{i:03d}=v" for i in range(100)]
    row = parse(status_doc(labels=list(reversed(labels))))
    assert row.labels == tuple(labels[:64])


def test_labels_fit_their_column_even_at_the_grammar_maximum() -> None:
    label = "a" * 64 + "=" + "b" * 256  # 321 characters: 64 of them would be over 16384 as JSON
    row = parse(status_doc(labels=[label[:-i] if i else label for i in range(64)]))
    assert len(harness.dump_list(row.labels)) <= harness.LABELS_COLUMN_MAX
    assert 0 < len(row.labels) < 64


@pytest.mark.parametrize(
    "label",
    ["Upper=x", "nokey", "=x", "k=has space", "k=" + "v" * 257, "k=é", "k=a\nb", "", 7, None],
)
def test_a_label_out_of_grammar_makes_the_document_invalid(label: object) -> None:
    with pytest.raises(harness.Invalid):
        parse(status_doc(labels=[label]), CS1)  # type: ignore[list-item]


def test_labels_split_at_the_first_equals_are_stored_whole() -> None:
    assert parse(status_doc(labels=["k=a=b"])).labels == ("k=a=b",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(csession_id="cs-zzzzzzzz"),  # another session
        lambda d: d.update(csession_id=7),
        lambda d: d.update(updated_at="yesterday"),
        lambda d: d.update(updated_at=None),
        lambda d: d.update(updated_at="2026-02-30T00:00:00Z"),
        lambda d: d.update(updated_at="2026-01-01t00:00:00z"),
        lambda d: d.update(labels="k=v"),
        lambda d: d.update(labels={"k": "v"}),
        lambda d: d.update(claude_session_ids="x"),
        lambda d: d.update(claude_session_ids=["x"]),
        lambda d: d.update(launch_kind="Bad Kind"),
        lambda d: d.update(launch_kind=7),
        lambda d: d.update(run=[]),
        lambda d: d["run"].update(outcome="DONE"),
        lambda d: d["run"].update(outcome=["done"]),
        lambda d: d["run"].update(end_reason="a b"),
        lambda d: d["run"].update(cost_usd="1"),
        lambda d: d["run"].update(cost_usd=-1),
        lambda d: d["run"].update(cost_usd=True),
        lambda d: d["run"].update(cost_usd=1e999),
        lambda d: d["run"].update(num_turns=-1),
        lambda d: d["run"].update(num_turns=2**63),
        lambda d: d["run"].update(num_turns=1.5),
        lambda d: d["run"].update(run=True),
        lambda d: d["run"].update(skill=5),
        lambda d: d.update(repo=5),
    ],
)
def test_a_field_of_the_wrong_type_or_grammar_makes_the_document_invalid(mutate: Any) -> None:
    doc = status_doc(CS1)
    mutate(doc)
    with pytest.raises(harness.Invalid):
        parse(doc)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "null",
        "7",
        '"cs"',
        "{",
        '{"csession_id": "cs-aaaaaaab", "csession_id": "cs-aaaaaaab"}',  # duplicate key
        '{"csession_id": "cs-aaaaaaab", "run": {"cost_usd": NaN}}',
        '{"csession_id": "cs-aaaaaaab", "run": {"cost_usd": Infinity}}',
        "[" * 200_000,  # nested far past the recursion limit
        '{"a":' * 200_000,
        "\x00",
        "9" * 100_000,  # an integer longer than the interpreter allows
    ],
)
def test_hostile_text_is_invalid_never_an_exception(text: str) -> None:
    with pytest.raises(harness.Invalid):
        harness.parse_status(text, CS1)
    with pytest.raises(harness.Invalid):
        harness.parse_list(text)


def test_a_duplicate_key_below_the_top_level_is_refused_too() -> None:
    text = dumps(status_doc(CS1)).replace('"run": {', '"run": {"run": 9, ', 1)
    with pytest.raises(harness.Invalid):
        harness.parse_status(text, CS1)


def test_strings_are_cleaned_and_capped() -> None:
    doc = status_doc(CS1, skill="s" * 1000, repo="r" * 1000)
    row = parse(doc)
    assert len(row.skill or "") == harness.NAME_CAP and len(row.repo or "") == harness.REPO_CAP
    text = dumps(status_doc(CS1, skill="X")).replace('"X"', '"a\\ud800b\\u0000c\\u202e"')
    cleaned = harness.parse_status(text, CS1).skill
    assert cleaned is not None and "\ud800" not in cleaned
    cleaned.encode("utf-8")  # always encodable
    assert parse(status_doc(CS1, skill="")).skill is None


def test_a_list_keeps_the_newest_entry_per_id_and_counts_the_malformed() -> None:
    doc = list_doc(
        [
            list_item(CS1, updated_at="2026-01-01T00:00:00Z"),
            list_item(CS1, updated_at="2026-01-03T00:00:00Z"),
            list_item(CS2, updated_at="2026-01-02T00:00:00+01:00"),
        ]
    )
    doc["sessions"] += [
        7,
        "x",
        {},
        {"csession_id": "cs-bad", "updated_at": "2026-01-01T00:00:00Z"},
        {"csession_id": "cs-aaaaaaae", "updated_at": "soon"},
        {"csession_id": "-rf", "updated_at": "2026-01-01T00:00:00Z"},
        {"csession_id": "cs-aaaaaaaf\n", "updated_at": "2026-01-01T00:00:00Z"},
    ]
    entries, bad = harness.parse_list(dumps(doc))
    assert {e.csession_id: e.updated_at.isoformat() for e in entries} == {
        CS1: "2026-01-03T00:00:00+00:00",
        CS2: "2026-01-01T23:00:00+00:00",
    }
    assert bad == 7


@pytest.mark.parametrize("doc", [{}, {"sessions": None}, {"sessions": {}}, []])
def test_a_list_without_a_sessions_array_is_invalid(doc: object) -> None:
    with pytest.raises(harness.Invalid):
        harness.parse_list(dumps(doc))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"),
        ("2026-01-01T00:00:00.5Z", "2026-01-01T00:00:00.500000+00:00"),
        ("2026-01-01T00:00:00.123456789Z", "2026-01-01T00:00:00.123456+00:00"),
        ("2026-01-01T00:00:00+05:30", "2025-12-31T18:30:00+00:00"),
        ("2026-01-01T00:00:00-05:30", "2026-01-01T05:30:00+00:00"),
        ("0001-01-01T00:00:00+01:00", None),  # before year 1 in UTC
        ("9999-12-31T23:59:59-01:00", None),  # after year 9999 in UTC
        ("2026-01-01T00:00:00+24:00", None),
        ("2026-01-01T00:00:00", None),  # no zone
        ("2026-01-01 00:00:00Z", None),
        ("2026-01-01T00:00:60Z", None),  # no leap second
        ("2026-1-1T00:00:00Z", None),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_parse_instant(text: object, expected: str | None) -> None:
    moment = harness.parse_instant(text)
    assert (moment.isoformat() if moment else None) == expected


# --- the upsert -------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):  # noqa: ANN201
    with open_store(ClockedGateway(), str(tmp_path / "state"), busy_timeout_ms=200) as opened:
        yield opened


def stored(store_: Any) -> list[tuple[Any, ...]]:
    return list(store_.conn.execute("SELECT * FROM harness_runs ORDER BY csession_id"))


def test_upsert_stores_a_row_and_replaces_it_only_when_newer(store: Any) -> None:
    first = parse(status_doc(CS1, updated_at="2026-01-01T00:00:00Z", labels=["a=1"], outcome=None))
    assert harness.store_row(store, first) is True
    [row] = stored(store)
    assert row[:2] == (CS1, "2026-01-01T00:00:00Z") and row[-1] == 0
    same = parse(status_doc(CS1, updated_at="2026-01-01T00:00:00Z", labels=["a=2"]))
    older = parse(status_doc(CS1, updated_at="2025-12-31T23:59:59Z", labels=["a=3"]))
    equal_other_zone = parse(
        status_doc(CS1, updated_at="2026-01-01T01:00:00+01:00", labels=["a=4"])
    )
    assert not any(harness.store_row(store, r) for r in (same, older, equal_other_zone))
    assert json.loads(stored(store)[0][4]) == ["a=1"]
    newer = parse(
        status_doc(CS1, updated_at="2026-01-01T00:00:00.000001Z", labels=["a=5"], outcome="done")
    )
    assert harness.store_row(store, newer) is True
    [row] = stored(store)
    assert (
        json.loads(row[4]) == ["a=5"] and row[-1] == 1 and row[1] == "2026-01-01T00:00:00.000001Z"
    )


def test_a_stored_value_that_is_not_a_timestamp_ranks_lowest(store: Any) -> None:
    store.conn.execute(
        "INSERT INTO harness_runs (csession_id, updated_at) VALUES (?, ?)", (CS1, "garbage")
    )
    assert harness.store_row(store, parse(status_doc(CS1, updated_at="1970-01-01T00:00:00Z")))
    assert stored(store)[0][1] == "1970-01-01T00:00:00Z"


def test_stored_values_are_json_arrays_and_iso_text(store: Any) -> None:
    harness.store_row(
        store, parse(status_doc(CS1, labels=["b=2", "a=1"], claude_session_ids=[S2, S1]))
    )
    [row] = stored(store)
    assert json.loads(row[2]) == [S1, S2] and json.loads(row[4]) == ["a=1", "b=2"]
    assert datetime.fromisoformat(row[1]).tzinfo is not None  # what prune parses
    assert row[1].endswith("Z")


def test_a_busy_store_is_a_retryable_store_error(store: Any, tmp_path: Path) -> None:
    other = sqlite3.connect(tmp_path / "state" / "state.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(CliError) as caught:
            harness.store_row(store, parse(status_doc(CS1)))
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert caught.value.exit_code == 12
    assert caught.value.details["reason"] == "store_unwritable"
    assert caught.value.details["retryable"] is True


def test_the_window_is_whole_days_of_retention_sessions() -> None:
    from datetime import timedelta

    assert harness.window_days(timedelta(days=400)) == 400
    assert harness.window_days(timedelta(hours=1)) == 1
    assert harness.window_days(timedelta(days=2, seconds=1)) == 3
    assert harness.CAP_PER_RUN == 500


# --- ingest with the fake claude-session ------------------------------------------------------


class World:
    """The claude-session sessions the fake knows: ``publish`` scripts ``list`` and one ``status``
    step per id."""

    def __init__(self, fake: FakeClaudeSession) -> None:
        self.fake = fake
        self.sessions: dict[str, dict[str, Any]] = {}
        self.forgotten: set[str] = set()

    def add(self, csid: str, claude_ids: list[str] | None = None, **fields: Any) -> World:
        self.sessions[csid] = {"claude_ids": claude_ids or [], **fields}
        return self

    def publish(self) -> None:
        items = []
        for csid, fields in self.sessions.items():
            kwargs = {k: v for k, v in fields.items() if k != "claude_ids"}
            list_kwargs = {
                k: v for k, v in kwargs.items() if k in ("updated_at", "labels", "outcome", "repo")
            }
            if "kind" in kwargs:
                list_kwargs["launch_kind"] = kwargs["kind"]
            items.append(list_item(csid, **list_kwargs))
            status_kwargs = dict(kwargs)
            if "kind" in status_kwargs:
                status_kwargs["launch_kind"] = status_kwargs.pop("kind")
            if csid in self.forgotten:
                self.fake.script(
                    f"status {csid}", step(error_doc("not_found", "no such session"), exit=10)
                )
            else:
                self.fake.script(
                    f"status {csid}",
                    step(
                        status_doc(csid, claude_session_ids=fields["claude_ids"], **status_kwargs)
                    ),
                )
        self.fake.script("list", step(list_doc(items)))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _private_environment: Path):  # noqa: ANN201
    directory = tmp_path / "fake-cs"
    directory.mkdir()
    one = FakeClaudeSession(directory)
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_SCENARIO", str(one.scenario_path))
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_LOG", str(one.log_path))
    one.script("--version", version_step("0.45.3"))
    Corpus(_private_environment)  # the transcript root exists (an empty one is still readable)
    configure(_private_environment, one.program)
    yield one
    one.assert_outputs_valid()


def configure(home: Path, program: Path | str, enabled: str = "auto") -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(
        f'harness:\n  enabled: "{enabled}"\n  claude_session_cmd: {program}\n', encoding="utf-8"
    )
    path.chmod(0o600)


@pytest.fixture
def world(fake: FakeClaudeSession) -> World:
    return World(fake)


@pytest.fixture
def gw() -> ClockedGateway:
    return ClockedGateway()


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


def keys(fake: FakeClaudeSession) -> list[str]:
    return [c["key"] for c in fake.calls()]


def snapshots(home: Path) -> dict[str, tuple[Any, ...]]:
    conn = connect(home)
    try:
        return {r[0]: r for r in conn.execute("SELECT * FROM harness_runs")}
    finally:
        conn.close()


def test_ingest_captures_after_the_file_reads_and_reports_the_counts(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1], labels=["team=x"], kind="audit").add(CS2, [S2]).publish()
    run = ingest(capsys, gw)
    assert run.code == 0
    validate(run.doc, "session-audit/cli/ingest.json")
    assert run.doc["harness"] == {"enabled": True, "matched": 2, "captured": 2, "errors": 0}
    assert keys(fake) == ["--version", "list", "status cs-aaaaaaac", "status cs-aaaaaaab"] or keys(
        fake
    ) == ["--version", "list", "status cs-aaaaaaab", "status cs-aaaaaaac"]
    assert fake.calls()[1]["argv"] == ["list", "--json", "--since", "400d"]
    assert fake.calls()[2]["argv"][2:] == ["--json"]
    assert set(snapshots(_private_environment)) == {CS1, CS2}
    assert meta(_private_environment)["last_complete_ingest_at"]
    assert_golden(run.doc, "ingest-harness")


def test_calls_are_newest_first_by_updated_at_then_id_descending(
    gw: ClockedGateway,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.add(CS1, updated_at="2026-01-01T00:00:00Z")
    world.add(CS2, updated_at="2026-01-03T00:00:00Z")
    world.add(CS3, updated_at="2026-01-03T00:00:00Z").publish()
    ingest(capsys, gw)
    assert [c["argv"][1] for c in fake.calls() if c["key"].startswith("status")] == [
        CS3,
        CS2,
        CS1,
    ]


def test_a_second_run_asks_status_only_for_a_changed_updated_at(
    gw: ClockedGateway,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    world.add(CS1, [S1], updated_at="2026-01-01T00:00:00Z", outcome=None, labels=["a=1"])
    world.add(CS2, [S2], updated_at="2026-01-01T00:00:00Z").publish()
    ingest(capsys, gw)
    assert snapshots(_private_environment)[CS1][-1] == 0  # live: not final
    again = ingest(capsys, gw)
    assert again.doc["harness"] == {"enabled": True, "matched": 0, "captured": 0, "errors": 0}
    assert sum(k.startswith("status") for k in keys(fake)) == 2  # nothing more
    world.add(CS1, [S1, S3], updated_at="2026-01-02T00:00:00Z", outcome="done", labels=["a=2"])
    world.publish()
    third = ingest(capsys, gw)
    assert third.doc["harness"] == {"enabled": True, "matched": 1, "captured": 1, "errors": 0}
    row = snapshots(_private_environment)[CS1]
    assert row[1] == "2026-01-02T00:00:00Z" and json.loads(row[2]) == sorted([S1, S3])
    assert json.loads(row[4]) == ["a=2"] and row[-1] == 1


def test_the_per_run_cap_leaves_the_rest_for_a_later_run(
    gw: ClockedGateway,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _private_environment: Path,
) -> None:
    monkeypatch.setattr(harness, "CAP_PER_RUN", 3)
    ids = [f"cs-aaaaaaa{c}" for c in "bcdefgh"]  # 7 sessions, oldest first
    for number, csid in enumerate(ids):
        world.add(csid, updated_at=f"2026-01-0{number + 1}T00:00:00Z")
    world.publish()
    first = ingest(capsys, gw).doc["harness"]
    assert first == {"enabled": True, "matched": 7, "captured": 3, "errors": 0}
    assert set(snapshots(_private_environment)) == set(ids[-3:])  # the newest three
    second = ingest(capsys, gw).doc["harness"]
    assert second["matched"] == 4 and second["captured"] == 3
    third = ingest(capsys, gw).doc["harness"]
    assert third["matched"] == 1 and third["captured"] == 1
    assert set(snapshots(_private_environment)) == set(ids)
    assert ingest(capsys, gw).doc["harness"]["matched"] == 0


def test_a_session_forgotten_since_the_list_is_skipped_without_an_error(
    gw: ClockedGateway,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    world.add(CS1, [S1]).add(CS2, [S2])
    world.forgotten.add(CS1)
    world.publish()
    run = ingest(capsys, gw)
    assert run.doc["harness"] == {"enabled": True, "matched": 2, "captured": 1, "errors": 0}
    assert set(snapshots(_private_environment)) == {CS2}


@pytest.mark.parametrize(
    "failure",
    [
        step(error_doc("failed", "boom"), exit=12),
        step(error_doc("internal", "boom"), exit=1),
        step("", exit=0, unvalidated=True),
        step("not json", unvalidated=True),
        step(["not", "an", "object"], unvalidated=True),
        step(status_doc(CS1, labels=["Bad Label"]), unvalidated=True),
        step(status_doc("cs-zzzzzzzz"), unvalidated=True),
        step({**status_doc(CS1), "updated_at": "never"}, unvalidated=True),
    ],
)
def test_a_failing_or_hostile_status_counts_an_error_and_the_others_go_on(
    gw: ClockedGateway,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    failure: dict[str, Any],
) -> None:
    world.add(CS1, [S1], updated_at="2026-01-03T00:00:00Z")
    world.add(CS2, [S2], updated_at="2026-01-02T00:00:00Z").publish()
    fake.script(f"status {CS1}", failure)
    run = ingest(capsys, gw)
    assert run.code == 0
    assert run.doc["harness"] == {"enabled": True, "matched": 2, "captured": 1, "errors": 1}
    assert set(snapshots(_private_environment)) == {CS2}


@pytest.mark.parametrize(
    "bad_list",
    [
        step(error_doc("failed", "boom"), exit=12),
        step("", unvalidated=True),
        step("{", unvalidated=True),
        step({"sessions": "x"}, unvalidated=True),
        step("[" * 100_000, unvalidated=True),
        step('{"sessions": [], "sessions": []}', unvalidated=True),
    ],
)
def test_a_failing_or_hostile_list_skips_the_capture_and_counts_one_error(
    gw: ClockedGateway,
    corpus: Corpus,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    bad_list: dict[str, Any],
) -> None:
    corpus.write_main(S1, messages("a", 2))
    fake.script("list", bad_list)
    run = ingest(capsys, gw)
    assert run.code == 0 and run.doc["files"]["new"] == 1
    assert run.doc["harness"] == {"enabled": True, "matched": 0, "captured": 0, "errors": 1}
    assert snapshots(_private_environment) == {}
    assert all(not k.startswith("status") for k in keys(fake))


def test_a_list_over_the_output_cap_is_a_failed_call(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "LIST_OUTPUT_LIMIT", 20_000)
    items = [list_item(f"cs-{n:08d}".translate(str.maketrans("018", "abc"))) for n in range(100)]
    fake.script("list", step(list_doc(items), unvalidated=True))
    run = ingest(capsys, gw)
    assert run.doc["harness"] == {"enabled": True, "matched": 0, "captured": 0, "errors": 1}
    assert all(not k.startswith("status") for k in keys(fake))


def test_a_huge_hostile_list_never_crashes_ingest(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "CAP_PER_RUN", 0)
    junk = [{"csession_id": "x" * 50, "updated_at": 5} for _ in range(50_000)]
    fake.script("list", step({"sessions": junk}, unvalidated=True))
    run = ingest(capsys, gw)
    assert run.code == 0
    assert run.doc["harness"] == {"enabled": True, "matched": 0, "captured": 0, "errors": 50_000}


def test_hostile_status_documents_store_nothing_unsafe(
    gw: ClockedGateway,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    world.add(CS1, [S1]).publish()
    doc = status_doc(CS1, claude_session_ids=[S1], skill="x")
    text = json.dumps(doc).replace('"skill": "x"', '"skill": "a\\ud800\\u001b[31m\\u0000b"')
    huge_ids = [
        {
            **doc["claude_session_ids"][0],
            "claude_session_id": f"{n:08x}-1111-4111-8111-111111111111",
        }
        for n in range(5000)
    ]
    huge = {**doc, "claude_session_ids": [*doc["claude_session_ids"], *huge_ids]}
    for hostile in (json.loads(text), huge):
        fake.script(f"status {CS1}", step(hostile, unvalidated=True))
        conn = (
            connect(_private_environment)
            if (state_dir_of(_private_environment) / "state.db").exists()
            else None
        )
        if conn is not None:
            conn.execute("DELETE FROM harness_runs")
            conn.commit()
            conn.close()
        run = ingest(capsys, gw)
        assert run.code == 0 and run.doc["harness"]["errors"] == 0
        row = snapshots(_private_environment)[CS1]
        assert len(json.loads(row[2])) <= 64
        (row[10] or "").encode("utf-8")


def test_a_slow_claude_session_is_a_failed_call_not_a_result(
    corpus: Corpus,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    class Quick(ClockedGateway):
        calls: list[tuple[list[str], float, int]] = []

        def run(self, argv: Any, *, timeout: float, **kwargs: Any) -> CallResult:
            Quick.calls.append((list(argv), timeout, kwargs.get("stdout_limit", 0)))
            return super().run(argv, timeout=min(timeout, 0.5), **kwargs)

    Quick.calls = []
    world.add(CS1, [S1]).add(CS2, [S2]).publish()
    fake.script(f"status {CS1}", step(status_doc(CS1), sleep=5))  # killed at 0.5 s
    run = ingest(capsys, Quick())
    assert run.code == 0
    assert run.doc["harness"] == {"enabled": True, "matched": 2, "captured": 1, "errors": 1}
    versions = [c for c in Quick.calls if c[0][1:] == ["--version"]]
    lists = [c for c in Quick.calls if c[0][1] == "list"]
    statuses = [c for c in Quick.calls if c[0][1] == "status"]
    assert [c[1] for c in versions] == [10.0]
    assert [(c[1], c[2]) for c in lists] == [(60.0, 16 * 1024 * 1024)]
    assert {(c[1], c[2]) for c in statuses} == {(60.0, 1024 * 1024)}


def test_a_hung_list_is_a_failed_call(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Quick(ClockedGateway):
        def run(self, argv: Any, *, timeout: float, **kwargs: Any) -> CallResult:
            return super().run(argv, timeout=min(timeout, 0.3), **kwargs)

    fake.script("list", step(list_doc(), sleep=5, ignore_term=True))
    gateway = Quick()
    gateway._kill_grace = 0.2  # type: ignore[attr-defined]
    run = ingest(capsys, gateway)
    assert run.code == 0
    assert run.doc["harness"] == {"enabled": True, "matched": 0, "captured": 0, "errors": 1}


# --- when claude-session is used --------------------------------------------------------------


def test_no_harness_and_off_never_call_claude_session(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1]).publish()
    run = ingest(capsys, gw, "--no-harness")
    assert run.doc["harness"] == {"enabled": False, "matched": 0, "captured": 0, "errors": 0}
    configure(_private_environment, fake.program, "off")
    corpus.append_main(S1, messages("b", 1))
    assert ingest(capsys, gw).doc["harness"]["enabled"] is False
    assert fake.calls() == [] and snapshots(_private_environment) == {}


def test_a_dry_run_never_calls_claude_session(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1]).publish()
    run = ingest(capsys, gw, "--dry-run")
    assert run.code == 0 and run.doc["harness"]["enabled"] is False
    assert fake.calls() == []
    configure(_private_environment, fake.program, "on")
    assert ingest(capsys, gw, "--dry-run").code == 0  # not even a missing tool matters here


def test_a_missing_claude_session_with_auto_is_not_an_error(
    gw: ClockedGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    _private_environment: Path,
) -> None:
    configure(_private_environment, tmp_path / "no-such-program")
    corpus.write_main(S1, messages("a", 2))
    run = ingest(capsys, gw)
    assert run.code == 0 and run.doc["warnings"] == []
    assert run.doc["harness"] == {"enabled": False, "matched": 0, "captured": 0, "errors": 0}


def test_a_missing_claude_session_with_on_is_exit_12_before_anything_is_read(
    gw: ClockedGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    _private_environment: Path,
) -> None:
    configure(_private_environment, tmp_path / "no-such-program", "on")
    corpus.write_main(S1, messages("a", 2))
    run = ingest(capsys, gw)
    assert run.code == 12
    assert run.doc["error"]["details"] == {"reason": "claude_session_missing", "retryable": False}
    assert scalar(_private_environment, "SELECT count(*) FROM files") == 0


@pytest.mark.parametrize(
    ("printed", "reason"),
    [("0.45.1", "claude_session_too_old"), ("nonsense", "claude_session_too_old")],
)
def test_an_old_or_unparsable_claude_session_with_on_is_too_old(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    printed: str,
    reason: str,
) -> None:
    configure(_private_environment, fake.program, "on")
    fake.script("--version", step(f"claude-session {printed}\n"))
    run = ingest(capsys, gw)
    assert run.code == 12 and run.doc["error"]["details"]["reason"] == reason
    assert all(k == "--version" for k in keys(fake))


def test_a_failed_version_call_with_on_is_claude_session_error(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    configure(_private_environment, fake.program, "on")
    fake.script("--version", step("boom\n", exit=3))
    run = ingest(capsys, gw)
    assert run.code == 12 and run.doc["error"]["details"]["reason"] == "claude_session_error"


@pytest.mark.parametrize("printed", ["claude-session 0.45.1\n", "nonsense\n"])
def test_an_unusable_claude_session_with_auto_warns_and_is_not_used(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    printed: str,
) -> None:
    fake.script("--version", step(printed))
    run = ingest(capsys, gw)
    assert run.code == 0
    assert run.doc["harness"]["enabled"] is False
    assert run.doc["warnings"] and run.doc["warnings"][0].startswith("claude_session_unusable")
    assert keys(fake) == ["--version"]


def test_an_old_version_that_compares_higher_as_text_is_still_checked_as_numbers(
    gw: ClockedGateway, fake: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.script("--version", version_step("0.100.0"))
    fake.script("list", step(list_doc()))
    assert ingest(capsys, gw).doc["harness"]["enabled"] is True


def test_an_already_running_ingest_reports_no_harness(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    directory = str(state_dir_of(_private_environment))
    with open_store(gw, directory) as holder:
        lease = acquire(holder, IngestTarget())
        assert lease is not None
        try:
            run = ingest(capsys, gw)
        finally:
            lease.release()
    assert run.doc["already_running"] is True
    assert run.doc["harness"] == {"enabled": False, "matched": 0, "captured": 0, "errors": 0}
    assert all(not k.startswith(("list", "status")) for k in keys(fake))


# --- ingest --session -------------------------------------------------------------------------


def test_session_writes_the_snapshot_and_reads_only_its_transcripts(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    corpus.write_main(S2, messages("b", 2), project="proj-b")
    world.add(CS1, [S1], labels=["owner=x"]).publish()
    run = ingest(capsys, gw, "--session", CS1)
    assert run.code == 0
    validate(run.doc, "session-audit/cli/ingest.json")
    assert run.doc["harness"] == {"enabled": True, "matched": 1, "captured": 1, "errors": 0}
    assert run.doc["files_deferred"] is False and run.doc["complete"] is True
    assert run.doc["files"]["new"] == 1 and run.doc["files"]["seen"] == 1
    assert run.doc["sessions"] == {"new": 1, "updated": 0}
    assert scalar(_private_environment, "SELECT count(*) FROM sessions") == 1
    assert keys(fake) == ["--version", f"status {CS1}"]
    assert fake.calls()[1]["argv"] == ["status", CS1, "--json"]
    assert "last_complete_ingest_at" not in meta(_private_environment)
    assert set(snapshots(_private_environment)) == {CS1}


def test_session_with_a_held_lease_writes_the_snapshot_and_defers_the_files(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1]).publish()
    ingest(capsys, gw, "--no-harness")  # creates the store
    with open_store(gw, str(state_dir_of(_private_environment))) as holder:
        lease = acquire(holder, IngestTarget())
        assert lease is not None
        try:
            run = ingest(capsys, gw, "--session", CS1)
        finally:
            lease.release()
    assert run.code == 0
    validate(run.doc, "session-audit/cli/ingest.json")
    assert run.doc["files_deferred"] is True and run.doc["already_running"] is False
    assert run.doc["complete"] is False
    assert run.doc["harness"]["captured"] == 1
    assert set(snapshots(_private_environment)) == {CS1}  # written without the lease


def test_session_with_no_recorded_transcripts_reads_no_file(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, []).publish()
    run = ingest(capsys, gw, "--session", CS1)
    assert run.code == 0 and run.doc["files"]["seen"] == 0
    assert scalar(_private_environment, "SELECT count(*) FROM files") == 0


def test_session_refreshes_a_snapshot_only_when_newer(
    gw: ClockedGateway,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    world.add(CS1, [S1], updated_at="2026-01-02T00:00:00Z", labels=["a=2"]).publish()
    assert ingest(capsys, gw, "--session", CS1).doc["harness"]["captured"] == 1
    world.add(CS1, [S1], updated_at="2026-01-01T00:00:00Z", labels=["a=1"]).publish()
    assert ingest(capsys, gw, "--session", CS1).doc["harness"]["captured"] == 0
    assert json.loads(snapshots(_private_environment)[CS1][4]) == ["a=2"]


def test_session_unknown_to_claude_session_is_exit_10(
    gw: ClockedGateway,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    world.add(CS1, [S1])
    world.forgotten.add(CS1)
    world.publish()
    run = ingest(capsys, gw, "--session", CS1)
    assert run.code == 10
    assert run.doc["error"]["code"] == "not_found" and run.doc["error"]["details"]["id"] == CS1
    assert snapshots(_private_environment) == {}


@pytest.mark.parametrize(
    "failure",
    [
        step(error_doc("failed", "boom"), exit=12),
        step("junk", unvalidated=True),
        step(status_doc(CS1, labels=["Bad Label"]), unvalidated=True),
    ],
)
def test_session_with_a_failing_status_is_claude_session_error(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    failure: dict[str, Any],
) -> None:
    fake.script("status", failure)
    run = ingest(capsys, gw, "--session", CS1)
    assert run.code == 12 and run.doc["error"]["details"]["reason"] == "claude_session_error"


def test_session_without_claude_session_is_exit_12_missing(
    gw: ClockedGateway,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    _private_environment: Path,
) -> None:
    configure(_private_environment, tmp_path / "nope")
    run = ingest(capsys, gw, "--session", CS1)
    assert run.code == 12 and run.doc["error"]["details"]["reason"] == "claude_session_missing"


def test_session_grammar_and_the_flags_it_cannot_combine_with(
    gw: ClockedGateway,
    fake: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    run = ingest(capsys, gw, "--session", "cs-ABC")
    assert run.code == 2 and run.doc["error"]["details"]["field"] == "--session"
    uuid = ingest(capsys, gw, "--session", S1)
    assert uuid.code == 2 and "Claude Code session id" in uuid.err
    both = ingest(capsys, gw, "--session", CS1, "--no-harness")
    assert both.code == 2 and both.doc["error"]["details"]["field"] == "--no-harness"
    dry = ingest(capsys, gw, "--session", CS1, "--dry-run")
    assert dry.code == 2 and dry.doc["error"]["details"]["field"] == "--dry-run"
    assert fake.calls() == []
    configure(_private_environment, fake.program, "off")
    off = ingest(capsys, gw, "--session", CS1)
    assert off.code == 2 and off.doc["error"]["details"]["field"] == "harness.enabled"
    assert fake.calls() == []


# --- the join at query time -------------------------------------------------------------------


def run_cli(capsys: pytest.CaptureFixture[str], gw: ClockedGateway, *argv: str) -> Any:
    capsys.readouterr()
    code = main([*argv, "--json"], gateway=gw, stdin_is_tty=False)
    assert code == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def seeded(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus.write_main(S1, messages("a", 2), project="proj-a")
    corpus.write_main(S2, messages("b", 3), project="proj-b")
    corpus.write_main(S3, messages("c", 4), project="proj-c")
    world.add(CS1, [S1], labels=["team=x", "owner=self-improve"], kind="improve", outcome="done")
    world.add(CS2, [S2], labels=["team=x", "k=a=b"], kind="review", outcome="failed", cost_usd=2.5)
    world.publish()
    assert ingest(capsys, gw).code == 0


def test_the_join_filters_and_the_default_exclusion_work_end_to_end(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded(gw, corpus, world, capsys)
    everything = run_cli(capsys, gw, "sessions", "--include-self-improve")
    by_id = {s["session_id"]: s["harness"] for s in everything["sessions"]}
    assert by_id[S1]["csession_id"] == CS1 and by_id[S1]["kind"] == "improve"
    assert by_id[S2]["labels"] == ["k=a=b", "team=x"] and by_id[S2]["outcome"] == "failed"
    assert by_id[S3] is None  # no recorded id: a plain session
    default = run_cli(capsys, gw, "sessions")
    assert {s["session_id"] for s in default["sessions"]} == {S2, S3}  # self-improve left out
    kinds = run_cli(capsys, gw, "sessions", "--kind", "review")
    assert [s["session_id"] for s in kinds["sessions"]] == [S2]
    outcome = run_cli(capsys, gw, "sessions", "--outcome", "done", "--include-self-improve")
    assert [s["session_id"] for s in outcome["sessions"]] == [S1]
    label = run_cli(capsys, gw, "sessions", "--label", "k=a=b")
    assert [s["session_id"] for s in label["sessions"]] == [S2]
    both = run_cli(capsys, gw, "sessions", "--label", "team=x", "--label", "k=a=b")
    assert [s["session_id"] for s in both["sessions"]] == [S2]
    shown = run_cli(capsys, gw, "show", S1[:8])
    assert shown["session"]["harness"]["csession_id"] == CS1  # explicit show still shows it


def test_usage_by_kind_and_label_and_harness_cost_is_never_added(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seeded(gw, corpus, world, capsys)
    by_kind = run_cli(capsys, gw, "usage", "--by", "kind", "--include-self-improve")
    rows = {r["key"][0]: r for r in by_kind["rows"]}
    assert set(rows) == {"improve", "review", None}  # S3 has no snapshot: a null key
    assert rows["review"]["sessions"] == 1
    by_label = run_cli(capsys, gw, "usage", "--by", "label=team", "--include-self-improve")
    assert "x" in {r["key"][0] for r in by_label["rows"]}
    default = run_cli(capsys, gw, "usage", "--by", "kind")
    assert "improve" not in {r["key"][0] for r in default["rows"]}
    blob = json.dumps(by_kind)
    assert "2.5" not in blob  # the harness cost_usd of CS2 is not in any usage figure


def test_a_snapshot_never_matches_by_directory_name_or_working_directory(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from .eventhelp import message

    # Transcripts whose project directory and cwd look like the claude-session run, but whose
    # session ids it never recorded.
    cwd = f"/state/sessions/{CS1}"
    corpus.write_main(
        S3,
        [message("c1", 5, t=1, cwd=cwd) if False else message("c1", 5, t=1)],
        project=f"-state-sessions-{CS1}",
    )
    world.add(CS1, [UNKNOWN_UUID], labels=["owner=x"], repo="host/org/repo").publish()
    assert ingest(capsys, gw).doc["harness"]["captured"] == 1
    sessions = run_cli(capsys, gw, "sessions")["sessions"]
    assert [s["session_id"] for s in sessions] == [S3]
    assert sessions[0]["harness"] is None
    assert run_cli(capsys, gw, "sessions", "--label", "owner=x")["sessions"] == []


def test_a_session_listed_by_several_runs_shows_the_newest_snapshot(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1], updated_at="2026-01-01T00:00:00Z", kind="old")
    world.add(CS2, [S1], updated_at="2026-01-02T00:00:00Z", kind="new").publish()
    ingest(capsys, gw)
    [session] = run_cli(capsys, gw, "sessions")["sessions"]
    assert session["harness"]["csession_id"] == CS2 and session["harness"]["kind"] == "new"


def test_the_transcript_and_the_snapshot_may_arrive_in_either_order(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world.add(CS1, [S1], labels=["team=x"]).publish()
    ingest(capsys, gw)  # the snapshot first: no transcript yet
    assert run_cli(capsys, gw, "sessions")["sessions"] == []
    corpus.write_main(S1, messages("a", 2))
    ingest(capsys, gw)
    [session] = run_cli(capsys, gw, "sessions")["sessions"]
    assert session["harness"]["labels"] == ["team=x"]


def test_a_stored_snapshot_survives_a_run_with_claude_session_gone(
    gw: ClockedGateway,
    corpus: Corpus,
    world: World,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    tmp_path: Path,
) -> None:
    corpus.write_main(S1, messages("a", 2))
    world.add(CS1, [S1]).publish()
    ingest(capsys, gw)
    configure(_private_environment, tmp_path / "gone")
    assert ingest(capsys, gw).doc["harness"]["enabled"] is False
    assert run_cli(capsys, gw, "sessions")["sessions"][0]["harness"]["csession_id"] == CS1
