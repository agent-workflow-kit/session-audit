"""``sessions`` and ``show``: filters, ordering, truncation, harness joins, prefix lookup, output
validation and the read-only store, through the real CLI on a synthetic corpus."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from session_audit import derive
from session_audit.cli import main
from session_audit.sessionquery import (
    USAGE_SQL,
    Filters,
    Snapshot,
    is_self_improve,
    project_matches,
)

from .eventhelp import message, raw, tool_error, tool_use, turn
from .golden import assert_golden
from .ingesthelp import S1, S2, S3, Corpus, TickGateway, ingest, state_dir_of
from .schemas import validate
from .storehelp import ClockedGateway


@pytest.fixture
def gw() -> TickGateway:
    return TickGateway()


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


def with_keys(line: bytes, **keys: object) -> bytes:
    obj = json.loads(line)
    obj.update(keys)
    return raw(obj)


def seed(corpus: Corpus, capsys: pytest.CaptureFixture[str], gw: TickGateway) -> None:
    """S1 /a/b (interactive, one agent), S2 /a/bc (headless, more tokens, later), S3 /a/b/c."""
    corpus.write_main(
        S1,
        [
            with_keys(message("a1", 10, t=0), cwd="/a/b", entrypoint="cli", version="1.0"),
            tool_use("a2", "toolu_1", out=5, t=1),
            tool_error("toolu_1", t=2),
            turn(4000, t=3),
        ],
        project="p1",
    )
    corpus.write_agent(
        S1,
        "x1",
        [message("ag1", 7, t=1, model="model-b", agent="x1")],
        project="p1",
        meta={"agentType": "Explore", "toolUseId": "toolu_9"},
    )
    corpus.write_main(
        S2,
        [
            with_keys(message(f"b{i}", 50, t=100 + i), cwd="/a/bc", entrypoint="sdk-cli")
            for i in range(3)
        ],
        project="p2",
    )
    corpus.write_main(
        S3, [with_keys(message("c1", 20, t=200, model="model-b"), cwd="/a/b/c")], project="p3"
    )
    assert ingest(capsys, gw).code == 0


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.out)  # type: ignore[no-any-return]

    @property
    def ids(self) -> list[str]:
        return [s["session_id"] for s in self.doc["sessions"]]


def run(capsys: pytest.CaptureFixture[str], gw: ClockedGateway, *argv: str) -> Result:
    capsys.readouterr()
    code = main([*argv], gateway=gw, stdin_is_tty=False)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


def sessions(capsys: pytest.CaptureFixture[str], gw: ClockedGateway, *argv: str) -> Result:
    result = run(capsys, gw, "sessions", "--json", *argv)
    if result.code == 0:
        validate(result.doc, "session-audit/cli/sessions.json")
    return result


def add_run(
    home: Path,
    csession: str,
    claude_ids: list[str],
    *,
    labels: list[str],
    kind: str = "audit",
    outcome: str | None = "done",
    updated: str = "2026-01-01T00:00:00Z",
) -> None:
    conn = sqlite3.connect(state_dir_of(home) / "state.db")
    try:
        conn.execute(
            "INSERT INTO harness_runs (csession_id, updated_at, claude_session_ids, kind, labels, "
            "run, outcome, end_reason, cost_usd, turns, skill, repo, final) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, NULL, 0.5, 3, NULL, NULL, 1)",
            (csession, updated, json.dumps(claude_ids), kind, json.dumps(labels), outcome),
        )
        conn.commit()
    finally:
        conn.close()


# --- no store -------------------------------------------------------------------------------------


def test_no_store_gives_an_empty_listing_and_creates_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    result = sessions(capsys, gw)
    assert result.code == 0
    assert result.doc == {
        "sessions": [],
        "truncated": False,
        "store": {"last_ingest_at": None, "last_complete_ingest_at": None},
    }
    assert_golden(result.doc, "sessions-empty")
    assert not state_dir_of(_private_environment).exists()


def test_show_without_a_store_is_not_found_and_creates_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    result = run(capsys, gw, "show", "--json", S1)
    assert result.code == 10
    assert json.loads(result.out)["error"]["code"] == "not_found"
    assert not state_dir_of(_private_environment).exists()


# --- listing, order, limit ------------------------------------------------------------------------


def test_listing_default_sort_is_newest_first_and_matches_the_schema(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = sessions(capsys, gw)
    assert result.ids == [S3, S2, S1]
    assert result.doc["truncated"] is False
    assert result.doc["store"]["last_ingest_at"] is not None
    assert all(s["harness"] is None for s in result.doc["sessions"])
    assert_golden(result.doc, "sessions-three")


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("start", [S3, S2, S1]),
        ("tokens", [S2, S1, S3]),  # S2 150+, S1 10+5+7 incl. subagent, S3 20+...
        ("cost", None),
        ("active", [S1, S2, S3]),  # only S1 has a turn; nulls last, ties by id
    ],
)
def test_sort_orders(
    sort: str,
    expected: list[str] | None,
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(corpus, capsys, gw)
    got = sessions(capsys, gw, "--sort", sort).ids
    if expected is None:
        assert sorted(got) == sorted([S1, S2, S3])
    else:
        assert got == expected


def test_limit_truncates_and_reports_it(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = sessions(capsys, gw, "--limit", "2")
    assert result.ids == [S3, S2]
    assert result.doc["truncated"] is True
    assert_golden(result.doc, "sessions-truncated")
    exact = sessions(capsys, gw, "--limit", "3")
    assert exact.doc["truncated"] is False


def test_equal_values_tie_by_session_id_ascending(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    for sid in (S3, S1, S2):
        corpus.write_main(sid, [message(f"m-{sid[:2]}", 10, t=5)], project=f"p{sid[:1]}")
    ingest(capsys, gw)
    assert sessions(capsys, gw, "--sort", "tokens").ids == [S1, S2, S3]
    assert sessions(capsys, gw, "--sort", "start").ids == [S1, S2, S3]


# --- filters --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("/a/b", {S1, S3}),
        ("/a/b/", {S1, S3}),
        ("/a/b/c", {S3}),
        ("/a/bc", {S2}),
        ("/a", {S1, S2, S3}),
        ("/a/", {S1, S2, S3}),
        ("/a/%", set()),
        ("/a/_", set()),
        ("/a/b\\", set()),
        ("%", set()),
        ("/a/b/c/d", set()),
    ],
)
def test_project_filter_is_a_path_boundary_without_wildcards(
    project: str,
    expected: set[str],
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(corpus, capsys, gw)
    assert set(sessions(capsys, gw, "--project", project).ids) == expected


def test_project_matches_unit() -> None:
    assert project_matches("/a/b", "/a/b")
    assert not project_matches("/a/bc", "/a/b")
    assert not project_matches(None, "/a")
    assert not project_matches("/ab", "/a%")
    assert not project_matches("/a_c", "/a_")
    assert not project_matches("/a\\b", "/a\\")


def test_since_and_until_use_session_times(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    # S1 ends 07:00:03 (turn event at t=3), S2 starts 07:01:40, S3 at 07:03:20.
    assert set(sessions(capsys, gw, "--since", "2021-05-06T07:03:00Z").ids) == {S3}
    assert set(sessions(capsys, gw, "--until", "2021-05-06T07:01:40Z").ids) == {S1}
    assert set(sessions(capsys, gw, "--until", "2021-05-06T07:01:41Z").ids) == {S1, S2}
    assert sessions(capsys, gw, "--since", "2021-05-07").ids == []
    assert set(sessions(capsys, gw, "--since", "2021-05-06", "--until", "2021-05-07").ids) == {
        S1,
        S2,
        S3,
    }


def test_a_date_is_midnight_in_the_configured_time_zone(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    seed(corpus, capsys, gw)
    config = _private_environment / "cfg.yaml"
    config.write_text("time_zone: Pacific/Auckland\n", encoding="utf-8")
    config.chmod(0o600)
    # 2021-05-07 00:00 in Auckland (UTC+12) is 2021-05-06T12:00Z: after every session.
    result = sessions(capsys, gw, "--config", str(config), "--since", "2021-05-07")
    assert result.code == 0
    assert result.ids == []
    inside = sessions(capsys, gw, "--config", str(config), "--until", "2021-05-07")
    assert set(inside.ids) == {S1, S2, S3}


def test_null_times_never_match_a_time_filter(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus.write_main(S1, [raw({"type": "bogus-kind"})], project="p1")
    ingest(capsys, gw)
    assert len(sessions(capsys, gw).ids) == 1
    assert sessions(capsys, gw, "--since", "2000-01-01T00:00:00Z").ids == []
    assert sessions(capsys, gw, "--until", "2100-01-01T00:00:00Z").ids == []


def test_a_duration_ago_uses_the_gateway_clock(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    # The fake clock is 2026-01-01; the corpus is from 2021.
    assert sessions(capsys, gw, "--since", "30d").ids == []
    assert len(sessions(capsys, gw, "--until", "30d").ids) == 3


def test_model_entrypoint_and_combined_filters_and(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    assert set(sessions(capsys, gw, "--model", "model-b").ids) == {S1, S3}  # S1 via its agent
    assert sessions(capsys, gw, "--model", "model").ids == []
    assert sessions(capsys, gw, "--entrypoint", "interactive").ids == [S1]
    assert sessions(capsys, gw, "--entrypoint", "headless").ids == [S2]
    assert sessions(capsys, gw, "--model", "model-b", "--project", "/a/b/c").ids == [S3]
    assert sessions(capsys, gw, "--model", "model-b", "--entrypoint", "headless").ids == []


def test_harness_filters_match_snapshots_only(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    seed(corpus, capsys, gw)
    for flag in (["--label", "team=x"], ["--kind", "audit"], ["--outcome", "done"]):
        assert sessions(capsys, gw, *flag).ids == []  # no snapshots yet
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=["team=x", "a=b"])
    add_run(
        _private_environment, "cs-bbbbbbbb", [S2], labels=["team=y"], kind="review", outcome=None
    )
    assert sessions(capsys, gw, "--label", "team=x").ids == [S1]
    assert sessions(capsys, gw, "--label", "team=x", "--label", "a=b").ids == [S1]
    assert sessions(capsys, gw, "--label", "team=x", "--label", "team=y").ids == []
    assert sessions(capsys, gw, "--label", "team=").ids == []
    assert sessions(capsys, gw, "--kind", "review").ids == [S2]
    assert sessions(capsys, gw, "--outcome", "done").ids == [S1]
    assert sessions(capsys, gw, "--kind", "audit", "--outcome", "done").ids == [S1]
    rows = {s["session_id"]: s["harness"] for s in sessions(capsys, gw).doc["sessions"]}
    assert rows[S3] is None
    assert rows[S1]["csession_id"] == "cs-aaaaaaaa"
    assert rows[S1]["source"] == "harness"
    assert rows[S1]["labels"] == ["team=x", "a=b"]
    assert_golden(sessions(capsys, gw, "--label", "team=x").doc, "sessions-harness")


def test_the_newest_snapshot_wins_then_the_lowest_csession_id(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    seed(corpus, capsys, gw)
    add_run(
        _private_environment, "cs-cccccccc", [S1], labels=["v=old"], updated="2026-01-01T00:00:05Z"
    )
    add_run(
        _private_environment,
        "cs-bbbbbbbb",
        [S1],
        labels=["v=new"],
        updated="2026-01-01T00:00:05.5Z",
    )
    add_run(
        _private_environment,
        "cs-dddddddd",
        [S1],
        labels=["v=tie"],
        updated="2026-01-01T00:00:05.5Z",
    )
    harness = {s["session_id"]: s["harness"] for s in sessions(capsys, gw).doc["sessions"]}[S1]
    assert harness["csession_id"] == "cs-bbbbbbbb"
    assert sessions(capsys, gw, "--label", "v=old").ids == []


def test_self_improve_sessions_are_left_out_by_default(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=["owner=self-improve", "k=v"])
    assert set(sessions(capsys, gw).ids) == {S2, S3}
    assert set(sessions(capsys, gw, "--include-self-improve").ids) == {S1, S2, S3}
    assert sessions(capsys, gw, "--label", "k=v").ids == []
    assert sessions(capsys, gw, "--label", "k=v", "--include-self-improve").ids == [S1]
    # show names a session explicitly: it is shown.
    shown = run(capsys, gw, "show", "--json", S1)
    assert shown.code == 0
    assert shown.doc["session"]["harness"]["labels"] == ["owner=self-improve", "k=v"]


def test_self_improve_predicate() -> None:
    def snap(*labels: str) -> Snapshot:
        return Snapshot(
            "cs-aaaaaaaa",
            "2026-01-01T00:00:00Z",
            "k",
            labels,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    assert is_self_improve(snap("a=b", "owner=self-improve"))
    assert not is_self_improve(snap("owner=self-improve2", "owner=other"))
    assert not is_self_improve(None)
    assert not Filters().snapshot_matches(snap("owner=self-improve"))
    assert Filters(include_self_improve=True).snapshot_matches(snap("owner=self-improve"))
    assert Filters().snapshot_matches(None)


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "0"],
        ["--limit", "1001"],
        ["--sort", "nope"],
        ["--label", "nokey"],
        ["--kind", "Bad Kind"],
        ["--outcome", "X"],
        ["--since", "yesterday"],
        ["--model", "x\x01"],
        ["--entrypoint", "other"],
    ],
)
def test_invalid_arguments_are_usage_errors(
    argv: list[str], gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, gw, "sessions", "--json", *argv).code == 2


# --- human output ----


def test_human_output_sanitizes_untrusted_text(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus.write_main(
        S1,
        [with_keys(message("a1", 10, t=0), cwd="/x/\x1b[31mred‮\nsecond")],
        project="p1",
    )
    ingest(capsys, gw)
    result = run(capsys, gw, "sessions")
    assert result.code == 0
    assert "\x1b" not in result.out and "‮" not in result.out
    assert result.out.count("\n") == 3  # one session line, the count, the freshness
    shown = run(capsys, gw, "show", S1)
    assert shown.code == 0
    assert "\x1b" not in shown.out and "‮" not in shown.out


# --- show ---


def test_show_prints_session_agents_and_usage(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = run(capsys, gw, "show", "--json", S1)
    assert result.code == 0
    doc = result.doc
    validate(doc, "session-audit/cli/show.json")
    assert doc["session"]["session_id"] == S1
    assert doc["truncated"] is False
    assert [a["agent_id"] for a in doc["agents"]] == ["x1"]
    assert doc["agents"][0]["agent_type"] == "Explore"
    rows = {(r["agent_id"], r["model"]): r for r in doc["usage"]}
    assert set(rows) == {(None, "model-a"), ("x1", "model-b")}
    assert rows[(None, "model-a")]["tool_calls"] == 1
    assert rows[(None, "model-a")]["tokens"]["output"] == 15
    # The tool error takes its model from the call; the turn carries the model of its line.
    assert rows[(None, "model-a")]["tool_errors"] == 1
    assert rows[(None, "model-a")]["active_seconds"] == 4.0
    assert rows[("x1", "model-b")]["tokens"]["output"] == 7
    assert all(r["day"] == "2021-05-06" for r in doc["usage"])
    total = sum(sum(r["tokens"].values()) for r in doc["usage"])
    session_tokens = doc["session"]["tokens"]
    assert (
        total
        == sum(v for k, v in session_tokens.items() if k != "subagents")
        + session_tokens["subagents"]
    )
    assert_golden(doc, "show-agent")


def test_show_resolves_a_unique_prefix_and_lowercases_it(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = run(capsys, gw, "show", "--json", S2[:8].upper())
    assert result.code == 0
    assert result.doc["session"]["session_id"] == S2


def test_show_unknown_is_exit_10_and_short_prefix_exit_2(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    missing = run(capsys, gw, "show", "--json", "99999999")
    assert missing.code == 10
    validate(json.loads(missing.out), "session-audit/cli/error.json")
    assert run(capsys, gw, "show", "--json", "1111111").code == 2


def test_show_ambiguous_prefix_is_exit_21_with_the_true_total(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [f"abcdef01-0000-4000-8000-{n:012d}" for n in range(25)]
    for sid in ids:
        corpus.write_main(sid, [message(f"m{sid[-3:]}", 1)], project="p" + sid[-3:])
    ingest(capsys, gw)
    result = run(capsys, gw, "show", "--json", "abcdef01")
    assert result.code == 21
    error = json.loads(result.out)
    validate(error, "session-audit/cli/error.json")
    assert error["error"]["details"]["matched"] == 25
    assert error["error"]["details"]["matches"] == sorted(ids)[:20]
    assert run(capsys, gw, "show", "--json", ids[3]).code == 0  # a full id always resolves
    assert run(capsys, gw, "show", "--json", ids[3][:-1]).code == 21


def test_show_caps_agents_and_usage_rows(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus.write_main(S1, [message("a1", 1, t=0)], project="p1")
    for i in range(3):
        corpus.write_agent(S1, f"x{i}", [message(f"g{i}", 1, t=i, agent=f"x{i}")], project="p1")
    ingest(capsys, gw)
    monkeypatch.setattr("session_audit.sessionquery.USAGE_ROW_MAX", 2)
    result = run(capsys, gw, "show", "--json", S1)
    doc = result.doc
    assert doc["truncated"] is True
    assert len(doc["usage"]) == 2
    assert doc["usage"][0]["agent_id"] is None  # the main agent first


def test_usage_query_is_constrained_by_the_session_index(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    conn = sqlite3.connect(state_dir_of(_private_environment) / "state.db")
    try:
        plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + USAGE_SQL, (S1,)))
    finally:
        conn.close()
    assert "events_session_counted" in plan
    assert "SCAN e" not in plan and "SCAN events" not in plan


def test_a_newer_store_refuses_both_commands(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    conn = sqlite3.connect(state_dir_of(_private_environment) / "state.db")
    try:
        conn.execute(
            "INSERT INTO schema_migrations (number, name, applied_at) VALUES (99999, 'x', 0)"
        )
        conn.commit()
    except sqlite3.Error:
        conn.close()
        pytest.skip("ledger shape differs")
    conn.close()
    for argv in (("sessions", "--json"), ("show", "--json", S1)):
        result = run(capsys, gw, *argv)
        assert result.code == 20
        assert json.loads(result.out)["error"]["details"] == {"reason": "store_newer"}


def test_derive_is_not_bypassed_for_values(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    from session_audit.config import Settings

    conn = sqlite3.connect(state_dir_of(_private_environment) / "state.db")
    try:
        expected = {v.session_id: v.to_json() for v in derive.iter_session_values(conn, Settings())}
    finally:
        conn.close()
    for session in sessions(capsys, gw).doc["sessions"]:
        assert {k: v for k, v in session.items() if k != "harness"} == expected[
            session["session_id"]
        ]
