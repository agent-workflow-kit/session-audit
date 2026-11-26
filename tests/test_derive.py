"""Session values, time buckets and cost with its basis (issue #14)."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from session_audit import derive
from session_audit.config import ModelPrice, PriceTable, Settings
from session_audit.derive import (
    Bucketer,
    CostAccumulator,
    TokenSums,
    agent_values,
    bucket_key,
    is_mismatch,
    iter_session_values,
    reported_sum,
    session_values,
)
from session_audit.reader import AgentMeta

from .schemas import Draft202012Validator, registry
from .eventhelp import (
    SESSION_A,
    SESSION_B,
    SESSION_C,
    Env,
    compaction,
    denial_and_interruption,
    make_env,
    message,
    raw,
    tool_error,
    tool_use,
    turn,
)

SETTINGS = Settings()


def validate_defs(name: str, doc: object) -> None:
    Draft202012Validator(
        {"$ref": f"session-audit/defs.json#/$defs/{name}"},
        registry=registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(doc)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = make_env(tmp_path)
    yield e
    e.close()


def at(iso: str, **fields: object) -> dict:
    return {"timestamp": iso, **fields}


def assistant(
    iso: str,
    mid: str,
    model: str | None = "claude-sonnet-4-5-20250929",
    *,
    inp: int = 0,
    out: int = 0,
    cr: int = 0,
    w5: int = 0,
    w1: int = 0,
    cwd: str | None = None,
    entry: str | None = None,
    version: str | None = None,
) -> bytes:
    msg: dict = {
        "id": mid,
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cr,
            "cache_creation": {
                "ephemeral_5m_input_tokens": w5,
                "ephemeral_1h_input_tokens": w1,
            },
        },
    }
    if model is not None:
        msg["model"] = model
    obj: dict = {"type": "assistant", "timestamp": iso, "message": msg}
    if cwd is not None:
        obj["cwd"] = cwd
    if entry is not None:
        obj["entrypoint"] = entry
    if version is not None:
        obj["version"] = version
    return raw(obj)


def cost_line(value: object, iso: str = "2026-01-02T03:04:05.000Z") -> bytes:
    return raw({"type": "cost-state", "timestamp": iso, "totalCostUSD": value})


def one(env: Env, sid: str = SESSION_A, settings: Settings = SETTINGS) -> derive.SessionValues:
    found = session_values(env.conn, settings, sid)
    assert found is not None
    return found


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


# --- time buckets -----------------------------------------------------------------------------


def slot_at(iso: str) -> int:
    return ms(iso) // 1000 // 900


def test_slot_keys_in_utc() -> None:
    slot = slot_at("2026-11-20T23:59:59Z")
    z = ZoneInfo("UTC")
    assert bucket_key(slot, "day", z) == "2026-11-20"
    assert bucket_key(slot, "week", z) == "2026-W47"
    assert bucket_key(slot, "month", z) == "2026-11"
    assert bucket_key(slot + 1, "day", z) == "2026-11-21"


def test_no_slot_has_no_key() -> None:
    assert bucket_key(None, "day", ZoneInfo("UTC")) is None
    assert Bucketer(ZoneInfo("UTC")).key(None, "month") is None


def test_a_45_minute_offset_zone_buckets_exactly() -> None:
    kathmandu = ZoneInfo("Asia/Kathmandu")  # +05:45
    before = slot_at("2026-11-20T18:14:59Z")  # 23:59:59 local
    after = slot_at("2026-11-20T18:15:00Z")  # 00:00:00 local, next day
    assert before != after
    assert bucket_key(before, "day", kathmandu) == "2026-11-20"
    assert bucket_key(after, "day", kathmandu) == "2026-11-21"
    # the same slots are one day in UTC: re-bucketing changes only the zone, not the data
    assert bucket_key(before, "day", ZoneInfo("UTC")) == bucket_key(after, "day", ZoneInfo("UTC"))


def test_half_hour_and_dst_zones_keep_every_slot_inside_one_local_day() -> None:
    zones = [
        "Asia/Kathmandu",
        "Australia/Lord_Howe",
        "Australia/Adelaide",
        "America/St_Johns",
        "America/New_York",
        "Europe/London",
        "Pacific/Chatham",
        "Pacific/Kiritimati",
        "Asia/Kolkata",
    ]
    start = slot_at("2026-01-01T00:00:00Z")
    for name in zones:
        zone = ZoneInfo(name)
        for slot in range(start, start + 96 * 366, 7):
            first = datetime.fromtimestamp(slot * 900, UTC).astimezone(zone)
            last = datetime.fromtimestamp(slot * 900 + 899, UTC).astimezone(zone)
            assert first.date() == last.date(), (name, slot)
            expected = f"{first.year:04d}-{first.month:02d}-{first.day:02d}"
            assert bucket_key(slot, "day", zone) == expected


def test_dst_fall_back_day_holds_25_hours_of_slots() -> None:
    zone = ZoneInfo("America/New_York")
    start = slot_at("2026-11-01T04:00:00Z")  # local midnight (EDT)
    keys = [bucket_key(start + i, "day", zone) for i in range(101)]
    assert keys.count("2026-11-01") == 25 * 4
    assert keys[-1] == "2026-11-02"


def test_iso_week_crosses_the_year_boundary() -> None:
    z = ZoneInfo("UTC")
    assert bucket_key(slot_at("2024-12-30T00:00:00Z"), "week", z) == "2025-W01"
    assert bucket_key(slot_at("2026-12-31T12:00:00Z"), "week", z) == "2026-W53"
    assert bucket_key(slot_at("2027-01-03T23:45:00Z"), "week", z) == "2026-W53"
    assert bucket_key(slot_at("2027-01-04T00:00:00Z"), "week", z) == "2027-W01"


def test_a_changed_time_zone_rebuckets_the_same_slots_exactly() -> None:
    slots = [slot_at("2026-03-08T00:00:00Z") + i for i in range(0, 96 * 3, 5)]

    def totals(zone: str) -> dict[str | None, int]:
        out: dict[str | None, int] = defaultdict(int)
        bucketer = Bucketer(ZoneInfo(zone))
        for s in slots:
            out[bucketer.key(s, "day")] += 1
        return dict(out)

    assert sum(totals("Asia/Kathmandu").values()) == len(slots)
    assert totals("UTC") != totals("Asia/Kathmandu")
    assert totals("Asia/Kathmandu") == totals("Asia/Kathmandu")  # stable, no state
    # switching back gives the first answer again: nothing was denormalized
    assert totals("UTC") == totals("UTC")


def test_out_of_range_slot_is_no_key() -> None:
    assert bucket_key(10**15, "day", ZoneInfo("UTC")) is None


# --- cost -------------------------------------------------------------------------------------


def prices(**models: ModelPrice) -> PriceTable:
    return PriceTable(as_of="2026-11-25", models=dict(models))


def price(i: float, o: float, r: float, w: float, w1: float | None = None) -> ModelPrice:
    extra = {} if w1 is None else {"cache_write_1h": w1}
    return ModelPrice(input=i, output=o, cache_read=r, cache_write=w, **extra)


def test_estimate_uses_the_1h_price_and_defaults_it_to_the_5m_price() -> None:
    sums = TokenSums(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write_5m=1_000_000, cache_write_1h=1_000_000)
    acc = CostAccumulator(prices(m=price(1, 2, 0.5, 1.25, 2.0)))
    acc.add("m", sums)
    assert acc.estimated == Decimal("6.75")
    acc = CostAccumulator(prices(m=price(1, 2, 0.5, 1.25)))
    acc.add("m", sums)
    assert acc.estimated == Decimal("6.0")


def test_prefix_matching_has_a_dash_boundary() -> None:
    table = prices(**{"claude-opus-4-5": price(5, 25, 0.5, 6.25)})
    acc = CostAccumulator(table)
    one_million = TokenSums(input=1_000_000)
    acc.add("claude-opus-4-5-20251101", one_million)
    assert acc.estimated == Decimal(5) and acc.unknown_tokens == 0
    acc.add("claude-opus-4-50", one_million)
    assert acc.estimated == Decimal(5) and acc.unknown_tokens == 1_000_000


def test_longest_prefix_wins() -> None:
    table = prices(**{"m": price(1, 1, 1, 1), "m-big": price(9, 9, 9, 9)})
    acc = CostAccumulator(table)
    acc.add("m-big-20250101", TokenSums(input=1_000_000))
    assert acc.estimated == Decimal(9)


def test_unknown_model_counts_tokens_and_adds_no_dollars() -> None:
    acc = CostAccumulator(prices(m=price(1, 1, 1, 1)))
    acc.add("mystery", TokenSums(input=3, output=4, cache_read=5, cache_write_5m=6, cache_write_1h=7))
    acc.add(None, TokenSums(input=1))  # a model-less message is "unknown", which has no price
    assert acc.estimated == 0
    assert acc.unknown_tokens == 3 + 4 + 5 + 6 + 7 + 1


def test_dollars_are_exact_and_rounded_once() -> None:
    # 0.1 + 0.2 style drift: three messages of 1 token at 0.3 per million
    acc = CostAccumulator(prices(m=price(0.3, 0.3, 0.3, 0.3)))
    for _ in range(3):
        acc.add("m", TokenSums(input=1_000_000))
    assert acc.estimated == Decimal("0.9")
    assert derive.usd_json(acc.estimated) == 0.9
    assert derive.usd_json(Decimal("0.0000005")) == 0.000001  # half up
    assert derive.usd_json(Decimal("0.0000004")) == 0.0
    assert derive.usd_json(Decimal("123456.1234565")) == 123456.123457


def test_reported_from_file_is_finite_nonnegative_micro_dollars() -> None:
    assert derive.reported_from_file(0.1 + 0.2) == Decimal("0.300000")
    assert derive.reported_from_file(None) is None
    assert derive.reported_from_file(float("nan")) is None
    assert derive.reported_from_file(float("inf")) is None
    assert derive.reported_from_file(-1.0) is None


def test_mismatch_margin_is_relative_to_the_estimate() -> None:
    est = Decimal(10)
    assert not is_mismatch(Decimal("11"), est, 0.10)  # exactly 10 percent
    assert is_mismatch(Decimal("11.000001"), est, 0.10)
    assert is_mismatch(Decimal("8.9"), est, 0.10)
    assert not is_mismatch(None, est, 0.10)
    assert is_mismatch(Decimal("0.01"), Decimal(0), 0.10)
    assert not is_mismatch(Decimal(0), Decimal(0), 0.10)


def test_reported_sum_is_all_or_null() -> None:
    a, b = Decimal("1.5"), Decimal("2.25")
    assert reported_sum([a, b]) == Decimal("3.75")
    assert reported_sum([a, None]) is None
    assert reported_sum([]) is None
    assert reported_sum([a, b], time_filtered=True) is None
    assert reported_sum([a, b], splits_session=True) is None


def test_cost_json_is_finite_and_stable() -> None:
    cost = derive.Cost(Decimal("0.1"), Decimal("1") / Decimal(3), 7)
    assert cost.to_json() == {"reported_usd": 0.1, "estimated_usd": 0.333333, "unknown_tokens": 7}


# --- session values from a store --------------------------------------------------------------


def test_backwards_timestamps_use_min_and_max_not_first_and_last(env: Env) -> None:
    env.write(
        "a",
        [
            assistant("2026-03-01T10:00:00.000Z", "m1", out=1),
            assistant("2026-03-01T09:00:00.000Z", "m2", out=1),
            assistant("2026-03-01T12:30:00.000Z", "m3", out=1),
            assistant("2026-03-01T11:00:00.000Z", "m4", out=1),
        ],
    )
    env.ingest("a")
    v = one(env)
    assert derive.format_ms(v.started_ms) == "2026-03-01T09:00:00Z"
    assert derive.format_ms(v.ended_ms) == "2026-03-01T12:30:00Z"
    assert v.ended_ms is not None and v.started_ms is not None and v.ended_ms >= v.started_ms


def test_tokens_main_and_subagents_and_counters(env: Env) -> None:
    env.write(
        "a",
        [
            assistant("2026-03-01T10:00:00.000Z", "m1", inp=10, out=20, cr=30, w5=40, w1=50),
            tool_use("m2", "t1", t=5),
            tool_error("t1", t=6),
            turn(1500, t=7, uuid="u1"),
            turn(250, t=8, uuid="u2"),
            compaction("c1", t=9),
            denial_and_interruption(t=10, tool_id="t1"),
        ],
    )
    env.write("ag", [assistant("2026-03-01T10:05:00.000Z", "a1", inp=1, out=2, cr=3, w5=4, w1=5), tool_use("a2", "at", t=1)])
    env.ingest("a")
    env.ingest("ag", agent="agent1", meta=AgentMeta(agent_type="Explore", tool_use_id="toolu_9"))
    v = one(env)
    assert v.tokens.to_json() == {"input": 10, "output": 25, "cache_read": 30, "cache_write": 90}
    assert v.subagent_tokens == 1 + 2 + 3 + 9 + 5
    assert v.agents == 1
    assert (v.tool_calls, v.tool_errors, v.denials, v.interruptions, v.compactions) == (2, 1, 1, 1, 1)
    assert v.active_seconds == 1.75
    json = v.to_json()
    assert json["tokens"] == {"input": 10, "output": 25, "cache_read": 30, "cache_write": 90, "subagents": 20}
    agents, truncated = agent_values(env.conn, SESSION_A)
    assert not truncated and len(agents) == 1
    a = agents[0]
    assert (a.agent_id, a.agent_type, a.parent_tool_call_id) == ("agent1", "Explore", "toolu_9")
    assert a.tokens.to_json() == {"input": 1, "output": 2, "cache_read": 3, "cache_write": 9}
    assert a.tool_calls == 1


def test_active_seconds_null_without_turns_and_sums_exactly(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", out=1)])
    env.ingest("a")
    assert one(env).active_seconds is None
    env.write("b", [turn(100, uuid=f"u{i}", t=i) for i in range(30)] + [turn(1, uuid="x", t=40)])
    env.ingest("b", session=SESSION_B)
    assert one(env, SESSION_B).active_seconds == 3.001  # 3001 ms, no float drift


def test_models_ordered_by_tokens_then_name_at_most_64(env: Env) -> None:
    lines = [assistant("2026-03-01T10:00:00.000Z", f"m{i}", model=f"model-{i:03d}", out=1 + (i % 3)) for i in range(70)]
    env.write("a", lines)
    env.ingest("a")
    v = one(env)
    assert len(v.models) == 64
    assert v.models[0] == "model-002"  # out 3, name order among the ties
    assert list(v.models[:3]) == ["model-002", "model-005", "model-008"]
    # cost still sees every model, not just the 64 shown
    assert v.cost.unknown_tokens == sum(1 + (i % 3) for i in range(70))


def test_message_without_a_model_is_unknown_and_priced_as_unknown(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", model=None, inp=5, out=6)])
    env.ingest("a")
    v = one(env)
    assert v.models == ("unknown",)
    assert v.cost.unknown_tokens == 11
    assert v.cost.estimated == 0


def test_project_entrypoint_version(env: Env) -> None:
    env.write(
        "a",
        [
            assistant("2026-03-01T10:00:00.000Z", "m1", out=1, cwd="/work/proj", entry="cli", version="2.1.0"),
            assistant("2026-03-01T10:01:00.000Z", "m2", out=1, cwd="/elsewhere", entry="sdk-cli", version="2.1.5"),
        ],
    )
    env.ingest("a", project=b"-work-proj")
    v = one(env)
    assert v.project == "/work/proj"
    assert v.entrypoint == "interactive"
    assert v.version == "2.1.5"
    assert "project_from_dir" not in v.flags


@pytest.mark.parametrize(
    ("value", "expected"),
    [("cli", "interactive"), ("sdk-cli", "headless"), ("sdk-ts", "headless"), ("sdk", "unknown"), ("mcp", "unknown"), (None, "unknown"), ("CLI", "unknown")],
)
def test_entrypoint_mapping(value: str | None, expected: str) -> None:
    assert derive.map_entrypoint(value) == expected


def test_project_falls_back_to_the_directory_name_with_a_flag(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", out=1)])
    env.ingest("a", project=b"-work-proj")
    v = one(env)
    assert v.project == "-work-proj"
    assert "project_from_dir" in v.flags
    assert v.entrypoint == "unknown"
    assert v.version is None


def test_flags_no_usage_no_timestamps_cost_invalid_and_order(env: Env) -> None:
    env.write("a", [raw({"type": "system", "subtype": "compact_boundary", "uuid": "c"}), cost_line(float("1e999") if False else "x")])
    env.ingest("a")
    v = one(env)
    assert v.flags == ("no_usage", "no_timestamps", "project_from_dir", "cost_invalid")
    assert v.cost.estimated == 0 and v.cost.reported is None
    assert v.started_ms is None and v.ended_ms is None
    assert v.parse_errors == 1  # the invalid cost line is counted as an error line


def test_old_rules_flag(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", out=1)])
    env.ingest("a")
    assert "old_rules" not in one(env).flags
    env.conn.execute("UPDATE meta SET value = 2 WHERE key = 'rules_version'")
    assert "old_rules" in one(env).flags


def test_parse_counters_sum_over_main_and_agent_files(env: Env) -> None:
    env.write("a", [b"not json\n", raw({"type": "mystery"}), assistant("2026-03-01T10:00:00.000Z", "m1", out=1)])
    env.write("ag", [b"{\n", raw({"type": "weird"}), raw({"type": "weird2"})])
    env.ingest("a")
    env.ingest("ag", agent="g1")
    v = one(env)
    assert (v.parse_errors, v.unknown_line_types) == (2, 3)


def test_times_fall_back_to_file_range_when_no_event_has_a_timestamp(env: Env) -> None:
    # a cost-state line carries a timestamp but yields no event
    env.write("a", [cost_line(1.0, "2026-03-01T08:00:00.000Z"), cost_line(2.0, "2026-03-01T09:00:00.000Z")])
    env.ingest("a")
    v = one(env)
    assert derive.format_ms(v.started_ms) == "2026-03-01T08:00:00Z"
    assert derive.format_ms(v.ended_ms) == "2026-03-01T09:00:00Z"
    assert "no_usage" in v.flags


def test_unknown_session_is_none(env: Env) -> None:
    assert session_values(env.conn, SETTINGS, SESSION_C) is None
    assert list(iter_session_values(env.conn, SETTINGS, [SESSION_C])) == []


# --- cost of a session ------------------------------------------------------------------------


def test_estimate_from_the_bundled_prices_with_a_dated_model(env: Env) -> None:
    env.write(
        "a",
        [assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5-20250929", inp=1_000_000, out=1_000_000, cr=1_000_000, w5=1_000_000, w1=1_000_000)],
    )
    env.ingest("a")
    v = one(env)
    # 3 + 15 + 0.3 + 3.75 + 6.0
    assert v.cost.estimated == Decimal("28.05")
    assert v.cost.to_json()["estimated_usd"] == 28.05
    assert v.cost.unknown_tokens == 0


def test_cost_lines_that_disagree_with_tokens_set_the_flag_and_change_neither_figure(env: Env) -> None:
    env.write(
        "a",
        [
            assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5", inp=1_000_000),
            cost_line(5.0),
        ],
    )
    env.ingest("a")
    v = one(env)
    assert v.cost.estimated == Decimal(3)
    assert v.cost.reported == Decimal("5.000000")
    assert "cost_mismatch" in v.flags
    close = Settings.model_validate({"cost": {"mismatch_margin": 1.0}})
    assert "cost_mismatch" not in one(env, settings=close).flags
    assert one(env, settings=close).cost == v.cost


def test_matching_cost_lines_set_no_flag_and_the_last_line_counts(env: Env) -> None:
    env.write(
        "a",
        [
            cost_line(100.0),
            assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5", inp=1_000_000),
            cost_line(3.1),
        ],
    )
    env.ingest("a")
    v = one(env)
    assert v.cost.reported == Decimal("3.1")
    assert "cost_mismatch" not in v.flags


def test_agents_are_in_the_estimate(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5", inp=1_000_000)])
    env.write("ag", [assistant("2026-03-01T10:00:00.000Z", "a1", model="claude-haiku-4-5", inp=1_000_000)])
    env.ingest("a")
    env.ingest("ag", agent="g")
    assert one(env).cost.estimated == Decimal(4)


def test_reported_is_null_under_copied_history_and_present_otherwise(env: Env) -> None:
    shared = [assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5", inp=1_000_000)]
    env.write("a", [*shared, cost_line(3.0)])
    env.ingest("a", session=SESSION_A)
    # a fork: its main file starts with a copy of A's line (A's copy is the owner: earlier file
    # path order and equal timestamp), then has its own spend
    env.write(
        "b",
        [*shared, assistant("2026-03-01T11:00:00.000Z", "m2", model="claude-sonnet-4-5", inp=1_000_000), cost_line(6.0)],
    )
    env.ingest("b", session=SESSION_B)
    a, b = one(env, SESSION_A), one(env, SESSION_B)
    assert a.cost.reported == Decimal(3)
    assert b.cost.reported is None
    assert b.cost.estimated == Decimal(3)  # only its own message counts
    assert "cost_mismatch" not in b.flags
    # the all-or-null sum over both is null
    assert reported_sum([a.cost.reported, b.cost.reported]) is None


def test_a_copy_owned_by_an_agent_of_the_same_session_keeps_the_reported_figure(env: Env) -> None:
    line = assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-sonnet-4-5", inp=1_000_000)
    env.write("ag", [line])
    env.ingest("ag", agent="g")  # the agent file reads first and owns the key
    env.write("a", [line, cost_line(3.0)])
    env.ingest("a")
    v = one(env)
    assert v.cost.reported == Decimal(3)


def test_copies_dropped_never_feeds_reported(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", out=1), cost_line(1.0)])
    env.ingest("a")
    env.conn.execute("UPDATE files SET copies_dropped = 99")
    assert one(env).cost.reported == Decimal(1)


# --- model and tool of errors and denials -----------------------------------------------------


def test_error_and_denial_take_model_and_tool_from_the_paired_call(env: Env) -> None:
    env.write(
        "a",
        [
            message("m1", 5, model="model-x", tools=(("t1", "Bash"),)),
            tool_error("t1", t=1, uuid="e1"),
            denial_and_interruption(t=2, uuid="d1", tool_id="t1"),
            raw({"type": "user", "uuid": "d2", "timestamp": "2026-03-01T10:00:00.000Z", "toolDenialKind": "x"}),
            tool_error("never-seen", t=3, uuid="e2"),
        ],
    )
    env.ingest("a")
    rows = env.conn.execute(
        f"SELECT kind, model, tool, ref IS NULL FROM {derive.COUNTED_EVENTS_SQL} e0 "
        "WHERE kind IN ('tool_error', 'denial') ORDER BY kind, ts, id"
    ).fetchall()
    got = [tuple(r) for r in rows]
    assert ("tool_error", "model-x", "Bash", 0) in got
    assert ("denial", "model-x", "Bash", 0) in got
    assert ("denial", None, None, 1) in got  # no tool-use id on the line: null ref, null model
    assert ("tool_error", None, None, 0) in got  # a call that was never read
    assert len(got) == 4


def test_error_in_an_agent_file_uses_a_call_in_another_file(env: Env) -> None:
    env.write("a", [message("m1", 5, model="model-y", tools=(("t9", "Read"),))])
    env.write("ag", [tool_error("t9", uuid="e9")])
    env.ingest("a")
    env.ingest("ag", agent="g")
    row = env.conn.execute(
        f"SELECT model, tool FROM {derive.COUNTED_EVENTS_SQL} e0 WHERE kind = 'tool_error'"
    ).fetchone()
    assert tuple(row) == ("model-y", "Read")


# --- the recount ------------------------------------------------------------------------------


def recount(env: Env, settings: Settings, sid: str) -> dict:
    """An independent Python recount from the raw rows (no SQL aggregate, no derive code)."""
    rows = env.conn.execute(
        "SELECT agent_id, kind, ts, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_5m_tokens, cache_write_1h_tokens, seconds FROM events "
        "WHERE counted = 1 AND session_id = ?",
        (sid,),
    ).fetchall()
    main = [0, 0, 0, 0]
    sub = 0
    counts: dict[str, int] = defaultdict(int)
    ms_total: int | None = None
    tss: list[int] = []
    per_model: dict[str, list[int]] = defaultdict(lambda: [0] * 5)
    for agent, kind, t, model, i, o, r, w5, w1, seconds in rows:
        counts[kind] += 1
        if t is None:
            counts["null_ts"] += 1
        else:
            tss.append(t)
        if kind == "turn":
            ms_total = (ms_total or 0) + round(seconds * 1000)
        if kind == "message":
            name = model or "unknown"
            for k, n in enumerate((i, o, r, w5, w1)):
                per_model[name][k] += n
            if agent is None:
                main = [main[0] + i, main[1] + o, main[2] + r, main[3] + w5 + w1]
            else:
                sub += i + o + r + w5 + w1
    exact = Fraction(0)
    unknown = 0
    for name, (i, o, r, w5, w1) in per_model.items():
        p = settings.prices.lookup(name)
        if p is None:
            unknown += i + o + r + w5 + w1
            continue
        w1p = p.cache_write if p.cache_write_1h is None else p.cache_write_1h
        for n, rate in ((i, p.input), (o, p.output), (r, p.cache_read), (w5, p.cache_write), (w1, w1p)):
            exact += Fraction(Decimal(repr(rate))) * n / 1_000_000
    return {
        "main": main,
        "sub": sub,
        "counts": counts,
        "active": None if ms_total is None else ms_total / 1000,
        "start": min(tss) if tss else None,
        "end": max(tss) if tss else None,
        "exact": exact,
        "unknown": unknown,
        "models": set(per_model),
    }


def random_session(rng: random.Random, env: Env, sid: str, tag: str, shared: list[bytes]) -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    models = ["claude-sonnet-4-5-20250929", "claude-haiku-4-5", "mystery-1", None]

    def stamp() -> str:
        return (base + timedelta(seconds=rng.randrange(0, 3 * 86400))).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def lines(prefix: str) -> list[bytes]:
        out: list[bytes] = []
        for n in range(rng.randrange(1, 25)):
            roll = rng.random()
            if roll < 0.5:
                mid = f"{prefix}-m{rng.randrange(8)}"  # repeats inside the file: streaming blocks
                out.append(
                    assistant(
                        stamp(), mid, rng.choice(models), inp=rng.randrange(1000), out=rng.randrange(1, 500),
                        cr=rng.randrange(1000), w5=rng.randrange(500), w1=rng.randrange(500),
                    )
                )
            elif roll < 0.65:
                out.append(raw({"type": "assistant", "timestamp": stamp(), "message": {"id": f"{prefix}-c{n}", "model": "claude-haiku-4-5", "usage": {"output_tokens": 3}, "content": [{"type": "tool_use", "id": f"{prefix}-t{n}", "name": "Bash"}]}}))
            elif roll < 0.75:
                out.append(tool_error(f"{prefix}-t{rng.randrange(25)}", uuid=f"{prefix}-e{n}"))
            elif roll < 0.85:
                out.append(raw({"type": "system", "subtype": "turn_duration", "durationMs": rng.randrange(1, 90000), "uuid": f"{prefix}-u{n}", "timestamp": stamp()}))
            elif roll < 0.9:
                out.append(compaction(f"{prefix}-k{n}"))
            else:
                out.append(raw({"type": "user", "uuid": f"{prefix}-d{n}", "timestamp": stamp(), "interruptedMessageId": "z"}))
        return out

    env.write(f"{tag}-main", [*lines(tag), *shared])
    env.ingest(f"{tag}-main", session=sid)
    for k in range(rng.randrange(0, 3)):
        env.write(f"{tag}-ag{k}", lines(f"{tag}a{k}"))
        env.ingest(f"{tag}-ag{k}", session=sid, agent=f"agent{k}")


@pytest.mark.parametrize("seed", range(12))
def test_session_values_equal_an_independent_recount(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    env = make_env(tmp_path)
    try:
        shared = [assistant("2026-05-02T00:00:00.000Z", "shared-1", "claude-haiku-4-5", out=40), assistant("2026-05-02T00:00:01.000Z", "shared-2", None, out=9)]
        sids = [SESSION_A, SESSION_B, SESSION_C]
        for sid, tag in zip(sids, "abc", strict=True):
            random_session(rng, env, sid, tag, shared)
        got = {v.session_id: v for v in iter_session_values(env.conn, SETTINGS)}
        assert sorted(got) == sorted(sids)
        for sid in sids:
            want = recount(env, SETTINGS, sid)
            v = got[sid]
            assert [v.tokens.input, v.tokens.output, v.tokens.cache_read, v.tokens.cache_write] == want["main"]
            assert v.subagent_tokens == want["sub"]
            c = want["counts"]
            assert (v.tool_calls, v.tool_errors, v.denials, v.interruptions, v.compactions) == (
                c["tool_call"], c["tool_error"], c["denial"], c["interruption"], c["compaction"],
            )
            assert v.active_seconds == want["active"]
            assert (v.started_ms, v.ended_ms) == (want["start"], want["end"]) or want["start"] is None
            assert Fraction(v.cost.estimated) == want["exact"]
            assert v.cost.unknown_tokens == want["unknown"]
            assert set(v.models) == want["models"]
            assert ("no_timestamps" in v.flags) == (c["null_ts"] > 0)
            assert ("no_usage" in v.flags) == (c["message"] == 0)
            assert v.to_json()["cost"]["estimated_usd"] == float(derive.usd(v.cost.estimated))  # type: ignore[index]
        # the batch path and the one-at-a-time path agree, in any order
        ids = list(reversed(sids))
        again = [session_values(env.conn, SETTINGS, s) for s in ids]
        assert again == [got[s] for s in ids]
        assert [v.session_id for v in iter_session_values(env.conn, SETTINGS, ids)] == ids
    finally:
        env.close()


def test_values_are_valid_against_the_schema(env: Env) -> None:
    env.write(
        "a",
        [assistant("2026-03-01T10:00:00.000Z", "m1", inp=5, out=6, cwd="/p", entry="cli", version="1"), turn(10, uuid="u"), cost_line(0.5)],
    )
    env.write("ag", [assistant("2026-03-01T10:00:00.000Z", "a1", out=1)])
    env.ingest("a")
    env.ingest("ag", agent="g", meta=AgentMeta(agent_type="Plan", tool_use_id="toolu_1"))
    doc = {**one(env).to_json(), "harness": None}
    validate_defs("session", doc)
    agents, _ = agent_values(env.conn, SESSION_A)
    validate_defs("agent", agents[0].to_json())


def test_json_numbers_are_finite_and_stable_under_repeat(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", model="claude-opus-4-5", inp=123456789, out=987654321, cr=5, w5=7, w1=11)])
    env.ingest("a")
    first = one(env).to_json()
    for _ in range(3):
        assert one(env).to_json() == first
    est = first["cost"]["estimated_usd"]  # type: ignore[index]
    assert est == float(f"{est:.6f}")


def test_agent_values_limit_and_order(env: Env) -> None:
    env.write("a", [assistant("2026-03-01T10:00:00.000Z", "m1", out=1)])
    env.ingest("a")
    for k, out in enumerate([5, 50, 50, 1]):
        env.write(f"g{k}", [assistant("2026-03-01T10:00:00.000Z", f"g{k}", out=out)])
        env.ingest(f"g{k}", agent=f"agent{k}")
    agents, truncated = agent_values(env.conn, SESSION_A, limit=3)
    assert [a.agent_id for a in agents] == ["agent1", "agent2", "agent0"]
    assert truncated
    assert not agent_values(env.conn, SESSION_A)[1]


def test_untouched_agent_row_has_zero_counters(env: Env) -> None:
    env.write("g", [])
    env.ingest("g", agent="agent9")
    agents, _ = agent_values(env.conn, SESSION_A)
    assert agents[0].tokens.total == 0 and agents[0].models == ()
