"""Parsing lines into events (issue #12): one synthetic fixture per row of the transcript field
map, the value rules, hostile lines and the per-file accumulator. Every fixture is synthetic."""

from __future__ import annotations

import dataclasses
import json
import math
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from session_audit.incremental import IncrementalRead, Progress
from session_audit.parser import (
    ARG_CAP,
    ERROR,
    MAX_BLOCKS,
    MIN_TS_MS,
    TEXT_CAP,
    DigestItem,
    DigestKind,
    Event,
    FileFacts,
    Kind,
    ParseContext,
    Status,
    parse_line,
    slot_of,
)

from .test_incremental import Ticking, opened

MTIME = datetime(2026, 11, 25, tzinfo=UTC).timestamp()
CTX = ParseContext(mtime_ns=int(MTIME * 1e9))
DCTX = ParseContext(mtime_ns=int(MTIME * 1e9), digest=True)
TS = "2026-03-04T05:06:07.250Z"
TS_MS = int(datetime(2026, 3, 4, 5, 6, 7, 250000, tzinfo=UTC).timestamp() * 1000)


def raw(obj: object) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def parse(obj: object, ctx: ParseContext = CTX):
    return parse_line(raw(obj), ctx)


def assistant(usage: object = None, content: object = None, **extra: object) -> dict:
    msg: dict = {"id": "msg_1", "model": "model-a"}
    if usage is not None:
        msg["usage"] = usage
    if content is not None:
        msg["content"] = content
    return {"type": "assistant", "uuid": "u1", "timestamp": TS, "message": msg, **extra}


# --- message tokens ----------------------------------------------------------------------------


def test_message_tokens_with_split():
    usage = {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 9,
        "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5},
    }
    r = parse(assistant(usage, sessionId="s", agentId="a1"))
    assert r.status is Status.OK
    (e,) = r.events
    assert e == Event(
        Kind.MESSAGE, "msg_1", None, 0, "s", "a1", TS_MS, None, "model-a", None, 1, 2, 3, 4, 5
    )
    assert (r.ts, r.cwd, r.entrypoint, r.version) == (TS_MS, None, None, None)


def test_session_and_agent_ids_over_128_chars_are_missing():
    r = parse(assistant({"output_tokens": 1}, sessionId="s" * 129, agentId="a" * 129))
    (e,) = r.events
    assert (e.line_session_id, e.agent_id) == (None, None)
    r = parse(assistant({"output_tokens": 1}, sessionId="s" * 128, agentId="a" * 128))
    assert (r.events[0].line_session_id, r.events[0].agent_id) == ("s" * 128, "a" * 128)


def test_message_without_split_counts_all_creation_as_5m():
    (e,) = parse(assistant({"cache_creation_input_tokens": 7})).events
    assert (e.cache_write_5m_tokens, e.cache_write_1h_tokens) == (7, 0)


def test_message_with_null_split_counts_all_creation_as_5m():
    usage = {"cache_creation_input_tokens": 7, "cache_creation": None}
    (e,) = parse(assistant(usage)).events
    assert (e.cache_write_5m_tokens, e.cache_write_1h_tokens) == (7, 0)


def test_message_missing_model_is_unknown_and_missing_id_is_none():
    obj = assistant({"output_tokens": 1})
    del obj["message"]["model"], obj["message"]["id"]
    (e,) = parse(obj).events
    assert (e.model, e.dedupe_id) == ("unknown", None)


def test_assistant_without_usage_has_no_message_event():
    assert parse(assistant()).events == ()


def test_synthetic_message_with_zero_usage_is_ignored_wholly():
    obj = assistant({"input_tokens": 0}, [{"type": "tool_use", "id": "t", "name": "Bash"}])
    obj["message"]["model"] = "<synthetic>"
    r = parse(obj, DCTX)
    assert (r.status, r.events, r.digest) == (Status.OK, (), ())


@pytest.mark.parametrize("usage", [None, "x", [1], {"output_tokens": None}])
def test_synthetic_message_with_absent_or_unusable_usage_is_zero_usage(usage):
    obj = assistant(None, [{"type": "tool_use", "id": "t", "name": "Bash"}])
    obj["message"]["model"] = "<synthetic>"
    if usage is not None:
        obj["message"]["usage"] = usage
    r = parse(obj, DCTX)
    assert (r.status, r.events, r.digest) == (Status.OK, (), ())


def test_synthetic_message_with_usage_is_kept():
    obj = assistant({"output_tokens": 1})
    obj["message"]["model"] = "<synthetic>"
    assert len(parse(obj).events) == 1


# --- tool call, tool error, denial, interruption ------------------------------------------------


def test_tool_call_per_tool_use_block_in_order():
    content = [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {}},
    ]
    r = parse(assistant(None, content))
    assert [(e.kind, e.dedupe_id, e.tool, e.model, e.block) for e in r.events] == [
        (Kind.TOOL_CALL, "t1", "Bash", "model-a", 1),
        (Kind.TOOL_CALL, "t2", "Read", "model-a", 2),
    ]


def test_message_and_tool_calls_on_one_line_are_separate_events():
    content = [{"type": "tool_use", "id": "t1", "name": "Bash"}]
    r = parse(assistant({"output_tokens": 1}, content))
    assert [e.kind for e in r.events] == [Kind.MESSAGE, Kind.TOOL_CALL]


def test_tool_use_without_id_or_name_is_kept_with_none():
    (e,) = parse(assistant(None, [{"type": "tool_use"}])).events
    assert (e.dedupe_id, e.tool) == (None, None)


def user(content: object = None, **extra: object) -> dict:
    msg: dict = {"role": "user"}
    if content is not None:
        msg["content"] = content
    return {"type": "user", "uuid": "u2", "timestamp": TS, "message": msg, **extra}


def test_tool_error_per_error_result_block():
    content = [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "boom"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "fine"},
        {"type": "tool_result", "tool_use_id": "t3", "is_error": "true"},
        {"type": "tool_result", "tool_use_id": "t4", "is_error": True},
    ]
    r = parse(user(content))
    assert [(e.kind, e.dedupe_id, e.ref_id, e.block) for e in r.events] == [
        (Kind.TOOL_ERROR, "t1", "t1", 0),
        (Kind.TOOL_ERROR, "t4", "t4", 3),
    ]


@pytest.mark.parametrize(
    "extra",
    [{"toolDenialKind": "user-rejected"}, {"permissionDecision": {"decision": "reject"}}],
)
def test_denial_takes_the_first_tool_result_id(extra):
    content = [
        {"type": "text", "text": "x"},
        {"type": "tool_result", "tool_use_id": "first"},
        {"type": "tool_result", "tool_use_id": "second"},
    ]
    r = parse(user(content, **extra))
    (e,) = r.events
    assert (e.kind, e.dedupe_id, e.ref_id) == (Kind.DENIAL, "u2", "first")


def test_denial_without_tool_result_has_no_id():
    (e,) = parse(user("text", toolDenialKind="x")).events
    assert (e.kind, e.ref_id) == (Kind.DENIAL, None)


def test_denial_and_error_on_one_line_are_two_events():
    content = [{"type": "tool_result", "tool_use_id": "t", "is_error": True}]
    r = parse(user(content, toolDenialKind="x"))
    assert [e.kind for e in r.events] == [Kind.TOOL_ERROR, Kind.DENIAL]


@pytest.mark.parametrize(
    "extra",
    [
        {"permissionDecision": {"decision": "allow"}},
        {"permissionDecision": "reject"},
        {"permissionDecision": None},
        {"toolDenialKind": None},
        {"toolDenialKind": False},
        {"toolDenialKind": 0},
        {"toolDenialKind": ""},
        {"toolDenialKind": {"a": 1}},
    ],
)
def test_no_denial(extra):
    assert parse(user("t", **extra)).events == ()


def test_interruption_has_no_tool_id():
    content = [{"type": "tool_result", "tool_use_id": "t"}]
    (e,) = parse(user(content, interruptedMessageId="m")).events
    assert (e.kind, e.dedupe_id, e.ref_id) == (Kind.INTERRUPTION, "u2", None)


def test_denial_fields_on_a_non_user_line_are_ignored():
    obj = assistant()
    obj["toolDenialKind"] = "x"
    assert parse(obj).events == ()


# --- turn, compaction, cost, identity ------------------------------------------------------------


def test_turn_duration():
    obj = {"type": "system", "subtype": "turn_duration", "uuid": "u3", "durationMs": 1500}
    (e,) = parse(obj).events
    assert (e.kind, e.dedupe_id, e.seconds) == (Kind.TURN, "u3", 1.5)


def test_turn_duration_missing_or_other_system_lines_have_no_event():
    assert parse({"type": "system", "subtype": "turn_duration"}).events == ()
    other = {"type": "system", "subtype": "stop_hook_summary", "durationMs": 5}
    r = parse(other)
    assert (r.status, r.events) == (Status.OK, ())


def test_compaction():
    obj = {"type": "system", "subtype": "compact_boundary", "uuid": "u4"}
    (e,) = parse(obj).events
    assert (e.kind, e.dedupe_id) == (Kind.COMPACTION, "u4")


def test_line_without_uuid_has_no_dedupe_id():
    (e,) = parse({"type": "system", "subtype": "compact_boundary"}).events
    assert e.dedupe_id is None


def test_identity_of_every_line_type():
    ident = {"cwd": "/p", "entrypoint": "cli", "version": "1.2.3", "timestamp": TS}
    for obj in ({"type": "user", **ident}, {"type": "zzz", **ident}, {"type": "system", **ident}):
        r = parse(obj)
        assert (r.ts, r.cwd, r.entrypoint, r.version) == (TS_MS, "/p", "cli", "1.2.3")


def test_agent_line_carries_parent_session_and_own_agent():
    (e,) = parse(assistant({"output_tokens": 1}, sessionId="parent", agentId="ag")).events
    assert (e.line_session_id, e.agent_id) == ("parent", "ag")


@pytest.mark.parametrize("value", [0, 1, 12.5, 1e6, 1000000, -0.0])
def test_cost_state_valid(value):
    r = parse({"type": "cost-state", "totalCostUSD": value})
    assert r.status is Status.OK and r.cost_usd == float(value) and not r.cost_invalid
    assert r.cost_usd is not None and math.copysign(1, r.cost_usd) > 0


@pytest.mark.parametrize(
    "text",
    [
        "1e999",
        "-1e999",
        "NaN",
        "Infinity",
        "-1",
        "true",
        "false",
        '"5"',
        "2e6",
        "null1",
        "[]",
        "{}",
        "1000000.01",
        "9" * 5000,
    ],
)
def test_cost_state_invalid_is_ignored_and_flagged(text):
    r = parse_line(b'{"type":"cost-state","totalCostUSD":' + text.encode() + b"}\n", CTX)
    if text == "null1":
        assert r.status is Status.ERROR and not r.cost_invalid  # not even JSON
        return
    assert r.status is Status.ERROR and r.cost_invalid and r.cost_usd is None


def test_cost_state_without_the_key_is_ignored_quietly():
    r = parse({"type": "cost-state"})
    assert (r.status, r.cost_usd, r.cost_invalid) == (Status.OK, None, False)


def test_cost_state_with_a_null_value_is_invalid():
    r = parse({"type": "cost-state", "totalCostUSD": None})
    assert (r.status, r.cost_usd, r.cost_invalid) == (Status.ERROR, None, True)


def test_cost_state_in_an_agent_file_is_ignored():
    ctx = ParseContext(mtime_ns=CTX.mtime_ns, main=False)
    for value in (5, -1):
        r = parse({"type": "cost-state", "totalCostUSD": value}, ctx)
        assert (r.status, r.cost_usd, r.cost_invalid) == (Status.OK, None, False)


# --- numeric rules -------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 2**32 + 1, True, False, "5", 1.5, 5.0, [], {}, 1e999])
@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"],
)
def test_bad_token_counter_is_an_error_line(field, bad):
    r = parse_line(json.dumps(assistant({field: bad})).encode(), CTX)
    assert r.status is Status.ERROR
    assert r.events == ()


def test_bad_split_counters_are_error_lines():
    for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        r = parse(assistant({"cache_creation": {key: -1}}))
        assert r.status is Status.ERROR


def test_bad_duration_is_an_error_line():
    for bad in (-1, 2**32 + 1, True, "5", 1.5):
        r = parse({"type": "system", "subtype": "turn_duration", "durationMs": bad})
        assert r.status is Status.ERROR


def test_boundary_values_are_kept():
    (e,) = parse(assistant({"input_tokens": 2**32, "output_tokens": 0})).events
    assert e.input_tokens == 2**32
    (t,) = parse({"type": "system", "subtype": "turn_duration", "durationMs": 2**32}).events
    assert t.seconds is not None and math.isfinite(t.seconds) and t.seconds == 2**32 / 1000


def test_null_counters_are_absent():
    (e,) = parse(assistant({"input_tokens": None, "output_tokens": 3})).events
    assert (e.input_tokens, e.output_tokens) == (0, 3)


def test_bad_value_in_one_block_drops_the_whole_line():
    r = parse(assistant({"input_tokens": -1}, [{"type": "tool_use", "id": "t"}]), DCTX)
    assert (r.status, r.events, r.digest, r.ts) == (Status.ERROR, (), (), None)


# --- timestamps ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stamp",
    [
        "2019-12-31T23:59:59Z",
        "2026-11-27T00:00:00Z",
        "nope",
        "",
        5,
        None,
        True,
        "2026-03-04",
        "2026-03-04T05:06:07",
        "0001-01-01T00:00:00+23:00",
        "9999-12-31T23:59:59-23:00",
        "2026-03-04T05:06:07Z" + " " * 100,
    ],
)
def test_unusable_timestamps_are_missing_not_errors(stamp):
    r = parse({"type": "user", "timestamp": stamp})
    assert (r.status, r.ts) == (Status.OK, None)


def test_timestamp_bounds_are_inclusive_and_zones_convert():
    assert parse({"type": "user", "timestamp": "2020-01-01T00:00:00Z"}).ts == MIN_TS_MS
    assert parse({"type": "user", "timestamp": "2020-01-01T01:00:00+01:00"}).ts == MIN_TS_MS
    upper = "2026-11-26T00:00:00Z"
    assert parse({"type": "user", "timestamp": upper}).ts is not None


def test_backwards_timestamps_are_kept_as_read():
    a = parse({"type": "user", "timestamp": "2026-03-04T05:06:07Z"}).ts
    b = parse({"type": "user", "timestamp": "2026-03-04T05:00:00Z"}).ts
    assert a is not None and b is not None and b < a


# --- strings -------------------------------------------------------------------------------------


def test_names_are_capped_and_surrogates_fixed():
    obj = assistant({"output_tokens": 1})
    obj["message"]["model"] = "m" * 500
    obj["cwd"] = "c" * 5000
    obj["version"] = "v\ud800" + "x" * 300
    obj["entrypoint"] = "e" * 200
    r = parse_line(json.dumps(obj, ensure_ascii=True).encode(), CTX)
    assert r.cwd and r.entrypoint and r.version and r.events[0].model
    assert len(r.events[0].model) == 128
    assert len(r.cwd) == 4096 and len(r.entrypoint) == 128
    assert r.version.startswith("v�") and len(r.version) == 128
    r.version.encode("utf-8")


def test_lone_surrogate_escape_in_id_is_replaced():
    obj = assistant({"output_tokens": 1})
    obj["message"]["id"] = "a\ud800b"
    (e,) = parse_line(json.dumps(obj).encode(), CTX).events
    assert e.dedupe_id == "a�b"


def test_overlong_or_wrong_type_ids_are_missing():
    obj = assistant({"output_tokens": 1}, [{"type": "tool_use", "id": "x" * 5000, "name": 5}])
    m, t = parse(obj).events
    assert (t.dedupe_id, t.tool) == (None, None) and m.dedupe_id == "msg_1"
    obj["message"]["id"] = 7
    assert parse(obj).events[0].dedupe_id is None


def test_wrong_shapes_are_missing_not_errors():
    for obj in (
        {"type": "assistant", "message": "x"},
        {"type": "assistant", "message": {"usage": 5, "content": 5}},
        {"type": "assistant", "message": {"content": [1, "a", None, [], {"type": 5}]}},
        {"type": "user", "message": {"content": [1, None, {"type": "tool_result"}]}},
        {"type": "user", "message": None},
        {"type": "system", "subtype": ["x"]},
    ):
        r = parse(obj)
        assert r.status is Status.OK and r.events == ()


# --- unknown and error lines ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj",
    [
        {},
        {"type": "queue-operation"},
        {"type": 5},
        {"type": ["a"]},
        {"type": None},
        {"type": {"a": 1}},
    ],
)
def test_unknown_types(obj):
    assert parse(obj).status is Status.UNKNOWN


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\n",
        b"   \n",
        b"[]\n",
        b"1\n",
        b'"s"\n',
        b"null\n",
        b"not json\n",
        b"{\n",
        b'{"a":1}x\n',
        b'{"type":"user"\x00}\n',
        b"\x00\n",
        b"\xef\xbb\xbf{}\n",
        b"\xff\xfe{\x00}\x00\n",
        b'{"a": NaN, "a": 1,}\n',
        b"{'a':1}\n",
    ],
)
def test_error_lines(data):
    assert parse_line(data, CTX) is ERROR


def test_invalid_utf8_and_cut_multibyte_are_tolerated():
    data = b'{"type":"user","cwd":"/p\xff\xe2\x82","uuid":"u"}\n'
    r = parse_line(data, CTX)
    assert r.status is Status.OK and r.cwd == "/p��"


def test_utf16_looking_input_is_not_sniffed():
    data = '{"type":"user"}'.encode("utf-16-le")
    assert parse_line(data, CTX).status is Status.ERROR


def test_nan_and_infinity_outside_numeric_fields_are_harmless():
    r = parse_line(b'{"type":"user","x":NaN,"y":[Infinity,-Infinity,1e999],"uuid":"u"}\n', CTX)
    assert r.status is Status.OK


def test_duplicate_keys_last_wins():
    r = parse_line(b'{"type":"cost-state","totalCostUSD":-1,"totalCostUSD":3}\n', CTX)
    assert r.cost_usd == 3.0


@pytest.mark.parametrize("depth", [500, 5000, 200_000])
def test_deep_nesting_is_an_error_line_quickly(depth):
    for open_, close in ((b"[", b"]"), (b'{"a":', b"}")):
        start = time.monotonic()
        r = parse_line(b'{"type":"user","x":' + open_ * depth + close * depth + b"}\n", CTX)
        assert r.status in (Status.OK, Status.ERROR)
        assert time.monotonic() - start < 5
    r = parse_line(b'{"type":"user","x":' + b"[" * depth + b"\n", CTX)
    assert r is ERROR


def test_huge_integers_and_exponents_never_raise():
    digits = b"9" * 20000
    for number in (digits, b"-" + digits, b"1e99999999999999", b"0." + b"0" * 100000 + b"1"):
        r = parse_line(b'{"type":"user","x":' + number + b',"uuid":"u"}\n', CTX)
        assert r.status is Status.OK
    r = parse_line(
        b'{"type":"system","subtype":"turn_duration","durationMs":' + digits + b"}\n", CTX
    )
    assert r.status is Status.ERROR


def test_huge_string_is_capped():
    big = "x" * (4 * 1024 * 1024)
    r = parse(assistant({"output_tokens": 1}, [{"type": "text", "text": big}]), DCTX)
    (item,) = r.digest
    assert len(item.text) == TEXT_CAP
    r = parse(user(big), DCTX)
    assert len(r.digest[0].text) == TEXT_CAP


def test_too_many_blocks_is_an_error_line():
    ok = [{"type": "tool_use", "id": str(i), "name": "t"} for i in range(MAX_BLOCKS)]
    assert len(parse(assistant(None, ok)).events) == MAX_BLOCKS
    assert parse(assistant(None, ok + [{}])).status is Status.ERROR
    assert parse(user(ok + [{}])).status is Status.ERROR


def test_ordering_is_deterministic_and_results_are_immutable():
    r = parse(assistant({"output_tokens": 1}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.events[0].kind = Kind.TURN  # type: ignore[misc]


# --- digest-only facts ---------------------------------------------------------------------------


def test_digest_items_only_when_asked():
    obj = user("hello there")
    assert parse(obj).digest == ()
    (item,) = parse(obj, DCTX).digest
    assert (item.kind, item.text, item.uuid, item.ts) == (
        DigestKind.PROMPT,
        "hello there",
        "u2",
        TS_MS,
    )


def test_prompt_rules():
    blocks = [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]
    assert parse(user(blocks), DCTX).digest[0].text == "a b"
    assert parse(user(blocks + [{"type": "tool_result"}]), DCTX).digest == ()
    assert parse(user("x", isMeta=True), DCTX).digest == ()
    assert parse(user("x", isCompactSummary=True), DCTX).digest == ()
    assert parse(user(""), DCTX).digest == ()
    assert parse(user([{"type": "image"}]), DCTX).digest == ()


def test_assistant_text_items_per_block_with_message_id():
    content = [
        {"type": "thinking", "thinking": "secret"},
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    items = parse(assistant({"output_tokens": 1}, content), DCTX).digest
    assert [(i.kind, i.text, i.block, i.message_id) for i in items] == [
        (DigestKind.ASSISTANT, "one", 1, "msg_1"),
        (DigestKind.ASSISTANT, "two", 2, "msg_1"),
    ]


def test_rejection_item_and_denial_event():
    content = [
        {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "no"}]}
    ]
    r = parse(user(content, toolDenialKind="user-rejected"), DCTX)
    assert [e.kind for e in r.events] == [Kind.DENIAL]
    assert [(i.kind, i.text) for i in r.digest] == [(DigestKind.REJECTION, "no")]
    other = parse(user(content, toolDenialKind="auto"), DCTX)
    assert other.digest == ()


def test_tool_call_argument_summaries():
    def call(name, inp):
        r = parse(
            assistant(None, [{"type": "tool_use", "id": "t", "name": name, "input": inp}]), DCTX
        )
        return [i.text for i in r.digest]

    assert call("Bash", {"command": "ls -l"}) == ["ls -l"]
    assert call("Read", {"file_path": "/a/b"}) == ["/a/b"]
    assert call("Grep", {"pattern": "x"}) == ["x"]
    assert call("WebFetch", {"url": "https://example.com:8080/p?q=1"}) == ["example.com"]
    assert call("Task", {"subagent_type": "Explore"}) == ["Explore"]
    assert call("Skill", {"skill": "s"}) == ["s"]
    assert call("mcp__x", {"command": "no"}) == [""]
    assert call("WebSearch", {"query": "q"}) == [""]
    assert call("Bash", {}) == [""]
    assert call("Bash", "not a dict") == [""]
    assert call("WebFetch", {"url": "http://[bad"}) == [""]
    assert len(call("Bash", {"command": "c" * 10000})[0]) == ARG_CAP


def test_digest_text_cannot_be_stored_or_printed_by_accident():
    event_fields = {f.name: f.type for f in dataclasses.fields(Event)}
    assert not any("text" in n or "content" in n or "input" == n for n in event_fields)
    assert set(event_fields.values()) <= {"Kind", "str | None", "int", "int | None", "float | None"}
    assert {f.name for f in dataclasses.fields(Event)}.isdisjoint(
        {f.name for f in dataclasses.fields(DigestItem)} - {"kind", "block", "uuid", "ts", "tool"}
    )
    (item,) = parse(user("SECRET-PROMPT"), DCTX).digest
    assert "SECRET-PROMPT" not in repr(item)
    r = parse(user("SECRET-PROMPT"), DCTX)
    assert "SECRET-PROMPT" not in repr(r)
    for e in parse(
        assistant({"output_tokens": 1}, [{"type": "text", "text": "SECRET"}]), DCTX
    ).events:
        assert "SECRET" not in repr(e)


def test_events_hold_no_text_from_the_line():
    secret = "SECRET-" * 10
    content = [
        {"type": "text", "text": secret},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": secret}},
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": secret},
    ]
    events = parse(assistant({"output_tokens": 1}, content), DCTX).events
    events += parse(user(content, toolDenialKind="user-rejected"), DCTX).events
    assert events and all("SECRET" not in repr(e) for e in events)


def test_error_results_carry_no_line_content():
    r = parse_line(b'{"type":"user","uuid":"SECRET-ID"\x00}\n', CTX)
    assert r is ERROR and "SECRET" not in repr(r)


# --- per-file accumulator ------------------------------------------------------------------------


def test_slot_of():
    assert slot_of(0) == 0 and slot_of(899_999) == 0 and slot_of(900_000) == 1


def test_file_facts_counts_and_facts():
    facts = FileFacts()
    lines = [
        {
            "type": "user",
            "timestamp": "2026-03-04T05:06:07Z",
            "cwd": "/a",
            "entrypoint": "cli",
            "version": "1",
            "uuid": "u",
        },
        {"type": "mystery"},
        {
            "type": "user",
            "timestamp": "2026-03-04T05:00:00Z",
            "cwd": "/b",
            "version": "2",
            "interruptedMessageId": "m",
        },
        {"type": "cost-state", "totalCostUSD": 2.5},
        {"type": "cost-state", "totalCostUSD": -1},
        {"type": "cost-state", "totalCostUSD": 3.5},
        {"type": "system", "subtype": "compact_boundary"},
    ]
    out = [facts.add(parse(o)) for o in lines]
    facts.add(parse_line(b"junk\n", CTX))
    assert (facts.parse_errors, facts.unknown_lines) == (2, 1)
    assert facts.cost_invalid and facts.cost_state_usd == 3.5
    assert (facts.first_cwd, facts.first_entrypoint, facts.last_version) == ("/a", "cli", "2")
    first = int(datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC).timestamp() * 1000)
    later = int(datetime(2026, 3, 4, 5, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert (facts.min_ts, facts.max_ts, facts.last_ts) == (later, first, later)
    # the interruption has its own ts; the compaction has none and takes the last_ts slot
    assert out[2][0].slot == slot_of(later)
    assert out[6][0].ts is None and out[6][0].slot == slot_of(later)


def test_slot_is_null_until_a_timestamp_was_read():
    facts = FileFacts()
    (e,) = facts.add(parse({"type": "system", "subtype": "compact_boundary"}))
    assert e.slot is None
    facts.add(parse({"type": "user", "timestamp": TS}))
    (e,) = facts.add(parse({"type": "system", "subtype": "compact_boundary"}))
    assert e.slot == slot_of(TS_MS)


def test_error_line_does_not_touch_identity():
    facts = FileFacts()
    facts.add(
        parse(
            {
                "type": "assistant",
                "cwd": "/x",
                "timestamp": TS,
                "message": {"usage": {"input_tokens": -1}},
            }
        )
    )
    assert (facts.first_cwd, facts.min_ts, facts.last_ts) == (None, None, None)


# --- every number is finite and bounded ----------------------------------------------------------


def numbers(result) -> list[float]:
    out: list[float] = []
    for e in result.events:
        for f in dataclasses.fields(e):
            v = getattr(e, f.name)
            if f.name in ("kind", "ts", "slot") or v is None or isinstance(v, str):
                continue
            out.append(v)
    out += [v for v in (result.cost_usd,) if v is not None]
    return out


def test_every_output_number_is_finite_and_capped():
    rng = random.Random(7)
    seen = 0
    facts = FileFacts()
    weird = [
        0,
        1,
        -1,
        2**32,
        2**32 + 1,
        2**63,
        10**30,
        1.5,
        1e999,
        -1e999,
        float("nan"),
        True,
        "7",
        None,
    ]
    for _ in range(300):
        usage = {
            k: rng.choice(weird)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        }
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": rng.choice(weird),
            "ephemeral_1h_input_tokens": rng.choice(weird),
        }
        for obj in (
            assistant(usage),
            {"type": "cost-state", "totalCostUSD": rng.choice(weird)},
            {
                "type": "system",
                "subtype": "turn_duration",
                "durationMs": rng.choice(weird),
                "timestamp": TS,
            },
        ):
            r = parse_line(json.dumps(obj).encode(), CTX)  # json.dumps emits NaN/Infinity tokens
            for n in numbers(r):
                seen += 1
                assert type(n) is not bool and math.isfinite(n) and 0 <= n <= 2**32 * 1.0 + 1e6
            assert r.ts in (None, TS_MS)
            for e in facts.add(r):
                assert e.ts in (None, TS_MS) and e.slot in (None, slot_of(TS_MS))
            json.dumps(numbers(r), allow_nan=False)
    assert seen > 0 and facts.min_ts in (None, TS_MS)


# --- chunked reads give the same events ----------------------------------------------------------


def corpus(rng: random.Random, n: int) -> list[bytes]:
    pool = [
        raw(
            assistant(
                {"input_tokens": 1, "output_tokens": 2},
                [{"type": "tool_use", "id": "t", "name": "Bash"}],
            )
        ),
        raw(
            user(
                [{"type": "tool_result", "tool_use_id": "t", "is_error": True}], toolDenialKind="x"
            )
        ),
        raw({"type": "system", "subtype": "turn_duration", "durationMs": 5, "timestamp": TS}),
        raw({"type": "system", "subtype": "compact_boundary", "uuid": "c"}),
        raw({"type": "cost-state", "totalCostUSD": 1.25}),
        raw({"type": "cost-state", "totalCostUSD": -3}),
        raw({"type": "user", "cwd": "/p", "timestamp": "2026-03-04T05:00:00Z"}),
        raw({"type": "zzz"}),
        b"not json\n",
        b"\n",
        b'{"type":"user","x":"\xff\xe2\x82"}\n',
        b'{"a":\x00}\n',
        b"[" * 3000 + b"\n",
    ]
    return [rng.choice(pool) for _ in range(n)]


def read_all(path: Path, cuts: list[int], blob: bytes) -> tuple[list, FileFacts]:
    """Append ``blob`` in pieces cut at ``cuts``; read after each piece with the stored progress."""
    path.write_bytes(b"")
    progress: Progress | None = None
    facts = FileFacts()
    events: list = []
    last = 0
    for cut in [*cuts, len(blob)]:
        with open(path, "ab") as handle:
            handle.write(blob[last:cut])
        last = cut
        handle = opened(path)
        try:
            run = IncrementalRead(
                handle,
                progress,
                gateway=Ticking(),
                rules_version=1,
                file_budget=__import__("datetime").timedelta(seconds=60),
                run_deadline=None,
            )
            for line in run:
                events.extend((line.offset, e) for e in facts.add(parse_line(line.data, CTX)))
            progress = run.checkpoint()
        finally:
            handle.close()
    return events, facts


@pytest.mark.parametrize("seed", range(5))
def test_chunked_reads_give_the_same_events(tmp_path: Path, seed: int):
    rng = random.Random(seed)
    blob = b"".join(corpus(rng, 80))
    whole_events, whole_facts = read_all(tmp_path / "a.jsonl", [], blob)
    cuts = sorted(rng.randrange(1, len(blob)) for _ in range(rng.randrange(1, 12)))
    part_events, part_facts = read_all(tmp_path / "b.jsonl", cuts, blob)
    assert whole_events and part_events == whole_events
    assert part_facts == whole_facts
