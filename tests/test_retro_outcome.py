"""The parsing of claude-session's launch output and the Outcome mapping table (issue #26): every
row of the table in order, hostile documents, and the id, directory and project name checks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from session_audit import errors
from session_audit.gateway import CallResult, CallStatus
from session_audit.harness import Invalid
from session_audit.retrooutcome import (
    Mapped,
    Outcome,
    launch_failed_id,
    map_exec,
    map_start,
    parse_forget,
    parse_outcome,
    parse_start,
    parse_status,
    single_csession_id,
    validated_session,
)

from .fake_claude_session import error_doc, forget_doc, outcome_doc, start_doc, status_doc

CSID = "cs-aaaaaaab"
ROOT = "/state/sessions"


def exited(code: int, stdout: str = "") -> CallResult:
    return CallResult(CallStatus.EXITED, code, stdout=stdout)


def outcome(
    kind: str, code: int, *, phase: str = "session", end_reason: str | None = "normal"
) -> Outcome:
    return Outcome(CSID, 1, phase, kind, end_reason, code, None, None)


# --- the table, row by row ----------------------------------------------------------------------

EXEC_ROWS: list[tuple[str, CallResult, Outcome | None, dict[str, Any], Mapped]] = [
    (
        "1 the deadline",
        CallResult(CallStatus.TIMEOUT),
        None,
        {"deadline": True},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "1 the deadline wins over a parsed outcome",
        exited(16),
        outcome("timeout", 16),
        {"deadline": True},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "2 exit 15 after a forwarded signal (SIGINT)",
        exited(15),
        outcome("killed", 15, end_reason="forwarded_int"),
        {"forwarded": 2},
        Mapped("canceled", "interrupted", 130),
    ),
    (
        "2 launch_interrupted after a forwarded SIGTERM",
        exited(12),
        outcome("not-started", 12, phase="launch", end_reason="launch_interrupted"),
        {"forwarded": 15},
        Mapped("canceled", "interrupted", 143),
    ),
    (
        "2 exit 15 after a forwarded SIGHUP",
        exited(15),
        outcome("killed", 15),
        {"forwarded": 1},
        Mapped("canceled", "interrupted", 129),
    ),
    (
        "5 phase launch, exit 12",
        exited(12),
        outcome("not-started", 12, phase="launch", end_reason="sandbox_unavailable"),
        {},
        Mapped("failed", "session_not_started", 12, end_reason="sandbox_unavailable"),
    ),
    (
        "5 phase launch, exit 11 (a veto), retryable end reason",
        exited(11),
        outcome("vetoed", 11, phase="launch", end_reason="hook_veto"),
        {},
        Mapped("failed", "session_not_started", 12, end_reason="hook_veto"),
    ),
    (
        "5 phase launch, exit 13, never_adopted is retryable",
        exited(13),
        outcome("escalated", 13, phase="launch", end_reason="never_adopted"),
        {},
        Mapped("failed", "session_not_started", 12, end_reason="never_adopted", retryable=True),
    ),
    (
        "5 launch_interrupted without a forwarded signal is retryable",
        exited(12),
        outcome("not-started", 12, phase="launch", end_reason="launch_interrupted"),
        {},
        Mapped(
            "failed", "session_not_started", 12, end_reason="launch_interrupted", retryable=True
        ),
    ),
    (
        "6 exit 0, done",
        exited(0),
        outcome("done", 0),
        {},
        Mapped("done", None, 0, outcome="done", validate=True),
    ),
    (
        "7 exit 13, blocked",
        exited(13),
        outcome("blocked", 13),
        {},
        Mapped("escalated", "session_blocked", 13, outcome="blocked"),
    ),
    (
        "7 exit 13, needs-input",
        exited(13),
        outcome("needs-input", 13),
        {},
        Mapped("escalated", "session_needs_input", 13, outcome="needs-input"),
    ),
    (
        "8 exit 14, exited-unreported",
        exited(14),
        outcome("exited-unreported", 14, end_reason="no_report"),
        {},
        Mapped("failed", "session_failed", 14, outcome="exited-unreported"),
    ),
    (
        "8 exit 16, timeout",
        exited(16),
        outcome("timeout", 16, end_reason="timeout"),
        {},
        Mapped("failed", "session_failed", 16, outcome="timeout"),
    ),
    (
        "8 exit 17, lost",
        exited(17),
        outcome("lost", 17, end_reason="lost"),
        {},
        Mapped("failed", "session_failed", 17, outcome="lost"),
    ),
    (
        "8 exit 18, reported failed",
        exited(18),
        outcome("failed", 18),
        {},
        Mapped("failed", "session_failed", 18, outcome="failed"),
    ),
    (
        "9 exit 15 not caused by the tool",
        exited(15),
        outcome("killed", 15, end_reason="stopped"),
        {},
        Mapped("failed", "session_failed", 15, outcome="killed"),
    ),
    (
        "10 exit 1",
        exited(1),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 2",
        exited(2),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 10",
        exited(10),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 20",
        exited(20),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 21",
        exited(21),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 an unparseable outcome after exit 0",
        exited(0),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 an unparseable outcome after exit 16",
        exited(16),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 a signal that nobody here sent",
        CallResult(CallStatus.SIGNALED, signal=9),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 a spawn failure",
        CallResult(CallStatus.SPAWN_FAILED),
        None,
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 an exit 22 (a wait timeout never ends an exec)",
        exited(22),
        outcome("timeout", 22),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 the process exit and the outcome disagree",
        exited(0),
        outcome("failed", 18),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 0 with an outcome that is not done",
        exited(0),
        outcome("blocked", 0),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 13 in the session phase with another outcome",
        exited(13),
        outcome("escalated", 13),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 18 with an outcome of another exit",
        exited(18),
        outcome("lost", 18),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "10 exit 14 in the launch phase",
        exited(14),
        outcome("exited-unreported", 14, phase="launch"),
        {},
        Mapped("failed", "claude_session_error", 12),
    ),
]


@pytest.mark.parametrize(
    ("result", "found", "kwargs", "expected"),
    [row[1:] for row in EXEC_ROWS],
    ids=[row[0] for row in EXEC_ROWS],
)
def test_every_exec_row_of_the_outcome_mapping(
    result: CallResult, found: Outcome | None, kwargs: dict[str, Any], expected: Mapped
) -> None:
    assert map_exec(result, found, **kwargs) == expected


def test_a_forwarded_signal_does_not_touch_a_finished_session() -> None:
    # A signal forwarded to the exec child after the session ended on its own: the row 2 test is
    # exit 15 or launch_interrupted only, so a reported outcome keeps its row.
    assert map_exec(exited(0), outcome("done", 0), forwarded=2).state == "done"
    assert map_exec(exited(18), outcome("failed", 18), forwarded=2).reason == "session_failed"


START_ROWS: list[tuple[str, CallResult, tuple[str, str, int | None] | None, Mapped]] = [
    (
        "3 launch_failed",
        exited(12),
        (CSID, "sandbox_unavailable", 1),
        Mapped("failed", "session_not_started", 12, end_reason="sandbox_unavailable"),
    ),
    (
        "3 launch_failed is not retryable even for a never_adopted-looking reason on start",
        exited(12),
        (CSID, "unsafe_cwd", 1),
        Mapped("failed", "session_not_started", 12, end_reason="unsafe_cwd"),
    ),
    (
        "4 exit 12 without the launch_failed envelope",
        exited(12),
        None,
        Mapped("failed", "claude_session_error", 12),
    ),
    ("4 exit 1", exited(1), None, Mapped("failed", "claude_session_error", 12)),
    ("4 exit 2", exited(2), None, Mapped("failed", "claude_session_error", 12)),
    ("4 exit 20", exited(20), None, Mapped("failed", "claude_session_error", 12)),
    (
        "4 a timeout",
        CallResult(CallStatus.TIMEOUT),
        None,
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "4 a signal",
        CallResult(CallStatus.SIGNALED, signal=15),
        None,
        Mapped("failed", "claude_session_error", 12),
    ),
    (
        "4 a launch_failed envelope on a call that timed out is not trusted",
        CallResult(CallStatus.TIMEOUT),
        (CSID, "sandbox_unavailable", 1),
        Mapped("failed", "claude_session_error", 12),
    ),
]


@pytest.mark.parametrize(
    ("result", "failed", "expected"),
    [row[1:] for row in START_ROWS],
    ids=[row[0] for row in START_ROWS],
)
def test_every_start_row_of_the_outcome_mapping(
    result: CallResult, failed: tuple[str, str, int | None] | None, expected: Mapped
) -> None:
    assert map_start(result, launch_failed=failed) == expected


def test_the_deadline_row_wins_for_start_too() -> None:
    assert map_start(exited(0), launch_failed=None, deadline=True).reason == "claude_session_error"


@pytest.mark.parametrize(
    ("mapped", "code", "key", "details"),
    [
        (
            Mapped("failed", "session_not_started", 12, end_reason="never_adopted", retryable=True),
            12,
            "failed",
            {
                "reason": "session_not_started",
                "retryable": True,
                "end_reason": "never_adopted",
                "retro_id": "ra-abcdefgh",
            },
        ),
        (
            Mapped("failed", "claude_session_error", 12),
            12,
            "failed",
            {"reason": "claude_session_error", "retryable": False, "retro_id": "ra-abcdefgh"},
        ),
        (
            Mapped("failed", "session_failed", 16, outcome="timeout"),
            16,
            "session_ended",
            {"outcome": "timeout", "retro_id": "ra-abcdefgh"},
        ),
        (
            Mapped("escalated", "session_blocked", 13, outcome="blocked"),
            13,
            "needs_human",
            {"reason": "session_blocked", "retro_id": "ra-abcdefgh"},
        ),
    ],
)
def test_each_row_builds_its_closed_error(
    mapped: Mapped, code: int, key: str, details: dict[str, Any]
) -> None:
    error = mapped.error("ra-abcdefgh")
    assert isinstance(error, errors.CliError)
    assert (error.exit_code, error.code, error.details) == (code, key, details)


def test_done_and_canceled_have_no_error() -> None:
    assert Mapped("done", None, 0, validate=True).error("ra-abcdefgh") is None
    assert Mapped("canceled", "interrupted", 130).error("ra-abcdefgh") is None


# --- parsing -----------------------------------------------------------------------------------


def test_a_start_document_is_parsed() -> None:
    started = parse_start(json.dumps(start_doc(CSID, run=3)))
    assert (started.csession_id, started.run, started.argv[1:3]) == (
        CSID,
        3,
        ("exec", "--session-id"),
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "null",
        '{"csession_id": "cs-aaaaaaab"}',
        '{"csession_id": "cs-aaaaaaab", "csession_dir": 5, "argv": [], "run": 1}',
        '{"csession_id": "cs-AAAAAAAB", "csession_dir": "/x", "argv": [], "run": 1}',
        '{"csession_id": "cs-aaaaaaab", "csession_dir": "/x", "argv": [1], "run": 1}',
        '{"csession_id": "cs-aaaaaaab", "csession_dir": "/x", "argv": [], "run": 0}',
        '{"csession_id": "cs-aaaaaaab", "csession_dir": "/x", "argv": [], "run": true}',
        '{"csession_id": "cs-aaaaaaab", "csession_id": "cs-aaaaaaac", "csession_dir": "/x", '
        '"argv": [], "run": 1}',
        '{"csession_id": "cs-aaaaaaab", "csession_dir": "/x", "argv": [], "run": NaN}',
        "[" * 100000,
        '{"a":' * 100000,
        '{"csession_id": "cs-aaaaaaab", "csession_dir": "' + "x" * 2_000_000 + '"',
    ],
)
def test_a_hostile_start_document_is_invalid(text: str) -> None:
    with pytest.raises(Invalid):
        parse_start(text)


def test_launch_failed_is_read_from_the_envelope() -> None:
    doc = error_doc(
        "launch_failed", "x", csession_id=CSID, run=1, end_reason="sandbox_unavailable", hint=None
    )
    assert launch_failed_id(json.dumps(doc)) == (CSID, "sandbox_unavailable", 1)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        json.dumps(error_doc("failed", "x")),
        json.dumps(error_doc("launch_failed", "x", csession_id="cs-nope", end_reason="x")),
        json.dumps(error_doc("launch_failed", "x", csession_id=CSID)),
        json.dumps(error_doc("launch_failed", "x", csession_id=CSID, end_reason="Bad Reason")),
        '{"error": {"code": "launch_failed", "code": "launch_failed"}}',
        '{"error": 1}',
    ],
)
def test_anything_but_a_good_launch_failed_envelope_is_none(text: str) -> None:
    assert launch_failed_id(text) is None


def test_one_distinct_id_token_is_found_in_the_text() -> None:
    assert single_csession_id(f"allocated {CSID}\n", f"retry {CSID}") == CSID


@pytest.mark.parametrize(
    "texts",
    [
        ("",),
        ("cs-aaaaaaab and cs-aaaaaaac",),
        ("cs-aaaaaaab", "cs-aaaaaaac"),
        ("xcs-aaaaaaab",),
        ("cs-aaaaaaabc",),
        ("cs-aaaaaaa",),
        ("CS-AAAAAAAB",),
        ("cs-aaaaaaa1",),
    ],
)
def test_no_id_or_an_ambiguous_one_is_none(texts: tuple[str, ...]) -> None:
    assert single_csession_id(*texts) is None


def test_a_session_is_valid_under_the_sessions_root() -> None:
    assert validated_session(CSID, f"{ROOT}/{CSID}", ROOT) == (
        CSID,
        f"{ROOT}/{CSID}",
        "-state-sessions-cs-aaaaaaab-work",
    )


@pytest.mark.parametrize(
    ("csid", "directory"),
    [
        ("cs-aaaaaaa", f"{ROOT}/cs-aaaaaaa"),
        ("../x", f"{ROOT}/../x"),
        (CSID, f"{ROOT}/cs-aaaaaaac"),
        (CSID, f"{ROOT}/../sessions/{CSID}"),
        (CSID, f"{ROOT}/{CSID}/"),
        (CSID, f"{ROOT}/{CSID}/.."),
        (CSID, f"/elsewhere/{CSID}"),
        (CSID, f"{ROOT}//{CSID}"),
        (CSID, f"{ROOT}/{CSID}\x00"),
        (CSID, f"{ROOT}/{CSID}é"),
        (CSID, f"relative/{CSID}"),
        (CSID, ""),
        (CSID, 5),
    ],
)
def test_a_malformed_id_or_directory_is_refused(csid: str, directory: Any) -> None:
    assert validated_session(csid, directory, ROOT) is None


def test_a_symlinked_session_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / CSID).symlink_to(elsewhere)
    assert validated_session(CSID, f"{root}/{CSID}", str(root)) is None


def test_a_session_directory_under_a_linked_root_names_its_project_from_the_real_root(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / CSID).mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)  # the root itself is reached through a link: the real path is encoded
    got = validated_session(CSID, f"{link}/{CSID}", str(link))
    assert got is not None
    assert got[2] == re.sub(r"[^A-Za-z0-9]", "-", f"{real}/{CSID}/work")


def test_a_symlinked_ancestor_of_the_root_is_resolved_in_the_project_name(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / "sessions" / CSID).mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)  # an ancestor of the root is a link: Claude Code encodes the real path
    root = f"{link}/sessions"
    got = validated_session(CSID, f"{root}/{CSID}", root)
    assert got is not None
    assert got[1] == f"{root}/{CSID}"  # the directory stays as written
    assert got[2] == re.sub(r"[^A-Za-z0-9]", "-", f"{real}/sessions/{CSID}/work")


def test_a_project_directory_name_over_200_characters_is_refused() -> None:
    root = "/" + "a" * 190
    assert validated_session(CSID, f"{root}/{CSID}", root) is None


def test_a_status_document_is_collected() -> None:
    doc = status_doc(CSID, cost_usd=0.25, num_turns=4)
    doc["run"]["sandbox_active"] = True
    got = parse_status(json.dumps(doc), CSID)
    assert (got.cost_usd, got.num_turns, got.sandbox_active, got.outcome) == (0.25, 4, True, "done")
    assert got.report is not None and got.report["status"] == "done"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(csession_id="cs-aaaaaaac"),
        lambda d: d.pop("run"),
        lambda d: d.update(run=[]),
        lambda d: d["run"].update(sandbox_active="yes"),
        lambda d: d["run"].update(cost_usd=-1),
        lambda d: d["run"].update(cost_usd=True),
        lambda d: d["run"].update(cost_usd=1e999),
        lambda d: d["run"].update(num_turns=-1),
        lambda d: d["run"].update(num_turns=1.5),
        lambda d: d["run"].update(outcome="Done!"),
        lambda d: d["run"].update(report=[]),
        lambda d: d["run"].pop("sandbox_active"),
    ],
)
def test_a_hostile_status_document_is_invalid(mutate: Any) -> None:
    doc = status_doc(CSID)
    mutate(doc)
    with pytest.raises(Invalid):
        parse_status(json.dumps(doc), CSID)


def test_a_status_with_a_huge_integer_is_invalid() -> None:
    doc = json.dumps(status_doc(CSID)).replace('"num_turns": 3', '"num_turns": ' + "9" * 5000)
    with pytest.raises(Invalid):
        parse_status(doc, CSID)


def test_an_outcome_document_is_parsed_for_its_run() -> None:
    doc = outcome_doc(CSID, outcome="timeout", exit_code=16)
    got = parse_outcome(json.dumps(doc), CSID, 1)
    assert (got.phase, got.outcome, got.end_reason, got.exit_code) == (
        "session",
        "timeout",
        "timeout",
        16,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(csession_id="cs-aaaaaaac"),
        lambda d: d.update(run=2),
        lambda d: d.update(run=True),
        lambda d: d.update(run=1.0),
        lambda d: d.update(phase="other"),
        lambda d: d.update(outcome="Timeout"),
        lambda d: d.update(end_reason="exit 3"),
        lambda d: d.update(exit_code=True),
        lambda d: d.update(exit_code=1000),
        lambda d: d.update(exit_code="16"),
        lambda d: d.update(cost_usd="1"),
        lambda d: d.update(num_turns=-3),
        lambda d: d.pop("phase"),
    ],
)
def test_a_hostile_outcome_document_is_invalid(mutate: Any) -> None:
    doc = outcome_doc(CSID)
    mutate(doc)
    with pytest.raises(Invalid):
        parse_outcome(json.dumps(doc), CSID, 1)


@pytest.mark.parametrize("text", ["", "NaN", "[1]", '{"a": 1, "a": 2}', "[" * 50000, "{" * 50000])
def test_an_unparseable_outcome_is_invalid(text: str) -> None:
    with pytest.raises(Invalid):
        parse_outcome(text, CSID, 1)


def test_forget_is_read_only_from_a_forget_object_naming_the_id() -> None:
    assert parse_forget(json.dumps(forget_doc(CSID)), CSID)
    assert not parse_forget(json.dumps(forget_doc("cs-aaaaaaac")), CSID)
    assert not parse_forget(json.dumps(error_doc("failed", "x")), CSID)
    assert not parse_forget('{"forgotten": 3}', CSID)
    assert not parse_forget("", CSID)
    assert not parse_forget('{"forgotten": [], "forgotten": []}', CSID)
