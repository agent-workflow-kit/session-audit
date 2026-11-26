"""SIGHUP, SIGINT and SIGTERM at every step of ``retro run`` (issue #28).

The signal is injected into this very process (``os.kill`` of its own pid) at an exact point of the
flow: just before a named step runs. The CLI's handler (``interrupt.Signals``) is installed by
``cli.main`` around the whole run, so the test process is never left to a default action; nothing
but this process (and, for the hook and exec points, the children the tool itself started) is ever
signaled. The fake claude-session records every call, so "stopped the exact session" is read from
its log."""

from __future__ import annotations

import os
import signal
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from session_audit import launchplan, retrolaunch
from session_audit import retrostate as retrostate_module

from .fake_claude_session import FakeClaudeSession, exec_doc, step
from .ingesthelp import Corpus, state_dir_of
from .procs import wait_until_gone
from .retrohelp import RetroGateway
from .schemas import validate
from .signalhelp import (
    RealMonotonicGateway,
    corpus_fixture,
    cs_fixture,
    gw_fixture,
    hook_config,
    payloads,
    recorder,
    write_config,
)
from .test_retro_launch import CSID, keys, launch, log_entries, prepared_corpus, rows, session_run

SIGS = {signal.SIGHUP: 129, signal.SIGINT: 130, signal.SIGTERM: 143}


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    return gw_fixture(monkeypatch, real_clock=True)


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return corpus_fixture(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    return cs_fixture(gw, tmp_path)


def fire(sig: signal.Signals) -> None:
    os.kill(os.getpid(), sig)  # this process: its handler runs in the main thread


def firing(real: Callable[..., Any], sig: signal.Signals) -> Any:
    """``real`` preceded by the signal, the first time it is called only (a second signal would
    be the double signal of the tests below)."""
    used: list[bool] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not used:
            used.append(True)
            fire(sig)
        return real(*args, **kwargs)

    return wrapper


def write_lock_is_free(home: Path) -> bool:
    conn = sqlite3.connect(state_dir_of(home) / "state.db", timeout=0.5, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def called_ids(cs: FakeClaudeSession, *words: str) -> list[list[str]]:
    return [c["argv"] for c in cs.calls() if c["key"] in words]


# --- the matrix: one first signal at every step --------------------------------------------------

# point -> (state, reason, whether a session exists, retro_starts left, claude-session words)
# "start" and "record_session" stop the recorded session; "exec" forwards the signal to the child
# instead (the child ends at once: no stop); every later point leaves the mapped state and exits by
# the signal.
POINTS = [
    "plan_check",
    "insert",
    "pre_retro_hook",
    "mark_running",
    "write_files",
    "start",
    "record_session",
    "exec",
    "collect",
    "artifact_check",
    "final_write",
    "cleanup",
    "post_hooks",
]
EXPECT: dict[str, tuple[str, str | None, bool, int]] = {
    "insert": ("canceled", "interrupted", False, 0),
    "pre_retro_hook": ("canceled", "interrupted", False, 0),
    "mark_running": ("canceled", "interrupted", False, 0),
    "write_files": ("canceled", "interrupted", False, 0),
    "start": ("canceled", "interrupted", True, 0),
    "record_session": ("canceled", "interrupted", True, 0),
    "exec": ("canceled", "interrupted", True, 1),
    "collect": ("canceled", "interrupted", True, 1),
    "artifact_check": ("failed", "no_artifact", True, 1),
    "final_write": ("escalated", "session_blocked", True, 1),
    "cleanup": ("escalated", "session_blocked", True, 1),
    "post_hooks": ("escalated", "session_blocked", True, 1),
}


def arm(monkeypatch: pytest.MonkeyPatch, gw: RetroGateway, point: str, sig: signal.Signals) -> None:
    launcher = retrolaunch.Launcher
    if point == "plan_check":
        monkeypatch.setattr(launchplan, "plan_check", firing(launchplan.plan_check, sig))
    elif point == "insert":
        monkeypatch.setattr(retrolaunch, "insert_retro", firing(retrolaunch.insert_retro, sig))
    elif point == "pre_retro_hook":
        monkeypatch.setattr(
            retrolaunch.hooks, "run_hooks", firing(retrolaunch.hooks.run_hooks, sig)
        )
    elif point == "mark_running":
        monkeypatch.setattr(launcher, "_mark_running", firing(launcher._mark_running, sig))
    elif point == "write_files":
        monkeypatch.setattr(
            retrolaunch.retrofiles,
            "write_run_files",
            firing(retrolaunch.retrofiles.write_run_files, sig),
        )
    elif point == "start":
        real_run = gw.run

        def run(argv: Any, **kwargs: Any) -> Any:
            if list(argv)[1:2] == ["start"] and "--dry-run" not in argv:  # not the plan check
                fire(sig)
            return real_run(argv, **kwargs)

        monkeypatch.setattr(gw, "run", run)
    elif point == "record_session":
        monkeypatch.setattr(launcher, "_record", firing(launcher._record, sig))
    elif point == "exec":
        real_spawn = gw.spawn

        def spawn(argv: Any, **kwargs: Any) -> Any:
            handle = real_spawn(argv, **kwargs)
            fire(sig)  # the child runs: the first look of the wait loop forwards the signal
            return handle

        monkeypatch.setattr(gw, "spawn", spawn)
    elif point == "collect":
        monkeypatch.setattr(launcher, "_collect", firing(launcher._collect, sig))
    elif point == "artifact_check":
        monkeypatch.setattr(
            retrolaunch,
            "validate_and_store_artifact",
            firing(retrolaunch.validate_and_store_artifact, sig),
        )
    elif point == "final_write":
        monkeypatch.setattr(launcher, "_end", firing(launcher._end, sig))
    elif point == "cleanup":
        monkeypatch.setattr(retrolaunch, "cleanup", firing(retrolaunch.cleanup, sig))
    elif point == "post_hooks":
        monkeypatch.setattr(
            retrostate_module.Retro, "post_hooks", firing(retrostate_module.Retro.post_hooks, sig)
        )
    else:  # pragma: no cover
        raise AssertionError(point)


def script_for(cs: FakeClaudeSession, point: str, tmp_path: Path) -> Path:
    """The scripted run for a point; returns the exec child's pid file."""
    pid_file = tmp_path / "exec.pid"
    if point == "artifact_check":
        session_run(cs, "done")
    else:
        session_run(cs, "blocked")
    if point == "exec":
        cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=30, pid_file=pid_file))
    return pid_file


MATRIX = [(point, signal.SIGINT) for point in POINTS] + [
    (point, sig)
    for point in ("pre_retro_hook", "start", "exec", "collect", "cleanup")
    for sig in (signal.SIGTERM, signal.SIGHUP)
]


@pytest.mark.parametrize(("point", "sig"), MATRIX, ids=[f"{p}-{s.name}" for p, s in MATRIX])
def test_a_signal_at_every_step_ends_in_the_documented_state(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    point: str,
    sig: signal.Signals,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    pid_file = script_for(cs, point, tmp_path)
    pre, post = tmp_path / "pre", tmp_path / "post"
    write_config(
        corpus.home,
        hook_config(pre_retro=recorder(pre), post_retro=recorder(post)),
    )
    arm(monkeypatch, gw, point, sig)
    started = time.monotonic()
    result = launch(capsys, gw)
    assert (result.code, result.out) == (SIGS[sig], "")  # exit by signal, no envelope
    assert (
        time.monotonic() - started < 20
    )  # the exec child (a 30 s sleep) was signaled, not waited for
    assert write_lock_is_free(corpus.home)
    if point == "plan_check":
        assert rows(corpus.home) == [] and rows(corpus.home, "retro_starts") == []
        assert payloads(pre) == [] and payloads(post) == [] and keys(cs) == []
        return
    [row] = rows(corpus.home)
    state, reason, has_session, starts = EXPECT[point]
    assert (row["state"], row["reason"]) == (state, reason)
    assert row["lease_pid"] is None and row["finished_at"] is not None
    assert len(rows(corpus.home, "retro_starts")) == starts
    sessions = rows(corpus.home, "retro_sessions")
    if has_session:
        [session] = sessions
        assert session["csession_id"] == CSID and session["cleanup_state"] == "done"
        # the exact session, and no other, is the target of every stop and forget
        for argv in called_ids(cs, "stop", "forget"):
            assert CSID in argv and not any(a.startswith("cs-") and a != CSID for a in argv)
    else:
        assert sessions == [] and keys(cs) == []
    if point in ("start", "record_session"):
        assert len(called_ids(cs, "stop")) == 1  # a session started: stopped by its recorded id
    if point == "exec":
        child = int(pid_file.read_text())
        assert wait_until_gone(child)  # the forwarded signal ended the exec child
        assert called_ids(cs, "stop") == []  # it ended in time: no stop needed
        assert "status" in keys(cs)  # collected
    if point in ("insert", "pre_retro_hook", "mark_running", "write_files"):
        assert "start" not in keys(cs)
    # the retro's own files are gone
    state_dir = state_dir_of(corpus.home)
    assert not (state_dir / "prompts" / row["id"]).exists()
    # post-retro fires once for a retro that reached its final state here (a signal during the
    # hooks themselves may cut them: not asserted for that point)
    if point != "post_hooks":
        [seen] = payloads(post)
        validate(seen, "session-audit/hooks/post-retro.json")
        assert (seen["state"], seen["reason"]) == (state, reason)
    states = [e["state"] for e in log_entries(corpus.home)]
    assert states[-1] == state
    cs.assert_outputs_valid()


# --- a hook ended by the signal, a stuck exec child, a stop that hangs ---------------------------


def test_a_signal_during_the_pre_retro_hook_ends_its_process_group(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    pids = tmp_path / "hook.pids"
    post = tmp_path / "post"
    command = f"echo $$ > {pids}; sleep 60 & echo $! >> {pids}; kill -s TERM {os.getpid()}; wait"
    write_config(corpus.home, hook_config(pre_retro=command, post_retro=recorder(post)))
    started = time.monotonic()
    result = launch(capsys, gw)
    assert (result.code, result.out) == (143, "")
    assert time.monotonic() - started < 30
    for pid in pids.read_text().split():
        assert wait_until_gone(int(pid))  # the shell and its background child are both gone
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert keys(cs) == []  # no start
    [seen] = payloads(post)
    assert (seen["state"], seen["reason"]) == ("canceled", "interrupted")
    assert not (state_dir_of(corpus.home) / "digests" / row["id"]).exists()


def test_a_stuck_exec_child_gets_the_signal_once_then_stop_then_its_group_ended(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert isinstance(gw, RealMonotonicGateway)
    monkeypatch.setattr(retrolaunch, "FORWARD_WAIT", 0.6)
    monkeypatch.setattr(retrolaunch, "STOP_WAIT", 0.6)
    monkeypatch.setattr(retrolaunch, "END_GRACE", 0.6)
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    pid_file = tmp_path / "exec.pid"
    # the child ignores SIGTERM: the forwarded signal does nothing, the stop does nothing (the fake
    # stop only answers), so the group is ended after the waits
    cs.script(
        "exec",
        step(exec_doc(CSID, "blocked"), exit=13, sleep=60, pid_file=pid_file, ignore_term=True),
    )
    sent: list[int] = []
    real_spawn = gw.spawn

    def spawn(argv: Any, **kwargs: Any) -> Any:
        handle = real_spawn(argv, **kwargs)
        real_signal = handle.signal

        def counting(signum: int) -> bool:
            sent.append(signum)
            return bool(real_signal(signum))

        handle.signal = counting  # type: ignore[method-assign]
        fire(signal.SIGTERM)
        return handle

    monkeypatch.setattr(gw, "spawn", spawn)
    started = time.monotonic()
    result = launch(capsys, gw)
    assert (result.code, result.out) == (143, "")
    assert time.monotonic() - started < 40
    assert wait_until_gone(int(pid_file.read_text()))
    assert sent == [signal.SIGTERM]  # forwarded once; the stop and the group end did the rest
    assert len(called_ids(cs, "stop")) == 1
    first_stop = [c for c in cs.calls() if c["key"] == "stop"][0]
    assert CSID in first_stop["argv"]
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert rows(corpus.home, "retro_sessions")[0]["cleanup_state"] == "done"


def test_the_signal_is_forwarded_to_the_exec_child_once(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    # the child ignores the forwarded SIGTERM and ends by itself after two seconds
    cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=2, ignore_term=True))
    monkeypatch.setattr(retrolaunch, "FORWARD_WAIT", 600.0)
    sent: list[int] = []
    real_spawn = gw.spawn

    def spawn(argv: Any, **kwargs: Any) -> Any:
        handle = real_spawn(argv, **kwargs)
        real_signal = handle.signal

        def counting(signum: int) -> bool:
            sent.append(signum)
            return bool(real_signal(signum))

        handle.signal = counting  # type: ignore[method-assign]
        fire(signal.SIGTERM)
        return handle

    monkeypatch.setattr(gw, "spawn", spawn)
    result = launch(capsys, gw)
    assert (result.code, result.out) == (143, "")
    assert sent == [signal.SIGTERM]  # once, though the wait looped for two seconds
    assert called_ids(cs, "stop") == []  # FORWARD_WAIT had not passed
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")


def test_a_stop_that_hangs_is_bounded_by_its_call_timeout(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit import retrocleanup

    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script("stop", step("{}", sleep=30, unvalidated=True))
    monkeypatch.setattr(retrocleanup, "CALL_TIMEOUT", 1.0)
    arm(monkeypatch, gw, "start", signal.SIGINT)
    started = time.monotonic()
    result = launch(capsys, gw)
    assert (result.code, result.out) == (130, "")
    assert time.monotonic() - started < 40
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")


# --- the cleanup bound ---------------------------------------------------------------------------


def test_a_signal_during_cleanup_ends_a_call_in_flight_after_the_bound(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    cs.script("forget", step("{}", sleep=40, unvalidated=True))  # a forget that hangs
    monkeypatch.setattr(retrolaunch, "CLEANUP_BOUND", 0.8)
    arm(monkeypatch, gw, "cleanup", signal.SIGINT)
    started = time.monotonic()
    result = launch(capsys, gw)
    assert (result.code, result.out) == (130, "")
    assert time.monotonic() - started < 30  # not the 60 s call timeout, not the 40 s sleep
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("escalated", "session_blocked")
    [session] = rows(corpus.home, "retro_sessions")
    assert session["cleanup_state"] == "pending"  # housekeeping finishes it


# --- the database is never touched from the handler ----------------------------------------------


def test_a_signal_inside_a_transaction_does_not_break_it(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal arrives while step 15's transaction is open: the handler only records it, the
    commit goes through, and the retro ends canceled at the next safe point."""
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    real = retrostate_module.Retro.write
    fired: list[bool] = []

    def write(self: Any, state: str, *args: Any, **kwargs: Any) -> None:
        also = kwargs.get("also")
        if state == "running" and also is not None and not fired:
            fired.append(True)

            def with_signal(conn: Any) -> None:
                also(conn)
                fire(signal.SIGINT)  # inside the open transaction

            kwargs["also"] = with_signal
        real(self, state, *args, **kwargs)

    monkeypatch.setattr(retrostate_module.Retro, "write", write)
    result = launch(capsys, gw)
    assert (result.code, result.out) == (130, "")
    [row] = rows(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "interrupted")
    assert rows(corpus.home, "retro_starts") == []  # canceled before exec: refunded
    assert write_lock_is_free(corpus.home)
    assert keys(cs) == []  # the start was never called
