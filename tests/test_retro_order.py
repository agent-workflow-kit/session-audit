"""``retro run``: the order of checks, steps 1 to 10 (issue #23). Every failing input is reported at
its own step and no later step runs; the hook points of steps 4 and 5 are pinned in place."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from session_audit import errors
from session_audit.command import Context
from session_audit.commands import retro_run
from session_audit.store import open_store

from .digesthelp import prompt, write
from .ingesthelp import S1, S2, S3, Corpus, connect, state_dir_of
from .retrohelp import RetroGateway, retro, seed
from .schemas import validate


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.preflight: Callable[[], None] = lambda: None

    def skill_preflight(self, ctx: Context, skill: str) -> None:
        self.calls.append("preflight")
        self.preflight()

    def housekeeping(self, ctx: Context) -> None:
        self.calls.append("housekeeping")


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(retro_run, "STEPS", recorder)
    return recorder


def write_config(home: Path, text: str, mode: int = 0o600) -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def expect(result, code: int, reason: str | None = None, field: str | None = None) -> None:  # noqa: ANN001
    assert result.code == code, result.err
    document = json.loads(result.out)
    validate(document, "session-audit/cli/error.json")
    details = document["error"]["details"]
    if reason is not None:
        assert details.get("reason") == reason
    if field is not None:
        assert details.get("field") == field


# --- step 1: flags --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "reason", "field"),
    [
        ([], None, None),
        (["--again"], None, None),
        (["--include-self-improve"], None, None),
        (["--since", "7d", "--mode", "interactive"], "mode_unsupported", None),
        (["--since", "7d", "--mode", "other"], None, "--mode"),
        (["--session", "cs-aaaaaaaa"], None, "--session"),
        (["--session", "abc"], None, "--session"),
        (["--session", "CS-AAAAAAAA"], None, "--session"),
        (
            [
                "--session",
                *["11111111"] * 1,
                *sum((["--session", "11111111"] for _ in range(1000)), []),
            ],
            None,
            "--session",
        ),
    ],
)
def test_step_1_flags_come_before_everything(
    argv: list[str],
    reason: str | None,
    field: str | None,
    corpus: Corpus,
    gw: RetroGateway,
    steps: Recorder,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    # An unsafe config, a missing claude-session and an empty store would each fail later.
    write_config(_private_environment, "digest:\n  max_bytes: 1000\n", mode=0o666)
    gw.version = None
    result = retro(capsys, gw, *argv)
    expect(result, 2, reason, field)
    assert steps.calls == []
    assert not state_dir_of(corpus.home).exists()


def test_a_claude_session_id_for_session_says_which_id_the_flag_takes(
    gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    result = retro(capsys, gw, "--session", "cs-abcdefgh")
    expect(result, 2, field="--session")
    assert "Claude Code session id" in result.err


# --- step 2: config and store ---------------------------------------------------------------------


def test_step_2_an_unsafe_config_is_config_unsafe_before_claude_session(
    corpus: Corpus,
    gw: RetroGateway,
    steps: Recorder,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(_private_environment, "digest:\n  max_bytes: 1000\n", mode=0o666)
    gw.version = None
    expect(retro(capsys, gw, "--since", "7d"), 20, "config_unsafe")
    assert steps.calls == [] and gw.runs == []


def test_step_2_a_lowered_sandbox_without_a_terminal_is_config_unsafe(
    corpus: Corpus,
    gw: RetroGateway,
    steps: Recorder,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(_private_environment, "retro:\n  sandbox: preferred\n")
    gw.version = None
    expect(retro(capsys, gw, "--since", "7d"), 20, "config_unsafe", "retro.sandbox")
    assert steps.calls == [] and gw.runs == []
    # With a terminal the run goes on to step 3.
    expect(retro(capsys, gw, "--since", "7d", tty=True), 12, "claude_session_missing")


def test_step_2_a_permission_profile_that_is_group_writable_is_config_unsafe(
    corpus: Corpus,
    gw: RetroGateway,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    profile = _private_environment / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    profile.chmod(0o666)
    write_config(_private_environment, f"retro:\n  permission_profile: {profile}\n")
    gw.version = None
    expect(retro(capsys, gw, "--since", "7d"), 20, "config_unsafe", "retro.permission_profile")


def test_step_2_a_newer_store_is_store_newer_before_claude_session(
    corpus: Corpus, gw: RetroGateway, steps: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    open_store(gw, str(state_dir_of(corpus.home))).close()
    conn = connect(corpus.home)
    conn.execute("INSERT INTO schema_migrations (number, name, applied_at) VALUES (999999, 'x', 0)")
    conn.commit()
    conn.close()
    gw.version = None
    expect(retro(capsys, gw, "--since", "7d"), 20, "store_newer")
    assert steps.calls == [] and gw.runs == []


# --- step 3: claude-session -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        (None, "claude_session_missing"),
        ("claude-session 0.45.1\n", "claude_session_too_old"),
        ("claude-session 0.9.9\n", "claude_session_too_old"),
        ("something else 9.9.9\n", "claude_session_too_old"),
        ("", "claude_session_too_old"),
    ],
)
def test_step_3_claude_session_missing_or_old_before_the_skill_check(
    version: str | None,
    reason: str,
    corpus: Corpus,
    gw: RetroGateway,
    steps: Recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gw.version = version
    result = retro(capsys, gw, "--since", "7d")
    expect(result, 12, reason)
    assert json.loads(result.out)["error"]["details"]["retryable"] is False
    assert steps.calls == []


def test_step_3_a_newer_claude_session_passes(
    corpus: Corpus, gw: RetroGateway, steps: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.version = "claude-session 0.100.0\n"
    expect(retro(capsys, gw, "--since", "7d"), 20, "empty_scope")  # on to step 6


# --- steps 4 and 5: the hook points ---------------------------------------------------------------


def test_step_4_the_skill_preflight_runs_after_step_3_and_before_housekeeping(
    corpus: Corpus, gw: RetroGateway, steps: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> None:
        raise errors.CliError(
            "failed",
            "the skill is not installed",
            details={"reason": "skill_not_found", "retryable": False},
        )

    steps.preflight = refuse
    expect(retro(capsys, gw, "--since", "7d"), 12, "skill_not_found")  # empty scope would be 20
    assert steps.calls == ["preflight"]


def test_step_5_housekeeping_runs_before_a_scope_refusal_and_never_in_a_dry_run(
    corpus: Corpus, gw: RetroGateway, steps: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    expect(retro(capsys, gw, "--since", "7d"), 20, "empty_scope")
    assert steps.calls == ["preflight", "housekeeping"]
    steps.calls.clear()
    expect(retro(capsys, gw, "--since", "7d", "--dry-run"), 20, "empty_scope")
    assert steps.calls == ["preflight"]


# --- step 6: scope --------------------------------------------------------------------------------


def test_step_6_scope_failures_in_their_exit_codes(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1)
    write(corpus, "11111111-2222-4222-8222-222222222222", [prompt("x", 1)])
    from .retrohelp import ingested

    ingested(capsys, gw)
    # unknown prefix: 10; ambiguous prefix: 21; nothing matched: 20 empty_scope
    result = retro(capsys, gw, "--session", "99999999")
    expect(result, 10)
    assert json.loads(result.out)["error"]["details"] == {"id": "99999999"}
    result = retro(capsys, gw, "--session", "11111111")
    expect(result, 21)
    assert json.loads(result.out)["error"]["details"]["matched"] == 2
    expect(retro(capsys, gw, "--project", "/nowhere"), 20, "empty_scope")
    expect(retro(capsys, gw, "--label", "a=b"), 20, "empty_scope")


def test_step_6_an_unknown_prefix_after_a_full_unknown_id_ingests_nothing(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1)  # on disk, not in the store
    result = retro(capsys, gw, "--session", S1, "--session", "99999999")
    expect(result, 10)
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()


def test_step_6_a_full_id_unknown_to_the_store_is_found_on_disk_and_ingested(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1)
    seed(corpus, S2)
    result = retro(capsys, gw, "--session", S1)
    assert result.code == 1  # the seam after step 10
    conn = connect(corpus.home)
    assert [r[0] for r in conn.execute("SELECT session_id FROM sessions")] == [S1]
    conn.close()
    # A full id that is on no disk is exit 10.
    expect(retro(capsys, gw, "--session", S3), 10)


def test_step_6_a_symlinked_transcript_is_never_looked_up(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(prompt("x", 1))
    link = corpus.main_path(S1)
    os.symlink(target, link)
    expect(retro(capsys, gw, "--session", S1), 10)


def test_step_6_an_explicit_list_over_the_cap_is_scope_too_large(
    corpus: Corpus,
    gw: RetroGateway,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(_private_environment, "retro:\n  max_sessions: 2\n")
    for sid in (S1, S2, S3):
        seed(corpus, sid)
    from .retrohelp import ingested

    ingested(capsys, gw)
    result = retro(capsys, gw, "--session", S1, "--session", S2, "--session", S3)
    expect(result, 20, "scope_too_large")
    details = json.loads(result.out)["error"]["details"]
    assert (details["matched"], details["limit"]) == (3, 2)
    # Duplicates and prefixes of the same sessions are one session each.
    result = retro(
        capsys, gw, "--session", S1, "--session", S1[:8], "--session", S1.upper(), "--session", S2
    )
    assert result.code == 1  # past step 10, at the seam


# --- step 8: nothing kept -------------------------------------------------------------------------


def test_step_8_live_sessions_only_and_empty_scope(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    from .retrohelp import ingested, mtime_of, touch

    live = seed(corpus, S1)
    touch(live, mtime_of(gw, 60))
    ingested(capsys, gw)
    expect(retro(capsys, gw, "--project", "/work/p1"), 20, "live_sessions_only")
    # A session with no item at all is skipped `empty`, so the scope is empty, not live.
    write(corpus, S2, [b'{"type": "summary"}\n'])
    ingested(capsys, gw)
    expect(retro(capsys, gw, "--session", S2), 20, "empty_scope")
    # Live and empty together: not every skip is live.
    expect(retro(capsys, gw, "--session", S1, "--session", S2), 20, "empty_scope")
    live.unlink()
    expect(retro(capsys, gw, "--session", S1), 20, "empty_scope")  # transcript_missing
