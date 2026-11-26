"""``retro run --json`` exits 11, 12 (a retro exists), 13, 14 to 18 and 22 print ONLY the error
envelope, with the retro id in ``details`` (Exit codes, "With ``--json``, a non-zero exit prints
only the envelope"); exit 20 ``daily_limit`` too, and exits 129, 130 and 143 print nothing (issue
#28). Each document is checked against ``schemas/cli/error.json`` (the envelope) and against a
golden file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .fake_claude_session import FakeClaudeSession, error_doc, step
from .golden import assert_golden
from .ingesthelp import Corpus
from .retrohelp import RetroGateway
from .signalhelp import corpus_fixture, cs_fixture, gw_fixture, hook_config, write_config
from .test_retro_launch import launch, prepared_corpus, rows, session_run


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    return gw_fixture(monkeypatch)


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return corpus_fixture(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    return cs_fixture(gw, tmp_path)


def normalize(document: Any) -> Any:
    """The retro id is random: golden files hold a fixed one."""
    text = json.dumps(document)
    for known in {d for d in _ids(document)}:
        text = text.replace(known, "ra-aaaaaaaa")
    return json.loads(text)


def _ids(document: Any) -> list[str]:
    found = document["error"]["details"].get("retro_id")
    return [found] if isinstance(found, str) else []


# (golden name, exit, how to get there)
CASES = [
    ("error-vetoed-retro-run", 11, "veto", None),
    ("error-needs_human-hook_escalated", 13, "escalate", None),
    ("error-needs_human-session_blocked", 13, "session", "blocked"),
    ("error-needs_human-session_needs_input", 13, "session", "needs-input"),
    ("error-session_ended-exited-unreported", 14, "session", "exited-unreported"),
    ("error-session_ended-killed", 15, "session", "killed"),
    ("error-session_ended-timeout-retro-run", 16, "session", "timeout"),
    ("error-session_ended-lost", 17, "session", "lost"),
    ("error-session_ended-failed", 18, "session", "failed"),
    ("error-artifact_invalid-no_artifact", 22, "session", "done"),
    ("error-failed-claude_session_error-retro-run", 12, "exec_error", None),
]


@pytest.mark.parametrize(("name", "code", "how", "outcome"), CASES, ids=[c[0] for c in CASES])
def test_a_failing_run_prints_only_the_envelope_with_the_retro_id(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    name: str,
    code: int,
    how: str,
    outcome: str | None,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, outcome or "blocked")
    if how == "veto":
        write_config(corpus.home, hook_config(pre_retro="exit 1"))
    elif how == "escalate":
        write_config(corpus.home, hook_config(pre_retro="exit 13"))
    elif how == "exec_error":  # an exec outside the outcome table: exit 12 with the retro id
        cs.script("exec", step(error_doc("internal", "boom"), exit=1))
    result = launch(capsys, gw)
    assert result.code == code, result.err
    assert result.out.count("\n") == 1 and result.out.endswith("\n")  # one line, nothing else
    document = json.loads(result.out)
    assert list(document) == ["error"]
    [row] = rows(corpus.home)
    assert document["error"]["details"]["retro_id"] == row["id"]
    assert result.err  # the message is on stderr as well
    assert_golden(document, name, normalize=normalize)


def test_a_refusal_for_the_daily_limit_prints_only_the_envelope(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "failed")
    write_config(corpus.home, "limits:\n  retros_per_day: 1\n")
    assert launch(capsys, gw).code == 18  # the first run is spent
    result = launch(capsys, gw, "--again")
    assert result.code == 20
    document = json.loads(result.out)
    assert result.out.count("\n") == 1 and list(document) == ["error"]
    assert_golden(document, "error-refused-daily_limit")


def test_an_exit_by_signal_prints_no_envelope(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    from .test_retro_signals import arm

    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    arm(monkeypatch, gw, "mark_running", signal.SIGTERM)
    result = launch(capsys, gw)
    assert (result.code, result.out) == (143, "")
