"""Housekeeping (issue #27): recovery of the retros a dead process left behind by recorded id, the
label sanity check, the cleanup retries, the 120 s bound, deferral without claude-session, expired
digests and old temporary files, and its two call sites (``retro run`` step 5 and ``prune``)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import cli, lease, retrorecovery
from session_audit.lease import identity

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    plan_doc,
    step,
    version_step,
)
from .ingesthelp import Corpus, connect, state_dir_of
from .recoveryhelp import (
    CSID,
    OTHER,
    PROJECT,
    hook_settings,
    housekeep,
    keys,
    log_states,
    not_found,
    orphan,
    payloads,
    plant_local_files,
    plant_session,
    plant_transcripts,
    retro_rows,
    script_cleanup,
    script_status,
    session_rows,
    touch_old,
)
from .retrohelp import RETRO, RetroGateway, live_lease, rid
from .schemas import validate


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


def running_with_session(corpus: Corpus, retro_id: str = RETRO, csid: str = CSID) -> None:
    plant_session(corpus.home, csid, retro_id)
    orphan(corpus.home, retro_id, csid=csid)


# --- a retro with no session -------------------------------------------------------------------


@pytest.mark.parametrize("state", ["digesting", "running", "validating"])
def test_a_non_final_retro_without_a_session_ends_failed_runner_lost(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, state: str, tmp_path: Path
) -> None:
    orphan(corpus.home, state=state)
    files = plant_local_files(corpus.home)
    hooks, retro_log, attention_log = hook_settings(tmp_path)
    result = housekeep(gw, corpus, hooks=hooks)
    assert result.recovered == [RETRO] and not result.deferred
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"]) == ("failed", "runner_lost")
    assert row["finished_at"] is not None and row["lease_pid"] is None
    assert keys(cs) == []  # no recorded id: claude-session is never asked anything
    assert not files["prompt"].parent.exists() and not files["outcome"].exists()
    assert not files["digest"].parent.exists()  # never launched: no use for the digest
    [entry] = log_states(corpus.home)
    validate(entry, "session-audit/log.json")
    assert (entry["state"], entry["reason"], entry["actor"]) == (
        "failed",
        "runner_lost",
        "auto:recover",
    )
    [post] = payloads(retro_log)
    validate(post, "session-audit/hooks/post-retro.json")
    assert (post["retro_id"], post["state"], post["reason"]) == (RETRO, "failed", "runner_lost")
    [attention] = payloads(attention_log)
    validate(attention, "session-audit/hooks/post-attention.json")
    assert attention["details"] == {
        "retro_id": RETRO,
        "failure_reason": "runner_lost",
        "csession_id": None,
    }


def test_the_start_of_a_recovered_retro_is_kept(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    orphan(corpus.home)
    conn = connect(corpus.home)
    conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES ('2026-01-01', ?)", (RETRO,))
    conn.commit()
    conn.close()
    housekeep(gw, corpus)
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM retro_starts").fetchone()[0] == 1
    conn.close()


def test_a_second_housekeeping_does_nothing_more(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    orphan(corpus.home, state="digesting")
    hooks, retro_log, _ = hook_settings(tmp_path)
    assert housekeep(gw, corpus, hooks=hooks).recovered == [RETRO]
    assert housekeep(gw, corpus, hooks=hooks).recovered == []
    assert len(payloads(retro_log)) == 1  # post-retro exactly once
    assert len(log_states(corpus.home)) == 1


# --- a retro with a recorded session -----------------------------------------------------------


def test_a_live_run_is_stopped_forgotten_and_cleaned_up(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    running_with_session(corpus)
    plant_transcripts(corpus)
    files = plant_local_files(corpus.home)
    sessions = corpus.home / "s" / CSID
    sessions.mkdir(parents=True)
    script_status(cs, live=True)
    script_cleanup(cs, purge=[sessions])
    hooks, retro_log, attention_log = hook_settings(tmp_path)
    conn = connect(corpus.home)
    conn.execute("UPDATE retro_sessions SET csession_dir = ?", (str(sessions),))
    conn.commit()
    conn.close()
    result = housekeep(gw, corpus, hooks=hooks)
    assert result.recovered == [RETRO]
    assert keys(cs) == ["status", "stop", "forget"]
    [stop] = [c["argv"] for c in cs.calls() if c["key"] == "stop"]
    assert stop == ["stop", CSID, "--grace", "10s", "--json"]
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"], row["lease_pid"]) == ("failed", "runner_lost", None)
    session = session_rows(corpus.home)[CSID]
    assert session["cleanup_state"] == "done" and session["forgotten_at"] is not None
    assert not (corpus.root / PROJECT).exists()
    assert not files["prompt"].parent.exists()
    assert files["digest"].exists()  # a launched retro keeps its digest until retention
    [post] = payloads(retro_log)
    assert post["state"] == "failed"
    [attention] = payloads(attention_log)
    assert attention["details"]["csession_id"] == CSID
    cs.assert_outputs_valid()


@pytest.mark.parametrize("live", [False, True])
def test_a_finished_run_is_not_stopped(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, live: bool
) -> None:
    running_with_session(corpus)
    script_status(cs, live=live)
    script_cleanup(cs)
    housekeep(gw, corpus)
    assert keys(cs) == (["status", "stop", "forget"] if live else ["status", "forget"])


def test_a_session_claude_session_no_longer_knows_is_not_stopped(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    running_with_session(corpus)
    cs.script("status", not_found())
    cs.script("forget", not_found())
    assert housekeep(gw, corpus).recovered == [RETRO]
    assert keys(cs) == ["status", "forget"]
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"


def test_a_validating_retro_is_runner_lost_too(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    orphan(corpus.home, state="validating", csid=CSID)
    script_status(cs)
    script_cleanup(cs)
    housekeep(gw, corpus)
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"]) == ("failed", "runner_lost")


# --- the label sanity check --------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels",
    [
        [],
        ["owner=someone-else", f"retro={RETRO}"],
        ["owner=session-audit"],
        ["owner=session-audit", "retro=ra-zzzzzzzz"],
        [f"retro={RETRO}"],
    ],
    ids=["none", "other-owner", "no-retro", "other-retro", "no-owner"],
)
def test_a_session_without_the_tools_labels_is_skipped_and_nothing_is_touched(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, labels: list[str], tmp_path: Path
) -> None:
    running_with_session(corpus)
    made = plant_transcripts(corpus)
    script_status(cs, labels=labels, live=True)
    script_cleanup(cs)
    hooks, retro_log, _ = hook_settings(tmp_path)
    result = housekeep(gw, corpus, hooks=hooks)
    assert result.recovered == [RETRO] and result.skipped == [RETRO]
    assert keys(cs) == ["status"]  # no stop, no forget
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "skipped"
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is None
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"]) == ("failed", "runner_lost")
    assert json.loads(row["warnings"]) == ["recovery_skipped"]
    assert made["project"].exists()  # not removed: the session is not the tool's own
    assert len(payloads(retro_log)) == 1  # it still reached a final state


@pytest.mark.parametrize("planted", ["no-row", "other-retro", "bad-id"])
def test_a_recorded_id_without_a_matching_session_row_is_never_looked_up(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, planted: str
) -> None:
    csid = "cs-../x" if planted == "bad-id" else CSID
    if planted == "other-retro":
        plant_session(corpus.home, CSID, rid(7))
    orphan(corpus.home, csid=csid)
    script_status(cs)
    script_cleanup(cs)
    result = housekeep(gw, corpus)
    assert result.recovered == [RETRO] and result.skipped == [RETRO]
    # forget and stop only ever run on a session the tool recorded for the retro: another
    # retro's row is verified by its own labels (not this retro's), so it is skipped
    assert "forget" not in keys(cs) and "stop" not in keys(cs)
    assert keys(cs) == (["status"] if planted == "other-retro" else [])
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], json.loads(row["warnings"])) == ("failed", ["recovery_skipped"])


def test_recovery_ignores_harness_enabled_off(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    running_with_session(corpus)
    script_status(cs)
    script_cleanup(cs)
    assert housekeep(gw, corpus, harness_enabled="off").recovered == [RETRO]


# --- deferral ----------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["missing", "old", "garbage"])
def test_without_a_usable_claude_session_a_retro_with_a_session_waits(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    version: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    running_with_session(corpus)
    hooks, retro_log, _ = hook_settings(tmp_path)
    if version == "missing":
        gw.version = None
        gw.fake = None
    elif version == "old":
        cs.script("--version", version_step("0.45.1"))
    else:
        cs.script("--version", step("hello\n", unvalidated=True))
    result = housekeep(gw, corpus, hooks=hooks)
    assert result.deferred and result.recovered == [] and result.warnings == ["recovery_deferred"]
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["lease_pid"]) == ("running", None)  # released, not final
    assert not retro_log.exists() and log_states(corpus.home) == []  # nothing fires
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "pending"
    # claude-session answers later: the next housekeeping recovers it
    gw.version = "claude-session 0.45.3\n"
    cs.script("--version", version_step())
    gw.fake = cs
    script_status(cs)
    script_cleanup(cs)
    assert housekeep(gw, corpus, hooks=hooks).recovered == [RETRO]


def test_a_retro_without_a_session_is_recovered_even_without_claude_session(
    gw: RetroGateway, corpus: Corpus
) -> None:
    gw.version = None
    orphan(corpus.home, state="digesting")
    result = housekeep(gw, corpus)
    assert result.recovered == [RETRO] and not result.deferred
    assert gw.runs == []  # not even a version check was needed


@pytest.mark.parametrize(
    "status",
    [
        step(error_doc("failed", "x"), exit=12),
        step(error_doc("internal", "x"), exit=1),
        step("not json", unvalidated=True),
        step({"csession_id": OTHER}, unvalidated=True),
        step("x" * 2_000_000, unvalidated=True),
    ],
    ids=["exit-12", "exit-1", "junk", "other-id", "flood"],
)
def test_a_status_that_cannot_be_used_defers_the_retro_untouched(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    status: dict[str, Any],
    tmp_path: Path,
) -> None:
    running_with_session(corpus)
    cs.script("status", status)
    script_cleanup(cs)
    hooks, retro_log, _ = hook_settings(tmp_path)
    result = housekeep(gw, corpus, hooks=hooks)
    assert result.deferred and result.recovered == []
    assert keys(cs) == ["status"]
    assert retro_rows(corpus.home)[RETRO]["state"] == "running"
    assert not retro_log.exists()


def test_a_status_that_hangs_is_cut_at_its_timeout(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_audit import retrocleanup

    running_with_session(corpus)
    cs.script("status", step({"csession_id": CSID}, sleep=30, unvalidated=True))
    monkeypatch.setattr(retrocleanup, "CALL_TIMEOUT", 1.0)
    started = time.monotonic()
    result = housekeep(gw, corpus)
    assert time.monotonic() - started < 20 and result.deferred
    assert retro_rows(corpus.home)[RETRO]["state"] == "running"


# --- leases ------------------------------------------------------------------------------------


def test_a_retro_whose_lease_is_live_is_left_alone(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    orphan(corpus.home, state="running", lease=live_lease(gw))
    result = housekeep(gw, corpus)
    assert result.recovered == [] and keys(cs) == []
    assert retro_rows(corpus.home)[RETRO]["state"] == "running"


@pytest.mark.parametrize("kind", ["expired", "dead-pid", "wrong-start"])
def test_a_lease_that_is_not_live_is_taken_over(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, kind: str
) -> None:
    now = int(gw.now().timestamp() * 1000)
    holder = identity(gw)
    leases = {
        "expired": (holder.pid, holder.start, now - 1),  # the holder lives but stopped renewing
        "dead-pid": (2**22 + 12345, "123", now + 60_000),
        "wrong-start": (holder.pid, "not-my-start", now + 60_000),  # a reused pid
    }
    orphan(corpus.home, state="digesting", lease=leases[kind])
    assert housekeep(gw, corpus).recovered == [RETRO]
    assert retro_rows(corpus.home)[RETRO]["lease_pid"] is None


def test_a_takeover_that_lost_the_compare_and_set_skips_the_retro(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan(corpus.home, state="digesting")
    monkeypatch.setattr(lease, "take_over", lambda *a, **k: None)  # another process won
    assert housekeep(gw, corpus).recovered == []
    assert retro_rows(corpus.home)[RETRO]["state"] == "digesting"


def test_the_row_is_read_again_under_the_lease(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retro that became final between the listing and the takeover is released untouched."""
    orphan(corpus.home, state="digesting")
    real = lease.take_over

    def finished_meanwhile(*args: Any, **kwargs: Any) -> Any:
        conn = connect(corpus.home)
        conn.execute("UPDATE retros SET state = 'done'")
        conn.commit()
        conn.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(lease, "take_over", finished_meanwhile)
    assert housekeep(gw, corpus).recovered == []
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["lease_pid"]) == ("done", None)


# --- the bound ---------------------------------------------------------------------------------


def test_housekeeping_starts_no_new_item_after_120_seconds(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        orphan(corpus.home, rid(n + 1), state="digesting", created=1000 + n)
    real = retrorecovery._Run._recover

    def slow(self: Any, retro_id: str) -> None:
        real(self, retro_id)
        gw.clock.advance(121)  # the first item took two minutes

    monkeypatch.setattr(retrorecovery._Run, "_recover", slow)
    result = housekeep(gw, corpus)
    assert result.recovered == [rid(1)] and result.deferred
    rows = retro_rows(corpus.home)
    assert [rows[rid(n)]["state"] for n in (1, 2, 3)] == ["failed", "digesting", "digesting"]


def test_the_cleanup_retries_stop_at_the_bound_too(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        plant_session(corpus.home, f"cs-aaaaaaa{'bcd'[n]}", rid(n + 1), created_at=n + 1)
        orphan(corpus.home, rid(n + 1), state="done", created=1000)
    script_status(cs)
    script_cleanup(cs)
    cs.script("status", step({"csession_id": CSID}, unvalidated=True))
    real = retrorecovery._Run._retry

    def slow(self: Any, csid: str) -> None:
        real(self, csid)
        gw.clock.advance(121)

    monkeypatch.setattr(retrorecovery._Run, "_retry", slow)
    result = housekeep(gw, corpus)
    assert result.deferred
    assert (
        sum(1 for r in session_rows(corpus.home).values() if r["cleanup_state"] == "pending") >= 2
    )


# --- cleanup retries ---------------------------------------------------------------------------


def final_retro_with_pending_session(corpus: Corpus, **session: Any) -> None:
    plant_session(corpus.home, **session)
    orphan(corpus.home, state="failed", csid=CSID, reason="session_failed")


def test_a_pending_row_of_a_final_retro_is_cleaned_up_by_housekeeping(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    final_retro_with_pending_session(corpus)
    made = plant_transcripts(corpus)
    script_status(cs)
    script_cleanup(cs)
    result = housekeep(gw, corpus)
    assert result.recovered == [] and result.cleaned == 1
    assert keys(cs) == ["status", "forget"]  # the labels are checked before any forget
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"
    assert not made["project"].exists()
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["lease_pid"]) == ("failed", None)  # the lease it took is released


def test_a_pending_row_whose_forget_was_done_does_not_call_claude_session_for_the_forget(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    final_retro_with_pending_session(corpus, forgotten_at=5)
    made = plant_transcripts(corpus)
    housekeep(gw, corpus)
    assert keys(cs) == []
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"
    assert not made["project"].exists()


def test_an_incomplete_row_stays_incomplete_until_claude_session_has_purged_its_copy(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    remaining = corpus.home / "s" / CSID
    remaining.mkdir(parents=True)
    final_retro_with_pending_session(
        corpus, state="incomplete", forgotten_at=5, csession_dir=str(remaining)
    )
    for _ in range(2):
        housekeep(gw, corpus)
        assert session_rows(corpus.home)[CSID]["cleanup_state"] == "incomplete"
    remaining.rmdir()
    housekeep(gw, corpus)
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"


def test_a_row_whose_retro_was_deleted_is_still_cleaned_up(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)  # no retro row at all
    script_status(cs)
    script_cleanup(cs)
    housekeep(gw, corpus)
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"


def test_a_row_of_a_running_retro_is_left_to_its_holder(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    orphan(corpus.home, state="running", csid=CSID, lease=live_lease(gw))
    housekeep(gw, corpus)
    assert keys(cs) == []
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "pending"


def test_a_final_retro_with_a_live_lease_is_cleaning_up_itself(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    orphan(corpus.home, state="failed", csid=CSID, lease=live_lease(gw), reason="session_failed")
    housekeep(gw, corpus)
    assert keys(cs) == []


def test_a_row_without_the_tools_labels_is_skipped_and_never_forgotten(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    final_retro_with_pending_session(corpus)
    made = plant_transcripts(corpus)
    script_status(cs, labels=["owner=other"])
    script_cleanup(cs)
    housekeep(gw, corpus)
    assert keys(cs) == ["status"]
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "skipped"
    assert made["project"].exists()


def test_cleanup_waits_for_claude_session(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    gw.version = None
    gw.fake = None
    final_retro_with_pending_session(corpus)
    made = plant_transcripts(corpus)
    result = housekeep(gw, corpus)
    assert result.deferred
    assert made[
        "project"
    ].exists()  # the doc: cleanup retries run only with a usable claude-session
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "pending"


def test_a_failing_item_is_a_warning_and_the_next_one_still_runs(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orphan(corpus.home, rid(1), state="digesting", created=1)
    orphan(corpus.home, rid(2), state="digesting", created=2)
    real = retrorecovery._Run._finish
    calls: list[str] = []

    def boom(self: Any, retro: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(retro.id)
        if retro.id == rid(1):
            raise RuntimeError("secret detail that must not be printed")
        real(self, retro, *args, **kwargs)

    monkeypatch.setattr(retrorecovery._Run, "_finish", boom)
    result = housekeep(gw, corpus, quiet=False)
    err = capsys.readouterr().err
    assert result.recovered == [rid(2)] and calls == [rid(1), rid(2)]
    assert "recovery failed (RuntimeError)" in err and "secret detail" not in err
    rows = retro_rows(corpus.home)
    assert rows[rid(1)]["state"] == "digesting" and rows[rid(1)]["lease_pid"] is None


# --- local files -------------------------------------------------------------------------------


def make_tmp(corpus: Corpus) -> Path:
    from .recoveryhelp import ensure_store

    ensure_store(corpus.home)
    tmp = state_dir_of(corpus.home) / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    return tmp


def test_tmp_entries_older_than_an_hour_go_including_stale_plan_directories(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    tmp = make_tmp(corpus)
    stale = tmp / "plan-0123456789abcdef"
    stale.mkdir(mode=0o700)
    (stale / "prompt.md").write_text("")
    (stale / "digest.md").write_text("")
    fresh = tmp / "plan-fedcba9876543210"
    fresh.mkdir(mode=0o700)
    old_file = tmp / "old.txt"
    old_file.write_text("x")
    for path in (stale, old_file):
        touch_old(path, 3601 + (time.time() - gw.now().timestamp()))
    housekeep(gw, corpus)
    assert not stale.exists() and not old_file.exists()
    assert fresh.exists()


def test_a_symlink_in_tmp_is_unlinked_never_followed_and_a_symlinked_tmp_is_not_entered(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "file").write_text("keep")
    tmp = make_tmp(corpus)
    link = tmp / "plan-link"
    link.symlink_to(target)
    touch_old(link, 7200 + (time.time() - gw.now().timestamp()))
    housekeep(gw, corpus)
    assert not os.path.lexists(link) and (target / "file").read_text() == "keep"
    tmp.rmdir()
    tmp.symlink_to(target)
    (target / "plan-x").mkdir()
    touch_old(target / "plan-x", 7200 + (time.time() - gw.now().timestamp()))
    housekeep(gw, corpus)
    assert (target / "plan-x").exists()  # never entered through the link


def test_expired_digests_go_with_a_housekeeping_that_has_no_retro_to_recover(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    old = int(gw.now().timestamp() * 1000) - 8 * 86_400_000
    orphan(corpus.home, rid(1), state="done", created=old)
    orphan(corpus.home, rid(2), state="running", created=old, lease=live_lease(gw))
    paths = {n: plant_local_files(corpus.home, rid(n))["digest"] for n in (1, 2)}
    housekeep(gw, corpus)
    assert not paths[1].parent.exists()  # retention.digests (7 days) passed
    assert paths[2].exists()  # a live retro's digest is never expired under it


def test_nothing_is_created_for_a_state_directory_that_does_not_exist(
    gw: RetroGateway, corpus: Corpus
) -> None:
    assert housekeep(gw, corpus).recovered == []
    assert not state_dir_of(corpus.home).exists()


# --- the two call sites ------------------------------------------------------------------------


def test_retro_run_recovers_at_step_5_before_the_scope_is_resolved(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from .retrohelp import retro as run_retro

    orphan(corpus.home, state="digesting")
    run = run_retro(capsys, gw, "--session", "11111111")  # an unknown session: exit 10 at step 6
    assert run.code == 10, run.err
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"]) == ("failed", "runner_lost")  # logged though refused
    assert "recovered 1 retro(s)" in run.err


def test_retro_run_dry_run_never_recovers(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from .ingesthelp import S1
    from .retrohelp import ingested, seed
    from .retrohelp import retro as run_retro

    seed(corpus, S1)
    ingested(capsys, gw)
    orphan(corpus.home, state="digesting")
    run = run_retro(capsys, gw, "--dry-run", "--project", "/work")
    assert run.code == 0, run.err
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["lease_pid"]) == ("digesting", None)
    assert log_states(corpus.home) == []


def prune(capsys: pytest.CaptureFixture[str], gw: RetroGateway, *argv: str) -> tuple[int, Any, str]:
    capsys.readouterr()
    code = cli.main(["prune", "--json", "--actor", "human:tester", *argv], gateway=gw)
    captured = capsys.readouterr()
    document = json.loads(captured.out) if captured.out.strip() else None
    return code, document, captured.err


def test_prune_recovers_lists_it_and_logs_the_count(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    orphan(corpus.home, rid(1), state="digesting")
    running_with_session(corpus, rid(2), CSID)
    script_status(cs, rid(2))
    script_cleanup(cs)
    code, doc, err = prune(capsys, gw)
    assert code == 0, err
    validate(doc, "session-audit/cli/prune.json")
    assert sorted(doc["recoveries"]) == [rid(1), rid(2)]
    assert doc["warnings"] == []
    entries = [
        json.loads(line)
        for line in (state_dir_of(corpus.home) / "log.jsonl").read_text().splitlines()
    ]
    [pruned] = [e for e in entries if e["action"] == "prune"]
    validate(pruned, "session-audit/log.json")
    assert pruned["recovered"] == 2
    assert {r["state"] for r in retro_rows(corpus.home).values()} == {"failed"}


def test_prune_dry_run_only_lists(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    running_with_session(corpus)
    orphan(corpus.home, rid(3), state="digesting", lease=live_lease(gw))  # live: not listed
    before = retro_rows(corpus.home)
    code, doc, err = prune(capsys, gw, "--dry-run")
    assert code == 0, err
    validate(doc, "session-audit/cli/prune.json")
    assert doc["dry_run"] is True and doc["recoveries"] == [RETRO]
    assert keys(cs) == [] and gw.runs == []  # no claude-session call at all
    after = retro_rows(corpus.home)
    assert {k: tuple(v) for k, v in after.items()} == {k: tuple(v) for k, v in before.items()}
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "pending"


def test_prune_without_claude_session_defers_with_a_warning_and_still_runs_retention(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.version = None
    gw.fake = None
    running_with_session(corpus)
    orphan(corpus.home, rid(1), state="digesting")  # no session: recovered all the same
    tmp = make_tmp(corpus)
    stale = tmp / "plan-aaaaaaaaaaaaaaaa"
    stale.mkdir(mode=0o700)
    touch_old(stale, 7200 + (time.time() - gw.now().timestamp()))
    code, doc, err = prune(capsys, gw)
    assert code == 0, err
    validate(doc, "session-audit/cli/prune.json")
    assert doc["warnings"] == ["recovery_deferred"] and doc["recoveries"] == [rid(1)]
    assert retro_rows(corpus.home)[RETRO]["state"] == "running"
    assert not stale.exists()  # local deletes still run


def test_housekeeping_never_changes_the_exit_code(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan(corpus.home, state="digesting")

    def explode(self: Any, retro_id: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(retrorecovery._Run, "_recover", explode)
    code, doc, err = prune(capsys, gw)
    assert code == 0 and doc["recoveries"] == []
    assert "recovery failed (RuntimeError)" in err and "boom" not in err
