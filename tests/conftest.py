"""Shared fixtures: every test runs in a private environment and can start only fakes.

Autouse fixtures:

- ``_private_environment``: ``HOME``, ``XDG_*`` and the state directory point into the test's
  own temporary directory, so no test reads or writes the user's real home, config or state,
  and ``XDG_RUNTIME_DIR`` and ``CLAUDE_CONFIG_DIR`` are unset. ``PATH`` gets a directory of
  tripwires (``crontab``, ``claude``, ``claude-session``) that fail loudly if anything starts
  them.
- ``_process_guard``: wraps ``subprocess.Popen`` (what the gateway uses) so that
  ``guard_argv`` refuses to start a program named ``crontab``, ``claude`` or ``claude-session``
  unless it is a fake under ``tests/fakebin`` or the pytest temporary directory, and records the
  PID of every process started at launch. At teardown a process still running is killed by its
  exact process group (it is still our unreaped child, so the group id cannot be reused) and the
  test fails, so a leak is never silently cleaned up.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ["pytester"]  # test_helpers.py runs the process guard in a pytest of its own

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
PROTECTED_PROGRAMS = frozenset({"crontab", "claude", "claude-session"})


class RealToolRefused(AssertionError):
    """A test tried to start the real crontab, claude or claude-session."""


def guard_argv(
    argv: Sequence[str | os.PathLike[str]] | str | os.PathLike[str],
    env: Mapping[str, str] | None,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (),
) -> None:
    """Raise ``RealToolRefused`` when ``argv`` starts a protected program that is not a fake
    under one of ``allowed_roots`` (and not under a ``forbidden_roots`` tripwire directory)."""
    program = os.fspath(argv if isinstance(argv, (str, os.PathLike)) else argv[0])
    if Path(program).name not in PROTECTED_PROGRAMS:
        return
    if os.sep in program:
        resolved: str | None = program
    else:
        search = (env or os.environ).get("PATH")
        resolved = shutil.which(program, path=search)
    if resolved is None:
        return  # nothing to run: Popen fails by itself
    real = Path(resolved).resolve()
    if any(real.is_relative_to(root.resolve()) for root in forbidden_roots) or not any(
        real.is_relative_to(root.resolve()) for root in allowed_roots
    ):
        raise RealToolRefused(f"a test tried to start {resolved!r}; only fakes may run")


@pytest.fixture(autouse=True)
def _private_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("private")
    home = root / "home"
    dirs = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_STATE_HOME": home / ".local" / "state",
    }
    for name, path in dirs.items():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    for name in ("XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in [n for n in os.environ if n.startswith("SESSION_AUDIT_")]:
        monkeypatch.delenv(name)
    tripwire = tripwire_dir(tmp_path_factory)
    monkeypatch.setenv("PATH", f"{tripwire}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    yield home


_TRIPWIRE: Path | None = None


def tripwire_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    global _TRIPWIRE
    if _TRIPWIRE is None or not _TRIPWIRE.is_dir():
        directory = tmp_path_factory.mktemp("tripwire")
        for name in sorted(PROTECTED_PROGRAMS):
            script = directory / name
            script.write_text(
                f'#!/bin/sh\necho "tripwire: a test started the real {name}" >&2\nexit 97\n',
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        _TRIPWIRE = directory
    return _TRIPWIRE


@pytest.fixture(autouse=True)
def _process_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[list[subprocess.Popen[Any]]]:
    base = tmp_path_factory.getbasetemp()
    tripwire = tripwire_dir(tmp_path_factory)
    real_popen = subprocess.Popen
    started: list[subprocess.Popen[Any]] = []

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, *pargs: Any, **kwargs: Any) -> None:
            guard_argv(args, kwargs.get("env"), [FAKEBIN, base], [tripwire])
            super().__init__(args, *pargs, **kwargs)
            started.append(self)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    yield started
    leaked = [p for p in started if p.poll() is None]
    for process in leaked:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # its own group: set by the gateway
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()
    if leaked:
        pytest.fail(f"a test left processes running: pids {[p.pid for p in leaked]}")
