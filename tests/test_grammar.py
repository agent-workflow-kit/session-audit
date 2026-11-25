"""The id, prefix, duration, size and time grammars (issue #5).

The source is pure ASCII: every special code point is written as an escape. The clock is always
passed in (``NOW``), so no test reads the real one.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from session_audit import grammar
from session_audit.grammar import (
    AmbiguousPrefixError,
    GrammarError,
    GrammarUsageError,
    PrefixNotFoundError,
    format_timestamp,
    format_timestamp_ms,
    parse_actor,
    parse_ago,
    parse_csession_id,
    parse_decision_id,
    parse_duration,
    parse_event_id,
    parse_ingest_every,
    parse_int,
    parse_label,
    parse_label_key,
    parse_label_value,
    parse_retro_id,
    parse_retro_prefix,
    parse_session_id,
    parse_session_prefix,
    parse_size,
    parse_time_zone,
    parse_when,
    resolve_prefix,
)

NOW = datetime(2026, 11, 25, 12, 30, 15, tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")
F = "--flag"
SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

# Text that must never be echoed raw by an error.
HOSTILE = [
    "\x1b[31mred\x1b[0m",
    "\x1b]0;title\x07",
    "a‮b",
    "x\nerror: forged",
    "\ud800",
    "\x00",
    "<U+0041>",
    "9" * 5000,
    "٣٤",  # Arabic-Indic digits
]

UUID_OK = "0f8fad5b-d9cb-469f-a165-70867728950e"
CS_OK = "cs-abcdefgh"
RA_OK = "ra-abcdefg2"
EV_OK = "ev-" + "a2" * 13


def refuses(call, *, field: str = F, cls: type[GrammarError] = GrammarUsageError) -> GrammarError:
    with pytest.raises(cls) as info:
        call()
    err = info.value
    assert err.field == field
    if cls is GrammarUsageError:
        assert err.exit_code == 2
        assert err.code == "usage"
    return err


def sec(n: float) -> timedelta:
    return timedelta(seconds=n)


# ---------------------------------------------------------------------------------------------
# errors


def test_usage_error_carries_the_envelope_pieces():
    err = refuses(lambda: parse_session_id("nope", field="--session"), field="--session")
    assert err.exit_code == 2
    assert err.code == "usage"
    assert err.details == {"field": "--session"}
    assert isinstance(err.message, str) and err.message
    assert "nope" not in err.message and "nope" not in str(err)


@pytest.mark.parametrize("text", HOSTILE)
def test_errors_never_echo_the_input(text: str):
    calls = [
        lambda: parse_session_id(text, field=F),
        lambda: parse_session_prefix(text, field=F),
        lambda: parse_retro_prefix(text, field=F),
        lambda: parse_csession_id(text, field=F),
        lambda: parse_label(text, field=F),
        lambda: parse_actor(text, field=F),
        lambda: parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE),
        lambda: parse_duration(text, field=F, maximum=sec(10)),
        lambda: parse_size(text, field=F),
        lambda: parse_int(text, field=F, minimum=1, maximum=9),
        lambda: parse_ingest_every(text, field=F),
        lambda: parse_time_zone(text, field=F),
    ]
    for call in calls:
        err = refuses(call)
        shown = str(err) + json.dumps(err.details) + (err.hint or "")
        assert text not in shown
        assert "\x1b" not in shown and "\x00" not in shown and "\n" not in shown


def test_hostile_field_name_is_sanitized_in_text_not_in_details():
    err = refuses(lambda: parse_session_id("x", field="a\x1b[0mb"), field="a\x1b[0mb")
    assert "\x1b" not in str(err)


def test_non_text_values_are_usage_errors():
    for bad in (None, b"5s", 1.5, [5], True):
        refuses(lambda bad=bad: parse_session_id(bad, field=F))  # type: ignore[arg-type]
        refuses(lambda bad=bad: parse_when(bad, field=F, now=NOW, time_zone=UTC_ZONE))  # type: ignore[arg-type]
    for bad in (None, b"5s", [5], True):  # a float is a valid duration (seconds)
        refuses(lambda bad=bad: parse_duration(bad, field=F, maximum=sec(9)))  # type: ignore[arg-type]
    for bad in (None, b"5s", 1.5, [5], True):
        refuses(lambda bad=bad: parse_size(bad, field=F))  # type: ignore[arg-type]
        refuses(lambda bad=bad: parse_int(bad, field=F, minimum=1, maximum=9))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# ids


def test_session_id_full_lowercase_uuid():
    assert parse_session_id(UUID_OK, field=F) == UUID_OK
    assert parse_session_id(str(uuid.uuid4()), field=F)


@pytest.mark.parametrize(
    "text",
    [
        "",
        UUID_OK.upper(),
        UUID_OK + "\n",
        " " + UUID_OK,
        UUID_OK[:-1],
        UUID_OK + "0",
        UUID_OK.replace("-", ""),
        "{" + UUID_OK + "}",
        "0f8fad5b-d9cb-469f-a165-70867728950g",
        "٠f8fad5b-d9cb-469f-a165-70867728950e",
    ],
)
def test_session_id_refusals(text: str):
    refuses(lambda: parse_session_id(text, field=F))


def test_csession_id():
    assert parse_csession_id(CS_OK, field=F) == CS_OK
    assert parse_csession_id("cs-234567ab", field=F) == "cs-234567ab"
    for bad in [
        "cs-abcdefg",
        "cs-abcdefghi",
        "cs-abcdefg1",
        "cs-ABCDEFGH",
        "cs-abcdefg8",
        "CS-abcdefgh",
        CS_OK + "\n",
    ]:
        refuses(lambda bad=bad: parse_csession_id(bad, field=F))


def test_csession_id_hint_when_given_a_claude_code_session_id():
    err = refuses(lambda: parse_csession_id(UUID_OK, field="--session"), field="--session")
    assert err.hint and "claude-session id" in err.hint
    err = refuses(lambda: parse_csession_id(UUID_OK[:8], field="--session"), field="--session")
    assert err.hint and "claude-session id" in err.hint
    other = refuses(lambda: parse_csession_id("garbage!", field="--session"), field="--session")
    assert not other.hint or "Claude Code session" not in other.hint


def test_session_prefix_rejects_a_claude_session_id_with_a_hint():
    err = refuses(lambda: parse_session_prefix(CS_OK, field="--session"), field="--session")
    assert err.hint and "Claude Code session id" in err.hint


def test_retro_id_and_event_id():
    assert parse_retro_id(RA_OK, field=F) == RA_OK
    assert parse_event_id(EV_OK, field=F) == EV_OK
    for bad in [
        "ra-abcdefg",
        "ra-abcdefg21",
        "ra-ABCDEFG2",
        "ra-abcdefg1",
        RA_OK + "\n",
        "ca-abcdefg2",
    ]:
        refuses(lambda bad=bad: parse_retro_id(bad, field=F))
    for bad in [
        "ev-" + "a" * 25,
        "ev-" + "a" * 27,
        "ev-" + "A" * 26,
        "ev-" + "1" * 26,
        EV_OK + "\n",
    ]:
        refuses(lambda bad=bad: parse_event_id(bad, field=F))


def test_label_key_boundaries():
    assert parse_label_key("a", field=F) == "a"
    assert parse_label_key("0_a.b-c", field=F) == "0_a.b-c"
    assert parse_label_key("a" * 64, field=F) == "a" * 64
    for bad in ["", "a" * 65, "_a", "-a", ".a", "A", "a b", "a=b", "a\n", "é"]:
        refuses(lambda bad=bad: parse_label_key(bad, field=F))


def test_label_value_boundaries():
    assert parse_label_value("", field=F) == ""
    assert parse_label_value("A-Za-z0-9._:/#@+=,-", field=F) == "A-Za-z0-9._:/#@+=,-"
    assert parse_label_value("v" * 256, field=F) == "v" * 256
    for bad in ["v" * 257, "a b", "a\n", "a;b", "é", "a\x00", "a*"]:
        refuses(lambda bad=bad: parse_label_value(bad, field=F))


def test_label_pair_splits_at_the_first_equals():
    assert parse_label("team=core", field="--label") == ("team", "core")
    assert parse_label("k=", field="--label") == ("k", "")
    assert parse_label("k=a=b,c", field="--label") == ("k", "a=b,c")
    for bad in ["", "=v", "noequals", "K=v", "k=v w", "k=" + "v" * 257, "k=v\n"]:
        refuses(lambda bad=bad: parse_label(bad, field="--label"), field="--label")


def test_actor_grammar():
    assert parse_actor("human:alice", field=F) == "human:alice"
    assert parse_actor("auto:cron.job@x/y-1", field=F) == "auto:cron.job@x/y-1"
    assert parse_actor("human:" + "a" * 64, field=F)
    for bad in [
        "human:",
        "human:" + "a" * 65,
        "bot:x",
        "Human:x",
        "human:a b",
        "human:a\n",
        "alice",
        "human:é",
    ]:
        refuses(lambda bad=bad: parse_actor(bad, field=F))


def test_decision_id_grammar():
    assert parse_decision_id("D-1:a/b@c._", field=F) == "D-1:a/b@c._"
    assert parse_decision_id("d" * 128, field=F)
    for bad in ["", "d" * 129, "a b", "a\n", "a#b", "é"]:
        refuses(lambda bad=bad: parse_decision_id(bad, field=F))


def test_id_grammars_agree_with_the_schemas():
    defs = json.loads((SCHEMAS / "defs.json").read_text())["$defs"]
    pairs = {
        "session_id": lambda s: parse_session_id(s, field=F),
        "csession_id": lambda s: parse_csession_id(s, field=F),
        "retro_id": lambda s: parse_retro_id(s, field=F),
        "label_key": lambda s: parse_label_key(s, field=F),
        "actor": lambda s: parse_actor(s, field=F),
        "decision_id": lambda s: parse_decision_id(s, field=F),
    }
    probes = [
        UUID_OK,
        UUID_OK.upper(),
        UUID_OK + "\n",
        CS_OK,
        "cs-abcdefg1",
        RA_OK,
        "ra-abcdefg21",
        "a",
        "A",
        "_a",
        "a" * 65,
        "human:x",
        "human:",
        "auto:a\n",
        "d" * 129,
        "a#b",
        "",
    ]
    for name, parse in pairs.items():
        pattern = re.compile(defs[name]["pattern"])
        for probe in probes:
            ok_schema = bool(pattern.search(probe)) and len(probe) <= defs[name].get(
                "maxLength", 10**9
            )
            try:
                parse(probe)
                ok_code = True
            except GrammarError:
                ok_code = False
            assert ok_code == ok_schema, (name, probe)


# ---------------------------------------------------------------------------------------------
# prefixes


def test_session_prefix_lowercases_and_checks_length():
    assert parse_session_prefix("0F8FAD5B", field=F) == "0f8fad5b"
    assert parse_session_prefix(UUID_OK[:9], field=F) == UUID_OK[:9]
    assert parse_session_prefix(UUID_OK[:13], field=F) == UUID_OK[:13]
    assert parse_session_prefix(UUID_OK.upper(), field=F) == UUID_OK
    for n in range(0, 8):
        refuses(lambda n=n: parse_session_prefix(UUID_OK[:n], field=F))


@pytest.mark.parametrize(
    "text",
    [
        UUID_OK + "0",
        "0f8fad5bd9cb",  # dashes are part of the shape
        "0f8fad5b-d9cbd",  # a hex digit where the dash belongs
        "0f8fad5b-d9cb-469f-a165-70867728950e\n",
        "0f8fad5b ",
        " 0f8fad5b",
        "0f8fad5g",
        "٠f8fad5b",
        "0f8fad5b-d9cb-469f-a165-7086772895-e",
        "İ" * 8,  # Turkish dotted I lowercases to two characters
        "０１２３４５６７",  # fullwidth digits
    ],
)
def test_session_prefix_refusals(text: str):
    refuses(lambda: parse_session_prefix(text, field=F))


def test_retro_prefix():
    assert parse_retro_prefix("RA-ABCD", field=F) == "ra-abcd"
    assert parse_retro_prefix("ra-abcdefg2", field=F) == "ra-abcdefg2"
    for bad in [
        "ra-",
        "ra-abc",
        "ra-ab1d",
        "abcd",
        "ra-abcdefg23",
        "ra-abcd\n",
        "ra_abcd",
        "ra-аbcd",
    ]:
        refuses(lambda bad=bad: parse_retro_prefix(bad, field=F))


def test_resolve_unique_prefix():
    ids = [UUID_OK, "ffffffff-d9cb-469f-a165-70867728950e"]
    assert resolve_prefix("0f8fad5b", ids, field=F) == UUID_OK
    assert resolve_prefix(UUID_OK, ids, field=F) == UUID_OK


def test_resolve_ambiguous_prefix_caps_and_sorts_the_matches():
    ids = [f"0f8fad5b-0000-0000-0000-{i:012x}" for i in range(45)]
    ids.append("ffffffff-0000-0000-0000-000000000000")
    shuffled = list(reversed(ids))
    with pytest.raises(AmbiguousPrefixError) as info:
        resolve_prefix("0f8fad5b", shuffled, field="--session")
    err = info.value
    assert err.exit_code == 21
    assert err.code == "ambiguous"
    assert err.details["matched"] == 45
    assert err.details["matches"] == sorted(ids[:45])[:20]
    assert len(list(err.details["matches"])) == 20  # type: ignore[call-overload]
    assert err.field == "--session"


def test_resolve_ambiguous_with_two_matches_lists_both():
    ids = ["ra-abcdefg2", "ra-abcdefg3", "ra-zzzzzzzz"]
    with pytest.raises(AmbiguousPrefixError) as info:
        resolve_prefix("ra-abcd", ids, field=F)
    assert info.value.details == {"matches": ["ra-abcdefg2", "ra-abcdefg3"], "matched": 2}


def test_resolve_exact_id_wins_over_longer_matches():
    ids = ["ra-abcdefg3", "ra-abcdefg2"]
    assert resolve_prefix("ra-abcdefg2", ids, field=F) == "ra-abcdefg2"


def test_resolve_not_found_is_exit_10_with_the_id():
    with pytest.raises(PrefixNotFoundError) as info:
        resolve_prefix("0f8fad5b", ["ffffffff-d9cb-469f-a165-70867728950e"], field=F)
    assert info.value.exit_code == 10
    assert info.value.code == "not_found"
    assert info.value.details == {"id": "0f8fad5b"}
    with pytest.raises(PrefixNotFoundError):
        resolve_prefix("0f8fad5b", [], field=F)


def test_resolve_accepts_any_iterable_and_scales():
    ids = (f"{i:08x}-0000-0000-0000-000000000000" for i in range(50000))
    ids = (f"{i:08x}-0000-0000-0000-000000000000" for i in range(50000))
    assert resolve_prefix("0000c34f", ids, field=F) == "0000c34f-0000-0000-0000-000000000000"


def test_resolve_does_not_lowercase_or_validate_for_the_caller():
    # The caller passes the output of parse_*_prefix; an uppercase prefix matches nothing.
    with pytest.raises(PrefixNotFoundError):
        resolve_prefix("0F8FAD5B", [UUID_OK], field=F)


# ---------------------------------------------------------------------------------------------
# time zone


def test_time_zone_names():
    assert parse_time_zone("UTC", field="time_zone").key == "UTC"
    assert parse_time_zone("America/New_York", field="time_zone").key == "America/New_York"
    assert parse_time_zone("America/Argentina/Buenos_Aires", field="time_zone")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Mars/Olympus",
        "../etc/passwd",
        "/etc/localtime",
        "America",
        "America/",
        "a" * 65,
        "UTC\n",
        "a/b/c/d",
        "é",
        " UTC",
        "zone.tab",
        ".",
        "localtime",
        "posixrules",
        "Factory",
        "posix/UTC",
        "posix/America/New_York",
        "right/UTC",
        "right/America/New_York",
    ],
)
def test_time_zone_refusals(text: str):
    refuses(lambda: parse_time_zone(text, field="time_zone"), field="time_zone")


# ---------------------------------------------------------------------------------------------
# WHEN


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1s", 1),
        ("90s", 90),
        ("5m", 300),
        ("2h", 7200),
        ("3d", 259200),
        ("999999999s", 999999999),
    ],
)
def test_ago_units(text: str, seconds: int):
    assert parse_ago(text, field=F) == sec(seconds)
    assert parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE) == NOW - sec(seconds)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5",
        "05",
        "20261125",
        "5x",
        "5ms",
        "5w",
        "5S",
        "5 m",
        " 5m",
        "5m ",
        "5m\n",
        "-5m",
        "+5m",
        "5.5m",
        "m",
        "1e3s",
        "٥m",
        "5١m",
        "1" * 13 + "s",
        "5mm",
        "5m5s",
        "0x10s",
        "0s",
        "0m",
        "00s",
        "05m",
        "007d",
        "1234567890s",
        "1" * 10 + "d",
    ],
)
def test_ago_refusals(text: str):
    refuses(lambda: parse_ago(text, field=F))
    refuses(lambda: parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE))


def test_ago_overflow_is_refused_not_raised():
    refuses(lambda: parse_when("999999999999d", field=F, now=NOW, time_zone=UTC_ZONE))
    refuses(
        lambda: parse_when(
            "99999999999s", field=F, now=datetime(1, 1, 2, tzinfo=UTC), time_zone=UTC_ZONE
        )
    )
    refuses(lambda: parse_ago("999999999999d", field=F))


def test_when_iso_with_z_and_offsets():
    assert parse_when("2026-11-20T10:00:00Z", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        2026, 11, 20, 10, tzinfo=UTC
    )
    got = parse_when("2026-11-20T10:00:00+05:30", field=F, now=NOW, time_zone=UTC_ZONE)
    assert got == datetime(2026, 11, 20, 4, 30, tzinfo=UTC)
    assert got.utcoffset() == timedelta(0)
    assert parse_when("2026-11-20T10:00:00-08:00", field=F, now=NOW, time_zone=UTC_ZONE) == (
        datetime(2026, 11, 20, 18, tzinfo=UTC)
    )
    assert parse_when(
        "2026-11-20T10:00:00.123456789Z", field=F, now=NOW, time_zone=UTC_ZONE
    ) == datetime(2026, 11, 20, 10, 0, 0, 123456, tzinfo=UTC)
    assert parse_when("2026-11-20T10:00:00.5Z", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        2026, 11, 20, 10, 0, 0, 500000, tzinfo=UTC
    )
    assert parse_when(
        "2026-11-20T23:59:59+23:59", field=F, now=NOW, time_zone=UTC_ZONE
    ) == datetime(2026, 11, 20, 0, 0, 59, tzinfo=UTC)


def test_when_iso_ignores_time_zone_and_clock():
    a = parse_when("2026-11-20T10:00:00Z", field=F, now=NOW, time_zone=ZoneInfo("Asia/Tokyo"))
    b = parse_when("2026-11-20T10:00:00Z", field=F, now=NOW + sec(9999), time_zone=UTC_ZONE)
    assert a == b


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-20T10:00:00",  # no offset
        "2026-11-20T10:00",
        "2026-11-20T10:00Z",  # seconds are required (defs.json)
        "2026-11-20T10:00+05:30",
        "2026-11-20T10:00-0800",
        "2026-11-20T10:00:00-08",  # only +-HH:MM
        "2026-11-20T10:00:00-0800",
        "2026-11-20T10:00:00+05",
        "2026-11-20 10:00:00Z",
        "2026-11-20t10:00:00Z",
        "2026-11-20T10:00:00z",
        "2026-11-20T10Z",
        "2026-11-20T24:00:00Z",
        "2026-11-20T23:60:00Z",
        "2026-11-20T23:59:60Z",  # leap second: refused
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-11-20T10:00:00+24:00",
        "2026-11-20T10:00:00+05:60",
        "2026-11-20T10:00:00+5:30",
        "2026-11-20T10:00:00 Z",
        "2026-11-20T10:00:00Z ",
        "2026-11-20T10:00:00Z\n",
        "2026-11-20T10:00:00.Z",
        "2026-11-20T10:00:00.1234567890Z",
        "2026-W47-5T10:00:00Z",
        "20261120T100000Z",
        "+2026-11-20T10:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-11-20T١٠:00:00Z",
        "２026-11-20T10:00:00Z",
        "2026-11-20T10:00:00−" + "05:00",  # unicode minus
        "2026-11-20T10:00:00Z" + "0" * 5000,
    ],
)
def test_when_iso_refusals(text: str):
    refuses(lambda: parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE))


def test_when_iso_out_of_range_after_conversion_is_refused():
    refuses(lambda: parse_when("0001-01-01T00:00:00+05:00", field=F, now=NOW, time_zone=UTC_ZONE))
    refuses(lambda: parse_when("9999-12-31T23:59:59-05:00", field=F, now=NOW, time_zone=UTC_ZONE))
    assert parse_when("9999-12-31T23:59:59Z", field=F, now=NOW, time_zone=UTC_ZONE).year == 9999


def test_when_date_is_midnight_in_the_zone():
    ny = ZoneInfo("America/New_York")
    assert parse_when("2026-11-20", field=F, now=NOW, time_zone=ny) == datetime(
        2026, 11, 20, 5, tzinfo=UTC
    )
    tokyo = ZoneInfo("Asia/Tokyo")
    assert parse_when("2026-11-20", field=F, now=NOW, time_zone=tokyo) == datetime(
        2026, 11, 19, 15, tzinfo=UTC
    )
    assert parse_when("2026-11-20", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        2026, 11, 20, tzinfo=UTC
    )
    kolkata = ZoneInfo("Asia/Kolkata")
    assert parse_when("2026-11-20", field=F, now=NOW, time_zone=kolkata) == datetime(
        2026, 11, 19, 18, 30, tzinfo=UTC
    )


def test_when_date_dst_transition_days():
    ny = ZoneInfo("America/New_York")
    # 2026-03-08 is the spring-forward day (02:00 EST -> 03:00 EDT): midnight is still EST.
    assert parse_when("2026-03-08", field=F, now=NOW, time_zone=ny) == datetime(
        2026, 3, 8, 5, tzinfo=UTC
    )
    # The day after starts in EDT.
    assert parse_when("2026-03-09", field=F, now=NOW, time_zone=ny) == datetime(
        2026, 3, 9, 4, tzinfo=UTC
    )
    # 2026-11-01 is the fall-back day (02:00 EDT -> 01:00 EST): midnight is EDT.
    assert parse_when("2026-11-01", field=F, now=NOW, time_zone=ny) == datetime(
        2026, 11, 1, 4, tzinfo=UTC
    )


def test_when_date_with_a_midnight_gap_is_the_first_instant_of_the_day():
    # Cuba springs forward at 00:00 -> 01:00 on 2026-03-08: local midnight does not exist.
    havana = ZoneInfo("America/Havana")
    got = parse_when("2026-03-08", field=F, now=NOW, time_zone=havana)
    assert got == datetime(2026, 3, 8, 5, tzinfo=UTC)  # 00:00 CST (UTC-5), the transition instant
    assert got.astimezone(havana).strftime("%Y-%m-%d %H:%M") == "2026-03-08 01:00"
    before = parse_when("2026-03-07", field=F, now=NOW, time_zone=havana)
    assert before == datetime(2026, 3, 7, 5, tzinfo=UTC)
    assert before < got


def test_when_date_with_an_ambiguous_midnight_is_the_first_occurrence():
    # Cuba falls back at 01:00 -> 00:00 on 2026-11-01: 00:00 happens twice; the day starts first.
    havana = ZoneInfo("America/Havana")
    got = parse_when("2026-11-01", field=F, now=NOW, time_zone=havana)
    assert got == datetime(2026, 11, 1, 4, tzinfo=UTC)  # 00:00 CDT (UTC-4)


def test_when_date_in_the_tz_database_far_past_and_future():
    assert parse_when("1970-01-01", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        1970, 1, 1, tzinfo=UTC
    )
    assert parse_when("9999-12-31", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        9999, 12, 31, tzinfo=UTC
    )


def test_when_date_refusals():
    for text in [
        "2026-02-30",
        "2026-13-01",
        "2026-00-10",
        "0000-01-01",
        "2026-1-1",
        "26-11-20",
        "2026/11/20",
        "2026-11-20\n",
        "2026-11-20 ",
        "٢026-11-20",
        "2026-11-200",
    ]:
        refuses(lambda text=text: parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE))


def test_when_date_edges_overflow_only_past_the_range():
    refuses(lambda: parse_when("0001-01-01", field=F, now=NOW, time_zone=ZoneInfo("Asia/Tokyo")))
    # A date near the top stays in range in a western zone (the day starts later in UTC).
    assert parse_when(
        "9999-12-31", field=F, now=NOW, time_zone=ZoneInfo("America/New_York")
    ) == datetime(9999, 12, 31, 5, tzinfo=UTC)


def test_when_leap_day():
    assert parse_when("2028-02-29", field=F, now=NOW, time_zone=UTC_ZONE) == datetime(
        2028, 2, 29, tzinfo=UTC
    )
    refuses(lambda: parse_when("2027-02-29", field=F, now=NOW, time_zone=UTC_ZONE))


def test_when_needs_an_aware_now():
    with pytest.raises(ValueError):
        parse_when("5m", field=F, now=datetime(2026, 1, 1), time_zone=UTC_ZONE)


def test_when_ago_across_a_dst_change_is_elapsed_time():
    now = datetime(2026, 3, 9, 12, tzinfo=UTC)
    assert parse_when("1d", field=F, now=now, time_zone=ZoneInfo("America/New_York")) == datetime(
        2026, 3, 8, 12, tzinfo=UTC
    )


def test_when_result_is_utc():
    got = parse_when("2026-11-20", field=F, now=NOW, time_zone=ZoneInfo("Asia/Tokyo"))
    assert got.tzinfo is UTC
    got = parse_when(
        "1h", field=F, now=NOW.astimezone(timezone(timedelta(hours=3))), time_zone=UTC_ZONE
    )
    assert got.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------------------------
# durations


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1ms", timedelta(milliseconds=1)),
        ("1500ms", timedelta(milliseconds=1500)),
        ("1s", sec(1)),
        ("30s", sec(30)),
        ("2m", sec(120)),
        ("45", sec(45)),
        (45, sec(45)),
        ("007", sec(7)),
        ("7200", sec(7200)),
        ("120m", sec(7200)),
        ("1.5s", timedelta(milliseconds=1500)),
        ("1.5", timedelta(milliseconds=1500)),
        ("0.5m", sec(30)),
        ("250.5ms", timedelta(microseconds=250500)),
        ("120.0m", sec(7200)),
        ("007.50s", sec(7.5)),
        (1.5, timedelta(milliseconds=1500)),
        (0.25, timedelta(milliseconds=250)),
        (7200.0, sec(7200)),
        (0.1, timedelta(milliseconds=100)),
    ],
)
def test_duration_accepts(value, expected):
    assert parse_duration(value, field=F, maximum=sec(7200)) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "0s",
        "0ms",
        "0m",
        "00",
        0,
        -1,
        "-1",
        "-5s",
        "+5s",
        "0.0",
        "0.0s",
        "0.00ms",
        "00.0m",
        ".5s",
        "5.",
        "5.s",
        "5.5.5s",
        "1.5h",
        "1234567890s",
        "1234567890.5s",
        "1e3",
        "5h",
        "5d",
        "5S",
        "5MS",
        "5 s",
        " 5s",
        "5s ",
        "5s\n",
        "s",
        "ms",
        "5sec",
        "5mm",
        "٥s",
        "٥",
        "0x10",
        "1_000",
        "NaN",
        "inf",
        "5s5s",
        "9" * 400,
        "9" * 5000 + "s",
        "1." + "0" * 40 + "s",
        0.0,
        -0.5,
        -0.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        1e300,
        True,
        False,
        None,
        7200.5,
        0.0005,
    ],
)
def test_duration_refusals(value):
    refuses(
        lambda: parse_duration(value, field="retro.timeout", maximum=sec(7200)),
        field="retro.timeout",
    )


def test_duration_days_only_where_allowed():
    assert parse_duration(
        "400d", field=F, maximum=timedelta(days=3650), allow_days=True
    ) == timedelta(days=400)
    refuses(lambda: parse_duration("400d", field=F, maximum=timedelta(days=3650)))
    refuses(lambda: parse_duration("0d", field=F, maximum=timedelta(days=3650), allow_days=True))
    refuses(lambda: parse_duration("1h", field=F, maximum=timedelta(days=3650), allow_days=True))


def test_duration_maximum_and_minimum_are_inclusive():
    assert parse_duration("7200", field=F, maximum=sec(7200)) == sec(7200)
    assert parse_duration("120m", field=F, maximum=sec(7200)) == sec(7200)
    refuses(lambda: parse_duration("7201", field=F, maximum=sec(7200)))
    refuses(lambda: parse_duration("7200001ms", field=F, maximum=sec(7200)))
    refuses(lambda: parse_duration("121m", field=F, maximum=sec(7200)))
    assert parse_duration("1s", field=F, maximum=sec(7200), minimum=sec(1)) == sec(1)
    refuses(lambda: parse_duration("999ms", field=F, maximum=sec(7200), minimum=sec(1)))


def test_duration_fraction_boundaries_are_exact():
    assert parse_duration("7200.0", field=F, maximum=sec(7200)) == sec(7200)
    refuses(lambda: parse_duration("7200.000001", field=F, maximum=sec(7200)))
    refuses(lambda: parse_duration("7200.0000000001", field=F, maximum=sec(7200)))
    refuses(lambda: parse_duration("120.000001m", field=F, maximum=sec(7200)))
    refuses(lambda: parse_duration("7200000.001ms", field=F, maximum=sec(7200)))
    # a float is compared by its exact binary value, never rounded onto the maximum
    refuses(lambda: parse_duration(7200.000000000001, field=F, maximum=sec(7200)))
    assert parse_duration(7200, field=F, maximum=sec(7200)) == sec(7200)
    refuses(lambda: parse_duration(0.000999, field=F, maximum=sec(7200), minimum=sec(1)))
    assert parse_duration("1.000", field=F, maximum=sec(7200), minimum=sec(1)) == sec(1)
    refuses(lambda: parse_duration("0.999999s", field=F, maximum=sec(7200), minimum=sec(1)))
    got = parse_duration("1.5d", field=F, maximum=timedelta(days=3650), allow_days=True)
    assert got == timedelta(days=1, hours=12)
    refuses(lambda: parse_duration("1.5d", field=F, maximum=timedelta(days=3650)))
    assert isinstance(parse_duration("1.5s", field=F, maximum=sec(7200)), timedelta)


def test_duration_huge_numbers_are_refused_quickly():
    refuses(
        lambda: parse_duration(
            "9" * 20 + "d", field=F, maximum=timedelta(days=3650), allow_days=True
        )
    )
    refuses(lambda: parse_duration(10**30, field=F, maximum=sec(10)))
    refuses(lambda: parse_duration("9" * 30, field=F, maximum=sec(10)))


def test_duration_limits_documented():
    assert grammar.RETENTION_MAX == timedelta(days=3650)
    assert grammar.RETRO_TIMEOUT_MIN == sec(1)
    assert grammar.RETRO_TIMEOUT_MAX == sec(7200)
    assert grammar.QUIET_AFTER_MAX == timedelta(days=7)
    assert grammar.HOOK_TIMEOUT_MAX == sec(600)
    assert grammar.INGEST_BUDGET_MAX == sec(3600)
    assert grammar.INGEST_FILE_BUDGET_MAX == sec(600)


# ---------------------------------------------------------------------------------------------
# sizes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", 1),
        (1, 1),
        ("200000", 200000),
        ("1B", 1),
        ("512B", 512),
        ("1KB", 1024),
        ("20KB", 20480),
        ("200KB", 204800),
        ("1MB", 1048576),
        ("16MB", 16 * 1048576),
        ("9999999999", 9999999999),
    ],
)
def test_size_accepts(value, expected):
    assert parse_size(value, field="digest.max_bytes", maximum=10**10) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "0B",
        "0KB",
        "0MB",
        0,
        -1,
        "-1KB",
        "+1KB",
        "1.5KB",
        "1.0MB",
        ".5MB",
        "1kb",
        "1Kb",
        "1mb",
        "1GB",
        "1K",
        "1M",
        "1KiB",
        "1 KB",
        " 1KB",
        "1KB ",
        "1KB\n",
        "KB",
        "B",
        "١KB",
        "1e3",
        "1_000",
        "0x10",
        "1KBB",
        "9" * 5000 + "B",
        "007KB",
        "01",
        "00",
        "007",
        "12345678901",
        "12345678901B",
        "99999999999MB",
        True,
        1.5,
    ],
)
def test_size_refusals(value):
    refuses(lambda: parse_size(value, field="digest.max_bytes"), field="digest.max_bytes")


def test_size_maximum():
    assert parse_size("1MB", field=F, maximum=1048576) == 1048576
    refuses(lambda: parse_size("1025KB", field=F, maximum=1048576))
    refuses(lambda: parse_size("1048577", field=F, maximum=1048576))
    refuses(lambda: parse_size(str(10**30), field=F))
    refuses(lambda: parse_size("99999999999MB", field=F))
    # 11 digits are refused by the grammar even under a huge maximum
    refuses(lambda: parse_size("12345678901", field=F, maximum=10**20))
    assert parse_size("9999999999", field=F, maximum=10**20) == 9999999999


def test_size_default_ceiling_is_one_gib():
    assert grammar.DEFAULT_SIZE_MAX == 1 << 30
    assert parse_size("1024MB", field=F) == 1 << 30
    refuses(lambda: parse_size("1025MB", field=F))


def test_int_range_for_counts_and_chars():
    assert parse_int("20", field="digest.prompt_chars", minimum=20, maximum=65536) == 20
    assert parse_int(65536, field=F, minimum=20, maximum=65536) == 65536
    assert parse_int("1", field="--limit", minimum=1, maximum=1000) == 1
    assert parse_int("1000", field="--limit", minimum=1, maximum=1000) == 1000
    for bad in [
        "19",
        "65537",
        "",
        "-20",
        "+20",
        "20.0",
        "2e1",
        " 20",
        "20\n",
        "٢٠",
        "1" * 5000,
        True,
        19,
        65537,
    ]:
        refuses(lambda bad=bad: parse_int(bad, field=F, minimum=20, maximum=65536))


def test_named_ranges():
    assert grammar.CHARS_RANGE == (20, 65536)
    assert grammar.LIMIT_RANGE == (1, 1000)
    assert grammar.MAX_TURNS_RANGE == (1, 1000)
    assert grammar.MAX_SESSIONS_RANGE == (1, 500)
    assert grammar.RETROS_PER_DAY_RANGE == (1, 1000)


# ---------------------------------------------------------------------------------------------
# cron --ingest-every


@pytest.mark.parametrize(
    ("text", "canonical", "minutes"),
    [
        ("5m", "5m", 5),
        ("6m", "6m", 6),
        ("10m", "10m", 10),
        ("12m", "12m", 12),
        ("15m", "15m", 15),
        ("20m", "20m", 20),
        ("30m", "30m", 30),
        ("60m", "1h", 60),
        ("1h", "1h", 60),
        ("2h", "2h", 120),
        ("3h", "3h", 180),
        ("4h", "4h", 240),
        ("6h", "6h", 360),
        ("8h", "8h", 480),
        ("12h", "12h", 720),
        ("24h", "24h", 1440),
        ("5", "5m", 5),
        ("30", "30m", 30),
        (15, "15m", 15),
    ],
)
def test_ingest_every_accepts(text, canonical, minutes):
    got = parse_ingest_every(text, field="--ingest-every")
    assert got.canonical == canonical
    assert got.minutes == minutes


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0",
        "1",
        "4m",
        "7m",
        "25m",
        "45m",
        "59m",
        "61m",
        "90m",
        "120m",
        "1440m",
        "0h",
        "5h",
        "7h",
        "9h",
        "10h",
        "11h",
        "13h",
        "23h",
        "25h",
        "48h",
        "60",
        60,
        "1d",
        "30s",
        "1.5h",
        "-5m",
        "+5m",
        " 5m",
        "5m ",
        "5M",
        "5H",
        "٥m",
        "05m",
        "005",
        "1" * 5000 + "m",
        True,
        False,
        -5,
        0,
        7,
        3.0,
    ],
)
def test_ingest_every_refusals(text):
    refuses(lambda: parse_ingest_every(text, field="--ingest-every"), field="--ingest-every")


# ---------------------------------------------------------------------------------------------
# output timestamps


def test_format_timestamp_is_utc_with_z():
    assert (
        format_timestamp(datetime(2026, 11, 25, 12, 30, 15, tzinfo=UTC)) == "2026-11-25T12:30:15Z"
    )
    assert (
        format_timestamp(datetime(2026, 11, 25, 12, 30, 15, 500000, tzinfo=UTC))
        == "2026-11-25T12:30:15.5Z"
    )
    assert (
        format_timestamp(datetime(2026, 11, 25, 12, 30, 15, 123456, tzinfo=UTC))
        == "2026-11-25T12:30:15.123456Z"
    )
    tz = timezone(timedelta(hours=-5, minutes=-30))
    assert format_timestamp(datetime(2026, 11, 25, 7, 0, tzinfo=tz)) == "2026-11-25T12:30:00Z"
    assert (
        format_timestamp(datetime(2026, 11, 25, 12, tzinfo=ZoneInfo("Asia/Tokyo")))
        == "2026-11-25T03:00:00Z"
    )


def test_format_timestamp_ms_always_has_three_digits_truncated():
    assert (
        format_timestamp_ms(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05.000Z"
    )
    assert (
        format_timestamp_ms(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC))
        == "2026-01-02T03:04:05.999Z"
    )


def test_format_timestamp_out_of_range_after_utc_conversion_is_a_value_error():
    low = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    high = datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-5)))
    for fn in (format_timestamp, format_timestamp_ms):
        for moment in (low, high):
            with pytest.raises(ValueError, match="timestamp out of range"):
                fn(moment)


def test_format_timestamp_needs_an_aware_datetime():
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        format_timestamp_ms(datetime(2026, 1, 1))


def test_format_timestamp_matches_the_schema_pattern_and_round_trips():
    defs = json.loads((SCHEMAS / "defs.json").read_text())["$defs"]
    ts = re.compile(defs["timestamp"]["pattern"])
    tsms = re.compile(defs["timestamp_ms"]["pattern"])
    for micro in (0, 1, 100000, 120000, 999999):
        moment = datetime(2026, 11, 25, 12, 30, 15, micro, tzinfo=UTC)
        text = format_timestamp(moment)
        assert ts.search(text)
        assert parse_when(text, field=F, now=NOW, time_zone=UTC_ZONE) == moment
        assert tsms.search(format_timestamp_ms(moment))
