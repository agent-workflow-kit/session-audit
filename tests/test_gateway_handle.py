"""``Gateway.spawn`` and its handle, and the signal dispositions of every child (issue #28)."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from session_audit.gateway import CallStatus, SystemGateway

from .procs import gone, wait_until_gone

TERMINAL = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
STATUS = "/proc/self/status"
DISPOSITIONS = (
    "import re\n"
    f"text = open({STATUS!r}).read()\n"
    "ignored = int(re.search(r'SigIgn:\\s*([0-9a-f]+)', text).group(1), 16)\n"
    "print(ignored)\n"
)


def gateway() -> SystemGateway:
    return SystemGateway(kill_grace=1.0)


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture
def ignoring() -> Iterator[None]:
    """The three terminal signals and SIGPIPE ignored in this process, as under ``nohup``."""
    saved = {s: signal.signal(s, signal.SIG_IGN) for s in (*TERMINAL, signal.SIGPIPE)}
    try:
        yield
    finally:
        for sig, old in saved.items():
            signal.signal(sig, old)


# --- the handle -----------------------------------------------------------------------------------


def test_a_finished_child_has_its_exit_and_output() -> None:
    with gateway().spawn(
        python("import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"), timeout=30
    ) as h:
        assert h.pid is not None
        assert h.wait(30) is True
        result = h.result()
    assert (result.status, result.returncode) == (CallStatus.EXITED, 3)
    assert (result.stdout, result.stderr) == ("out\n", "err")


def test_wait_returns_false_while_the_child_runs_and_true_after_a_signal(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    code = f"import time; open({str(ready)!r}, 'w').close(); time.sleep(60)"
    with gateway().spawn(python(code), timeout=60) as h:
        assert h.wait(0.2) is False
        pid = h.pid
        assert pid is not None
        end = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.01)  # the interpreter is up: SIGTERM then ends it by default action
        assert h.signal(signal.SIGTERM) is True
        assert h.wait(30) is True
        result = h.result()
    assert (result.status, result.signal) == (CallStatus.SIGNALED, signal.SIGTERM)
    assert wait_until_gone(pid)


def test_signal_refuses_once_the_child_is_reaped() -> None:
    with gateway().spawn(python("pass"), timeout=30) as h:
        assert h.wait(30) is True
        assert h.signal(signal.SIGTERM) is False  # reaped: the pid is not ours any more
        assert h.signal(0) is False
        assert h.result().ok


def test_signal_goes_to_the_child_alone_not_its_group(tmp_path: Path) -> None:
    grandchild = tmp_path / "grandchild.pid"
    code = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(grandchild)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    with gateway().spawn(python(code), timeout=60) as h:
        end = time.monotonic() + 30
        while not grandchild.exists() and time.monotonic() < end:
            h.wait(0.05)
        member = int(grandchild.read_text())
        time.sleep(0.2)  # the leader is in its sleep: SIGTERM ends it
        assert h.signal(signal.SIGTERM) is True
        assert h.wait(30) is True
        assert not gone(member)  # the group member was not signaled
        h.end_group(1.0)  # the leader is reaped, a member lives: its group is ended
        assert wait_until_gone(member)


def test_end_group_after_everything_is_gone_signals_nothing() -> None:
    with gateway().spawn(python("pass"), timeout=30) as h:
        assert h.wait(30) is True
        h.end_group(0.5)  # no member left: returns without touching the (reusable) group id


def test_the_deadline_ends_the_group_and_reports_timeout() -> None:
    started = time.monotonic()
    with gateway().spawn(python("import time; time.sleep(60)"), timeout=0.3) as h:
        pid = h.pid
        assert h.wait(30) is True
        result = h.result()
    assert result.status is CallStatus.TIMEOUT
    assert time.monotonic() - started < 20
    assert pid is not None and wait_until_gone(pid)


def test_output_past_the_cap_ends_the_call() -> None:
    with gateway().spawn(
        python("import sys\nwhile True:\n    sys.stdout.write('x' * 65536)"),
        timeout=60,
        stdout_limit=1000,
    ) as h:
        pid = h.pid
        assert h.wait(30) is True
        assert h.result().status is CallStatus.OUTPUT_LIMIT
    assert pid is not None and wait_until_gone(pid)


def test_the_pipes_are_served_while_waiting_so_a_chatty_child_finishes() -> None:
    code = "import sys; sys.stdout.write('y' * 400000); sys.stderr.write('z' * 300000)"
    with gateway().spawn(python(code), timeout=60, stdout_limit=1024 * 1024) as h:
        assert h.wait(30) is True
        result = h.result()
    assert result.ok and len(result.stdout) == 400000 and len(result.stderr) == 300000


def test_stdin_reaches_the_child() -> None:
    with gateway().spawn(
        python("import sys; print(sys.stdin.read().upper())"), timeout=30, stdin="abc" * 100000
    ) as h:
        assert h.wait(30) is True
        assert h.result().stdout.strip() == "ABC" * 100000


def test_close_ends_a_running_child_and_its_group(tmp_path: Path) -> None:
    h = gateway().spawn(python("import time; time.sleep(60)"), timeout=60)
    pid = h.pid
    assert pid is not None and h.wait(0.1) is False
    h.close()
    assert wait_until_gone(pid)
    h.close()  # idempotent
    assert h.result().status is CallStatus.TIMEOUT


def test_abandon_leaves_the_child_alone() -> None:
    h = gateway().spawn(python("import time; time.sleep(60)"), timeout=60)
    pid = h.pid
    assert pid is not None
    try:
        h.abandon()
        assert not gone(pid)  # still ours to end: the test does it by the recorded pid
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_a_program_that_cannot_start_is_a_finished_handle() -> None:
    h = gateway().spawn(["/nonexistent/program-for-tests"], timeout=5)
    assert h.pid is None and h.wait(0) is True and h.signal(signal.SIGTERM) is False
    assert h.result().status is CallStatus.SPAWN_FAILED
    h.close()


def test_run_accepts_a_cancel_event() -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        result = gateway().run(python("import time; time.sleep(60)"), timeout=60, cancel=cancel)
    finally:
        timer.cancel()
    assert result.status is CallStatus.TIMEOUT


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_spawn_checks_its_arguments_like_run(bad: float) -> None:
    with pytest.raises(ValueError):
        gateway().spawn(["true"], timeout=30, kill_grace=bad)
    with pytest.raises(ValueError):
        gateway().spawn(["true"], timeout=bad)
    with pytest.raises(ValueError):
        gateway().spawn(["true\0"], timeout=30)


# --- dispositions ---------------------------------------------------------------------------------


def bits(*signals: signal.Signals) -> int:
    mask = 0
    for sig in signals:
        mask |= 1 << (int(sig) - 1)
    return mask


@pytest.mark.usefixtures("ignoring")
def test_a_child_does_not_inherit_ignored_terminal_signals_from_run() -> None:
    result = gateway().run(python(DISPOSITIONS), timeout=30)
    assert result.ok, result.stderr
    assert int(result.stdout) & bits(*TERMINAL) == 0


@pytest.mark.usefixtures("ignoring")
def test_a_child_gets_sigpipe_back_too() -> None:
    """The interpreter of a Python child ignores SIGPIPE itself, so a shell reads the mask."""
    result = gateway().run(["/bin/sh", "-c", f"grep SigIgn {STATUS}"], timeout=30)
    mask = int(result.stdout.split()[1], 16)
    assert mask & bits(*TERMINAL, signal.SIGPIPE) == 0


@pytest.mark.usefixtures("ignoring")
def test_a_child_does_not_inherit_ignored_terminal_signals_from_spawn() -> None:
    with gateway().spawn(python(DISPOSITIONS), timeout=30) as h:
        assert h.wait(30)
        result = h.result()
    assert result.ok, result.stderr
    assert int(result.stdout) & bits(*TERMINAL) == 0


@pytest.mark.usefixtures("ignoring")
def test_a_hook_does_not_inherit_ignored_terminal_signals(tmp_path: Path) -> None:
    script = tmp_path / "dispositions.py"
    script.write_text(DISPOSITIONS, encoding="utf-8")
    result = gateway().run_hook(f"{sys.executable} {script}", timeout=30, stdin="", env_extra={})
    assert result.ok, result.stderr
    assert int(result.stdout) & bits(*TERMINAL) == 0


@pytest.mark.usefixtures("ignoring")
def test_the_control_a_plain_popen_would_inherit_them() -> None:
    import subprocess

    out = subprocess.run(  # noqa: S603 - the test's own interpreter
        python(DISPOSITIONS), capture_output=True, text=True, check=True
    ).stdout
    assert int(out) & bits(*TERMINAL) == bits(*TERMINAL)  # the test can fail


def test_without_an_ignored_signal_no_preexec_function_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit import gateway as module

    assert module._spawn_preexec() is None or any(
        signal.getsignal(s) is signal.SIG_IGN for s in TERMINAL
    )
    saved = {s: signal.signal(s, signal.SIG_DFL) for s in TERMINAL}
    try:
        assert module._spawn_preexec() is None
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        assert module._spawn_preexec() is module._reset_terminal_signals
    finally:
        for sig, old in saved.items():
            signal.signal(sig, old)


def test_an_output_over_the_cap_that_arrives_with_the_exit_is_an_output_limit() -> None:
    with gateway().spawn(python("print('x' * 200)"), timeout=30, stdout_limit=50) as h:
        popen = h._popen  # type: ignore[attr-defined]
        assert popen is not None
        popen.wait(30)  # the child is gone with its output still unread
        selector = h._selector  # type: ignore[attr-defined]
        selector.select = lambda step: []  # the loop never drains: only the final drain reads
        assert h.wait(30) is True
        result = h.result()
    assert result.status is CallStatus.OUTPUT_LIMIT
