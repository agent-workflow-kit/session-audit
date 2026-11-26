"""The crash safety matrix (issue #27): an exception injected at EVERY step of ``retro run``
(steps 13 to 16 of the Order of checks and every step of ``cleanup``), the holder's lease left to
run out as it does when the process dies, then housekeeping. Each ends in the documented state,
repeats are idempotent and three real processes recovering the same retro agree on one winner."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import retrocleanup, retrolaunch, retrorecovery, retrostate
from session_audit.config import HookSettings

from .conftest import FAKEBIN, tripwire_dir
from .fake_claude_session import FakeClaudeSession, plan_doc, step, version_step
from .ingesthelp import Corpus, state_dir_of
from .recoveryhelp import (
    CSID,
    expire_lease,
    housekeep,
    keys,
    plant_transcripts,
    retro_rows,
    script_cleanup,
    script_status,
    session_rows,
)
from .retrohelp import RetroGateway
from .test_retro_launch import launch, prepared_corpus, session_run, write_config
from .test_retro_launch_flow import hook_config, payloads, recorder


class Boom(Exception):
    """The injected crash."""


def boom(*args: Any, **kwargs: Any) -> Any:
    raise Boom


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


def hooks_for(tmp_path: Path) -> tuple[HookSettings, Path, Path]:
    retro_log, attention_log = tmp_path / "post-retro.log", tmp_path / "post-attention.log"
    settings = HookSettings.model_validate(
        {
            "post-retro": [{"cmd": recorder(retro_log)}],
            "post-attention": [{"cmd": recorder(attention_log)}],
        }
    )
    return settings, retro_log, attention_log


# Where the injected crash is: (id, what is patched, the retro's state afterwards, whether a
# retro_sessions row exists, whether the retro is already final). The patched names are the steps
# of the order: retrolaunch.Launcher methods and the module functions they call.
POINTS: list[tuple[str, str, bool, bool]] = [
    ("pre_retro_hook", "digesting", False, False),
    ("mark_running", "digesting", False, False),
    ("write_files", "running", False, False),
    ("start_called", "running", False, False),
    ("record_session", "running", False, False),
    ("exec", "running", True, False),
    ("collect", "running", True, False),
    ("artifact_check", "validating", True, False),
    ("final_write", "validating", True, False),
    ("cleanup", "failed", True, True),
    ("post_hooks", "failed", True, True),
]


def inject(monkeypatch: pytest.MonkeyPatch, point: str) -> None:
    launcher = retrolaunch.Launcher
    if point == "pre_retro_hook":
        monkeypatch.setattr(retrolaunch.hooks, "run_hooks", boom)
    elif point == "mark_running":
        monkeypatch.setattr(launcher, "_mark_running", boom)
    elif point == "write_files":
        monkeypatch.setattr(retrolaunch.retrofiles, "write_run_files", boom)
    elif point == "start_called":
        monkeypatch.setattr(launcher, "_after_start", boom)
    elif point == "record_session":
        monkeypatch.setattr(launcher, "_record", boom)
    elif point == "exec":
        monkeypatch.setattr(launcher, "_exec", boom)
    elif point == "collect":
        monkeypatch.setattr(launcher, "_collect", boom)
    elif point == "artifact_check":
        monkeypatch.setattr(retrolaunch, "validate_and_store_artifact", boom)
    elif point == "final_write":
        monkeypatch.setattr(launcher, "_end", boom)
    elif point == "cleanup":
        monkeypatch.setattr(retrolaunch, "cleanup", boom)
    elif point == "post_hooks":
        monkeypatch.setattr(retrostate.Retro, "post_hooks", boom)
    else:  # pragma: no cover
        raise AssertionError(point)


@pytest.mark.parametrize(
    ("point", "state", "has_session", "final"), POINTS, ids=[p[0] for p in POINTS]
)
def test_a_crash_at_every_step_of_the_run_then_recovery_ends_in_the_documented_state(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    point: str,
    state: str,
    has_session: bool,
    final: bool,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done")
    settings, retro_log, attention_log = hooks_for(tmp_path)
    write_config(
        corpus.home,
        hook_config(
            tmp_path, post_retro=recorder(retro_log), post_attention=recorder(attention_log)
        ),
    )
    inject(monkeypatch, point)
    result = launch(capsys, gw)
    assert result.code != 0
    monkeypatch.undo()  # the next process does not crash
    monkeypatch.setattr(gw, "machine_time_zone", lambda env: "UTC")
    [row] = retro_rows(corpus.home).values()
    assert row["state"] == state
    retro_id = row["id"]
    sessions = session_rows(corpus.home)
    assert (CSID in sessions) is has_session
    fired_before = len(payloads(retro_log))
    need_forget = has_session and (
        sessions[CSID]["cleanup_state"] in ("pending", "incomplete")
        and sessions[CSID]["forgotten_at"] is None
    )
    if has_session and sessions[CSID]["cleanup_state"] == "pending":
        plant_transcripts(corpus, sessions[CSID]["project_dir"])
    expire_lease(corpus.home)  # the holder is dead: its lease runs out
    script_status(cs, retro_id, CSID, live=point == "exec")
    script_cleanup(cs)
    before = len(cs.calls())
    recovered = housekeep(gw, corpus, hooks=settings)
    after = retro_rows(corpus.home)[retro_id]
    assert after["lease_pid"] is None
    if final:
        assert recovered.recovered == [] and after["state"] == "failed"
        assert after["reason"] != "runner_lost"  # a final retro is never recovered again
    else:
        assert recovered.recovered == [retro_id]
        assert (after["state"], after["reason"]) == ("failed", "runner_lost")
    if has_session:
        assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"
        assert session_rows(corpus.home)[CSID]["forgotten_at"] is not None
        assert not (corpus.root / sessions[CSID]["project_dir"]).exists()
        assert (corpus.root / "p1").exists()  # the audited sessions' own transcripts stay
    state_dir = state_dir_of(corpus.home)
    assert not (state_dir / "prompts" / retro_id).exists()
    assert not (state_dir / "outcomes" / f"{retro_id}.json").exists() or final is False
    # post-retro: once for a retro that reached its final state in this run or in recovery, never
    # twice, and a final retro whose process died before its hooks is not fired for (no outbox)
    fired = len(payloads(retro_log))
    assert fired - fired_before == (0 if final else 1)
    assert fired <= 1
    # the second housekeeping changes nothing and asks claude-session nothing
    calls = len(cs.calls())
    again = housekeep(gw, corpus, hooks=settings)
    assert again.recovered == [] and again.cleaned == 0 and not again.deferred
    assert len(cs.calls()) == calls
    assert len(payloads(retro_log)) == fired
    forgets = [c for c in cs.calls()[before:] if c["key"] == "forget"]
    assert len(forgets) == (1 if need_forget else 0)  # exactly the one the row still needed
    cs.assert_outputs_valid()


# --- every step of cleanup ---------------------------------------------------------------------

CLEANUP_STEPS = [
    ("forget", retrocleanup, "forget_session"),
    ("purge_check", retrocleanup, "lexists"),
    ("transcripts", retrocleanup, "remove_run_transcripts"),
    ("delete_local", retrocleanup.retrofiles, "delete_local"),
    ("state", retrocleanup, "set_cleanup_state"),
]


@pytest.mark.parametrize(
    ("name", "module", "attribute"), CLEANUP_STEPS, ids=[c[0] for c in CLEANUP_STEPS]
)
def test_a_crash_at_every_step_of_cleanup_is_finished_by_the_next_housekeeping(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    module: Any,
    attribute: str,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    monkeypatch.setattr(module, attribute, boom)
    result = launch(capsys, gw)
    assert result.code != 0
    monkeypatch.undo()
    monkeypatch.setattr(gw, "machine_time_zone", lambda env: "UTC")
    [row] = retro_rows(corpus.home).values()
    assert row["state"] == "escalated"  # the retro's own outcome was written before the cleanup
    sessions = session_rows(corpus.home)
    assert sessions[CSID]["cleanup_state"] == "pending"
    plant_transcripts(corpus, sessions[CSID]["project_dir"])
    expire_lease(corpus.home)
    script_status(cs, row["id"], CSID)
    script_cleanup(cs)
    for _ in range(2):  # repeated until done, then nothing more
        housekeep(gw, corpus)
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"
    assert not (corpus.root / sessions[CSID]["project_dir"]).exists()
    assert retro_rows(corpus.home)[row["id"]]["state"] == "escalated"  # untouched
    forgets = [c for c in cs.calls() if c["key"] == "forget"]
    assert len(forgets) <= 2  # at most the crashed run's own and one retry


def test_cleanup_repeated_until_done_is_idempotent(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession
) -> None:
    from .recoveryhelp import plant_session

    plant_session(corpus.home, state="incomplete", forgotten_at=3)
    plant_transcripts(corpus)
    for _ in range(4):
        housekeep(gw, corpus)
    row = session_rows(corpus.home)[CSID]
    assert row["cleanup_state"] == "done" and row["forgotten_at"] == 3
    assert keys(cs) == []


# --- processes ---------------------------------------------------------------------------------


def test_three_processes_recovering_the_same_retro_agree_on_one_winner(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from .recoveryhelp import orphan, plant_session

    plant_session(corpus.home)
    orphan(corpus.home, csid=CSID, created=int(time.time() * 1000))  # real processes: real time
    plant_transcripts(corpus)
    script_status(cs)
    cs.script("status", step(_status_document(), sleep=1.5))
    script_cleanup(cs)
    retro_log = tmp_path / "post-retro.log"
    write_config(
        corpus.home,
        f"harness:\n  claude_session_cmd: {FAKEBIN / 'claude-session'}\n"
        + hook_config(tmp_path, post_retro=recorder(retro_log)),
    )
    env = {**cs.env(), "PATH": f"{tripwire_dir(tmp_path_factory)}{os.pathsep}{os.environ['PATH']}"}
    command = [sys.executable, "-m", "session_audit", "prune", "--json", "--actor", "auto:test"]
    options: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "process_group": 0,
        "stdin": subprocess.DEVNULL,
    }
    processes: list[subprocess.Popen[str]] = []
    try:
        for _ in range(3):
            processes.append(subprocess.Popen(command, **options))
        outputs = [p.communicate(timeout=120) for p in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()
    assert [p.returncode for p in processes] == [0, 0, 0], outputs
    listed = [json.loads(out)["recoveries"] for out, _err in outputs]
    assert sorted(sum(listed, [])) == [RETRO_ID_OF(corpus)]  # exactly one process recovered it
    assert len(payloads(retro_log)) == 1  # post-retro once
    row = retro_rows(corpus.home)[RETRO_ID_OF(corpus)]
    assert (row["state"], row["reason"], row["lease_pid"]) == ("failed", "runner_lost", None)
    # prune removes a done row whose project directory is gone (retention), so done or absent
    assert session_rows(corpus.home).get(CSID, {"cleanup_state": "done"})["cleanup_state"] == "done"
    forgets = [c for c in cs.calls() if c["key"] == "forget"]
    assert len(forgets) <= 3 and all(c["argv"][1] == CSID for c in forgets)


def RETRO_ID_OF(corpus: Corpus) -> str:  # noqa: N802
    return next(iter(retro_rows(corpus.home)))


def _status_document() -> dict[str, Any]:
    from .fake_claude_session import status_doc
    from .retrohelp import RETRO

    return status_doc(
        CSID, outcome="done", labels=["owner=session-audit", f"retro={RETRO}"], sandbox_active=True
    )


# the names the matrix patches exist (a rename would otherwise turn a crash point into a no-op)
def test_the_patched_names_exist() -> None:
    for module, attribute in [(m, a) for _n, m, a in CLEANUP_STEPS]:
        assert callable(getattr(module, attribute))
    for name in ("_mark_running", "_after_start", "_record", "_exec", "_collect", "_end"):
        assert callable(getattr(retrolaunch.Launcher, name))
    assert callable(retrorecovery.run_housekeeping)


# --- a hard crash (os._exit) -------------------------------------------------------------------


def _hard_crash_points() -> dict[str, Any]:
    """Where a housekeeping child dies without any cleanup (no ``finally``, no ``atexit``)."""

    def die(*_args: Any, **_kwargs: Any) -> Any:
        os._exit(137)

    def before_write(real: Any) -> Any:
        return die

    def after_write(real: Any) -> Any:
        def wrapper(self: Any, state: str, *args: Any, **kwargs: Any) -> Any:
            real(self, state, *args, **kwargs)
            if state == "failed":
                os._exit(137)

        return wrapper

    def after_call(key: str) -> Any:
        def make(real: Any) -> Any:
            def wrapper(ctx: Any, *argv: str, **kwargs: Any) -> Any:
                result = real(ctx, *argv, **kwargs)
                if argv and argv[0] == key:
                    os._exit(137)
                return result

            return wrapper

        return make

    return {
        "sql_write_after_takeover": (retrostate.Retro, "write", before_write),
        "after_the_final_state_commit": (retrostate.Retro, "write", after_write),
        "after_a_status_call": (retrorecovery, "call", after_call("status")),
        "after_a_forget_call": (retrocleanup, "call", after_call("forget")),
        "mid_cleanup": (retrocleanup, "set_cleanup_state", before_write),
    }


HARD_CRASHES = _hard_crash_points()


def reap_within(pid: int, seconds: float) -> int:
    """Reap the forked child within a bound; on timeout kill that recorded pid only, then fail."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    pytest.fail(f"the housekeeping child did not exit within {seconds:.0f} seconds")


@pytest.mark.parametrize("point", list(HARD_CRASHES))
def test_a_housekeeping_killed_hard_is_converged_by_the_next_one(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, tmp_path: Path, point: str
) -> None:
    from .recoveryhelp import hook_settings, orphan, plant_session
    from .retrohelp import RETRO

    plant_session(corpus.home)
    orphan(corpus.home, csid=CSID)
    plant_transcripts(corpus)
    script_status(cs, live=True)
    script_cleanup(cs)
    settings, retro_log, _attention = hook_settings(tmp_path)
    target, name, wrap = HARD_CRASHES[point]
    pid = os.fork()
    if pid == 0:  # the child: patch, run, and never return into pytest
        try:
            setattr(target, name, wrap(getattr(target, name)))
            housekeep(gw, corpus, hooks=settings)
        finally:
            os._exit(0)
    status = reap_within(pid, 60.0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 137, status
    expire_lease(corpus.home)
    housekeep(gw, corpus, hooks=settings)
    row = retro_rows(corpus.home)[RETRO]
    assert (row["state"], row["reason"], row["lease_pid"]) == ("failed", "runner_lost", None)
    session = session_rows(corpus.home)[CSID]
    assert (session["cleanup_state"], session["forgotten_at"] is not None) == ("done", True)
    foreign = [c for c in cs.calls() if c["key"] in ("stop", "forget") and c["argv"][1] != CSID]
    assert foreign == []
    assert len(payloads(retro_log)) <= 1  # post-retro at most once
    again = housekeep(gw, corpus, hooks=settings)
    assert again.recovered == [] and again.cleaned == 0
