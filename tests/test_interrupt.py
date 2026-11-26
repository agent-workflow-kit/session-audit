"""The signal controller (``interrupt.Signals``) and its policy through ``cli.main`` (issue #28):
the first signal raises, later ones are ignored, nothing is set to ``SIG_IGN`` after it (so no
child inherits it), the deferred mode of ``retro run``, and the plan check's cleanup on SIGTERM."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from session_audit import cli, launchplan
from session_audit.command import Command, Context
from session_audit.gateway import SystemGateway
from session_audit.interrupt import Interrupted, Signals

from .test_cli import run

TERMINAL = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
MASK = (
    "import re\n"
    "text = open('/proc/self/status').read()\n"
    "print(int(re.search(r'SigIgn:\\s*([0-9a-f]+)', text).group(1), 16))\n"
)


def bits(*signals: signal.Signals) -> int:
    mask = 0
    for sig in signals:
        mask |= 1 << (int(sig) - 1)
    return mask


def command(fn: Any) -> Command:
    return Command(("w",), "x", lambda p: None, lambda c, a: None, fn)


# --- the policy through cli.main -----------------------------------------------------------------


def test_after_the_first_signal_nothing_is_sig_ign_and_a_child_starts_with_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The contract of ``cli._signals`` before #28 was ``SIG_IGN`` until ``main`` returns, which a
    child spawned during a command's cleanup inherited. The handler stays installed instead (it
    does nothing for a later signal) and the child's dispositions are the defaults."""
    seen: dict[str, Any] = {}

    def run_until_signaled(ctx: Context, args: argparse.Namespace) -> int:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            return 0
        finally:
            seen["dispositions"] = [signal.getsignal(s) for s in TERMINAL]
            os.kill(os.getpid(), signal.SIGINT)  # a second signal during the cleanup: ignored
            seen["child"] = SystemGateway().run([sys.executable, "-c", MASK], timeout=30)

    assert run(capsys, ["w"], commands=(command(run_until_signaled),)) == (143, "", "")
    assert all(callable(d) for d in seen["dispositions"])  # a handler, never SIG_IGN
    assert seen["child"].ok, seen["child"].stderr
    assert int(seen["child"].stdout) & bits(*TERMINAL) == 0


def test_a_child_started_while_a_signal_was_ignored_on_entry_gets_the_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as under nohup
    try:
        seen: list[Any] = []

        def look(ctx: Context, args: argparse.Namespace) -> int:
            seen.append(signal.getsignal(signal.SIGHUP))  # the tool itself keeps ignoring it
            seen.append(SystemGateway().run([sys.executable, "-c", MASK], timeout=30))
            return 0

        assert run(capsys, ["w"], commands=(command(look),))[0] == 0
        assert seen[0] is signal.SIG_IGN
        assert seen[1].ok and int(seen[1].stdout) & bits(signal.SIGHUP) == 0
    finally:
        signal.signal(signal.SIGHUP, old)


def test_the_second_signal_while_unwinding_is_ignored_in_raise_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    steps: list[str] = []

    def work(ctx: Context, args: argparse.Namespace) -> int:
        try:
            os.kill(os.getpid(), signal.SIGHUP)
            return 0
        finally:
            steps.append("cleanup start")
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
            steps.append("cleanup end")  # not cut short

    assert run(capsys, ["w"], commands=(command(work),)) == (129, "", "")
    assert steps == ["cleanup start", "cleanup end"]


def test_the_command_sees_the_controller_on_its_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    got: list[Any] = []

    def work(ctx: Context, args: argparse.Namespace) -> int:
        got.append(ctx.interrupts)
        return 0

    assert run(capsys, ["w"], commands=(command(work),))[0] == 0
    assert isinstance(got[0], Signals) and got[0].first is None and not got[0].pending


def test_a_deferring_command_gets_the_first_signal_as_a_flag_and_the_second_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deferred mode (``retro run`` from step 13): the first signal returns, the second raises
    with the code of the FIRST, a third is ignored."""
    seen: list[Any] = []

    def work(ctx: Context, args: argparse.Namespace) -> int:
        assert ctx.interrupts is not None
        with ctx.interrupts.defer():
            os.kill(os.getpid(), signal.SIGINT)
            seen.append((ctx.interrupts.pending, ctx.interrupts.first))
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                os.kill(os.getpid(), signal.SIGHUP)  # a third one while unwinding: ignored
                seen.append("unwinding")
        seen.append("not reached")
        return 0

    assert run(capsys, ["w"], commands=(command(work),)) == (130, "", "")
    assert seen == [(True, signal.SIGINT), "unwinding"]


def test_leaving_the_deferred_block_restores_the_raising_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def work(ctx: Context, args: argparse.Namespace) -> int:
        assert ctx.interrupts is not None
        with ctx.interrupts.defer():
            pass
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    assert run(capsys, ["w"], commands=(command(work),)) == (143, "", "")


# --- the controller on its own --------------------------------------------------------------------


def test_watch_events_are_set_by_the_first_signal_only_while_the_block_runs() -> None:
    signals = Signals()
    with signals.defer(), signals.watch() as early:
        signals.handle(signal.SIGINT)
        assert early.is_set() and signals.exit_code() == 130
        with signals.watch() as late:
            assert not late.is_set()  # whoever starts after the first signal is not cut short
    assert signals._events == []


def test_a_preset_watch_event_is_set_when_a_signal_is_already_pending() -> None:
    signals = Signals()
    with signals.defer():
        with signals.watch(preset=True) as before:
            assert not before.is_set()  # nothing happened yet
        signals.handle(signal.SIGINT)
        with signals.watch(preset=True) as after:
            assert after.is_set()  # a hook is never started after a signal


def test_a_bound_is_set_30_s_after_the_signal_or_its_own_start_whichever_is_later() -> None:
    now = [100.0]
    signals = Signals(clock=lambda: now[0])
    bound = signals.bound(30)
    assert not bound.is_set()
    with signals.defer():
        signals.handle(signal.SIGTERM)
    now[0] = 129.0
    assert not bound.is_set()
    now[0] = 130.0
    assert bound.is_set()
    later = signals.bound(30)  # made after the signal: counted from its own start
    now[0] = 159.0
    assert not later.is_set()
    now[0] = 160.0
    assert later.is_set()
    assert isinstance(bound, threading.Event)


def test_raise_if_pending_raises_for_the_first_signal() -> None:
    signals = Signals()
    signals.raise_if_pending()
    with signals.defer():
        signals.handle(signal.SIGHUP)
    with pytest.raises(Interrupted) as raised:
        signals.raise_if_pending()
    assert raised.value.signum == signal.SIGHUP


def test_install_restores_the_handlers_and_leaves_an_ignored_one_alone() -> None:
    before = {s: signal.getsignal(s) for s in (*TERMINAL, signal.SIGPIPE)}
    signals = Signals()
    with signals.install():
        assert all(signal.getsignal(s) == signals.handle for s in TERMINAL)
        assert signal.getsignal(signal.SIGPIPE) is signal.SIG_IGN
    assert {s: signal.getsignal(s) for s in before} == before


# --- the plan check's cleanup on SIGTERM ---------------------------------------------------------


def test_sigterm_during_the_plan_check_removes_its_placeholder_directory(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _private_environment: Path,
    tmp_path: Path,
) -> None:
    """The note of the #24 PR: SIGTERM used to skip the ``finally`` of ``plan_check``; it is an
    exception now, so the ``<state>/tmp/plan-*`` directory is removed."""
    from .fake_claude_session import FakeClaudeSession, plan_doc, step, version_step
    from .ingesthelp import Corpus, state_dir_of
    from .retrohelp import RetroGateway, ingested, seed

    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    corpus = Corpus(_private_environment)
    seed(corpus, "11111111-1111-4111-8111-111111111111")
    ingested(capsys, gateway)
    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gateway.fake = fake
    seen: list[list[str]] = []
    real_run = gateway.run

    def run_plan(argv: Any, **kwargs: Any) -> Any:
        if "--dry-run" in list(argv):
            tmp = state_dir_of(_private_environment) / "tmp"
            seen.append(sorted(p.name for p in tmp.iterdir() if p.name.startswith("plan-")))
            os.kill(os.getpid(), signal.SIGTERM)  # while the placeholder directory exists
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gateway, "run", run_plan)
    code = cli.main(
        ["retro", "run", "--json", "--project", "/work"], gateway=gateway, stdin_is_tty=False
    )
    capsys.readouterr()
    assert code == 143
    assert len(seen) == 1 and len(seen[0]) == 1  # the directory existed at the signal
    tmp = state_dir_of(_private_environment) / "tmp"
    leftovers = [p for p in tmp.iterdir() if p.name.startswith("plan-")] if tmp.exists() else []
    assert leftovers == []  # and is gone
    assert launchplan.plan_check  # the function under test exists
