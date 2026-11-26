"""Double signals, real processes and hostile terminals (issue #28).

A second signal while the first is handled exits at once with the code of the first; what it leaves
is what a crash leaves, and housekeeping (recovery by the recorded id, #27) finishes it. Every
process a test starts is recorded and ended by its exact pid or process group."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import tripwire_dir
from .fake_claude_session import FakeClaudeSession, exec_doc, step
from .ingesthelp import Corpus, state_dir_of
from .procs import gone, wait_until_gone
from .recoveryhelp import CSID, housekeep, retro_rows, script_cleanup, script_status, session_rows
from .retrohelp import RetroGateway
from .signalhelp import (
    corpus_fixture,
    cs_fixture,
    gw_fixture,
    hook_config,
    payloads,
    recorder,
    retro_child,
    wait_for,
    write_config,
)
from .test_retro_launch import keys, launch, prepared_corpus, rows, session_run
from .test_retro_signals import arm, write_lock_is_free


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    return gw_fixture(monkeypatch, real_clock=True)


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return corpus_fixture(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    return cs_fixture(gw, tmp_path)


# --- a second signal, in-process ---------------------------------------------------------------

# point -> (state before recovery, whether the retro has a recorded session, whether it is final)
DOUBLE = {
    "mark_running": ("digesting", False, False),
    "start": ("running", False, False),
    "exec": ("running", True, False),
    "cleanup": ("escalated", True, True),
    "post_hooks": ("escalated", True, True),
}


@pytest.mark.parametrize("point", list(DOUBLE))
def test_a_second_signal_exits_at_once_with_the_first_ones_code_and_recovery_finishes(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    point: str,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    pid_file = tmp_path / "exec.pid"
    if point == "exec":
        cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=60, pid_file=pid_file))
    post = tmp_path / "post"
    write_config(corpus.home, hook_config(post_retro=recorder(post)))
    arm(monkeypatch, gw, point, signal.SIGINT, second=True)
    started = time.monotonic()
    try:
        result = launch(capsys, gw)
        assert (result.code, result.out) == (130, "")  # the code of the FIRST signal
        assert "Traceback" not in result.err
        assert time.monotonic() - started < 30
        assert write_lock_is_free(corpus.home)
        [row] = retro_rows(corpus.home).values()
        state, has_session, final = DOUBLE[point]
        assert row["state"] == state and row["lease_pid"] is None  # the lease was released
        assert (CSID in session_rows(corpus.home)) is has_session
        if point == "exec":
            assert wait_for(pid_file, 30)
            assert not gone(int(pid_file.read_text()))  # left to its own timeout and to recovery
        fired = len(payloads(post))
        assert fired == 0  # a second signal skips the post hooks (no outbox)
        # recovery by the recorded id finishes what was left
        monkeypatch.undo()
        monkeypatch.setattr(gw, "machine_time_zone", lambda env: "UTC")
        script_status(cs, row["id"], CSID, live=point == "exec")
        script_cleanup(cs)
        recovered = housekeep(gw, corpus)
        after = retro_rows(corpus.home)[row["id"]]
        if final:
            assert recovered.recovered == []
            assert (after["state"], after["reason"]) == ("escalated", "session_blocked")
        else:
            assert recovered.recovered == [row["id"]]
            assert (after["state"], after["reason"]) == ("failed", "runner_lost")
        assert after["lease_pid"] is None
        if has_session:
            [session] = session_rows(corpus.home).values()
            assert session["cleanup_state"] == "done" and session["forgotten_at"] is not None
        assert not (state_dir_of(corpus.home) / "prompts" / row["id"]).exists()
        again = housekeep(gw, corpus)  # idempotent
        assert again.recovered == [] and again.cleaned == 0
        cs.assert_outputs_valid()
    finally:
        if point == "exec" and pid_file.exists() and not gone(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)  # the recorded exec child
            assert wait_until_gone(int(pid_file.read_text()))


# --- real processes -----------------------------------------------------------------------------


def finish(child: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return child.communicate(timeout=120)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)  # the group this test created
            child.wait()


def test_a_real_sigint_during_exec_stops_the_run_cleanly(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    exec_pid = tmp_path / "exec.pid"
    cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=60, pid_file=exec_pid))
    post = tmp_path / "post"
    write_config(corpus.home, hook_config(post_retro=recorder(post)))
    child = retro_child(cs, corpus.home, tmp_path_factory, "--project", "/work")
    try:
        assert wait_for(exec_pid, 90, lambda: child.poll() is None), child.communicate()
        started = time.monotonic()
        child.send_signal(signal.SIGINT)  # the exact pid of the tool we started
        out, err = finish(child)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    assert (child.returncode, out) == (130, "")
    assert "Traceback" not in err
    assert time.monotonic() - started < 30
    assert wait_until_gone(int(exec_pid.read_text()))
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert row["lease_pid"] is None and write_lock_is_free(corpus.home)
    [session] = rows(corpus.home, "retro_sessions")
    assert session["cleanup_state"] == "done"
    assert [p["state"] for p in payloads(post)] == ["canceled"]
    assert len(rows(corpus.home, "retro_starts")) == 1  # the model may have run: not refunded
    assert [c for c in cs.calls() if c["key"] == "stop"] == []


def test_a_real_sighup_with_a_closed_terminal_still_ends_clean(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The terminal goes away: SIGHUP, and both standard streams are broken (EPIPE on every
    progress line). The run still cancels, cleans up and exits 129 without a traceback."""
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    exec_pid = tmp_path / "exec.pid"
    cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=60, pid_file=exec_pid))
    child = retro_child(cs, corpus.home, tmp_path_factory, "--project", "/work")
    try:
        assert wait_for(exec_pid, 90, lambda: child.poll() is None), child.communicate()
        assert child.stdout is not None and child.stderr is not None
        child.stdout.close()  # the reader is gone: every write is EPIPE
        child.stderr.close()
        child.send_signal(signal.SIGHUP)
        assert child.wait(timeout=60) == 129
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert wait_until_gone(int(exec_pid.read_text()))
    assert rows(corpus.home, "retro_sessions")[0]["cleanup_state"] == "done"


def test_two_real_signals_in_quick_succession_exit_at_once_and_recovery_ends_the_rest(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    exec_pid = tmp_path / "exec.pid"
    # the exec child ignores SIGTERM: the forwarded signal does nothing, so only the second signal
    # ends the tool, and the child is left running (it is ended below by its recorded pid)
    cs.script(
        "exec",
        step(exec_doc(CSID, "blocked"), exit=13, sleep=60, pid_file=exec_pid, ignore_term=True),
    )
    child = retro_child(cs, corpus.home, tmp_path_factory, "--project", "/work")
    started = 0.0
    try:
        assert wait_for(exec_pid, 90, lambda: child.poll() is None), child.communicate()
        started = time.monotonic()
        child.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        child.send_signal(signal.SIGINT)
        out, err = finish(child)
        assert (child.returncode, out) == (143, "")  # the first signal's code
        assert "Traceback" not in err
        assert time.monotonic() - started < 20  # not the 30 s of the forward wait
        [row] = rows(corpus.home)
        assert (row["state"], row["lease_pid"]) == ("running", None)
        # recovery: the session is live, so it is stopped by its id, then the retro is runner_lost
        script_status(cs, row["id"], CSID, live=True)
        script_cleanup(cs)
        monkeypatch.setattr(gw, "machine_time_zone", lambda env: "UTC")
        recovered = housekeep(gw, corpus)
        assert recovered.recovered == [row["id"]]
        after = retro_rows(corpus.home)[row["id"]]
        assert (after["state"], after["reason"]) == ("failed", "runner_lost")
        assert [c["argv"][-1] for c in cs.calls() if c["key"] == "stop"][0:1] != []
    finally:
        if exec_pid.exists() and not gone(int(exec_pid.read_text())):
            os.kill(int(exec_pid.read_text()), signal.SIGKILL)
            assert wait_until_gone(int(exec_pid.read_text()))


def test_a_real_sigterm_during_the_pre_retro_hook_ends_the_hook_group(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    pids = tmp_path / "hook.pids"
    command = f"echo $$ > {pids}; sleep 60 & echo $! >> {pids}; wait"
    write_config(corpus.home, hook_config(pre_retro=command))
    child = retro_child(cs, corpus.home, tmp_path_factory, "--project", "/work")
    try:
        assert wait_for(pids, 90, lambda: child.poll() is None), child.communicate()
        time.sleep(0.3)  # the background child's pid is written right behind the shell's
        child.send_signal(signal.SIGTERM)
        out, _err = finish(child)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    assert (child.returncode, out) == (143, "")
    for pid in pids.read_text().split():
        assert wait_until_gone(int(pid))
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert keys(cs) == []


def test_housekeeping_after_a_signal_matrix_leaves_nothing_behind(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A canceled run has nothing for recovery: no row recovered, no cleanup left, no file left."""
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    arm(monkeypatch, gw, "start", signal.SIGHUP)
    result = launch(capsys, gw)
    assert (result.code, result.out) == (129, "")
    monkeypatch.undo()
    monkeypatch.setattr(gw, "machine_time_zone", lambda env: "UTC")
    recovered = housekeep(gw, corpus)
    assert recovered.recovered == [] and recovered.cleaned == 0
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    state = state_dir_of(corpus.home)
    assert not any((state / "prompts").iterdir()) if (state / "prompts").exists() else True
    assert json.loads(json.dumps(row["state"])) == "canceled"
    assert tripwire_dir  # the tripwire fixture module is in use
