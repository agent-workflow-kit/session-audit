"""The fake claude-session: scriptable per test, every document it prints valid."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

from session_audit.gateway import CallStatus, SystemGateway

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    forget_doc,
    list_doc,
    start_doc,
    step,
)
from .procs import wait_until_gone
from .schemas import MIN_CLAUDE_SESSION_VERSION, PINNED_SCHEMA_ROOT, PINNED_VERSION


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
    assert call(fake, "status", "--json").stdout == "{not json"
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
    result = call(fake, "status", "--json", timeout=0.5)
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
