"""The store as a command sees it: opened lazily through ``Context.store()``, closed after the
run, and refused with the documented envelopes."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from session_audit.cli import main
from session_audit.command import Command, Context
from session_audit.commands import COMMANDS
from session_audit.migrations import Migration
from session_audit.store import Store, open_store

from .schemas import validate
from .storehelp import V3, ClockedGateway

SEEN: list[Store] = []


def opening(command: Command) -> Command:
    def run(ctx: Context, args: argparse.Namespace) -> int:
        SEEN.append(ctx.store())
        assert ctx.store() is SEEN[-1]  # one store per run
        ctx.say("opened")
        return 0

    return dataclasses.replace(command, run=run)


def lazy(command: Command) -> Command:
    def run(ctx: Context, args: argparse.Namespace) -> int:
        ctx.say("did not open")
        return 0

    return dataclasses.replace(command, run=run)


OPENING = tuple(opening(c) if c.path == ("sessions",) else lazy(c) for c in COMMANDS)


@pytest.fixture(autouse=True)
def _clear() -> None:
    SEEN.clear()


def state_dir() -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "session-audit"


def test_a_command_that_never_asks_never_touches_the_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["usage", "--by", "day"], commands=OPENING, stdin_is_tty=False) == 0
    assert not state_dir().exists()
    assert capsys.readouterr().out == "did not open\n"


def test_the_store_opens_on_first_use_under_xdg_state_home_and_closes_after(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sessions"], commands=OPENING, stdin_is_tty=False) == 0
    assert capsys.readouterr().out == "opened\n"
    assert (state_dir() / "state.db").is_file()
    assert oct(os.lstat(state_dir()).st_mode & 0o777) == "0o700"
    with pytest.raises(Exception, match="closed|Cannot operate"):
        SEEN[0].conn.execute("SELECT 1")


def test_a_newer_store_is_exit_20_store_newer(capsys: pytest.CaptureFixture[str]) -> None:
    open_store(ClockedGateway(), str(state_dir()), migrations=V3).close()
    # The shipped registry is older than what the file holds.
    code = main(["sessions", "--json"], commands=OPENING, stdin_is_tty=False)
    out = capsys.readouterr().out
    document = json.loads(out)
    validate(document, "session-audit/cli/error.json")
    assert code == 20
    assert document["error"]["code"] == "refused"
    assert document["error"]["details"] == {"reason": "store_newer"}


def test_a_corrupt_store_is_exit_12_store_unwritable(capsys: pytest.CaptureFixture[str]) -> None:
    state_dir().mkdir(parents=True, mode=0o700)
    (state_dir() / "state.db").write_bytes(b"garbage" * 100)
    (state_dir() / "state.db").chmod(0o600)
    code = main(["sessions", "--json"], commands=OPENING, stdin_is_tty=False)
    document = json.loads(capsys.readouterr().out)
    validate(document, "session-audit/cli/error.json")
    assert code == 12
    assert document["error"]["details"] == {"reason": "store_unwritable", "retryable": False}


def test_an_unsafe_state_directory_is_exit_12(capsys: pytest.CaptureFixture[str]) -> None:
    state_dir().mkdir(parents=True)
    state_dir().chmod(0o777)
    try:
        code = main(["sessions"], commands=OPENING, stdin_is_tty=False)
    finally:
        state_dir().chmod(0o700)
    err = capsys.readouterr().err
    assert code == 12
    assert "store_unwritable" in err or "cannot be created or written" in err
    assert str(state_dir()) not in err  # no path in the message


def test_the_state_directory_override_of_the_context(tmp_path: Path) -> None:
    ctx = Context(ClockedGateway(), out=None, state_directory=str(tmp_path / "s"))  # type: ignore[arg-type]
    with ctx.store() as store:
        assert store.directory == str(tmp_path / "s")
    ctx.close()
    ctx.close()  # twice is fine


def test_the_migration_type_is_exported() -> None:
    assert Migration(900, "x", ("CREATE TABLE x (a)",)).number == 900
    assert isinstance(SEEN, list) and Any
