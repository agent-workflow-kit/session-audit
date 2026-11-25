"""The config as the commands see it: loaded in the composition root, refused or reported."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path

import pytest

from session_audit.cli import main
from session_audit.command import Command, Context
from session_audit.commands import COMMANDS

SEEN: list[tuple[tuple[str, ...], Context]] = []


def _record(command: Command) -> Command:
    def run(ctx: Context, args: argparse.Namespace) -> int:
        SEEN.append((command.path, ctx))
        return 0

    return dataclasses.replace(command, run=run)


STUBBED = tuple(_record(c) for c in COMMANDS)
MUTATING = [
    ["ingest"],
    ["prune"],
    ["retro", "run", "--since", "1d"],
    ["retro", "delete", "ra-abcd", "--yes"],
    ["cron", "install"],
    ["cron", "uninstall"],
]
READ_ONLY = [
    ["sessions"],
    ["usage", "--by", "day"],
    ["show", "0a1b2c3d"],
    ["retro", "list"],
    ["export", "sessions"],
]


@pytest.fixture(autouse=True)
def _clear() -> None:
    SEEN.clear()


@pytest.fixture
def config_dir(_private_environment: Path) -> Path:
    path = _private_environment / ".config" / "session-audit"
    path.mkdir(mode=0o700)
    return path


def put(directory: Path, text: str, mode: int = 0o600, name: str = "config.yaml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = main(argv, commands=STUBBED, stdin_is_tty=False)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("argv", MUTATING)
def test_a_command_that_changes_something_refuses_an_unsafe_config(
    capsys: pytest.CaptureFixture[str], config_dir: Path, argv: list[str]
) -> None:
    put(config_dir, "{}\n", mode=0o666)
    code, out, err = run(capsys, [*argv, "--json"])
    assert code == 20 and SEEN == []
    error = json.loads(out)["error"]
    assert error["code"] == "refused" and error["details"] == {"reason": "config_unsafe"}
    assert "writable by group or other" in err and str(config_dir) not in err + out


@pytest.mark.parametrize("argv", READ_ONLY)
def test_a_read_only_command_runs_and_reports_an_unsafe_config(
    capsys: pytest.CaptureFixture[str], config_dir: Path, argv: list[str]
) -> None:
    put(config_dir, "{}\n", mode=0o666)
    code, _, err = run(capsys, argv)
    assert code == 0 and len(SEEN) == 1
    assert "warning: config_unsafe: the config file is writable by group or other" in err


def test_quiet_silences_the_warning_but_not_the_context(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "{}\n", mode=0o666)
    code, _, err = run(capsys, ["sessions", "--quiet"])
    assert code == 0 and err == "" and len(SEEN[0][1].config_issues) == 1


def test_a_safe_config_is_loaded_into_the_context(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "limits: {retros_per_day: 8}\nme: {name: pat}\n")
    code, _, err = run(capsys, ["prune"])
    ctx = SEEN[0][1]
    assert (code, err) == (0, "") and ctx.settings.limits.retros_per_day == 8
    assert ctx.config_issues == () and ctx.config_error is None


def test_me_name_is_the_default_actor_on_a_terminal(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "me: {name: pat}\n")
    assert main(["prune"], commands=STUBBED, stdin_is_tty=True) == 0
    assert SEEN[0][1].actor == "human:pat"
    assert main(["prune", "--actor", "human:given"], commands=STUBBED, stdin_is_tty=True) == 0
    assert SEEN[1][1].actor == "human:given"


def test_the_configured_time_zone_reaches_the_context(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "time_zone: Asia/Tokyo\n")
    run(capsys, ["sessions", "--since", "2026-01-01"])
    assert SEEN[0][1].settings.zone().key == "Asia/Tokyo"


@pytest.mark.parametrize("argv", [*MUTATING[:1], *READ_ONLY[:2]])
def test_an_invalid_config_is_exit_2_naming_the_key(
    capsys: pytest.CaptureFixture[str], config_dir: Path, argv: list[str]
) -> None:
    put(config_dir, "retro: {max_turns: SEKRET-VALUE}\n")
    code, out, err = run(capsys, [*argv, "--json"])
    assert code == 2 and SEEN == []
    assert json.loads(out)["error"] == {
        "code": "config_invalid",
        "message": "the config has an invalid value",
        "details": {"field": "retro.max_turns"},
    }
    assert "SEKRET" not in out + err


def test_an_unsafe_and_invalid_config_is_refused_as_unsafe_by_a_mutating_command(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "not: [valid\n", mode=0o666)
    assert run(capsys, ["ingest"])[0] == 20
    assert run(capsys, ["sessions"])[0] == 2


def test_flag_errors_come_before_config_errors(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "not: [valid\n")
    code, _, _ = run(capsys, ["sessions", "--limit", "0"])
    assert code == 2 and SEEN == []
    code, out, _ = run(capsys, ["sessions", "--limit", "0", "--json"])
    assert json.loads(out)["error"]["details"]["field"] == "--limit"


def test_validate_runs_with_a_broken_or_unsafe_config(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "not: [valid\n", mode=0o666)
    code, _, _ = run(capsys, ["validate"])
    assert code == 0 and len(SEEN) == 1
    ctx = SEEN[0][1]
    assert ctx.config_error is not None and ctx.config_issues
    assert ctx.settings.limits.retros_per_day == 5  # the defaults


def test_version_works_with_a_broken_config(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    put(config_dir, "not: [valid\n", mode=0o666)
    assert run(capsys, ["--version"])[0] == 0


def test_config_flag_loads_that_file_and_a_missing_one_is_exit_2(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    other = put(config_dir, "limits: {retros_per_day: 3}\n", name="other.yaml")
    assert run(capsys, ["--config", str(other), "sessions"])[0] == 0
    assert SEEN[0][1].settings.limits.retros_per_day == 3
    code, out, _ = run(capsys, ["--config", str(config_dir / "no.yaml"), "sessions", "--json"])
    assert code == 2 and json.loads(out)["error"]["details"] == {"field": "--config"}


def test_the_example_file_via_the_cli_is_the_default_config(
    capsys: pytest.CaptureFixture[str], config_dir: Path
) -> None:
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    put(config_dir, example.read_text(encoding="utf-8"), name="ex.yaml")
    run(capsys, ["--config", str(config_dir / "ex.yaml"), "sessions"])
    run(capsys, ["sessions"])
    assert SEEN[0][1].settings == SEEN[1][1].settings
