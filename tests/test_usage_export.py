"""``usage`` and ``export``: aggregation, totals, dimensions, filters, streaming and output
validation, through the real CLI on a synthetic corpus ingested into a private store."""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from session_audit import aggregate, derive
from session_audit.cli import main
from session_audit.command import Output

from .eventhelp import raw, tool_error, tool_use, turn
from .golden import assert_golden
from .ingesthelp import S1, S2, S3, Corpus, TickGateway, ingest, state_dir_of
from .schemas import validate
from .storehelp import ClockedGateway
from .test_sessions_show import add_run


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> TickGateway:
    gateway = TickGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


MTIME = 1_800_000_000  # 2027-01: a line's timestamp is usable up to its file's mtime


def write_main(corpus: Corpus, session: str, lines: list[bytes], project: str = "proj-a") -> None:
    corpus.write_main(session, lines, project=project, mtime=MTIME)


def asst(
    iso: str,
    mid: str,
    *,
    out: int = 10,
    inp: int = 1,
    model: str = "model-a",
    cwd: str | None = None,
    entry: str | None = None,
    tools: tuple[tuple[str, str], ...] = (),
) -> bytes:
    msg: dict[str, Any] = {
        "id": mid,
        "model": model,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 3},
        "content": [{"type": "tool_use", "id": i, "name": n, "input": {}} for i, n in tools],
    }
    obj: dict[str, Any] = {"type": "assistant", "timestamp": iso, "message": msg}
    if cwd:
        obj["cwd"] = cwd
    if entry:
        obj["entrypoint"] = entry
    return raw(obj)


def stamped(line: bytes, iso: str) -> bytes:
    obj = json.loads(line)
    obj["timestamp"] = iso
    return raw(obj)


def seed(corpus: Corpus, capsys: pytest.CaptureFixture[str], gw: TickGateway) -> None:
    """S1 /a/b interactive: two days, two models, a tool call that errors, a turn.
    S2 /a/bc headless: one day. S3 /a/b/c: a later week and month."""
    write_main(
        corpus,
        S1,
        [
            asst("2026-11-20T23:50:00.000Z", "a1", model="model-a", cwd="/a/b", entry="cli"),
            stamped(tool_use("a2", "toolu_1", out=5), "2026-11-20T23:55:00.000Z"),
            stamped(tool_error("toolu_1"), "2026-11-20T23:56:00.000Z"),
            stamped(turn(4000), "2026-11-20T23:57:00.000Z"),
            asst("2026-11-21T00:10:00.000Z", "a3", model="model-b", out=20),
        ],
        project="p1",
    )
    write_main(
        corpus,
        S2,
        [
            asst(f"2026-11-22T10:0{i}:00.000Z", f"b{i}", out=50, cwd="/a/bc", entry="sdk-cli")
            for i in range(3)
        ],
        project="p2",
    )
    write_main(
        corpus,
        S3,
        [asst("2026-12-01T01:00:00.000Z", "c1", out=7, model="model-b", cwd="/a/b/c")],
        project="p3",
    )
    assert ingest(capsys, gw).code == 0


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.out)  # type: ignore[no-any-return]

    @property
    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.out.splitlines()]


def run(capsys: pytest.CaptureFixture[str], gw: ClockedGateway, *argv: str) -> Result:
    capsys.readouterr()
    code = main([*argv], gateway=gw, stdin_is_tty=False)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


def usage(capsys: pytest.CaptureFixture[str], gw: ClockedGateway, *argv: str) -> Result:
    result = run(capsys, gw, "usage", "--json", *argv)
    if result.code == 0:
        validate(result.doc, "session-audit/cli/usage.json")
    return result


def keys(result: Result) -> list[list[Any]]:
    return [r["key"] for r in result.doc["rows"]]


def by_key(result: Result) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(r["key"]): r for r in result.doc["rows"]}


# --- no store ---------------------------------------------------------------------------------


def test_no_store_gives_empty_rows_and_zero_totals_and_creates_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    result = usage(capsys, gw, "--by", "day")
    assert result.code == 0
    assert result.doc["rows"] == []
    assert result.doc["totals"]["sessions"] == 0
    assert result.doc["totals"]["cost"]["reported_usd"] is None
    assert_golden(result.doc, "usage-empty")
    assert not state_dir_of(_private_environment).exists()


# --- dimensions -------------------------------------------------------------------------------


def test_by_day_buckets_totals_and_golden(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = usage(capsys, gw, "--by", "day")
    assert keys(result) == [["2026-11-20"], ["2026-11-21"], ["2026-11-22"], ["2026-12-01"]]
    day = by_key(result)[("2026-11-20",)]
    assert day["sessions"] == 1
    assert day["tool_calls"] == 1 and day["tool_errors"] == 1
    assert day["active_seconds"] == 4.0
    assert day["tokens"]["output"] == 15
    totals = result.doc["totals"]
    assert totals["sessions"] == 3
    assert totals["tokens"]["output"] == 15 + 20 + 150 + 7
    assert_golden(result.doc, "usage-by-day")


def test_tz_rebuckets_exactly_including_45_minute_offsets(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    ny = usage(capsys, gw, "--by", "day", "--tz", "America/New_York")
    assert keys(ny)[0] == ["2026-11-20"] and by_key(ny)[("2026-11-20",)]["tokens"]["output"] == 35
    kathmandu = usage(capsys, gw, "--by", "day", "--tz", "Asia/Kathmandu")
    # 23:50Z is 05:35 next day at +05:45; 23:45Z is the slot start (05:30), still the 21st
    assert keys(kathmandu)[0] == ["2026-11-21"]
    assert by_key(kathmandu)[("2026-11-21",)]["tokens"]["output"] == 35


def test_week_and_month_keys(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    assert keys(usage(capsys, gw, "--by", "week")) == [["2026-W47"], ["2026-W49"]]
    assert keys(usage(capsys, gw, "--by", "month")) == [["2026-11"], ["2026-12"]]


def test_project_entrypoint_session_and_model_dimensions(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    # tokens descending: S2 (150 out) first
    assert keys(usage(capsys, gw, "--by", "project"))[0] == ["/a/bc"]
    assert keys(usage(capsys, gw, "--by", "entrypoint"))[0] == ["headless"]
    assert keys(usage(capsys, gw, "--by", "session"))[0] == [S2]
    model = usage(capsys, gw, "--by", "model")
    rows = by_key(model)
    assert rows[("model-b",)]["tokens"]["output"] == 27
    # the interruption-free corpus: null-model row exists only for tool errors without a call
    assert all(r["key"] != [None] or r["tokens"]["output"] == 0 for r in model.doc["rows"])
    assert model.doc["totals"]["tokens"]["output"] == 15 + 20 + 150 + 7


def test_two_dimensions_keep_order_and_use_null_for_missing(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = usage(capsys, gw, "--by", "day,model")
    assert result.doc["by"] == ["day", "model"]
    assert keys(result)[0] == ["2026-11-20", "model-a"]
    kind = usage(capsys, gw, "--by", "kind")
    assert keys(kind) == [[None]]
    assert kind.doc["rows"][0]["sessions"] == 3


def test_tool_dimension_nulls_tokens_cost_active_and_interruptions(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = usage(capsys, gw, "--by", "tool")
    assert keys(result) == [["Bash"]]
    row = result.doc["rows"][0]
    assert (row["tool_calls"], row["tool_errors"], row["denials"], row["sessions"]) == (1, 1, 0, 1)
    for name in ("tokens", "cost", "active_seconds", "interruptions"):
        assert row[name] is None
    combined = usage(capsys, gw, "--by", "tool,day")
    assert keys(combined) == [["Bash", "2026-11-20"]]
    assert combined.doc["totals"]["tool_calls"] == 1


def test_tool_error_takes_model_and_tool_of_its_call(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    row = by_key(usage(capsys, gw, "--by", "model"))[("model-a",)]
    assert row["tool_errors"] == 1 and row["tool_calls"] == 1


def test_model_dimension_counts_turns_only_in_the_null_model_row(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    rows = by_key(usage(capsys, gw, "--by", "model"))
    assert rows[(None,)]["active_seconds"] == 4.0
    assert rows[("model-a",)]["active_seconds"] is None


# --- labels and self-improve ----------------------------------------------------------------


def test_label_rows_overlap_but_totals_do_not(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=["team=x", "team=y", "other=1"])
    add_run(_private_environment, "cs-bbbbbbbb", [S2], labels=["team=x", "team=x"])
    result = usage(capsys, gw, "--by", "label=team")
    rows = by_key(result)
    assert rows[("x",)]["sessions"] == 2
    assert rows[("y",)]["sessions"] == 1
    assert rows[(None,)]["sessions"] == 1  # S3 has no team label
    assert sum(r["sessions"] for r in result.doc["rows"]) == 4
    assert result.doc["totals"]["sessions"] == 3
    assert result.doc["totals"]["tokens"]["output"] == 192  # not the sum of overlapping rows
    assert sum(r["tokens"]["output"] for r in result.doc["rows"]) > 192


def test_label_key_with_pattern_characters_matches_literally(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=["a.b=real", "axb=decoy", "a_b=u"])
    rows = by_key(usage(capsys, gw, "--by", "label=a.b"))
    assert rows[("real",)]["sessions"] == 1
    assert ("decoy",) not in rows and ("u",) not in rows


def test_self_improve_sessions_are_left_out_unless_included(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S2], labels=["owner=self-improve"])
    assert usage(capsys, gw, "--by", "session").doc["totals"]["sessions"] == 2
    included = usage(capsys, gw, "--by", "session", "--include-self-improve")
    assert included.doc["totals"]["sessions"] == 3
    export = run(capsys, gw, "export", "sessions")
    assert {x["session_id"] for x in export.lines} == {S1, S3}
    export = run(capsys, gw, "export", "sessions", "--include-self-improve")
    assert len(export.lines) == 3
    assert {x["session_id"] for x in run(capsys, gw, "export", "usage").lines} == {S1, S3}


def test_kind_dimension_uses_the_snapshot(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=[], kind="review")
    rows = by_key(usage(capsys, gw, "--by", "kind"))
    assert rows[("review",)]["sessions"] == 1 and rows[(None,)]["sessions"] == 2


# --- time filters and cost ----------------------------------------------------------------------


def test_time_filters_select_events_not_sessions(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = usage(capsys, gw, "--by", "session", "--since", "2026-11-21", "--until", "2026-11-22")
    assert keys(result) == [[S1]]
    # only the 00:10 message of the 21st: 20 output tokens, no tool call, no turn
    row = result.doc["rows"][0]
    assert row["tokens"]["output"] == 20 and row["tool_calls"] == 0
    assert row["active_seconds"] is None
    assert result.doc["totals"]["sessions"] == 1


def test_until_is_exclusive_and_a_null_ts_never_matches(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    exact = usage(capsys, gw, "--by", "session", "--until", "2026-11-21T00:10:00Z")
    assert result_tokens(exact) == 15  # the 00:10:00 message is excluded
    inclusive = usage(capsys, gw, "--by", "session", "--until", "2026-11-21T00:10:00.001Z")
    assert result_tokens(inclusive) == 35


def result_tokens(result: Result) -> int:
    return int(result.doc["totals"]["tokens"]["output"])


def cost_line(value: float, iso: str) -> bytes:
    return raw({"type": "cost-state", "timestamp": iso, "totalCostUSD": value})


def test_reported_usd_is_all_or_null_and_null_when_a_dimension_splits(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    write_main(
        corpus,
        S1,
        [
            asst("2026-11-20T10:00:00.000Z", "a1", cwd="/p"),
            cost_line(1.5, "2026-11-20T10:01:00.000Z"),
        ],
    )
    write_main(
        corpus,
        S2,
        [
            asst("2026-11-21T10:00:00.000Z", "b1", cwd="/p"),
            cost_line(2.25, "2026-11-21T10:01:00.000Z"),
        ],
        project="p2",
    )
    write_main(corpus, S3, [asst("2026-11-22T10:00:00.000Z", "c1", cwd="/q")], project="p3")
    assert ingest(capsys, gw).code == 0
    rows = by_key(usage(capsys, gw, "--by", "project"))
    assert rows[("/p",)]["cost"]["reported_usd"] == 3.75
    assert rows[("/q",)]["cost"]["reported_usd"] is None  # no figure: never 0
    totals = usage(capsys, gw, "--by", "project").doc["totals"]
    assert totals["cost"]["reported_usd"] is None  # one contributor lacks it
    assert by_key(usage(capsys, gw, "--by", "day"))[("2026-11-20",)]["cost"]["reported_usd"] is None
    only = usage(capsys, gw, "--by", "project", "--project", "/p")
    assert only.doc["totals"]["cost"]["reported_usd"] == 3.75
    windowed = usage(capsys, gw, "--by", "project", "--project", "/p", "--since", "2026-01-01")
    assert windowed.doc["totals"]["cost"]["reported_usd"] is None
    assert windowed.doc["rows"][0]["cost"]["reported_usd"] is None


def test_estimate_is_rounded_once_from_exact_sums_and_unknown_tokens_are_separate(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [
        asst(f"2026-11-20T10:0{i}:00.000Z", f"a{i}", inp=1, out=1, model="claude-sonnet-4-5")
        for i in range(5)
    ]
    lines.append(asst("2026-11-20T10:09:00.000Z", "z", out=100, model="no-such-model"))
    write_main(corpus, S1, lines)
    assert ingest(capsys, gw).code == 0
    result = usage(capsys, gw, "--by", "model")
    sonnet = by_key(result)[("claude-sonnet-4-5",)]["cost"]
    odd = by_key(result)[("no-such-model",)]["cost"]
    assert sonnet["unknown_tokens"] == 0 and sonnet["estimated_usd"] > 0
    assert odd["unknown_tokens"] == 104 and odd["estimated_usd"] == 0
    total = result.doc["totals"]["cost"]
    assert total["unknown_tokens"] == 104
    assert total["estimated_usd"] == sonnet["estimated_usd"]


# --- order, limit, hostile ---------------------------------------------------------------------


def test_limit_truncates_after_ordering_and_session_limit(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = usage(capsys, gw, "--by", "session", "--limit", "2")
    assert keys(result) == [[S2], [S1]] and result.doc["truncated"] is True
    assert result.doc["totals"]["sessions"] == 3
    assert usage(capsys, gw, "--by", "session", "--limit", "3").doc["truncated"] is False
    assert keys(usage(capsys, gw, "--by", "day", "--limit", "1")) == [["2026-11-20"]]


def test_sessions_with_null_times_have_null_day_rows_sorted_last(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    write_main(
        corpus,
        "44444444-4444-4444-8444-444444444444",
        [
            raw(
                {
                    "type": "assistant",
                    "message": {"id": "n1", "model": "m", "usage": {"output_tokens": 4}},
                }
            )
        ],
        project="p4",
    )
    assert ingest(capsys, gw).code == 0
    result = usage(capsys, gw, "--by", "day")
    assert keys(result)[-1] == [None]
    assert "no_timestamps" in result.doc["rows"][-1]["flags"]
    assert "no_timestamps" in result.doc["totals"]["flags"]
    assert (
        usage(capsys, gw, "--by", "session", "--since", "2000-01-01").doc["totals"]["sessions"] == 3
    )


def test_hostile_text_is_sanitized_once_in_the_table(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    write_main(corpus, S1, [asst("2026-11-20T10:00:00.000Z", "a1", cwd="/a\nforged line\x1b[31m")])
    assert ingest(capsys, gw).code == 0
    result = run(capsys, gw, "usage", "--by", "project")
    assert result.code == 0
    assert "<U+000A>" in result.out and "\x1b" not in result.out
    assert "<U+0A>" not in result.out and "<U+003C>" not in result.out
    assert all(not line.startswith("forged") for line in result.out.splitlines())


def test_human_table_lists_rows_totals_and_freshness(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    out = run(capsys, gw, "usage", "--by", "day,model").out
    assert "2026-11-20 / model-a" in out and "total" in out and "last ingest:" in out


# --- performance and plan -----------------------------------------------------------------------


def test_the_event_statements_use_the_session_index(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    conn = sqlite3.connect(state_dir_of(_private_environment) / "state.db")
    try:
        for model in ("dimension", "messages", "none"):
            for tool in (False, True):
                for window in (False, True):
                    sql = aggregate.usage_sql(model=model, tool=tool, marks=2, window=window)
                    params: list[Any] = [S1, S2] + ([0, 1] if window else [])
                    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
                    assert "events_session_counted" in plan, plan
                    assert "SCAN e" not in plan and "SCAN events" not in plan, plan
    finally:
        conn.close()


def make_many(home: Path, sessions: int, per: int) -> None:
    """A store with ``sessions`` x ``per`` message events, written directly (one day each)."""
    from session_audit.store import open_store

    directory = state_dir_of(home)
    open_store(ClockedGateway(), str(directory)).close()
    db = sqlite3.connect(directory / "state.db")
    try:
        db.execute("BEGIN")
        for s in range(sessions):
            sid = f"{s:08x}-0000-4000-8000-000000000000"
            db.execute(
                "INSERT INTO files (path, dev, inode) VALUES (?, 1, ?)", (f"/f{s}".encode(), s + 1)
            )
            fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO sessions (session_id, project_dir, main_file_id, first_ingest_at, "
                "last_ingest_at) VALUES (?, 'p', ?, 1, 1)",
                (sid, fid),
            )
            for e in range(per):
                slot = 1_800_000_000 // 900 + (s % 40) * 96 + e
                db.execute(
                    "INSERT INTO events (key, file_id, line_offset, kind, ts, slot, session_id, "
                    "model, output_tokens, counted) "
                    "VALUES (?, ?, ?, 'message', ?, ?, ?, 'm', 3, 1)",
                    (f"{s:08x}{e:08x}".ljust(16, "0"), fid, e, slot * 900_000, slot, sid),
                )
        db.execute("COMMIT")
    finally:
        db.close()


def test_a_large_filter_set_is_chunked_and_agrees_with_one_big_chunk(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_many(_private_environment, 1300, 3)
    big = usage(capsys, gw, "--by", "day", "--limit", "1000")
    assert big.doc["totals"]["sessions"] == 1300
    assert big.doc["totals"]["tokens"]["output"] == 1300 * 3 * 3
    monkeypatch.setattr(aggregate, "CHUNK", 7)
    small = usage(capsys, gw, "--by", "day", "--limit", "1000")
    assert small.doc == big.doc
    assert sum(r["sessions"] for r in big.doc["rows"]) >= 1300


# --- export ---------------------------------------------------------------------------------------


def test_export_sessions_oldest_first_validated_with_since(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = run(capsys, gw, "export", "sessions")
    assert result.code == 0, result.err
    for line in result.lines:
        validate(line, "session-audit/cli/export-sessions.json")
    assert [x["session_id"] for x in result.lines] == [S1, S2, S3]
    assert_golden(result.lines[0], "export-sessions-first")
    since = run(capsys, gw, "export", "sessions", "--since", "2026-11-22", "--json")
    assert [x["session_id"] for x in since.lines] == [S2, S3]
    assert run(capsys, gw, "export", "sessions", "--since", "2027-01-01").out == ""


def test_export_usage_lines_equal_show_rows_and_are_ordered_by_day(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    result = run(capsys, gw, "export", "usage")
    assert result.code == 0
    for line in result.lines:
        validate(line, "session-audit/cli/export-usage.json")
    days = [x["day"] for x in result.lines]
    assert days == sorted(days)
    shown = [
        row
        for sid in (S1, S2, S3)
        for row in json.loads(run(capsys, gw, "show", "--json", sid).out)["usage"]
    ]
    key = lambda r: (r["day"] or "~", r["session_id"], r["agent_id"] or "", r["model"] or "~")  # noqa: E731
    assert sorted(result.lines, key=key) == sorted(shown, key=key)
    assert_golden(result.lines[0], "export-usage-first")


def test_export_usage_since_keeps_days_on_or_after_the_day_in_time_zone(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, capsys, gw)
    days = lambda *a: sorted({x["day"] for x in run(capsys, gw, "export", "usage", *a).lines})  # noqa: E731
    assert days("--since", "2026-11-22T23:59:59Z") == ["2026-11-22", "2026-12-01"]
    assert days("--since", "2026-11-21") == ["2026-11-21", "2026-11-22", "2026-12-01"]


def test_export_empty_store_prints_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    for kind in ("sessions", "usage"):
        result = run(capsys, gw, "export", kind)
        assert (result.code, result.out) == (0, "")
    assert not state_dir_of(_private_environment).exists()


def test_export_retros_is_still_a_stub(gw: TickGateway, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, gw, "export", "retros").code == 1


class ClosedPipe:
    """A stdout whose reader went away after ``allowed`` writes."""

    encoding = "utf-8"

    def __init__(self, allowed: int) -> None:
        self.allowed, self.writes = allowed, 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > self.allowed:
            raise BrokenPipeError(32, "broken pipe")
        return len(text)

    def flush(self) -> None:
        return None


def test_a_closed_pipe_ends_the_export_with_exit_1(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(corpus, capsys, gw)
    pipe = ClosedPipe(1)
    monkeypatch.setattr("sys.stdout", pipe)
    code = main(["export", "sessions"], gateway=gw, stdin_is_tty=False)
    assert code == 1
    assert pipe.writes <= 3  # it stopped at the first failure, it did not produce the rest


def test_export_usage_is_streamed_with_bounded_memory(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    make_many(_private_environment, 600, 8)  # 4800 events over 40 days, 600 sessions
    from session_audit.command import Context
    from session_audit.sessionquery import read_stream

    ctx = Context(gateway=gw, out=Output(lambda: None), json=True)
    ctx.state_directory = str(state_dir_of(_private_environment))
    from session_audit import config

    ctx.settings = config.Settings()
    lines = read_stream(
        ctx,
        lambda conn: aggregate.usage_lines(
            conn, ctx, aggregate.Filters(), ctx.settings.zone(), None, None
        ),
    )
    tracemalloc.start()
    first = next(lines)
    after_first = tracemalloc.get_traced_memory()[0]
    count = 1
    peak_total = 0
    for _ in lines:
        count += 1
        peak_total = max(peak_total, tracemalloc.get_traced_memory()[0])
    tracemalloc.stop()
    assert first["day"] is not None and count == 600
    # 600 lines of ~1 KB each are 600 KB if held; the stream keeps a few days of groups
    assert peak_total - after_first < 250_000


def test_export_sessions_derives_in_batches_not_all_at_once(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_many(_private_environment, 450, 1)
    calls: list[int] = []
    real = derive.iter_session_values

    def spy(conn: Any, settings: Any, ids: Any = None) -> Any:
        ids = list(ids) if ids is not None else None
        calls.append(len(ids) if ids is not None else -1)
        return real(conn, settings, ids)

    monkeypatch.setattr(derive, "iter_session_values", spy)
    result = run(capsys, gw, "export", "sessions")
    assert len(result.lines) == 450
    # one full pass for the sort keys, then batches of at most BATCH ids
    assert calls[0] == -1 and max(calls[1:]) <= derive.BATCH and len(calls) > 3


def test_store_errors_map_to_exit_12_and_20_for_both_commands(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    directory = state_dir_of(_private_environment)
    directory.mkdir(parents=True, mode=0o700)
    db = directory / "state.db"
    db.write_bytes(b"")
    db.chmod(0o600)
    for argv in (("usage", "--by", "day"), ("export", "usage"), ("export", "sessions")):
        result = run(capsys, gw, *argv, "--json")
        assert result.code == 12
        assert json.loads(result.out)["error"]["details"]["reason"] == "store_unwritable"
    db.unlink()
    seed(corpus, capsys, gw)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO schema_migrations (number, name, applied_at) VALUES (99999, 'x', 0)")
    conn.commit()
    conn.close()
    for argv in (("usage", "--by", "day"), ("export", "usage"), ("export", "sessions")):
        result = run(capsys, gw, *argv, "--json")
        assert result.code == 20
        assert json.loads(result.out)["error"]["details"] == {"reason": "store_newer"}


def test_a_huge_stored_label_value_is_cut_to_the_schema_cap(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed(corpus, capsys, gw)
    add_run(_private_environment, "cs-aaaaaaaa", [S1], labels=["k=" + "v" * 9000])
    result = usage(capsys, gw, "--by", "label=k")
    assert result.code == 0
    (long,) = [k[0] for k in keys(result) if k[0] is not None]
    assert len(long) == 4096  # the schema validated it
