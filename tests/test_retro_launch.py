"""``retro run`` steps 13 to 16 end to end against the fake claude-session (issue #26): the row,
the files, ``start``, the adopting ``exec``, every row of the Outcome mapping with its state,
reason and exit, the sandbox rule, hostile ``start`` output, the deadline and cleanup.

The fake validates every document it prints against claude-session's pinned schemas."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import lease, retrofiles, retrolaunch, retrostate
from session_audit.errors import CliError

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    exec_doc,
    forget_doc,
    plan_doc,
    start_doc,
    status_doc,
    step,
    stop_doc,
    version_step,
)
from .ingesthelp import S1, S2, Corpus, connect, state_dir_of
from .procs import wait_until_gone
from .retrohelp import RetroGateway, ingested, seed
from .retrohelp import retro as run_retro
from .schemas import validate

CSID = "cs-aaaaaaab"
EXIT_OF = {
    "done": 0,
    "blocked": 13,
    "needs-input": 13,
    "failed": 18,
    "exited-unreported": 14,
    "killed": 15,
    "timeout": 16,
    "lost": 17,
}


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gw.fake = fake
    return fake


def session_run(
    cs: FakeClaudeSession,
    outcome: str = "done",
    *,
    sandbox: bool | None = True,
    artifact: bool = False,
    exec_exit: int | None = None,
) -> None:
    """Script a whole launch that ends with ``outcome``."""
    code = EXIT_OF[outcome] if exec_exit is None else exec_exit
    cs.script("start", step(start_doc(CSID)))
    cs.script("exec", step(exec_doc(CSID, outcome, artifact=artifact), exit=code))
    cs.script(
        "status",
        step(
            status_doc(CSID, outcome=outcome, sandbox_active=sandbox, artifact=artifact),
            unvalidated=sandbox is None,  # the pinned schema has no null here: defensive case
        ),
    )
    cs.script("forget", step(forget_doc(CSID)))
    cs.script("stop", step(stop_doc(CSID)))


def prepared_corpus(gw: RetroGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]) -> None:
    seed(corpus, S1, "first secret prompt")
    seed(corpus, S2, "second", project="p2")
    ingested(capsys, gw)


def launch(
    capsys: pytest.CaptureFixture[str], gw: RetroGateway, *flags: str, tty: bool = False
) -> Any:
    return run_retro(capsys, gw, "--project", "/work", *flags, tty=tty)


def rows(home: Path, table: str = "retros") -> list[sqlite3.Row]:
    conn = connect(home)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - a test constant
    finally:
        conn.close()


def the_retro(home: Path) -> sqlite3.Row:
    [row] = rows(home)
    return row


def error_of(result: Any, code: int, reason: str | None = None) -> dict[str, Any]:
    assert result.code == code, result.err
    document = json.loads(result.out)
    validate(document, "session-audit/cli/error.json")
    if reason is not None:
        assert document["error"]["details"]["reason"] == reason
    return document["error"]  # type: ignore[no-any-return]


def keys(fake: FakeClaudeSession) -> list[str]:
    return [c["key"] for c in fake.calls() if c["key"] not in ("--version", "start --dry-run")]


def log_entries(home: Path) -> list[dict[str, Any]]:
    path = state_dir_of(home) / "log.jsonl"
    found = [json.loads(line) for line in path.read_text().splitlines()]
    for entry in found:
        validate(entry, "session-audit/log.json")
    return [e for e in found if e["action"] == "retro_state"]


def write_config(home: Path, text: str) -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


# --- the Outcome mapping, every row, end to end ------------------------------------------------

# (outcome, sandbox, artifact) -> (state, reason, exit, error code, details subset)
SESSION_ROWS = [
    (
        "done",
        True,
        False,
        "failed",
        "no_artifact",
        22,
        "artifact_invalid",
        {"reason": "no_artifact"},
    ),
    (
        "blocked",
        True,
        False,
        "escalated",
        "session_blocked",
        13,
        "needs_human",
        {"reason": "session_blocked"},
    ),
    (
        "needs-input",
        True,
        False,
        "escalated",
        "session_needs_input",
        13,
        "needs_human",
        {"reason": "session_needs_input"},
    ),
    (
        "exited-unreported",
        True,
        False,
        "failed",
        "session_failed",
        14,
        "session_ended",
        {"outcome": "exited-unreported"},
    ),
    (
        "timeout",
        True,
        False,
        "failed",
        "session_failed",
        16,
        "session_ended",
        {"outcome": "timeout"},
    ),
    ("lost", True, False, "failed", "session_failed", 17, "session_ended", {"outcome": "lost"}),
    ("failed", True, False, "failed", "session_failed", 18, "session_ended", {"outcome": "failed"}),
    ("killed", True, False, "failed", "session_failed", 15, "session_ended", {"outcome": "killed"}),
]


@pytest.mark.parametrize(
    ("outcome", "sandbox", "artifact", "state", "reason", "code", "error", "details"),
    SESSION_ROWS,
    ids=[row[0] for row in SESSION_ROWS],
)
def test_every_session_row_of_the_outcome_mapping(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    sandbox: bool,
    artifact: bool,
    state: str,
    reason: str,
    code: int,
    error: str,
    details: dict[str, Any],
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, outcome, sandbox=sandbox, artifact=artifact)
    result = launch(capsys, gw)
    envelope = error_of(result, code)
    assert envelope["code"] == error
    assert details.items() <= envelope["details"].items()
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"], row["csession_id"]) == (state, reason, CSID)
    assert envelope["details"]["retro_id"] == row["id"]
    assert row["outcome"] == outcome and row["finished_at"] is not None
    assert row["lease_pid"] is None  # released at the end
    assert keys(cs) == ["start", "exec", "status", "forget"]
    cs.assert_outputs_valid()
    [session] = rows(corpus.home, "retro_sessions")
    assert session["csession_id"] == CSID and session["cleanup_state"] == "pending"
    assert session["forgotten_at"] is not None
    assert (session["retro_id"], session["csession_dir"]) == (row["id"], f"/state/sessions/{CSID}")
    assert session["private_dir"] == f"/state/private/{CSID}"
    assert session["project_dir"] == f"-state-sessions-{CSID}-work"
    assert len(rows(corpus.home, "retro_starts")) == 1  # the run reached the model: no refund
    assert not any(rows(corpus.home, "retro_artifacts"))


LAUNCH_ROWS = [
    ("not-started", "sandbox_unavailable", 12, "failed", {"retryable": False}),
    ("not-started", "never_adopted", 12, "failed", {"retryable": True}),
    ("not-started", "launch_interrupted", 12, "failed", {"retryable": True}),
    ("vetoed", "hook_veto", 11, "failed", {"retryable": False}),
    ("escalated", "hook_escalate", 13, "failed", {"retryable": False}),
]


@pytest.mark.parametrize(
    ("outcome", "end_reason", "exit_code", "code", "details"),
    LAUNCH_ROWS,
    ids=[f"{r[0]}-{r[1]}" for r in LAUNCH_ROWS],
)
def test_an_exec_that_ends_in_the_launch_phase_is_session_not_started(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    end_reason: str,
    exit_code: int,
    code: str,
    details: dict[str, Any],
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script(
        "exec",
        step(
            exec_doc(CSID, outcome, phase="launch", end_reason=end_reason),
            exit=exit_code,
        ),
    )
    result = launch(capsys, gw)
    envelope = error_of(result, 12, "session_not_started")
    assert envelope["code"] == code
    assert envelope["details"]["end_reason"] == end_reason
    assert envelope["details"]["retryable"] == details["retryable"]
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "session_not_started")
    assert keys(cs) == ["start", "exec", "forget"]  # no status: the run never started
    assert rows(corpus.home, "retro_starts") == []  # refunded: the run never reached the model


@pytest.mark.parametrize(
    "step_of",
    [
        lambda: step(error_doc("internal", "boom"), exit=1),
        lambda: step(error_doc("usage", "bad"), exit=2),
        lambda: step(error_doc("not_found", "gone"), exit=10),
        lambda: step(error_doc("refused", "no", reason="live"), exit=20),
        lambda: step(error_doc("ambiguous", "which", matches=[CSID]), exit=21),
        lambda: step("not json at all", unvalidated=True),
        lambda: step("", unvalidated=True),
        lambda: step("[1, 2", exit=0, unvalidated=True),
        lambda: step('{"csession_id": "cs-aaaaaaab", "csession_id": "x"}', unvalidated=True),
        lambda: step("NaN", unvalidated=True),
        lambda: step("[" * 100_000, unvalidated=True),
        lambda: step("x" * 2_000_000, unvalidated=True),  # past the output cap: a failed call
        lambda: step(exec_doc(CSID, "done"), exit=18, unvalidated=True),  # they disagree
    ],
    ids=[
        "exit-1",
        "exit-2",
        "exit-10",
        "exit-20",
        "exit-21",
        "text",
        "empty",
        "truncated",
        "duplicate-key",
        "nan",
        "deep",
        "huge",
        "exit-disagrees",
    ],
)
def test_an_exec_outside_the_table_is_claude_session_error(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    step_of: Any,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("exec", step_of())
    result = launch(capsys, gw)
    envelope = error_of(result, 12, "claude_session_error")
    assert envelope["details"]["retryable"] is False
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")
    assert "forget" in keys(cs)  # the recorded session is cleaned up


def test_the_outcome_file_is_the_fallback_when_stdout_is_not_an_outcome(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    document = json.dumps(exec_doc(CSID, "blocked"))
    cs.script("exec", step("partial {", exit=13, unvalidated=True, outcome_file=document))
    result = launch(capsys, gw)
    assert error_of(result, 13, "session_blocked")["code"] == "needs_human"
    assert (the_retro(corpus.home)["state"]) == "escalated"


@pytest.mark.parametrize(
    "content",
    ["not json", "{", json.dumps(exec_doc("cs-aaaaaaac", "blocked")), "x" * 2_000_000],
    ids=["text", "truncated", "other-session", "huge"],
)
def test_a_bad_outcome_file_does_not_rescue_a_bad_stdout(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("exec", step("junk", exit=13, unvalidated=True, outcome_file=content))
    error_of(launch(capsys, gw), 12, "claude_session_error")


def test_a_symlink_as_the_outcome_file_is_not_read(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(exec_doc(CSID, "blocked")))
    # The fake writes through open(): replace the path with a symlink before it is written by
    # planting the link at the expected name (the retro id is not known: plant it from the step).
    cs.script("exec", step("junk", exit=13, unvalidated=True))
    original = retrolaunch.Launcher._read_outcome

    def planted(self: Any, result: Any, csid: str, run: int) -> Any:
        assert self.files is not None
        Path(self.files.outcome).symlink_to(target)
        return original(self, result, csid, run)

    monkeypatch.setattr(retrolaunch.Launcher, "_read_outcome", planted)
    error_of(launch(capsys, gw), 12, "claude_session_error")


# --- collect and the sandbox rule --------------------------------------------------------------


@pytest.mark.parametrize("sandbox", [False, None])
def test_under_required_a_run_without_an_active_sandbox_is_failed_and_its_artifact_not_stored(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    sandbox: bool | None,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", sandbox=sandbox, artifact=True)
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")
    assert row["sandbox"] == (None if sandbox is None else 0)
    assert rows(corpus.home, "retro_artifacts") == []


def test_a_done_run_with_an_active_sandbox_reaches_the_artifact_check_in_state_validating(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", sandbox=True, artifact=True)
    result = launch(capsys, gw)
    error_of(result, 1)  # TODO(#29): the artifact check is a seam that raises not-implemented
    row = the_retro(corpus.home)
    assert row["state"] == "validating" and row["finished_at"] is None
    assert row["sandbox"] == 1 and row["cost_usd"] == 0.5 and row["turns"] == 3
    assert row["lease_pid"] is None  # released: recovery (#27) can take the row at once
    assert rows(corpus.home, "retro_artifacts") == []


def test_a_done_run_whose_status_fails_is_claude_session_error(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done")
    cs.script("status", step(error_doc("internal", "boom"), exit=1))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")
    assert row["cost_usd"] == 0.5  # the outcome object's own figures are kept


def test_a_failing_status_does_not_change_the_mapping_of_another_outcome(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "timeout")
    cs.script("status", step("not json", unvalidated=True))
    envelope = error_of(launch(capsys, gw), 16)
    assert envelope["code"] == "session_ended" and envelope["details"]["outcome"] == "timeout"


def test_the_collected_figures_are_stored_on_the_row(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script(
        "status",
        step(status_doc(CSID, outcome="blocked", cost_usd=1.25, num_turns=7, sandbox_active=True)),
    )
    error_of(launch(capsys, gw), 13)
    row = the_retro(corpus.home)
    assert (row["cost_usd"], row["turns"], row["sandbox"], row["outcome"]) == (
        1.25,
        7,
        1,
        "blocked",
    )


def test_with_a_lowered_sandbox_a_run_without_one_goes_on(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    write_config(corpus.home, "retro:\n  sandbox: preferred\n")
    cs.script(
        "start --dry-run",
        step(plan_doc({"sandbox.mode": "preferred", "sandbox_lowered": True})),
    )
    session_run(cs, "done", sandbox=False)
    # done without an artifact: the no_artifact check, so the sandbox rule did not stop the run
    error_of(launch(capsys, gw, tty=True), 22, "no_artifact")


# --- start ---------------------------------------------------------------------------------------


def test_a_failed_start_ends_session_not_started_and_the_leaked_session_is_forgotten(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    envelope = error_doc(
        "launch_failed",
        "the launch failed",
        csession_id=CSID,
        run=1,
        end_reason="sandbox_unavailable",
        hint="install bubblewrap",
    )
    cs.script("start", step(envelope, exit=12))
    error = error_of(launch(capsys, gw), 12, "session_not_started")
    assert error["details"]["end_reason"] == "sandbox_unavailable"
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"], row["csession_id"]) == (
        "failed",
        "session_not_started",
        CSID,
    )
    assert keys(cs) == ["start", "forget"]  # no exec, no status
    [session] = rows(corpus.home, "retro_sessions")
    assert session["forgotten_at"] is not None
    assert rows(corpus.home, "retro_starts") == []  # refunded
    forget = [c for c in cs.calls() if c["key"] == "forget"][0]["argv"]
    assert forget == ["forget", CSID, "--purge", "--json"]


@pytest.mark.parametrize(
    ("step_of", "recorded"),
    [
        (lambda: step(error_doc("internal", "boom"), exit=1), False),
        (lambda: step(error_doc("usage", "bad"), exit=2), False),
        (lambda: step(error_doc("refused", "no", reason="live"), exit=20), False),
        (lambda: step("garbage", exit=3, unvalidated=True), False),
        (lambda: step("", exit=0, unvalidated=True), False),
        (lambda: step("allocated cs-aaaaaaab", exit=1, unvalidated=True), True),
        (lambda: step("", exit=1, stderr="oops cs-aaaaaaab oops", unvalidated=True), True),
        (lambda: step(error_doc("failed", "x", detail="cs-aaaaaaab"), exit=12), True),
        (lambda: step("cs-aaaaaaab cs-aaaaaaac", exit=1, unvalidated=True), False),
    ],
    ids=[
        "exit-1",
        "exit-2",
        "exit-20",
        "text",
        "empty-0",
        "id-in-stdout",
        "id-in-stderr",
        "id-in-envelope-detail",
        "two-ids",
    ],
)
def test_any_other_start_result_is_claude_session_error_and_a_printed_id_is_cleaned_up(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    step_of: Any,
    recorded: bool,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("start", step_of())
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")
    assert (row["csession_id"] == CSID) is recorded
    assert (len(rows(corpus.home, "retro_sessions")) == 1) is recorded
    assert ("forget" in keys(cs)) is recorded
    assert "exec" not in keys(cs)
    assert rows(corpus.home, "retro_starts") == []


def test_a_start_that_times_out_after_printing_an_id_is_recorded_and_forgotten(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("start", step(start_doc(CSID), sleep_after=30))
    monkeypatch.setattr(retrolaunch, "START_TIMEOUT", 1.5)
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["csession_id"]) == ("failed", CSID)
    assert keys(cs) == ["start", "forget"]
    [session] = rows(corpus.home, "retro_sessions")
    assert session["forgotten_at"] is not None


MALFORMED = [
    pytest.param({"csession_dir": "/etc"}, id="directory-elsewhere"),
    pytest.param({"csession_dir": "/state/sessions/cs-aaaaaaac"}, id="directory-of-another-id"),
    pytest.param({"csession_dir": "/state/sessions/../sessions/cs-aaaaaaab"}, id="dotdot"),
    pytest.param({"csession_dir": "/state/sessions/cs-aaaaaaab/"}, id="trailing-slash"),
    pytest.param({"csession_dir": "/state//sessions/cs-aaaaaaab"}, id="double-slash"),
    pytest.param({"csession_dir": "/state/sessions/cs-aaaaaaab/work"}, id="deeper"),
    pytest.param({"csession_dir": "/state/sessions/cs-aaaaaaabé"}, id="non-ascii"),
    pytest.param({"csession_dir": "relative/cs-aaaaaaab"}, id="relative"),
    pytest.param({"csession_dir": ""}, id="empty"),
]


@pytest.mark.parametrize("change", MALFORMED)
def test_start_output_with_a_malformed_directory_is_refused_without_forget_or_stop(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    document = {**start_doc(CSID), **change}
    cs.script("start", step(document, exit=0, unvalidated=True))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"], row["csession_id"]) == (
        "failed",
        "claude_session_error",
        None,
    )
    assert rows(corpus.home, "retro_sessions") == []
    assert keys(cs) == ["start"]  # no exec, no forget, no stop
    assert rows(corpus.home, "retro_starts") == []
    state = state_dir_of(corpus.home)  # nothing is left behind for a retro that never ran
    assert not (state / "prompts" / row["id"]).exists()
    assert not (state / "digests" / row["id"]).exists()


@pytest.mark.parametrize(
    "csid",
    ["cs-AAAAAAAB", "cs-aaaaaaa", "cs-aaaaaaabc", "../../x", "cs-aaaaaaa1", ""],
)
def test_start_output_with_a_malformed_id_is_refused_without_forget_or_stop(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    csid: str,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script(
        "start",
        step(
            {**start_doc(CSID), "csession_id": csid, "csession_dir": f"/state/sessions/{csid}"},
            unvalidated=True,
        ),
    )
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert keys(cs) == ["start"]
    assert rows(corpus.home, "retro_sessions") == []


def test_a_launch_failed_envelope_with_a_malformed_id_is_refused_without_forget(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    bad = error_doc(
        "launch_failed", "x", csession_id="../etc", run=1, end_reason="unsafe_cwd", hint=None
    )
    cs.script("start", step(bad, exit=12, unvalidated=True))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert keys(cs) == ["start"]


def test_a_session_id_another_retro_already_recorded_is_never_forgotten_here(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    conn = connect(corpus.home)
    conn.execute(
        "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, private_dir, "
        "project_dir, cleanup_state, created_at) "
        "VALUES (?, 'ra-zzzzzzzz', '/x', '/y', 'p', 'pending', 1)",
        (CSID,),
    )
    conn.commit()
    conn.close()
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert keys(cs) == ["start"]
    [session] = rows(corpus.home, "retro_sessions")
    assert session["retro_id"] == "ra-zzzzzzzz" and session["forgotten_at"] is None


def test_a_start_argv_that_is_not_the_adopting_exec_is_refused_but_the_session_is_forgotten(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    document = start_doc(CSID)
    document["argv"] = ["/bin/sh", "-c", "exec", "evil", "--run", "1"]
    cs.script("start", step(document))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert keys(cs) == ["start", "forget"]  # nothing of argv is ever run


def test_the_adopting_exec_is_built_by_the_tool_from_the_validated_session(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script("start", step(start_doc(CSID, run=3)))
    error_of(launch(capsys, gw), 12)  # the fake's outcome says run 1, the exec ran run 3
    argv = [c["argv"] for c in cs.calls() if c["key"] == "exec"][0]
    row = the_retro(corpus.home)
    assert argv[:5] == ["exec", "--session-id", CSID, "--run", "3"]
    assert argv[5] == "--outcome-file"
    assert argv[6] == str(state_dir_of(corpus.home) / "outcomes" / f"{row['id']}.json")
    assert argv[7:] == ["--json"]


# --- forget ------------------------------------------------------------------------------------


def test_a_live_session_is_stopped_and_forgotten_again_on_exit_20(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script(
        "forget",
        step(error_doc("refused", "live", reason="live"), exit=20),
        step(forget_doc(CSID)),
    )
    error_of(launch(capsys, gw), 13)
    assert keys(cs) == ["start", "exec", "status", "forget", "stop", "forget"]
    [session] = rows(corpus.home, "retro_sessions")
    assert session["forgotten_at"] is not None


@pytest.mark.parametrize(
    ("forget", "forgotten"),
    [
        (step(error_doc("not_found", "gone"), exit=10), True),
        (step(forget_doc(CSID)), True),
        (step(forget_doc("cs-aaaaaaac")), False),
        (step(error_doc("failed", "x"), exit=12), False),
        (step(error_doc("internal", "x"), exit=1), False),
        (step("junk", unvalidated=True), False),
        (step(forget_doc(CSID), exit=1, unvalidated=True), False),
    ],
    ids=["not-found", "forgotten", "other-id", "error-12", "internal", "junk", "exit-1"],
)
def test_the_forgotten_time_is_set_only_for_exit_10_or_a_forget_object_naming_the_id(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    forget: dict[str, Any],
    forgotten: bool,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script("forget", forget)
    error_of(launch(capsys, gw), 13)  # the retro's own outcome does not depend on the cleanup
    [session] = rows(corpus.home, "retro_sessions")
    assert (session["forgotten_at"] is not None) is forgotten
    assert session["cleanup_state"] == "pending"  # TODO(#27) finishes the cleanup


# --- the deadline ------------------------------------------------------------------------------


def test_the_deadline_stops_the_exec_child_and_maps_to_claude_session_error(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done")
    pid_file = tmp_path / "exec.pid"
    cs.script("exec", step(exec_doc(CSID, "done"), sleep=60, pid_file=pid_file))
    monkeypatch.setattr(retrolaunch, "EXEC_SLACK", 0.0)
    monkeypatch.setattr(retrolaunch, "TERM_WAIT", 2.0)
    started = time.monotonic()
    result = launch(capsys, gw, "--timeout", "1s")
    assert time.monotonic() - started < 40
    error_of(result, 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")
    assert keys(cs) == ["start", "exec", "stop", "status", "forget"]  # stop, collect, cleanup
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)
    assert len(rows(corpus.home, "retro_starts")) == 1  # it reached the model: no refund


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] != "Z"


def test_the_exec_child_pid_is_recorded_for_the_signal_handling_of_28(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    seen: list[int] = []
    original = retrolaunch.Launcher._exec_started

    def spy(self: Any, pid: int) -> None:
        seen.append(pid)
        original(self, pid)

    monkeypatch.setattr(retrolaunch.Launcher, "_exec_started", spy)
    error_of(launch(capsys, gw), 13)
    [pid] = seen
    assert pid == [c["pid"] for c in cs.calls() if c["key"] == "exec"][0]


# --- R1: the store never loses a commit to a fresh reader --------------------------------------

READER = (
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute(sys.argv[2]).fetchall())"
)


def fresh_reader(home: Path, sql: str) -> str:
    """What a separate, short-lived process (``retro show``, a monitor) sees."""
    done = subprocess.run(
        [sys.executable, "-c", READER, str(state_dir_of(home) / "state.db"), sql],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return done.stdout.strip()


def test_a_fresh_reader_sees_running_and_the_final_state_while_the_lease_is_renewed(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    # The post-retro hook is a fresh process that runs while the CLI is still alive: what it
    # sees is what any other process sees (a clean exit would checkpoint a lost log into the file).
    from_hook = tmp_path / "from-hook.txt"
    code = (
        "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
        "print(c.execute('SELECT state,reason FROM retros').fetchall())"
    )
    db = state_dir_of(corpus.home) / "state.db"
    command = f"python3 -c {json.dumps(code)} {db} > {from_hook}"
    write_config(
        corpus.home,
        f"hooks:\n  post-retro:\n    - cmd: {json.dumps(command)}\n      timeout: 20\n",
    )
    cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=1.0))
    monkeypatch.setattr(lease, "RENEW_SECONDS", 1.0)  # the renewal thread reopens the store
    seen: list[str] = []
    real_run = gw.run

    def run(argv: Any, **kwargs: Any) -> Any:
        if list(argv)[1:2] == ["status"]:  # after the exec, the renewal has reopened the store
            seen.append(fresh_reader(corpus.home, "SELECT state FROM retros"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gw, "run", run)
    gw.tick = 1.0  # the renewal thread's monotonic clock moves: it renews during the exec
    error_of(launch(capsys, gw), 13)
    gw.tick = 0.0
    assert seen == ["[('running',)]"]
    assert from_hook.read_text().strip() == "[('escalated', 'session_blocked')]"


# --- R2 to R6, R10, R12, R14 ---------------------------------------------------------------------


def test_a_start_that_exits_0_with_an_unparseable_document_still_cleans_up_its_id(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("start", step(f"started {CSID} (not a document)", unvalidated=True))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["csession_id"]) == ("failed", CSID)
    assert keys(cs) == ["start", "forget"]
    [session] = rows(corpus.home, "retro_sessions")
    assert session["forgotten_at"] is not None


@pytest.mark.parametrize("outcome", ["blocked", "needs-input"])
@pytest.mark.parametrize("sandbox", [False, None])
def test_under_required_an_escalated_run_without_an_active_sandbox_is_failed(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    sandbox: bool | None,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, outcome, sandbox=sandbox)
    error_of(launch(capsys, gw), 12, "claude_session_error")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "claude_session_error")


def test_under_required_a_failed_exec_row_keeps_its_own_mapping_without_a_sandbox(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "timeout", sandbox=False)
    envelope = error_of(launch(capsys, gw), 16)
    assert envelope["details"]["outcome"] == "timeout"


def test_an_escalated_run_with_an_active_sandbox_stays_escalated(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked", sandbox=True)
    error_of(launch(capsys, gw), 13)
    assert the_retro(corpus.home)["state"] == "escalated"


def test_a_launch_outcome_that_disagrees_with_the_exit_is_an_error_and_keeps_the_start(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    doc = exec_doc(CSID, "not-started", phase="launch", end_reason="never_adopted")
    cs.script("exec", step(doc, exit=14, unvalidated=True))  # the outcome says 12 or so
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert len(rows(corpus.home, "retro_starts")) == 1


def test_a_launch_outcome_with_an_exit_outside_11_to_13_is_not_refunded(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    doc = exec_doc(CSID, "not-started", phase="launch", end_reason="never_adopted")
    doc["exit_code"] = 14
    cs.script("exec", step(doc, exit=14, unvalidated=True))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert len(rows(corpus.home, "retro_starts")) == 1


def test_files_the_step_refused_are_not_deleted(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    mine: list[Path] = []

    def refuse(gateway: Any, state: str, retro_id: str, token: str, fenced: Any) -> Any:
        keep = Path(state) / "prompts" / retro_id / "mine"
        keep.parent.mkdir(parents=True, mode=0o700)
        keep.write_text("not the tool's")
        mine.append(keep)
        raise retrofiles.FilesError("a directory of the retro already exists")

    monkeypatch.setattr(retrofiles, "write_run_files", refuse)
    error_of(launch(capsys, gw), 12, "store_unwritable")
    assert mine and mine[0].read_text() == "not the tool's"


def test_a_failing_log_after_the_insert_does_not_strand_the_row(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)

    def boom(self: Any) -> None:
        raise CliError("internal", "scripted log failure")

    monkeypatch.setattr(retrostate.Retro, "log", boom)
    result = launch(capsys, gw)
    assert result.code != 0
    row = the_retro(corpus.home)
    assert row["state"] == "digesting" and row["lease_pid"] is None  # recovery may take it at once


def test_a_term_ignoring_exec_child_is_killed_after_the_grace_and_only_it(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done")
    pid_file = tmp_path / "exec.pid"
    cs.script("exec", step(exec_doc(CSID, "done"), sleep=60, pid_file=pid_file, ignore_term=True))
    monkeypatch.setattr(retrolaunch, "EXEC_SLACK", 0.0)
    monkeypatch.setattr(retrolaunch, "TERM_WAIT", 1.0)
    started = time.monotonic()
    error_of(launch(capsys, gw, "--timeout", "1s"), 12, "claude_session_error")
    assert time.monotonic() - started < 40
    pid = int(pid_file.read_text())
    assert wait_until_gone(pid, 10.0)  # SIGKILL after the grace: it ignored SIGTERM
    assert _alive(os.getpid())  # this process was not signalled
