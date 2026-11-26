"""The gateway's view of a process: its own pid and the start time that tells a reused pid from the
holder it replaced (the lease liveness rule)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from session_audit import gateway
from session_audit.gateway import SystemGateway

PY = sys.executable


def test_pid_is_the_process_id() -> None:
    assert SystemGateway().pid() == os.getpid()


def test_own_start_time_is_a_stable_nonempty_token() -> None:
    gw = SystemGateway()
    first = gw.process_start(os.getpid())
    assert first
    assert len(first) <= 64
    assert gw.process_start(os.getpid()) == first


def test_a_missing_process_has_no_start_time() -> None:
    child = subprocess.Popen([PY, "-c", "pass"])
    pid = child.pid
    child.wait()  # reaped: the pid is free
    assert SystemGateway().process_start(pid) is None


def test_a_zombie_is_not_running() -> None:
    child = subprocess.Popen([PY, "-c", "pass"])  # exits, stays unreaped
    try:
        gw = SystemGateway()
        end = time.monotonic() + 10
        while time.monotonic() < end and gw.process_start(child.pid) is not None:
            time.sleep(0.02)
        assert gw.process_start(child.pid) is None
    finally:
        child.wait()


def test_different_processes_have_different_tokens_or_pids() -> None:
    child = subprocess.Popen(["sleep", "30"])
    try:
        gw = SystemGateway()
        mine, theirs = gw.process_start(os.getpid()), gw.process_start(child.pid)
        assert theirs is not None
        assert mine != theirs
    finally:
        child.kill()
        child.wait()


def test_pid_zero_and_negative_have_no_start_time() -> None:
    gw = SystemGateway()
    assert gw.process_start(0) is None
    assert gw.process_start(-5) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "123 (sleep) S 1 456 456 0 -1 4194304 1 2 3 4 5 6 7 8 9 10 11 12 987654 0",
            ("S", "987654"),
        ),
        (
            "123 (a b) ) (x) R 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 424242 1",
            ("R", "424242"),
        ),
        ("123 (cut) S 1 2 3", None),
        ("garbage", None),
        ("123 (x) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 notdigits 1", None),
    ],
)
def test_proc_stat_start_time_is_parsed_from_the_last_parenthesis(
    text: str, expected: tuple[str, str] | None
) -> None:
    assert gateway._parse_proc_start(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ss   Tue Nov 25 10:00:00 2026\n", "Tue Nov 25 10:00:00 2026"),
        ("S    Tue Nov  3 01:02:03 2026\n", "Tue Nov 3 01:02:03 2026"),
        ("Z    Tue Nov 25 10:00:00 2026\n", None),
        ("", None),
        ("S\n", None),
    ],
)
def test_ps_output_is_parsed_for_the_start_time(text: str, expected: str | None) -> None:
    assert gateway._parse_ps_start(text) == expected


def test_without_proc_the_start_time_comes_from_ps(tmp_path: Path) -> None:
    gw = SystemGateway(proc=str(tmp_path / "no-proc"))
    token = gw.process_start(os.getpid())
    assert token
    assert gw.process_start(os.getpid()) == token
    assert gw.process_start(2**22 + 12345) is None
