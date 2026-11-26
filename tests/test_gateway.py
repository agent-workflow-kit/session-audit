"""The system gateway: processes, files outside the store, the clock and the crontab."""

from __future__ import annotations

import errno
import math
import os
import signal
import stat
import subprocess
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
    result = SystemGateway().run(["printenv"], timeout=10, env={"ONLY": "1"})
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
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": math.nan},
        {"timeout": math.inf},
        {"timeout": -math.inf},
        {"timeout": 1, "stdout_limit": 0},
    ],
)
def test_bad_limits_are_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(["true"], **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "env",
    [{"A\0B": "x"}, {"A": "b\0"}, {"": "x"}, {"A=B": "x"}, {"A": 1}, {1: "x"}],
)
def test_a_bad_environment_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(["true"], timeout=10, env=env)


def test_stdin_with_a_lone_surrogate_is_refused_before_anything_starts(
    _process_guard: list[subprocess.Popen[bytes]],
) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(["cat"], timeout=10, stdin="bad \ud800 text")
    assert _process_guard == []  # nothing was started, so nothing can be left behind


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
    assert "stdout cap" in result.detail and "after" not in result.detail


def test_stdout_over_the_cap_kills_a_grandchild_too(tmp_path: Path) -> None:
    pids = tmp_path / "grandchild.pid"
    flood = "import sys\nwhile True: sys.stdout.write('x' * 65536); sys.stdout.flush()"
    script = f"sleep 60 & echo $! > '{pids}'; exec '{PY}' -c \"{flood}\""
    result = SystemGateway(kill_grace=2).run(["sh", "-c", script], timeout=30, stdout_limit=1000)
    assert result.status is CallStatus.OUTPUT_LIMIT
    assert wait_until_gone(read_pid(pids)), "the grandchild survived the group kill"


def test_stdout_over_the_cap_of_a_program_that_already_exited() -> None:
    code = "import sys; sys.stdout.write('x' * 200000)"
    result = SystemGateway().run([PY, "-c", code], timeout=30, stdout_limit=1000)
    assert result.status is CallStatus.OUTPUT_LIMIT and not result.ok
    assert "stdout cap" in result.detail


def test_a_stderr_flood_cannot_starve_the_timeout() -> None:
    code = "import sys\nwhile True: sys.stderr.write('e' * 65536); sys.stderr.flush()"
    started = time.monotonic()
    result = SystemGateway(kill_grace=2).run([PY, "-c", code], timeout=0.5, stdout_limit=10_000)
    assert result.status is CallStatus.TIMEOUT
    assert time.monotonic() - started < 15


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


def test_output_that_arrives_just_before_the_exit_is_taken_by_the_drain_after_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The selector-driven reads are switched off, so only the read after the leader's exit can
    # deliver the output; the grandchild keeps the pipe open (no EOF) as in the real case.
    original = SystemGateway._drain

    def only_after_exit(stream, selector, buffer, limit):  # type: ignore[no-untyped-def]
        if selector is None:
            original(stream, selector, buffer, limit)

    monkeypatch.setattr(SystemGateway, "_drain", staticmethod(only_after_exit))
    pids = tmp_path / "grandchild.pid"
    started = time.monotonic()
    result = SystemGateway().run(
        ["sh", "-c", f"sleep 30 & echo $! > '{pids}'; echo late-output"], timeout=20
    )
    leftover = read_pid(pids)
    try:
        assert result.ok and result.stdout == "late-output\n"
        assert time.monotonic() - started < 10
    finally:
        if not gone(leftover) and Path(f"/proc/{leftover}/cmdline").read_bytes().startswith(
            b"sleep"
        ):
            os.kill(leftover, signal.SIGKILL)


def test_an_error_inside_the_supervision_still_ends_the_whole_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(stream, selector, buffer, limit):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(SystemGateway, "_drain", staticmethod(boom))
    pids = tmp_path / "pids"
    script = f"sleep 60 & echo $$ $! > '{pids}'; echo hi; wait"
    with pytest.raises(RuntimeError, match="boom"):
        SystemGateway(kill_grace=2).run(["sh", "-c", script], timeout=30)
    leader, grandchild = (int(n) for n in pids.read_text(encoding="utf-8").split())
    assert wait_until_gone(leader) and wait_until_gone(grandchild)


# -- ending a group --------------------------------------------------------------------------

MEMBER = """
import pathlib, signal, sys, time
def cleanup(*_):
    time.sleep(0.5)
    pathlib.Path(sys.argv[1]).write_text("cleaned")
    sys.exit(0)
signal.signal(signal.SIGTERM, cleanup)
pathlib.Path(sys.argv[2]).write_text("ready")
time.sleep(60)
"""


def start_group(tmp_path: Path) -> subprocess.Popen[bytes]:
    member = tmp_path / "member.py"
    member.write_text(MEMBER, encoding="utf-8")
    script = (
        f"'{PY}' '{member}' '{tmp_path / 'cleaned'}' '{tmp_path / 'ready'}' & "
        f"while [ ! -f '{tmp_path / 'ready'}' ]; do sleep 0.02; done; "
        f"trap 'exit 0' TERM; : > '{tmp_path / 'leader-ready'}'; while :; do sleep 0.05; done"
    )
    popen = subprocess.Popen(["sh", "-c", script], process_group=0)
    assert wait_for_file(tmp_path / "leader-ready"), "the group did not come up"
    return popen


def wait_for_file(path: Path, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not path.exists():
        time.sleep(0.02)
    return path.exists()


def test_end_group_waits_for_a_member_still_cleaning_up_after_the_leader_exits(
    tmp_path: Path,
) -> None:
    leader = start_group(tmp_path)
    started = time.monotonic()
    SystemGateway(kill_grace=10)._end_group(leader)
    assert (tmp_path / "cleaned").read_text(encoding="utf-8") == "cleaned"
    assert time.monotonic() - started < 9  # it did not sit out the whole grace


def test_end_group_still_kills_a_member_that_outlasts_the_grace(tmp_path: Path) -> None:
    leader = start_group(tmp_path)
    SystemGateway(kill_grace=0.2)._end_group(leader)  # the member needs 0.5 s to clean up
    assert not (tmp_path / "cleaned").exists()


def test_end_group_with_nothing_left_after_the_leader_returns_at_once(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    leader = subprocess.Popen(
        ["sh", "-c", f"trap 'exit 0' TERM; : > '{ready}'; while :; do sleep 0.05; done"],
        process_group=0,
    )
    assert wait_for_file(ready), "the trap was not installed"
    started = time.monotonic()
    SystemGateway(kill_grace=30)._end_group(leader)
    assert leader.returncode == 0
    assert time.monotonic() - started < 10


def test_a_group_of_only_zombies_is_not_alive() -> None:
    leader = subprocess.Popen([PY, "-c", "pass"], process_group=0)  # exits, stays unreaped
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end and gateway._group_member_states(leader.pid) != ["Z"]:
            time.sleep(0.02)
        assert gateway._group_member_states(leader.pid) == ["Z"]
        os.killpg(leader.pid, 0)  # the plain probe still sees the group
        assert not gateway._group_alive(leader.pid)
    finally:
        leader.wait()


def test_a_group_with_a_running_member_is_alive() -> None:
    leader = subprocess.Popen(["sleep", "30"], process_group=0)
    try:
        assert gateway._group_alive(leader.pid)
    finally:
        leader.kill()
        leader.wait()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("123 (sleep) S 1 456 456 0 -1", ("S", 456)),
        ("123 (a b) Z 1 77 77 0 -1", ("Z", 77)),
        ("123 (we ird) ) (x) R 1 88 88 0", ("R", 88)),
        ("123 (cut) S 1", None),
        ("garbage", None),
        ("123 (x) S 1 notanumber 5", None),
    ],
)
def test_proc_stat_is_parsed_from_the_last_parenthesis(
    text: str, expected: tuple[str, int] | None
) -> None:
    assert gateway._parse_proc_stat(text) == expected


def test_group_states_are_unknown_without_a_readable_proc(tmp_path: Path) -> None:
    assert gateway._group_member_states(1, proc=str(tmp_path / "missing")) is None


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


def test_read_file_reads_up_to_the_limit_and_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"abcdef")
    gw = SystemGateway()
    whole = gw.read_file(tmp_path / "f", 6)
    assert (whole.data, whole.truncated) == (b"abcdef", False)
    cut = gw.read_file(tmp_path / "f", 5)
    assert (cut.data, cut.truncated) == (b"abcde", True)
    assert gw.read_file(tmp_path / "f", 0).truncated is True
    assert stat.S_ISREG(whole.stat.st_mode) and whole.stat.st_uid == gw.uid()


def test_read_file_refuses_a_symlink_and_does_not_read_a_fifo_or_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "t").write_bytes(b"x")
    os.symlink(tmp_path / "t", tmp_path / "l")
    gw = SystemGateway()
    with pytest.raises(OSError) as raised:
        gw.read_file(tmp_path / "l", 10)
    assert raised.value.errno == errno.ELOOP
    os.mkfifo(tmp_path / "p")
    fifo = gw.read_file(tmp_path / "p", 10)
    assert fifo.data == b"" and stat.S_ISFIFO(fifo.stat.st_mode)
    assert stat.S_ISDIR(gw.read_file(tmp_path, 10).stat.st_mode)
    with pytest.raises(FileNotFoundError):
        gw.read_file(tmp_path / "missing", 10)


def test_stat_follows_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    os.symlink(tmp_path / "d", tmp_path / "l")
    gw = SystemGateway()
    assert stat.S_ISDIR(gw.stat(tmp_path / "l").st_mode)
    assert stat.S_ISLNK(gw.lstat(tmp_path / "l").st_mode)


def test_machine_time_zone_prefers_tz_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = SystemGateway()
    assert gw.machine_time_zone({"TZ": ":Europe/Paris"}) == "Europe/Paris"
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # the passed mapping wins, not the process env
    assert gw.machine_time_zone({"TZ": "Europe/Paris"}) == "Europe/Paris"
    result = gw.machine_time_zone({})
    assert result is None or isinstance(result, str)


def test_a_posix_tz_string_falls_through_to_the_machine_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zone_file = tmp_path / "timezone"
    zone_file.write_text("Europe/Berlin\n", encoding="utf-8")
    monkeypatch.setattr(SystemGateway, "LOCALTIME", str(tmp_path / "no-localtime"))
    monkeypatch.setattr(SystemGateway, "TIMEZONE_FILE", str(zone_file))
    gw = SystemGateway()
    assert gw.machine_time_zone({"TZ": "EST5EDT,M3.2.0,M11.1.0"}) == "Europe/Berlin"
    assert gw.machine_time_zone({"TZ": "Not/AZone"}) == "Europe/Berlin"
    assert gw.machine_time_zone({}) == "Europe/Berlin"
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/Asia/Tokyo")
    monkeypatch.setattr(SystemGateway, "LOCALTIME", str(link))
    assert gw.machine_time_zone({"TZ": "garbage"}) == "Asia/Tokyo"
    zone_file.write_text("Not/AZone\n", encoding="utf-8")
    link.unlink()
    assert gw.machine_time_zone({"TZ": "garbage"}) is None


# --- the state directory (the store's own files) ----------------------------------------------


def test_umask_is_set_for_the_block_and_restored(tmp_path: Path) -> None:
    gateway = SystemGateway()
    before = os.umask(0o022)
    try:
        with gateway.umask(0o077):
            assert os.umask(0o077) == 0o077
        assert os.umask(0o022) == 0o022
        with pytest.raises(RuntimeError), gateway.umask(0o077):
            raise RuntimeError
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o277, 0o777])
def test_mkdir_gives_exactly_the_mode_whatever_the_umask(tmp_path: Path, mask: int) -> None:
    before = os.umask(mask)
    try:
        SystemGateway().mkdir(tmp_path / "d", 0o700)
    finally:
        os.umask(before)
    assert stat.S_IMODE(os.lstat(tmp_path / "d").st_mode) == 0o700


def test_mkdir_refuses_an_existing_path_including_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    for name in ("real", "link"):
        with pytest.raises(FileExistsError):
            SystemGateway().mkdir(tmp_path / name, 0o700)
    with pytest.raises(FileNotFoundError):
        SystemGateway().mkdir(tmp_path / "no" / "parent", 0o700)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o277])
def test_open_private_creates_with_the_exact_mode(tmp_path: Path, mask: int) -> None:
    before = os.umask(mask)
    try:
        handle = SystemGateway().open_private(tmp_path / "f", create=True)
        handle.close()
    finally:
        os.umask(before)
    assert stat.S_IMODE(os.lstat(tmp_path / "f").st_mode) == 0o600


def test_open_private_keeps_an_existing_file_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    path.chmod(0o640)
    handle = SystemGateway().open_private(path, create=True)
    handle.close()
    assert path.read_bytes() == b"abc" and stat.S_IMODE(path.stat().st_mode) == 0o640


def test_open_private_without_create_does_not_create(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SystemGateway().open_private(tmp_path / "f", create=False)
    assert not (tmp_path / "f").exists()


def test_open_private_never_follows_a_final_symlink(tmp_path: Path) -> None:
    (tmp_path / "t").write_bytes(b"")
    (tmp_path / "l").symlink_to(tmp_path / "t")
    for create in (False, True):
        with pytest.raises(OSError):
            SystemGateway().open_private(tmp_path / "l", create=create)


def test_open_private_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "p", 0o600)
    handle = SystemGateway().open_private(tmp_path / "p", create=True)
    try:
        assert not stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
    finally:
        handle.close()


def test_append_mode_writes_at_the_end_whatever_the_position(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one\n")
    handle = SystemGateway().open_private(path, create=True, append=True)
    handle.seek(0)
    handle.write(b"two\n")
    handle.close()
    assert path.read_bytes() == b"one\ntwo\n"


def test_lock_exclusive_waits_then_gives_up_and_releases_on_close(tmp_path: Path) -> None:
    gateway = SystemGateway()
    first = gateway.open_private(tmp_path / "l", create=True)
    second = gateway.open_private(tmp_path / "l", create=True)
    assert gateway.lock_exclusive(first, 1.0)
    started = time.monotonic()
    assert not gateway.lock_exclusive(second, 0.2)
    assert 0.15 <= time.monotonic() - started < 3
    first.close()
    assert gateway.lock_exclusive(second, 1.0)
    second.close()


def test_open_private_read_only_opens_a_file_the_user_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"data")
    path.chmod(0o400)
    gateway = SystemGateway()
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            gateway.open_private(path, create=False)
    handle = gateway.open_private(path, create=False, read_only=True)
    try:
        assert handle.read() == b"data"
        with pytest.raises(OSError):
            handle.write(b"x")
    finally:
        handle.close()


def test_open_private_read_only_refuses_create_and_append(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SystemGateway().open_private(tmp_path / "f", create=True, read_only=True)
    with pytest.raises(ValueError):
        SystemGateway().open_private(tmp_path / "f", create=False, append=True, read_only=True)
