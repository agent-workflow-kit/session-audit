"""``live``: the retro launch against the real claude-session (issue #26; never run in CI).

Run by hand with ``uv run pytest -m live tests/test_live_retro_launch.py``. Both tests use a
private ``HOME`` (a synthetic transcript corpus, no real ``~/.claude``); the second one starts a
real model session and is billed (about a cent), so it also needs ``SESSION_AUDIT_LIVE_LAUNCH=1``
and a Claude login in the environment (``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN``).

The live fact (docs/design.md, Live verification): claude-session refuses a working directory
under its state directory or a hidden home entry, but its own per-session directory is exempt, so
a retro whose ``--cwd session-dir`` lives under the default ``~/.local/state`` (and under an
``XDG_STATE_HOME``) must not end ``unsafe_cwd``, ``unsafe_path`` or ``session_dir_unsafe``.

The bundled profile (``plugin/profiles/audit-retro.yaml``) is part of the plan check, so a profile
claude-session refuses fails these tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .ingesthelp import S1, S2, Corpus
from .retrohelp import seed

pytestmark = pytest.mark.live

REFUSED_CWD = ("unsafe_cwd", "unsafe_path", "session_dir_unsafe")
LOGIN_NAMES = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def environment(home: Path, xdg_state: Path | None) -> dict[str, str]:
    """The real PATH and login variables, a private home, and (or not) an XDG state directory."""
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME",)}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    if xdg_state is not None:
        env["XDG_STATE_HOME"] = str(xdg_state)
    return env


def session_audit(
    env: dict[str, str], *argv: str, timeout: float = 900
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the tool under test, an argv list
        [sys.executable, "-m", "session_audit", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


@pytest.fixture(params=["default-state-dir", "xdg-state-home"])
def home_and_env(
    request: pytest.FixtureRequest, _private_environment: Path, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    if shutil.which("claude-session") is None:
        pytest.skip("claude-session is not installed")
    home = _private_environment
    xdg = None if request.param == "default-state-dir" else tmp_path / "xdg-state"
    corpus = Corpus(home)
    seed(corpus, S1, "first prompt")
    seed(corpus, S2, "second prompt", project="p2")
    env = environment(home, xdg)
    ingest = session_audit(env, "ingest", "--no-harness")
    assert ingest.returncode == 0, ingest.stderr
    return home, env


def test_the_plan_check_accepts_a_session_directory_under_the_state_directory(
    home_and_env: tuple[Path, dict[str, str]],
) -> None:
    _home, env = home_and_env
    result = session_audit(env, "retro", "run", "--dry-run", "--json", "--project", "/work")
    document = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert document["dry_run"] is True
    assert "--cwd" in document["launch_flags"]


@pytest.mark.skipif(
    os.environ.get("SESSION_AUDIT_LIVE_LAUNCH") != "1"
    or not any(os.environ.get(name) for name in LOGIN_NAMES),
    reason="a billed launch: set SESSION_AUDIT_LIVE_LAUNCH=1 and a Claude login variable",
)
def test_a_real_launch_is_not_refused_for_its_working_directory(
    home_and_env: tuple[Path, dict[str, str]],
) -> None:
    _home, env = home_and_env
    result = session_audit(
        env, "retro", "run", "--json", "--project", "/work", "--max-turns", "3", "--timeout", "5m"
    )
    document = json.loads(result.stdout)
    if "error" in document:
        details = document["error"]["details"]
        assert details.get("end_reason") not in REFUSED_CWD, details
    # Whatever the retro ends as (the artifact check and the bundled skill exist), the
    # session was started from its own directory under the state directory, was recorded, and was
    # forgotten again.
    assert result.returncode in (0, 12, 13, 14, 15, 16, 17, 18, 22), result.stderr
