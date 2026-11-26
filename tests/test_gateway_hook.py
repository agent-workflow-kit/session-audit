"""``Gateway.run_hook``: ``/bin/sh -c`` in its own process group, the whole group ended after the
leader, stdout read to a cap and the rest discarded, the payload on stdin, extra environment."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from session_audit.gateway import HOOK_STDOUT_LIMIT, CallStatus, SystemGateway

from .procs import gone, wait_until_gone

PY = sys.executable
ENV = {"SESSION_AUDIT_HOOK": "post-retro", "SESSION_AUDIT_ID": "ra-abcdefg2"}


def read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def hook(command: str, *, stdin: str = "{}", timeout: float = 20, grace: float = 1.0, **kw: object):
    return SystemGateway(kill_grace=grace).run_hook(
        command,
        timeout=timeout,
        stdin=stdin,
        env_extra=kw.pop("env_extra", ENV),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def test_the_command_runs_through_sh_with_the_payload_on_stdin() -> None:
    result = hook("cat", stdin='{"a": 1}')
    assert result.ok and result.stdout == '{"a": 1}'


def test_the_extra_variables_are_added_to_the_inherited_environment(monkeypatch) -> None:
    monkeypatch.setenv("SESSION_AUDIT_TEST_INHERITED", "kept")
    monkeypatch.setenv("SESSION_AUDIT_ID", "stale")
    result = hook('echo "$SESSION_AUDIT_TEST_INHERITED $SESSION_AUDIT_ID $SESSION_AUDIT_HOOK"')
    assert result.stdout == "kept ra-abcdefg2 post-retro\n"


def test_a_command_with_quotes_and_newlines_is_one_script() -> None:
    result = hook("echo 'a b'\necho \"c\\\"d\"; printf '%s' $((1+2))")
    assert result.ok and result.stdout == "a b\nc\"d\n3"


def test_the_exit_code_is_reported_and_13_is_just_an_exit() -> None:
    result = hook("exit 13")
    assert result.exit_is(13) and not result.ok


def test_the_hook_leads_its_own_process_group() -> None:
    result = hook("ps -o pgid= -p $$; ps -o sid= -p $$")
    pgid, sid = (int(x) for x in result.stdout.split())
    assert pgid != os.getpgrp()
    assert pgid == int(result.stdout.split()[0])
    assert sid == os.getsid(0)  # same session: a group of its own, not a new session


def test_a_grandchild_is_killed_when_the_leader_exits_normally(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    started = time.monotonic()
    result = hook(f"sleep 60 & echo $! > '{pids}'; echo done", grace=2)
    grandchild = read_pid(pids)
    try:
        assert result.ok and result.stdout == "done\n"
        assert wait_until_gone(grandchild), "the grandchild outlived the call"
        assert time.monotonic() - started < 15
    finally:
        if not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_a_grandchild_that_ignores_term_is_killed_after_the_grace(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    script = f"(trap '' TERM; exec sleep 60) & echo $! > '{pids}'"
    result = hook(script, grace=0.5)
    grandchild = read_pid(pids)
    try:
        assert result.ok
        assert wait_until_gone(grandchild), "SIGKILL did not reach the grandchild"
    finally:
        if not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_a_timeout_ends_the_group_and_is_a_failed_call(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    result = hook(f"sleep 60 & echo $! > '{pids}'; wait", timeout=0.5, grace=1)
    grandchild = read_pid(pids)
    try:
        assert result.status is CallStatus.TIMEOUT and result.returncode is None
        assert wait_until_gone(grandchild)
    finally:
        if not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_a_hook_that_ignores_term_is_killed_after_the_grace(tmp_path: Path) -> None:
    pids = tmp_path / "leader.pid"
    script = f"echo $$ > '{pids}'; trap '' TERM; while :; do sleep 0.05; done"
    started = time.monotonic()
    result = hook(script, timeout=0.5, grace=0.4)
    assert result.status is CallStatus.TIMEOUT
    assert time.monotonic() - started >= 0.8
    assert wait_until_gone(read_pid(pids))


def test_a_death_by_signal_is_a_failed_call_with_the_signal() -> None:
    result = hook("kill -9 $$")
    assert result.status is CallStatus.SIGNALED and result.signal == 9 and result.returncode is None


def test_stdout_beyond_the_cap_is_discarded_and_the_hook_is_not_killed() -> None:
    code = "import sys\nfor _ in range(64): sys.stdout.write('x' * 65536)\nsys.exit(7)"
    result = hook(f"{PY} -c \"{code}\"")
    assert result.exit_is(7)  # not OUTPUT_LIMIT: the hook ran to its own exit
    assert len(result.stdout.encode()) == HOOK_STDOUT_LIMIT  # one byte over the 64 KiB read


def test_stdout_exactly_at_the_cap_is_complete() -> None:
    result = hook(f"{PY} -c \"import sys; sys.stdout.write('x' * 65536)\"")
    assert result.ok and len(result.stdout) == 65536


def test_a_large_payload_to_a_slow_reader_does_not_deadlock_a_flooding_hook(
    tmp_path: Path,
) -> None:
    # The hook writes a lot before it reads its stdin: both directions progress.
    payload = "y" * (4 * 1024 * 1024)
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "for _ in range(40):\n"
        "    sys.stdout.write('z' * 65536)\n"
        "    sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stderr.write(str(len(sys.stdin.read())))\n"
        "sys.exit(5)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = hook(f"{PY} '{script}'", stdin=payload, timeout=30)
    assert result.exit_is(5) and result.stderr == str(len(payload))
    assert time.monotonic() - started < 25


def test_a_hook_that_never_reads_its_stdin_is_not_a_failure() -> None:
    result = hook("exit 0", stdin="x" * (1024 * 1024))
    assert result.ok


def test_a_hook_that_closes_stdout_and_stderr_still_ends() -> None:
    result = hook("exec >&- 2>&-; exit 3")
    assert result.exit_is(3) and result.stdout == ""


def test_a_hook_that_closes_its_stdout_but_keeps_running_ends_at_the_timeout() -> None:
    result = hook("exec >&- 2>&-; sleep 60", timeout=0.5, grace=0.5)
    assert result.status is CallStatus.TIMEOUT


def test_a_missing_shell_is_a_spawn_failure(monkeypatch) -> None:
    monkeypatch.setattr("session_audit.gateway.HOOK_SHELL", "/nonexistent/sh")
    result = hook("true")
    assert result.status is CallStatus.SPAWN_FAILED
