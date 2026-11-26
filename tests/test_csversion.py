"""The claude-session version check: parsing, comparison and the distinct outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from session_audit.csversion import (
    MIN_CS,
    VERSION_TIMEOUT,
    Presence,
    check_version,
    parse_version,
)
from session_audit.gateway import CallResult, CallStatus, SystemGateway

from .fake_claude_session import FakeClaudeSession, step, version_step
from .fake_gateway import FakeGateway


def exited(code: int, out: str = "", err: str = "") -> CallResult:
    return CallResult(CallStatus.EXITED, code, stdout=out, stderr=err)


def check(*results: CallResult):  # noqa: ANN201
    gw = FakeGateway()
    gw.on(["claude-session", "--version"], *results)
    return check_version(gw, "claude-session"), gw


def test_the_minimum_is_0_45_2() -> None:
    assert MIN_CS == (0, 45, 2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("claude-session 0.45.3\n", (0, 45, 3)),
        ("0.100.0", (0, 100, 0)),
        ("claude-session version 12.0.7 (tag v12.0.7)\nignored 9.9.9", (12, 0, 7)),
        ("name 1.2.3.4", (1, 2, 3)),
        ("v0.45.2-rc1", (0, 45, 2)),
        ("", None),
        ("claude-session\n0.45.3", None),  # only the first line counts
        ("1.2", None),
        ("v1.2.x", None),
        ("a.b.c", None),
        ("٣.٤.٥", None),  # not ASCII digits
        ("1234567890.1.1", None),  # more digits than any real version
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_a_version_with_a_hostile_first_line_never_crashes() -> None:
    assert parse_version("x" * 100_000 + " 0.45.3") is None
    assert parse_version("\x00\x1b[31m0.45.3") == (0, 45, 3)


def test_integer_tuple_comparison_not_text() -> None:
    result, _ = check(exited(0, "claude-session 0.100.0\n"))
    assert result.presence is Presence.OK and result.version == (0, 100, 0)
    result, _ = check(exited(0, "claude-session 0.9.0\n"))
    assert result.presence is Presence.TOO_OLD


def test_the_minimum_itself_is_accepted_and_one_below_is_not() -> None:
    assert check(exited(0, "0.45.2"))[0].ok
    assert check(exited(0, "0.45.1"))[0].presence is Presence.TOO_OLD
    assert check(exited(0, "0.44.99"))[0].presence is Presence.TOO_OLD
    assert check(exited(0, "1.0.0"))[0].ok


def test_a_program_that_cannot_start_is_missing() -> None:
    result, _ = check(CallResult(CallStatus.SPAWN_FAILED, detail="FileNotFoundError"))
    assert result.presence is Presence.MISSING and not result.ok


def test_output_without_a_version_is_unparsable_and_not_missing() -> None:
    result, _ = check(exited(0, "hello\n"))
    assert result.presence is Presence.UNPARSABLE


@pytest.mark.parametrize(
    "failure",
    [
        CallResult(CallStatus.TIMEOUT, detail="timed out"),
        CallResult(CallStatus.OUTPUT_LIMIT),
        CallResult(CallStatus.SIGNALED, signal=9),
        # A non-zero exit is a failed call even when it printed a good version.
        exited(1, "claude-session 0.45.3"),
        exited(10, "claude-session 0.45.3"),
        exited(2, ""),
    ],
)
def test_a_failed_call_is_failed_never_read_as_a_version(failure: CallResult) -> None:
    result, _ = check(failure)
    assert result.presence is Presence.FAILED and result.result is failure


def test_the_call_is_argv_only_with_a_ten_second_timeout() -> None:
    _, gw = check(exited(0, "0.45.3"))
    [call] = gw.calls
    assert call["argv"] == ["claude-session", "--version"]
    assert call["timeout"] == VERSION_TIMEOUT == 10.0


def test_the_real_gateway_runs_the_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("--version", version_step("0.45.3"))
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_SCENARIO", str(fake.scenario_path))
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_LOG", str(fake.log_path))
    result = check_version(SystemGateway(kill_grace=1), str(fake.program))
    assert result.ok and result.version == (0, 45, 3)
    fake.script("--version", step("garbage\n"))
    assert check_version(SystemGateway(kill_grace=1), str(fake.program)).presence is (
        Presence.UNPARSABLE
    )
    assert check_version(SystemGateway(kill_grace=1), str(tmp_path / "nope")).presence is (
        Presence.MISSING
    )
