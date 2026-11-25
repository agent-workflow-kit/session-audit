"""The system gateway: processes, files outside the store, the clock and the crontab."""

from __future__ import annotations

import errno
import os
import signal
import stat
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from session_audit import gateway
from session_audit.gateway import CallStatus, SystemGateway

from .conftest import RealToolRefused
from .procs import gone, wait_until_gone

PY = sys.executable


def read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


# -- results ---------------------------------------------------------------------------------


def test_exit_zero_is_ok_with_output() -> None:
    result = SystemGateway().run([PY, "-c", "print('hi')"], timeout=10)
    assert (result.status, result.returncode, result.stdout) == (CallStatus.EXITED, 0, "hi\n")
    assert result.ok and result.exit_is(0)


def test_exit_ten_is_an_exit_not_a_failed_call() -> None:
    result = SystemGateway().run([PY, "-c", "import sys; sys.exit(10)"], timeout=10)
    assert result.exit_is(10) and not result.ok and not result.exit_is(0)


def test_stderr_is_captured_separately() -> None:
    result = SystemGateway().run([PY, "-c", "import sys; sys.stderr.write('oops')"], timeout=10)
    assert (result.stdout, result.stderr) == ("", "oops")


def test_stdin_reaches_the_child_and_argv_is_not_a_shell_line() -> None:
    result = SystemGateway().run(["cat"], timeout=10, stdin="héllo\n")
    assert result.stdout == "héllo\n"
    echoed = SystemGateway().run(["echo", "$HOME; touch x"], timeout=10)
    assert echoed.stdout == "$HOME; touch x\n"


def test_large_stdin_does_not_deadlock_against_large_stdout() -> None:
    data = "x" * 3_000_000
    result = SystemGateway().run(["cat"], timeout=20, stdin=data)
    assert result.ok and result.stdout == data


def test_child_without_stdin_sees_eof() -> None:
    result = SystemGateway().run(["cat"], timeout=10)
    assert result.ok and result.stdout == ""


def test_invalid_utf8_output_is_replaced_not_raised() -> None:
    result = SystemGateway().run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"], timeout=10
    )
    assert result.stdout == "a�b"


def test_environment_is_exactly_what_the_caller_passes() -> None:
    result = SystemGateway().run(["env"], timeout=10, env={"ONLY": "1"})
    assert result.stdout == "ONLY=1\n"


def test_working_directory(tmp_path: Path) -> None:
    result = SystemGateway().run(
        [PY, "-c", "import os; print(os.getcwd())"], timeout=10, cwd=str(tmp_path)
    )
    assert result.stdout.strip() == str(tmp_path.resolve())


# -- failed calls ----------------------------------------------------------------------------


def test_spawn_failure_is_a_failed_call() -> None:
    result = SystemGateway().run(["/nonexistent/program"], timeout=10)
    assert result.status is CallStatus.SPAWN_FAILED
    assert result.returncode is None and not result.ok and not result.exit_is(0, 10)


def test_death_by_signal_is_a_failed_call() -> None:
    result = SystemGateway().run(["sh", "-c", "kill -KILL $$"], timeout=10)
    assert result.status is CallStatus.SIGNALED and result.signal == signal.SIGKILL
    assert result.returncode is None and not result.exit_is(0, 10)


@pytest.mark.parametrize("argv", [[], ["a\0b"], [b"x"]])
def test_bad_argv_is_refused(argv: list[str]) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(argv, timeout=10)


@pytest.mark.parametrize(
    "kwargs", [{"timeout": 0}, {"timeout": -1}, {"timeout": 1, "stdout_limit": 0}]
)
def test_bad_limits_are_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(["true"], **kwargs)  # type: ignore[arg-type]


def test_child_leads_its_own_process_group() -> None:
    code = f"import os; print(os.getpid() == os.getpgid(0), os.getpgid(0) != {os.getpgid(0)})"
    result = SystemGateway().run([PY, "-c", code], timeout=10)
    assert result.stdout.strip() == "True True"


def test_default_kill_grace_is_five_seconds() -> None:
    assert gateway.KILL_GRACE_SECONDS == 5.0
    assert SystemGateway()._kill_grace == 5.0


def test_timeout_kills_the_child_and_is_a_failed_call() -> None:
    started = time.monotonic()
    result = SystemGateway(kill_grace=2).run([PY, "-c", "import time; time.sleep(60)"], timeout=0.3)
    assert result.status is CallStatus.TIMEOUT
    assert result.returncode is None and not result.ok
    assert not result.exit_is(0) and not result.exit_is(10)
    assert time.monotonic() - started < 10


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > '{pids}'; wait"
    result = SystemGateway(kill_grace=2).run(["sh", "-c", script], timeout=0.5)
    assert result.status is CallStatus.TIMEOUT
    grandchild = read_pid(pids)
    assert wait_until_gone(grandchild), "the grandchild survived the group kill"


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace(tmp_path: Path) -> None:
    pids = tmp_path / "child.pid"
    script = f"echo $$ > '{pids}'; trap '' TERM; while :; do sleep 0.05; done"
    started = time.monotonic()
    result = SystemGateway(kill_grace=0.4).run(["sh", "-c", script], timeout=0.5)
    elapsed = time.monotonic() - started
    assert result.status is CallStatus.TIMEOUT
    assert 0.8 <= elapsed < 8  # timeout, then the whole grace, then SIGKILL
    assert wait_until_gone(read_pid(pids))


def test_term_is_sent_before_kill(tmp_path: Path) -> None:
    marker = tmp_path / "got-term"
    script = f"trap 'touch {marker}; exit 0' TERM; while :; do sleep 0.05; done"
    result = SystemGateway(kill_grace=3).run(["sh", "-c", script], timeout=0.5)
    assert result.status is CallStatus.TIMEOUT  # exiting on TERM does not make it a success
    assert marker.exists()


def test_stdout_over_the_cap_kills_the_group_and_is_a_failed_call() -> None:
    code = "import sys\nwhile True: sys.stdout.write('x' * 65536); sys.stdout.flush()"
    result = SystemGateway(kill_grace=2).run([PY, "-c", code], timeout=30, stdout_limit=1000)
    assert result.status is CallStatus.OUTPUT_LIMIT
    assert len(result.stdout) <= 1000 and result.returncode is None and not result.ok


def test_stdout_over_the_cap_of_a_program_that_already_exited() -> None:
    code = "import sys; sys.stdout.write('x' * 200000)"
    result = SystemGateway().run([PY, "-c", code], timeout=30, stdout_limit=1000)
    assert result.status is CallStatus.OUTPUT_LIMIT and not result.ok


def test_stdout_exactly_at_the_cap_is_fine() -> None:
    result = SystemGateway().run([PY, "-c", "print('x' * 99)"], timeout=30, stdout_limit=100)
    assert result.ok and len(result.stdout) == 100


def test_stderr_is_capped_without_failing_the_call() -> None:
    code = "import sys; sys.stderr.write('e' * 500000)"
    result = SystemGateway().run([PY, "-c", code], timeout=30, stdout_limit=1000)
    assert result.ok and len(result.stderr) == 1000


def test_a_grandchild_holding_the_pipes_does_not_hang_the_call(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    started = time.monotonic()
    result = SystemGateway().run(
        ["sh", "-c", f"sleep 30 & echo $! > '{pids}'; echo done"], timeout=20
    )
    leftover = read_pid(pids)
    try:
        assert result.ok and result.stdout == "done\n"
        assert time.monotonic() - started < 10
    finally:
        # Only the exact process this test started, after checking it is what we launched.
        if not gone(leftover) and Path(f"/proc/{leftover}/cmdline").read_bytes().startswith(
            b"sleep"
        ):
            os.kill(leftover, signal.SIGKILL)


# -- clock and files -------------------------------------------------------------------------


def test_now_is_timezone_aware_utc_and_close_to_the_system_clock() -> None:
    now = SystemGateway().now()
    assert now.utcoffset() == timedelta(0)
    assert abs(time.time() - now.timestamp()) < 5


def test_monotonic_never_goes_back() -> None:
    gw = SystemGateway()
    first = gw.monotonic()
    assert gw.monotonic() >= first


def test_open_read_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"line\n")
    with SystemGateway().open_read(path) as handle:
        assert handle.read() == b"line\n"


def test_open_read_refuses_a_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError) as exc:
        SystemGateway().open_read(link)
    assert exc.value.errno == errno.ELOOP


def test_open_read_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with SystemGateway().open_read(fifo) as handle:
        assert stat.S_ISFIFO(os.fstat(handle.fileno()).st_mode)


def test_lstat_does_not_follow_and_list_dir_is_sorted(tmp_path: Path) -> None:
    (tmp_path / "b").write_text("x")
    (tmp_path / "a").symlink_to(tmp_path / "b")
    gw = SystemGateway()
    assert stat.S_ISLNK(gw.lstat(tmp_path / "a").st_mode)
    assert gw.list_dir(tmp_path) == ["a", "b"]


# -- crontab ---------------------------------------------------------------------------------


def test_the_real_crontab_cannot_be_reached_from_a_test() -> None:
    with pytest.raises(RealToolRefused):
        SystemGateway().crontab_read()
    with pytest.raises(RealToolRefused):
        SystemGateway().crontab_write("* * * * * true\n")


def test_crontab_goes_through_the_program_named_crontab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    fake = bindir / "crontab"
    fake.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\ncat >> "{log}" 2>/dev/null\nexit 1\n', encoding="utf-8"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    gw = SystemGateway()
    assert gw.crontab_read().exit_is(1)
    assert gw.crontab_write("0 * * * * job\n").exit_is(1)
    assert log.read_text(encoding="utf-8").splitlines() == ["-l", "-", "0 * * * * job"]
