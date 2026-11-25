"""The fake claude-session: scriptable per test, every document it prints valid."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import ValidationError

from session_audit.gateway import CallStatus, SystemGateway

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    forget_doc,
    list_doc,
    outcome_doc,
    start_doc,
    step,
)
from .procs import wait_until_gone
from .schemas import MIN_CLAUDE_SESSION_VERSION, PINNED_SCHEMA_ROOT, PINNED_VERSION, validate


def call(fake: FakeClaudeSession, *args: str, timeout: float = 10, stdin: str | None = None):  # noqa: ANN201
    return SystemGateway(kill_grace=1).run(
        [str(fake.program), *args], env=fake.env(), timeout=timeout, stdin=stdin
    )


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_the_pinned_schemas_are_a_version_the_design_supports() -> None:
    assert version_tuple(PINNED_VERSION) >= version_tuple(MIN_CLAUDE_SESSION_VERSION)
    assert PINNED_VERSION in (PINNED_SCHEMA_ROOT / "PINNED").read_text(encoding="utf-8")


def test_scripted_documents_are_printed_and_calls_are_logged(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("start", step(start_doc("cs-aaaaaaab")))
    fake.script("list", step(list_doc()))
    started = call(fake, "start", "--json", "--kind", "audit")
    assert started.ok
    assert json.loads(started.stdout)["csession_id"] == "cs-aaaaaaab"
    assert json.loads(call(fake, "list", "--json").stdout) == {"sessions": []}
    assert [c["argv"] for c in fake.calls()] == [
        ["start", "--json", "--kind", "audit"],
        ["list", "--json"],
    ]
    fake.assert_outputs_valid()


def test_steps_play_in_order_and_the_last_repeats(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script(
        "forget",
        step(error_doc("not_found", "no such session"), exit=10),
        step(forget_doc("cs-aaaaaaab")),
    )
    results = [call(fake, "forget", "--purge", "--json", "cs-aaaaaaab") for _ in range(3)]
    assert [r.returncode for r in results] == [10, 0, 0]
    fake.assert_outputs_valid()


def test_dry_run_and_plain_calls_have_separate_scripts(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("start --dry-run", step({"x": 1}, unvalidated=True, exit=12))
    fake.script("start", step(start_doc()))
    assert call(fake, "start", "--dry-run", "--json").returncode == 12
    assert call(fake, "start", "--json").ok
    assert [c["key"] for c in fake.calls()] == ["start --dry-run", "start"]


def test_a_launch_failure_envelope_validates(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    doc = error_doc(
        "launch_failed",
        "launch refused",
        csession_id="cs-aaaaaaab",
        run=1,
        end_reason="sandbox_unavailable",
        hint="install bubblewrap",
    )
    fake.script("start", step(doc, exit=12))
    result = call(fake, "start", "--json")
    assert result.exit_is(12)
    fake.assert_outputs_valid()


def test_version_is_plain_text(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("--version", step(f"claude-session {PINNED_VERSION}\n"))
    assert call(fake, "--version").stdout == f"claude-session {PINNED_VERSION}\n"


def test_an_invalid_document_cannot_be_scripted(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    bad = start_doc()
    bad["csession_id"] = "not-an-id"
    with pytest.raises(ValidationError):
        fake.script("start", step(bad))
    extra = start_doc()
    extra["surprise"] = True
    with pytest.raises(ValidationError):
        fake.script("start", step(extra))


def test_a_malformed_output_must_be_asked_for_explicitly(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    with pytest.raises(AssertionError):
        fake.script("status", step("{not json"))
    fake.script("status", step("{not json", unvalidated=True))
    assert call(fake, "status", "cs-aaaaaaab", "--json").stdout == "{not json"
    fake.assert_outputs_valid()  # a deliberately unvalidated step is skipped


def test_a_printed_invalid_document_is_caught_after_the_fact(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("start", step(start_doc()))
    call(fake, "start", "--json")
    log = fake.calls()
    log[0]["printed"]["stdout"] = json.dumps({"csession_id": "x"})
    fake.log_path.write_text("".join(json.dumps(c) + "\n" for c in log), encoding="utf-8")
    with pytest.raises(ValidationError):
        fake.assert_outputs_valid()


def test_an_unscripted_call_fails_loudly(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    result = call(fake, "stop", "cs-aaaaaaab")
    assert result.exit_is(99) and "no step scripted" in result.stderr


def test_stdin_is_logged_when_asked(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("exec", step({"x": 1}, unvalidated=True, read_stdin=True))
    call(fake, "exec", "--json", stdin="prompt text")
    assert fake.calls()[0]["stdin"] == "prompt text"


def test_a_hung_call_is_killed_by_the_gateway_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    fake = FakeClaudeSession(tmp_path)
    fake.script("status", step({"x": 1}, unvalidated=True, sleep=60, pid_file=pid_file))
    result = call(fake, "status", "cs-aaaaaaab", "--json", timeout=0.5)
    assert result.status is CallStatus.TIMEOUT
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert wait_until_gone(pid)


def test_a_term_ignoring_fake_is_killed_after_the_grace(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("stop", step({"x": 1}, unvalidated=True, sleep=60, ignore_term=True))
    result = call(fake, "stop", "cs-aaaaaaab", timeout=0.5)
    assert result.status is CallStatus.TIMEOUT


def test_the_fake_needs_only_a_plain_python3() -> None:
    first_line = (Path(__file__).parent / "fakebin" / "claude-session").read_text().splitlines()[0]
    assert first_line == "#!/usr/bin/env python3"
    assert sys.version_info >= (3, 12)


# -- the grammar of the commands the fake checks ---------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "list"],  # a global flag before the subcommand
        ["--config", "c.yaml", "list", "--json"],
        ["start"],  # start without --json
        ["start", "--dry-run"],
        ["start", "--json", "extra"],  # start takes no positional
        ["status", "--json"],  # no id
        ["stop"],
        ["stop", "--json"],
        ["status", "cs-aaaaaaab", "cs-aaaaaaac", "--json"],  # one id too many
        ["list", "--bogus"],
        ["list", "--json", "--bogus"],
        ["list", "--jso"],  # no abbreviations
        ["list", "--json=1"],  # a flag without a value given one
        ["list", "--json", "--state"],  # a missing value
        ["list", "--json", "--state", "--live"],  # the value is a flag
        ["start", "--json", "--tools"],  # --tools needs at least one name
        ["list", "--", "--json"],  # -- is not part of the grammar
        ["status", "--json", "--", "cs-aaaaaaab"],
        ["exec", "--json", "--grace", "5"],  # a flag of another command
        [],
    ],
)
def test_a_call_the_real_program_would_refuse_is_exit_2_and_logged_as_usage(
    tmp_path: Path, argv: list[str]
) -> None:
    fake = FakeClaudeSession(tmp_path)
    for key in ("start", "status", "stop", "list", "exec"):
        fake.script(key, step({"x": 1}, unvalidated=True))
    result = call(fake, *argv)
    assert result.exit_is(2)
    assert result.stderr.startswith("claude-session: error: ")
    assert result.stderr.count("\n") == 1
    if "--json" in argv:
        validate(json.loads(result.stdout), "claude-session/cli/error.json")
        assert json.loads(result.stdout)["error"]["code"] == "usage"
    else:
        assert result.stdout == ""
    assert [c["key"] for c in fake.calls()] == ["<usage>"]
    fake.assert_outputs_valid()


def test_a_refused_call_consumes_no_step(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("list", step({"sessions": [1]}, unvalidated=True), step(list_doc()))
    assert call(fake, "list", "--bogus").exit_is(2)
    assert json.loads(call(fake, "list", "--json").stdout) == {"sessions": [1]}


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["status", "cs-aaaaaaab", "--version"],
        ["list", "--version"],
        ["status", "--version"],  # argparse exits at --version before the missing id is noticed
        ["list", "--bogus", "--version"],
        ["start", "--json", "--kind", "audit", "--label", "a=b", "--label", "c=d"],
        ["start", "--json", "--tools", "Read", "Grep", "--no-auto-exit", "--implicit-done"],
        ["start", "--json", "--resume", "cs-aaaaaaab", "--fork", "--reset", "prompt"],
        ["start", "--json", "--max-turns", "-1"],  # a negative number is a value
        ["start", "--json", "--timeout=10m", "--dry-run", "--actor", "me"],
        ["exec", "--session-id", "cs-aaaaaaab", "--run", "2", "--json"],
        ["status", "cs-aaaaaaab", "--json", "--run", "2"],
        ["status", "--json", "cs-aaaaaaab"],
        ["stop", "cs-aaaaaaab", "--grace", "5s", "--dry-run", "--json"],
        ["list", "--config", "c.yaml", "--verbose", "--yes", "--json", "--state", "running"],
        ["list", "--json", "--limit", "5", "--live", "--report-status", "done"],
        ["forget", "--purge", "--json", "cs-aaaaaaab"],
        ["forget", "--older-than", "30d", "--json"],
    ],
)
def test_a_well_formed_call_gets_its_scripted_step(tmp_path: Path, argv: list[str]) -> None:
    fake = FakeClaudeSession(tmp_path)
    for key in (
        "--version",
        "start",
        "start --dry-run",
        "exec",
        "status",
        "stop",
        "list",
        "forget",
    ):
        fake.script(key, step("ok\n", unvalidated=True))
    result = call(fake, *argv)
    assert result.ok and result.stdout == "ok\n", result.stderr


# -- scripting a step the real program could not produce -------------------------------------


@pytest.mark.parametrize(
    ("code", "extra", "exit_code"),
    [
        ("usage", {}, 2),
        ("not_found", {}, 10),
        ("refused", {"reason": "live"}, 20),
        ("ambiguous", {"matches": ["cs-aaaaaaab", "cs-aaaaaaac"]}, 21),
        ("failed", {}, 12),
        ("schema_newer", {}, 12),
        ("internal", {}, 1),
    ],
)
def test_an_error_code_must_come_with_its_exit(
    tmp_path: Path, code: str, extra: dict[str, object], exit_code: int
) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("stop", step(error_doc(code, "m", **extra), exit=exit_code))
    for wrong in sorted({1, 2, 10, 12, 20, 21} - {exit_code}):
        with pytest.raises(AssertionError, match="goes with exit"):
            fake.script("stop", step(error_doc(code, "m", **extra), exit=wrong))


def test_launch_failed_is_for_start_only(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    doc = error_doc(
        "launch_failed", "m", csession_id="cs-aaaaaaab", run=1, end_reason="unsafe_cwd", hint=None
    )
    fake.script("start", step(doc, exit=12))
    with pytest.raises(AssertionError, match="only for start"):
        fake.script("status", step(doc, exit=12))


@pytest.mark.parametrize(
    ("key", "exit_code"),
    [("status", 13), ("list", 11), ("start", 11), ("start", 20 + 3), ("stop", 14), ("forget", 16)],
)
def test_an_exit_the_command_never_ends_with_cannot_be_scripted(
    tmp_path: Path, key: str, exit_code: int
) -> None:
    fake = FakeClaudeSession(tmp_path)
    with pytest.raises(AssertionError, match="is not one of"):
        fake.script(key, step(error_doc("failed", "m"), exit=exit_code))


def test_an_outcome_document_must_say_the_exit_the_step_has(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("exec", step(outcome_doc(exit_code=16), exit=16))
    with pytest.raises(AssertionError, match="exit_code 16"):
        fake.script("exec", step(outcome_doc(exit_code=16), exit=15))
    assert call(fake, "exec", "--json").exit_is(16)
    fake.assert_outputs_valid()


def test_an_unvalidated_step_is_not_checked_against_the_exit_rules(tmp_path: Path) -> None:
    FakeClaudeSession(tmp_path).script("status", step({"x": 1}, exit=13, unvalidated=True))


# -- the call log ----------------------------------------------------------------------------


def test_a_call_killed_while_it_sleeps_is_logged_but_never_as_printed(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("status", step({"x": 1}, unvalidated=True, sleep=60))
    assert call(fake, "status", "cs-aaaaaaab", "--json", timeout=0.5).status is CallStatus.TIMEOUT
    (logged,) = fake.calls()
    assert logged["key"] == "status" and logged["printed"] is None
    fake.assert_outputs_valid()


def test_a_finished_call_is_logged_as_printed(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("list", step(list_doc(), exit=0))
    call(fake, "list", "--json")
    (logged,) = fake.calls()
    assert logged["printed"] == {"stdout": json.dumps(list_doc()), "exit": 0}


def test_concurrent_calls_each_get_their_own_step(tmp_path: Path) -> None:
    fake = FakeClaudeSession(tmp_path)
    fake.script("list", *(step(str(n), unvalidated=True) for n in range(6)))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: call(fake, "list", "--json"), range(6)))
    assert sorted(r.stdout for r in results) == [str(n) for n in range(6)]
    assert len(fake.calls()) == 6
