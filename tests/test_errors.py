"""The exit-code table and the closed error envelope (``errors.py``)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import ValidationError

from session_audit import errors
from session_audit.errors import CliError
from session_audit.gateway import CallResult, CallStatus
from session_audit.grammar import (
    AmbiguousPrefixError,
    GrammarUsageError,
    PrefixNotFoundError,
    parse_int,
    parse_retro_prefix,
    resolve_prefix,
)
from tests.golden import assert_golden
from tests.schemas import OWN_SCHEMA_ROOT, validate

ERROR_SCHEMA = "session-audit/cli/error.json"
DEFS = json.loads((OWN_SCHEMA_ROOT / "defs.json").read_text(encoding="utf-8"))["$defs"]["error"]
RETRO_ID = "ra-abcdefg2"


def _samples() -> dict[str, CliError]:
    """One valid error per code (the goldens)."""
    return {
        "internal": CliError("internal", "internal error"),
        "usage": errors.usage("a flag is out of range", field="--limit"),
        "config_invalid": CliError(
            "config_invalid", "the config is invalid", details={"field": "retro.timeout"}
        ),
        "not_found": CliError("not_found", "not found", details={"id": "0123abcd"}),
        "vetoed": CliError(
            "vetoed",
            "a pre-retro hook vetoed the run",
            details={"reason": "hook_vetoed", "retro_id": RETRO_ID, "hook": "pre-retro"},
        ),
        "failed": CliError(
            "failed",
            "the operation failed",
            details={"reason": "claude_session_error", "retryable": False},
        ),
        "needs_human": CliError(
            "needs_human",
            "a human is needed",
            details={"reason": "session_blocked", "retro_id": RETRO_ID},
        ),
        "session_ended": CliError(
            "session_ended",
            "the retro session ended without a report",
            exit_code=16,
            details={"outcome": "timeout", "retro_id": RETRO_ID},
        ),
        "refused": CliError(
            "refused", "refused", details={"reason": "daily_limit", "limit": 3, "matched": 3}
        ),
        "ambiguous": CliError(
            "ambiguous", "ambiguous prefix", details={"matches": ["a", "b"], "matched": 2}
        ),
        "artifact_invalid": CliError(
            "artifact_invalid",
            "the artifact is invalid",
            details={"reason": "no_artifact", "retro_id": RETRO_ID},
        ),
    }


def test_the_code_enum_is_the_schema_enum() -> None:
    assert set(errors.EXIT_CODES) == set(DEFS["properties"]["code"]["enum"])
    assert set(_samples()) == set(errors.EXIT_CODES)


def test_the_closed_detail_keys_are_the_schema_keys() -> None:
    assert errors.DETAIL_KEYS == set(DEFS["properties"]["details"]["properties"])


def test_the_reason_sets_are_the_schema_enums() -> None:
    all_reasons = set(DEFS["properties"]["details"]["properties"]["reason"]["enum"])
    union: set[str] = set()
    for code, reasons in errors.REASONS.items():
        assert reasons <= all_reasons, code
        union |= reasons
    assert union == all_reasons
    by_code = {}
    for clause in DEFS["allOf"]:
        const = clause["if"]["properties"]["code"]["const"]
        reason = clause["then"]["properties"]["details"].get("properties", {}).get("reason")
        if reason:
            by_code[const] = set(reason.get("enum", [reason.get("const")]))
    for code, reasons in by_code.items():
        assert errors.REASONS[code] == reasons, code


@pytest.mark.parametrize("code", sorted(errors.EXIT_CODES))
def test_each_error_code_has_a_golden_envelope_that_validates(code: str) -> None:
    error = _samples()[code]
    assert_golden(error.envelope(), f"error-{code}")


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("internal", 1),
        ("usage", 2),
        ("config_invalid", 2),
        ("not_found", 10),
        ("vetoed", 11),
        ("failed", 12),
        ("needs_human", 13),
        ("refused", 20),
        ("ambiguous", 21),
        ("artifact_invalid", 22),
    ],
)
def test_the_exit_code_of_each_code(code: str, exit_code: int) -> None:
    assert _samples()[code].exit_code == exit_code


@pytest.mark.parametrize("exit_code", [14, 15, 16, 17, 18])
def test_session_ended_takes_the_passed_through_exits(exit_code: int) -> None:
    error = CliError(
        "session_ended",
        "ended",
        exit_code=exit_code,
        details={"outcome": "lost", "retro_id": RETRO_ID},
    )
    assert error.exit_code == exit_code


@pytest.mark.parametrize("exit_code", [None, 0, 1, 12, 13, 19, 130])
def test_session_ended_refuses_any_other_exit(exit_code: int | None) -> None:
    with pytest.raises(ValueError, match="exit"):
        CliError(
            "session_ended",
            "ended",
            exit_code=exit_code,
            details={"outcome": "lost", "retro_id": RETRO_ID},
        )


@pytest.mark.parametrize("exit_code", [0, 3, 10, 130])
def test_an_exit_outside_the_codes_table_is_refused(exit_code: int) -> None:
    with pytest.raises(ValueError, match="exit"):
        CliError("usage", "bad", exit_code=exit_code)


def test_no_error_exits_zero() -> None:
    assert all(error.exit_code != 0 for error in _samples().values())


@pytest.mark.parametrize(
    "details",
    [
        {"path": "/x"},  # not a key of the closed set
        {"reason": "nope"},  # not a reason of the code
        {"retryable": "yes"},
        {"matched": True},  # a bool is not a count
        {"matched": -1},
        {"matches": "ab"},
        {"matches": ["x"] * 21},
        {"field": 3},
        {"hook": "pre-commit"},
    ],
)
def test_details_outside_the_closed_object_are_programming_errors(
    details: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="details"):
        CliError("usage", "bad", details=details)


@pytest.mark.parametrize(
    ("code", "details"),
    [
        ("refused", {}),
        ("refused", {"reason": "store_newer", "x": 1}),
        ("failed", {"reason": "crontab_unavailable"}),  # retryable missing
        ("vetoed", {"reason": "hook_escalated"}),
        ("needs_human", {}),
        ("artifact_invalid", {"reason": "daily_limit"}),
        ("session_ended", {"outcome": "lost"}),
        ("session_ended", {"outcome": "lost", "retro_id": RETRO_ID, "reason": "x"}),
        ("ambiguous", {}),
        ("not_found", {}),
    ],
)
def test_required_keys_and_reasons_per_code_are_enforced(
    code: str, details: dict[str, Any]
) -> None:
    exit_code = 16 if code == "session_ended" else None
    with pytest.raises(ValueError, match="details"):
        CliError(code, "bad", exit_code=exit_code, details=details)


def test_an_unknown_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="code"):
        CliError("oops", "bad")


@pytest.mark.parametrize("message", ["", "x" * 2049])
def test_a_message_must_be_non_empty_and_within_the_schema_bound(message: str) -> None:
    with pytest.raises(ValueError, match="message"):
        CliError("internal", message)


def test_an_envelope_that_breaks_the_schema_fails_validation() -> None:
    envelope = _samples()["usage"].envelope()
    envelope["error"]["details"]["path"] = "/x"
    with pytest.raises(ValidationError):
        validate(envelope, ERROR_SCHEMA)


def test_usage_helper_names_a_field_or_a_reason() -> None:
    assert errors.usage("m", field="--limit").details == {"field": "--limit"}
    reason = errors.usage("m", reason="mode_unsupported", hint="h")
    assert reason.details == {"reason": "mode_unsupported"}
    assert reason.hint == "h"
    assert errors.usage("m").details == {}


def test_from_grammar_usage_error_keeps_code_exit_message_field_and_hint() -> None:
    with pytest.raises(GrammarUsageError) as caught:
        parse_int("0", field="--limit", minimum=1, maximum=1000)
    error = errors.from_grammar(caught.value)
    assert (error.code, error.exit_code) == ("usage", 2)
    assert error.message == caught.value.message
    assert error.details == {"field": "--limit"}
    assert error.hint == caught.value.hint
    validate(error.envelope(), ERROR_SCHEMA)


def test_from_grammar_never_echoes_the_input() -> None:
    hostile = "\x1b[31mSECRET\x1b[0m"
    with pytest.raises(GrammarUsageError) as caught:
        parse_retro_prefix(hostile, field="RETRO_ID")
    error = errors.from_grammar(caught.value)
    assert "SECRET" not in json.dumps(error.envelope())
    assert "SECRET" not in (error.hint or "")


def test_from_grammar_ambiguous_prefix_is_exit_21_with_matches_and_matched() -> None:
    known = [f"ra-aaaa{c}{d}22" for c in "abc" for d in "abcdefg"]
    with pytest.raises(AmbiguousPrefixError) as caught:
        resolve_prefix("ra-aaaa", known, field="RETRO_ID")
    error = errors.from_grammar(caught.value)
    assert (error.code, error.exit_code) == ("ambiguous", 21)
    assert error.details["matched"] == 21
    assert len(error.details["matches"]) == 20
    validate(error.envelope(), ERROR_SCHEMA)


def test_from_grammar_not_found_is_exit_10_with_the_id() -> None:
    with pytest.raises(PrefixNotFoundError) as caught:
        resolve_prefix("ra-zzzz", ["ra-aaaaaaaa"], field="RETRO_ID")
    error = errors.from_grammar(caught.value)
    assert (error.code, error.exit_code) == ("not_found", 10)
    assert error.details == {"id": "ra-zzzz"}
    validate(error.envelope(), ERROR_SCHEMA)


def test_from_grammar_drops_a_detail_key_outside_the_closed_set_instead_of_leaking_it() -> None:
    odd = GrammarUsageError("bad", "--x", details={"field": "--x", "value": "secret"})
    error = errors.from_grammar(odd)
    assert error.details == {"field": "--x"}


@pytest.mark.parametrize(
    "result",
    [
        CallResult(CallStatus.TIMEOUT),
        CallResult(CallStatus.OUTPUT_LIMIT),
        CallResult(CallStatus.SIGNALED, signal=9),
        CallResult(CallStatus.SPAWN_FAILED, detail="no such file"),
        CallResult(CallStatus.EXITED, 1),
        CallResult(CallStatus.EXITED, 10),
        CallResult(CallStatus.EXITED, 2),
    ],
)
def test_a_failed_claude_session_call_is_exit_12_never_0_or_10(result: CallResult) -> None:
    error = errors.claude_session_failed(result)
    assert (error.code, error.exit_code) == ("failed", 12)
    assert error.details == {"reason": "claude_session_error", "retryable": False}
    validate(error.envelope(), ERROR_SCHEMA)


def test_a_successful_call_is_not_a_failure() -> None:
    with pytest.raises(ValueError, match="succeeded"):
        errors.claude_session_failed(CallResult(CallStatus.EXITED, 0))


def test_every_message_is_ascii_free_of_format_fields() -> None:
    for error in _samples().values():
        assert "{" not in error.message and "%" not in error.message


def test_interrupt_exits() -> None:
    assert errors.interrupt_exit(1) == 129
    assert errors.interrupt_exit(2) == 130
    assert errors.interrupt_exit(15) == 143
