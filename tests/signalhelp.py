"""Shared fixtures and helpers of the daily-limit and signal tests (issue #28): the gateway with the
fake claude-session, the corpus, a config writer, hook commands, and the environment of a real
``session-audit`` child process that talks to the fake."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import FAKEBIN, tripwire_dir
from .fake_claude_session import FakeClaudeSession, plan_doc, step, version_step
from .ingesthelp import Corpus
from .retrohelp import RetroGateway
from .test_retro_launch import write_config

__all__ = [
    "RealMonotonicGateway",
    "corpus_fixture",
    "cs_fixture",
    "gw_fixture",
    "hook_config",
    "payloads",
    "recorder",
    "retro_child",
    "wait_for",
    "write_config",
]


class RealMonotonicGateway(RetroGateway):
    """``RetroGateway`` with the real monotonic clock: a test that waits for the exec child, a
    grace period or a bound measures real seconds."""

    def monotonic(self) -> float:
        return time.monotonic()


def gw_fixture(monkeypatch: pytest.MonkeyPatch, *, real_clock: bool = False) -> RetroGateway:
    gateway: RetroGateway = RealMonotonicGateway() if real_clock else RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


def corpus_fixture(home: Path) -> Corpus:
    return Corpus(home)


def cs_fixture(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gw.fake = fake
    return fake


def hook_config(**commands: str) -> str:
    lines = ["hooks:"]
    for name, command in commands.items():
        lines += [
            f"  {name.replace('_', '-')}:",
            f"    - cmd: {json.dumps(command)}",
            "      timeout: 30",
        ]
    return "\n".join(lines) + "\n"


def recorder(path: Path, then: str = "") -> str:
    """A hook command that appends its stdin (one JSON line) to ``path``, then runs ``then``."""
    code = "import sys;print(sys.stdin.read().replace(chr(10), ' '))"
    return f'python3 -c "{code}" >> {path}; {then or "true"}'


def payloads(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def wait_for(path: Path, seconds: float = 60.0, alive: Any = None) -> bool:
    """Wait (bounded) until ``path`` exists; stop early when ``alive()`` says the process ended."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        if alive is not None and not alive():
            return path.exists()
        time.sleep(0.02)
    return path.exists()


def retro_child(
    cs: FakeClaudeSession,
    home: Path,
    tmp_path_factory: pytest.TempPathFactory,
    *argv: str,
    script: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """A real ``session-audit retro run`` (or a wrapper script) that talks to the fake
    claude-session, in its own process group. The caller records ``pid``, waits for it and kills
    only that process group if it is still running."""
    write_config(home, _config_with_fake(home))
    env = {
        **cs.env(),
        "PATH": f"{tripwire_dir(tmp_path_factory)}{os.pathsep}{os.environ['PATH']}",
        **(extra_env or {}),
    }
    command = (
        [sys.executable, str(script), *argv]
        if script is not None
        else [sys.executable, "-m", "session_audit", "retro", "run", "--json", *argv]
    )
    return subprocess.Popen(  # noqa: S603 - our own interpreter and argv
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
    )


def _config_with_fake(home: Path) -> str:
    """The test's own config (hooks, limits) plus the harness key that names the fake."""
    path = home / ".config" / "session-audit" / "config.yaml"
    extra = path.read_text(encoding="utf-8") if path.exists() else ""
    if "claude_session_cmd" in extra:
        return extra
    return f"harness:\n  claude_session_cmd: {FAKEBIN / 'claude-session'}\n" + extra
