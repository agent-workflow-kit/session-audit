"""Shared fixtures: every test runs in a private environment and can start only fakes.

Autouse fixtures:

- ``_private_environment``: ``HOME``, ``XDG_*`` and the state directory point into the test's
  own temporary directory, so no test reads or writes the user's real home, config or state,
  and ``XDG_RUNTIME_DIR`` and ``CLAUDE_CONFIG_DIR`` are unset. ``PATH`` gets a directory of
  tripwires (``crontab``, ``claude``, ``claude-session``) that fail loudly if anything starts
  them.
- ``_process_guard``: wraps ``subprocess.Popen`` (what the gateway uses) so that
  ``guard_argv`` refuses a call that names ``crontab``, ``claude`` or ``claude-session`` anywhere
  (any argv element, any word of a ``sh -c`` string) unless it is a fake under ``tests/fakebin``
  or the pytest temporary directory, refuses a shell, ``env`` or interpreter given an ``env``
  without the tripwire directory first on ``PATH``, and records the
  PID of every process started at launch. At teardown a process still running is killed by its
  exact process group (it is still our unreaped child, so the group id cannot be reused) and the
  test fails, so a leak is never silently cleaned up.
"""

from __future__ import annotations

import os
import re
import shlex
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


# Programs that run other programs, so a protected program can hide behind them.
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish"})
RUNNERS = SHELLS | {"env", "xargs", "nohup", "timeout", "setsid", "perl", "ruby", "node"}
_PYTHON = re.compile(r"python[0-9.]*$")
_WORD = re.compile(r"[\w./+-]+")


def _is_runner(program: str) -> bool:
    name = Path(program).name
    return name in RUNNERS or bool(_PYTHON.match(name))


def _shell_words(text: str) -> list[str]:
    """Every word a shell command line could run or pass: the shlex tokens and the plain words
    between quotes and metacharacters (``$(crontab -l)``, ``a;claude-session``)."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = []
    return [*tokens, *_WORD.findall(text)]


def _check_program(
    program: str,
    search: str | None,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path],
) -> None:
    if Path(program).name not in PROTECTED_PROGRAMS:
        return
    if os.sep in program:
        resolved: str | None = program
    else:
        resolved = shutil.which(program, path=search)
    if resolved is None:
        return  # nothing to run: Popen fails by itself
    real = Path(resolved).resolve()
    if any(real.is_relative_to(root.resolve()) for root in forbidden_roots) or not any(
        real.is_relative_to(root.resolve()) for root in allowed_roots
    ):
        raise RealToolRefused(f"a test tried to start {resolved!r}; only fakes may run")


def guard_argv(
    argv: Sequence[str | os.PathLike[str]] | str | os.PathLike[str],
    env: Mapping[str, str] | None,
    allowed_roots: Sequence[Path],
    forbidden_roots: Sequence[Path] = (),
    *,
    shell: bool = False,
) -> None:
    """Raise ``RealToolRefused`` when the call would start a protected program that is not a fake
    under one of ``allowed_roots`` (and not under a ``forbidden_roots`` tripwire directory).

    Every element of ``argv`` is checked by its basename, not only the first (``env crontab -l``),
    and so are the words of a ``sh -c`` string (or of a ``shell=True`` command line). A program
    that runs other programs (a shell, ``env``, an interpreter) and is given an ``env`` whose
    ``PATH`` does not start with the tripwire directory (``forbidden_roots[0]``) is refused too:
    it could find the real tool through that ``PATH`` where this guard cannot see it."""
    if isinstance(argv, (str, os.PathLike)):
        words = [os.fsdecode(argv)]
    else:
        words = [os.fsdecode(a) for a in argv]
    if not words:
        return
    search = (os.environ if env is None else env).get("PATH")
    names = list(words)
    if shell and isinstance(argv, str):
        names = _shell_words(argv)
    elif Path(words[0]).name in SHELLS and "-c" in words[1:]:
        index = words.index("-c", 1)
        if index + 1 < len(words):
            names += _shell_words(words[index + 1])
    for name in names:
        _check_program(name, search, allowed_roots, forbidden_roots)
    if env is not None and (shell or _is_runner(words[0])):
        first = (env.get("PATH") or "").split(os.pathsep)[0]
        if not first or not any(
            Path(first).resolve() == root.resolve() for root in forbidden_roots[:1]
        ):
            raise RealToolRefused(
                f"a test started {words[0]!r} with an env whose PATH does not start with the "
                "tripwire directory; pass env={..., 'PATH': os.environ['PATH']}"
            )


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
            guard_argv(
                args,
                kwargs.get("env"),
                [FAKEBIN, base],
                [tripwire],
                shell=bool(kwargs.get("shell")),
            )
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
