"""The CLI skeleton: registry, global flags, usage errors, the envelope and the exits.

In-process tests call ``main`` with the real registry whose ``run`` functions are replaced by a
recorder (so what is under test is parsing, ``check`` and the reporting); process tests start
``python -m session_audit`` for the behavior that only a real process has (broken pipes, closed
descriptors, signals, the C locale).
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest

from session_audit import __version__, errors
from session_audit.cli import main
from session_audit.command import Command, Context, Output, StdoutFailed
from session_audit.commands import COMMANDS
from session_audit.errors import CliError
from tests.golden import assert_golden
from tests.schemas import validate

ROOT = Path(__file__).resolve().parent.parent
ERROR_SCHEMA = "session-audit/cli/error.json"
HOSTILE = "\x1b[31mEVIL‮\x07"
SESSION = "0a1b2c3d-0000-4000-8000-000000000000"

CALLS: list[tuple[tuple[str, ...], argparse.Namespace, Context]] = []


def _record(command: Command) -> Command:
    def run(ctx: Context, args: argparse.Namespace) -> int:
        CALLS.append((command.path, args, ctx))
        return 0

    return dataclasses.replace(command, run=run)


STUBBED = tuple(_record(c) for c in COMMANDS)


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    CALLS.clear()


def run(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    *,
    commands: tuple[Command, ...] = STUBBED,
    tty: bool = False,
) -> tuple[int, str, str]:
    code = main(argv, commands=commands, stdin_is_tty=tty)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def envelope(out: str) -> dict[str, Any]:
    """The one envelope on stdout, validated against the schema."""
    assert out.endswith("\n") and out.count("\n") == 1, out
    document = json.loads(out)
    validate(document, ERROR_SCHEMA)
    assert set(document) == {"error"}
    return document["error"]  # type: ignore[no-any-return]


# --version ---------------------------------------------------------------------------------


def test_version_prints_package_metadata_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["--version"])
    assert (code, err) == (0, "")
    assert out.strip() == f"session-audit {version('session-audit')}"


def test_version_is_never_the_placeholder() -> None:
    assert __version__ not in {"0.0.0", "", "unknown"}


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"], (
        f"the installed metadata says {__version__} but pyproject.toml says "
        f"{pyproject['project']['version']}: run `uv sync` after a release bump"
    )


class _NoGateway:
    """A gateway that fails on any use: ``--version`` must never reach it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"gateway.{name} used")


def test_version_works_with_a_broken_config_and_a_newer_store(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--version`` must read neither a config nor a store, even a broken one or a missing
    ``--config`` file: no command ``check`` or ``run`` and no gateway call happens (the config is
    loaded in the composition root, after ``--version`` is answered)."""
    broken = tmp_path / "config.yaml"
    broken.write_text("{ not: [yaml", encoding="utf-8")
    state = Path(os.environ["XDG_STATE_HOME"]) / "session-audit"
    state.mkdir(parents=True)
    (state / "state.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 200)
    for argv in (
        ["--version"],
        ["--config", str(broken), "--version"],
        ["--config", str(tmp_path / "missing.yaml"), "--version"],
        ["sessions", "--config", str(broken), "--version"],
        ["retro", "run", "--version"],
    ):
        code, out, err = run(capsys, argv)
        assert (code, out, err) == (0, f"session-audit {__version__}\n", ""), argv
    assert CALLS == []  # no command ran
    assert main(["--version"], gateway=cast(Any, _NoGateway()), commands=STUBBED) == 0


# registry -----------------------------------------------------------------------------------

# One valid command line per command of the interface.
VALID: list[list[str]] = [
    ["ingest"],
    [
        "ingest",
        "--since",
        "2h",
        "--path",
        "/x",
        "--no-harness",
        "--budget",
        "5m",
        "--dry-run",
        "--actor",
        "human:me",
    ],
    ["ingest", "--session", "cs-abcdefgh"],
    ["ingest", "--rebuild", "--yes"],
    ["ingest", "--rebuild", "--dry-run"],
    ["sessions"],
    [
        "sessions",
        "--since",
        "2026-11-01",
        "--until",
        "2026-11-20T10:00:00Z",
        "--project",
        "/p",
        "--label",
        "a=b",
        "--kind",
        "review",
        "--outcome",
        "done",
        "--entrypoint",
        "headless",
        "--model",
        "claude-opus-4-5",
        "--sort",
        "cost",
        "--limit",
        "1000",
    ],
    ["usage", "--by", "day"],
    ["usage", "--by", "label=team,tool", "--tz", "America/New_York", "--limit", "1"],
    ["show", SESSION[:8]],
    ["show", SESSION],
    ["retro", "run", "--session", SESSION[:8]],
    [
        "retro",
        "run",
        "--label",
        "a=b",
        "--skill",
        "session-audit:audit",
        "--mode",
        "headless",
        "--model",
        "m1",
        "--timeout",
        "30m",
        "--max-turns",
        "50",
        "--again",
        "--decision-id",
        "d1",
        "--dry-run",
        "--actor",
        "auto:cron",
    ],
    ["retro", "run", "--since", "7d"],
    ["retro", "list"],
    ["retro", "list", "--state", "done", "--state", "failed", "--key", "a" * 64, "--limit", "5"],
    ["retro", "show", "ra-abcd"],
    ["retro", "show", "ra-abcdefg2", "--digest"],
    ["retro", "delete", "ra-abcd", "--yes"],
    ["prune"],
    ["prune", "--dry-run", "--actor", "human:x"],
    ["export", "sessions"],
    ["export", "retros", "--since", "1d"],
    ["validate"],
    ["validate", "--probe"],
    ["cron", "install"],
    ["cron", "install", "--ingest-every", "30m", "--env-file", "/e", "--dry-run"],
    ["cron", "uninstall", "--dry-run"],
]


@pytest.mark.parametrize("argv", VALID, ids=lambda a: " ".join(a)[:70])
def test_every_valid_command_line_reaches_its_run(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, [*argv, "--json"])
    assert (code, out, err) == (0, "", "")
    assert len(CALLS) == 1


def test_every_command_of_the_interface_is_registered_once() -> None:
    paths = [c.path for c in COMMANDS]
    assert len(paths) == len(set(paths))
    assert set(paths) == {
        ("ingest",),
        ("sessions",),
        ("usage",),
        ("show",),
        ("retro", "run"),
        ("retro", "list"),
        ("retro", "show"),
        ("retro", "delete"),
        ("prune",),
        ("export",),
        ("validate",),
        ("cron", "install"),
        ("cron", "uninstall"),
    }
    assert {tuple(a[: len(c.path)]) for a in VALID for c in COMMANDS if a[0] == c.path[0]} >= set(
        paths
    )


def test_actor_is_accepted_only_by_commands_that_change_the_store() -> None:
    assert {c.path for c in COMMANDS if c.accepts_actor} == {
        ("ingest",),
        ("retro", "run"),
        ("retro", "delete"),
        ("prune",),
    }


@pytest.mark.parametrize("command", [c.path for c in COMMANDS], ids=" ".join)
def test_help_of_every_command_exits_zero_on_stdout(
    command: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, [*command, "--help"])
    assert (code, err) == (0, "")
    assert out.startswith("usage: session-audit")


def test_no_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    code, out, err = run(capsys, ["--json"])
    assert code == 2
    assert envelope(out)["code"] == "usage"
    assert "required" in err
    code, out, _ = run(capsys, ["retro", "--json"])
    assert (code, envelope(out)["code"]) == (2, "usage")


def test_the_command_registry_is_the_only_place_that_names_commands() -> None:
    source = (ROOT / "src" / "session_audit" / "cli.py").read_text(encoding="utf-8")
    for name in ("ingest", "sessions", "retro", "prune", "cron", "validate"):
        assert f'"{name}"' not in source, name


# global flags -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "sessions"],
        ["sessions", "--json"],
        ["--json", "sessions", "--json"],
        ["--json", "retro", "list"],
        ["retro", "--json", "list"],
        ["retro", "list", "--json"],
    ],
)
def test_json_is_accepted_before_and_after_the_command(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, argv)[0] == 0
    assert CALLS[0][2].json is True


def test_a_global_flag_before_the_command_survives_the_subparser_defaults(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    first, second = str(tmp_path / "c.yaml"), str(tmp_path / "d.yaml")
    for name in (first, second):
        Path(name).write_text("{}\n", encoding="utf-8")
        os.chmod(name, 0o600)
    run(capsys, ["--config", first, "--verbose", "sessions"])
    ctx = CALLS[0][2]
    assert (ctx.config, ctx.verbose, ctx.quiet, ctx.json) == (first, True, False, False)
    run(capsys, ["--config", first, "sessions", "--config", second, "--quiet"])
    assert (CALLS[1][2].config, CALLS[1][2].quiet) == (second, True)


def test_dry_run_and_yes_reach_the_context(capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, ["prune", "--dry-run"])
    run(capsys, ["retro", "delete", "ra-abcd", "--yes"])
    assert CALLS[0][2].dry_run is True and CALLS[0][2].yes is False
    assert CALLS[1][2].yes is True and CALLS[1][2].dry_run is False


@pytest.mark.parametrize(
    "argv",
    [
        ["sessions", "--dry-run"],
        ["sessions", "--yes"],
        ["sessions", "--actor", "human:x"],
        ["validate", "--actor", "human:x"],
        ["show", SESSION, "--dry-run"],
        ["retro", "list", "--yes"],
        ["export", "usage", "--until", "1d"],
        ["sessions", "--bogus"],
        ["bogus"],
        ["sessions", "extra"],
    ],
)
def test_a_flag_a_command_does_not_have_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, [*argv, "--json"])
    assert code == 2
    assert envelope(out)["code"] == "usage"
    assert CALLS == []


def test_verbose_and_quiet_cannot_be_combined(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, ["sessions", "--verbose", "--quiet", "--json"])
    assert (code, envelope(out)["details"]) == (2, {"field": "--quiet"})
    code, out, _ = run(capsys, ["--verbose", "sessions", "--quiet", "--json"])
    assert code == 2


@pytest.mark.parametrize("value", ["", "a" * 4097, "x\x00y"])
def test_config_path_is_bounded(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, ["--config", value, "sessions", "--json"])
    assert (code, envelope(out)["details"]) == (2, {"field": "--config"})


def test_abbreviations_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--js", "sessions"], ["sessions", "--lim", "3"], ["sess"], ["retro", "li"]):
        code, out, _ = run(capsys, [*argv, "--json"])
        assert code == 2, argv


def test_actor_default_comes_from_the_terminal_state(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("session_audit.args.os_user", lambda: "j o$hn")
    monkeypatch.setattr("session_audit.config.os_user", lambda: "j o$hn")
    run(capsys, ["prune"], tty=False)
    run(capsys, ["prune"], tty=True)
    run(capsys, ["prune", "--actor", "human:given"], tty=True)
    assert [c[2].actor for c in CALLS] == ["auto:unknown", "human:j_o_hn", "human:given"]


def test_a_command_without_actor_has_none(capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, ["sessions"])
    assert CALLS[0][2].actor is None


def test_stdin_tty_state_defaults_to_the_real_stdin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["prune"], commands=STUBBED) == 0
    assert CALLS[0][2].stdin_is_tty is False  # pytest's stdin is not a terminal


# named usage errors ---------------------------------------------------------------------


def _usage(
    capsys: pytest.CaptureFixture[str], argv: list[str], *, tty: bool = False
) -> dict[str, Any]:
    code, out, err = run(capsys, [*argv, "--json"], tty=tty)
    assert code == 2, (argv, out, err)
    assert CALLS == []
    error = envelope(out)
    assert error["code"] == "usage"
    return error


@pytest.mark.parametrize("value", ["0", "1001", "-1", "abc", "1.5", "", "99999999999999999999"])
@pytest.mark.parametrize("command", [["sessions"], ["usage", "--by", "day"], ["retro", "list"]])
def test_limit_outside_1_to_1000_is_exit_2_naming_the_flag(
    command: list[str], value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _usage(capsys, [*command, "--limit", value])
    assert error["details"] == {"field": "--limit"}


def test_limit_default_is_100_and_bounds_are_inclusive(capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, ["sessions"])
    run(capsys, ["sessions", "--limit", "1"])
    run(capsys, ["sessions", "--limit", "1000"])
    assert [c[1].limit for c in CALLS] == [100, 1, 1000]


def test_mode_interactive_is_mode_unsupported(capsys: pytest.CaptureFixture[str]) -> None:
    error = _usage(capsys, ["retro", "run", "--session", SESSION[:8], "--mode", "interactive"])
    assert error["details"] == {"reason": "mode_unsupported"}
    assert_golden({"error": error}, "error-usage-mode-unsupported")


def test_any_other_mode_names_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    error = _usage(capsys, ["retro", "run", "--session", SESSION[:8], "--mode", HOSTILE])
    assert error["details"] == {"field": "--mode"}


@pytest.mark.parametrize(
    "argv",
    [["ingest", "--rebuild"], ["retro", "delete", "ra-abcd"]],
)
def test_confirmation_is_required_without_a_terminal_and_without_yes(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    error = _usage(capsys, argv)
    assert error["details"] == {"reason": "confirmation_required"}


def test_confirmation_golden(capsys: pytest.CaptureFixture[str]) -> None:
    error = _usage(capsys, ["ingest", "--rebuild"])
    assert_golden({"error": error}, "error-usage-confirmation-required")


@pytest.mark.parametrize(
    "argv",
    [["ingest", "--rebuild"], ["retro", "delete", "ra-abcd"]],
)
def test_a_terminal_or_yes_passes_the_confirmation_check(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, argv, tty=True)[0] == 0
    assert run(capsys, [*argv, "--yes"])[0] == 0


def test_rebuild_dry_run_needs_no_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, ["ingest", "--rebuild", "--dry-run"])[0] == 0


@pytest.mark.parametrize(
    "other", [["--path", "/x"], ["--since", "1h"], ["--session", "cs-abcdefgh"]]
)
def test_rebuild_with_path_since_or_session_is_a_usage_error(
    other: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    error = _usage(capsys, ["ingest", "--rebuild", "--yes", *other])
    assert error["details"] == {"field": "--rebuild"}


def test_ingest_dry_run_with_session_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    error = _usage(capsys, ["ingest", "--dry-run", "--session", "cs-abcdefgh"])
    assert error["details"] == {"field": "--dry-run"}


def test_ingest_session_must_be_a_claude_session_id_and_the_hint_names_the_mix_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["ingest", "--session", SESSION, "--json"])
    assert (code, envelope(out)["details"]) == (2, {"field": "--session"})
    assert "claude-session id" in err


def test_retro_run_session_cannot_be_a_claude_session_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["retro", "run", "--session", "cs-abcdefgh", "--json"])
    assert (code, envelope(out)["details"]) == (2, {"field": "--session"})
    assert "Claude Code session id" in err


def test_more_than_1000_session_flags_is_a_usage_error_and_1000_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    many = [x for i in range(1001) for x in ("--session", f"{i:08x}")]
    error = _usage(capsys, ["retro", "run", *many])
    assert error["details"] == {"field": "--session", "limit": 1000, "matched": 1001}
    assert run(capsys, ["retro", "run", *many[:2000]])[0] == 0
    assert len(CALLS[0][1].session) == 1000


def test_a_retro_run_without_a_scope_flag_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = _usage(capsys, ["retro", "run"])
    assert error["details"] == {}
    _usage(capsys, ["retro", "run", "--dry-run", "--again"])
    for scope in (["--label", "a=b"], ["--project", "/p"], ["--until", "1d"], ["--kind", "k"]):
        assert run(capsys, ["retro", "run", *scope])[0] == 0


@pytest.mark.parametrize(
    "skill", ["audit", "a:", ":b", "a:B", "a:b:c", "a b:c", "a/b:c", "-a:b", "a:-b", "é:b"]
)
def test_a_malformed_skill_names_the_flag(skill: str, capsys: pytest.CaptureFixture[str]) -> None:
    error = _usage(capsys, ["retro", "run", "--since", "1d", "--skill", skill])
    assert error["details"] == {"field": "--skill"}


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "0s"),
        ("--timeout", "7201"),
        ("--timeout", "2h"),
        ("--timeout", "abc"),
        ("--max-turns", "0"),
        ("--max-turns", "1001"),
        ("--max-turns", "x"),
        ("--decision-id", "a b"),
        ("--decision-id", ""),
        ("--model", "-x"),
        ("--model", "a\x07b"),
        ("--model", "a" * 129),
    ],
)
def test_retro_run_flags_out_of_range_name_the_flag(
    flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _usage(capsys, ["retro", "run", "--since", "1d", flag, value])
    assert error["details"] == {"field": flag}


@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "--budget", "0"],
        ["ingest", "--budget", "2h"],
        ["ingest", "--budget", "1h"],  # h is not a duration unit
        ["ingest", "--since", "5"],
        ["ingest", "--since", "007d"],
        ["ingest", "--path", ""],
        ["sessions", "--since", "tomorrow"],
        ["sessions", "--until", "2026-13-01"],
        ["sessions", "--label", "nokey"],
        ["sessions", "--label", "A=b"],
        ["sessions", "--kind", "Review"],
        ["sessions", "--outcome", "a b"],
        ["sessions", "--project", ""],
        ["sessions", "--entrypoint", "batch"],
        ["sessions", "--sort", "name"],
        ["usage", "--by", "day,week,month"],
        ["usage", "--by", "day,day"],
        ["usage", "--by", "color"],
        ["usage", "--by", "label="],
        ["usage", "--by", "label=A"],
        ["usage", "--by", ""],
        ["usage", "--by", "day", "--tz", "Not/AZone"],
        ["usage", "--by", "day", "--tz", "../etc"],
        ["retro", "list", "--state", "nope"],
        ["retro", "list", "--key", "abc"],
        ["retro", "list", "--skill", "bare"],
        ["retro", "list", "--session", "short"],
        ["cron", "install", "--ingest-every", "60"],
        ["cron", "install", "--ingest-every", "7m"],
        ["cron", "install", "--env-file", ""],
        ["ingest", "--actor", "root"],
    ],
    ids=lambda a: " ".join(a)[:60],
)
def test_a_bad_flag_value_is_exit_2_naming_that_flag(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    flag = next(a for a in reversed(argv[:-1]) if a.startswith("--"))
    error = _usage(capsys, argv)
    assert error["details"] == {"field": flag}


@pytest.mark.parametrize(
    ("argv", "field"),
    [
        (["show", "abc"], "SESSION_ID"),
        (["show", "cs-abcdefgh"], "SESSION_ID"),
        (["show", "ZZZZZZZZ"], "SESSION_ID"),
        (["retro", "show", "ra-abc"], "RETRO_ID"),
        (["retro", "show", "abcdefgh"], "RETRO_ID"),
        (["retro", "delete", "ra-abcd", "ra-x"], "RETRO_ID"),
    ],
)
def test_a_bad_id_or_prefix_names_its_argument(
    argv: list[str], field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _usage(capsys, argv)["details"] == {"field": field}


def test_a_missing_required_flag_or_argument(capsys: pytest.CaptureFixture[str]) -> None:
    assert _usage(capsys, ["usage"])["details"] == {"field": "--by"}
    assert _usage(capsys, ["show"])["details"] == {}
    assert _usage(capsys, ["retro", "delete"])["details"] == {}
    assert _usage(capsys, ["export"])["details"] == {}
    assert _usage(capsys, ["export", "nope"])["details"] == {}


def test_prefixes_are_lowercased_and_values_are_normalized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(capsys, ["show", SESSION[:8].upper()])
    run(capsys, ["retro", "show", "RA-ABCD"])
    run(capsys, ["usage", "--by", "label=team,day"])
    run(capsys, ["ingest", "--budget", "90s"])
    assert CALLS[0][1].session_id == SESSION[:8]
    assert CALLS[1][1].retro_id == "ra-abcd"
    assert CALLS[2][1].by == ["label=team", "day"]
    assert CALLS[3][1].budget.total_seconds() == 90


# the envelope: messages never carry input -------------------------------------------------

HOSTILE_ARGVS: list[list[str]] = [
    ["ingest", "--since", HOSTILE],
    ["ingest", "--session", HOSTILE],
    ["ingest", "--budget", HOSTILE],
    ["ingest", "--actor", HOSTILE],
    ["ingest", "--bogus" + HOSTILE],
    ["sessions", "--since", HOSTILE],
    ["sessions", "--label", HOSTILE],
    ["sessions", "--label", "a=" + HOSTILE],
    ["sessions", "--kind", HOSTILE],
    ["sessions", "--model", HOSTILE],
    ["sessions", "--sort", HOSTILE],
    ["sessions", "--entrypoint", HOSTILE],
    ["sessions", "--limit", HOSTILE],
    ["sessions", HOSTILE],
    ["usage", "--by", HOSTILE],
    ["usage", "--by", "label=" + HOSTILE],
    ["usage", "--by", "day", "--tz", HOSTILE],
    ["show", HOSTILE],
    ["retro", "run", "--session", HOSTILE],
    ["retro", "run", "--since", "1d", "--skill", HOSTILE],
    ["retro", "run", "--since", "1d", "--mode", HOSTILE],
    ["retro", "run", "--since", "1d", "--timeout", HOSTILE],
    ["retro", "run", "--since", "1d", "--decision-id", HOSTILE],
    ["retro", "list", "--state", HOSTILE],
    ["retro", "list", "--key", HOSTILE],
    ["retro", "show", HOSTILE],
    ["retro", "delete", HOSTILE],
    ["export", HOSTILE],
    ["cron", "install", "--ingest-every", HOSTILE],
    ["cron", HOSTILE],
    [HOSTILE],
    ["--" + HOSTILE],
    ["--config=" + HOSTILE + "x" * 5000],
]


@pytest.mark.parametrize(
    "argv", HOSTILE_ARGVS, ids=lambda a: " ".join(a)[:50].encode("ascii", "replace").decode()
)
def test_a_hostile_value_is_never_echoed_in_the_envelope_or_on_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, [*argv, "--json"])
    assert code == 2
    error = envelope(out)
    assert "EVIL" not in out and "\x1b" not in out and "‮" not in out
    assert "EVIL" not in err and "\x1b" not in err and "‮" not in err
    assert "\x07" not in err
    assert error["message"] and "EVIL" not in error["message"]


def test_huge_arguments_are_refused_quickly_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    big = "9" * 1_000_000
    for argv in (
        ["sessions", "--since", big],
        ["sessions", "--limit", big],
        ["sessions", "--project", big],
        ["sessions", "--label", "a=" + big],
        ["usage", "--by", big],
        ["show", big],
        ["retro", "show", "ra-" + big],
        ["ingest", "--path", big],
        ["--config", big, "sessions"],
        [big],
    ):
        code, out, err = run(capsys, [*argv, "--json"])
        assert code == 2, argv[:2]
        assert "9999" not in out and "9999" not in err
        envelope(out)


def test_surrogateescaped_argv_bytes_are_refused_or_accepted_without_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = os.fsdecode(b"\xff\xfe\x80")  # what argv holds for invalid UTF-8
    assert "\udcff" in bad
    for argv in (
        ["sessions", "--since", bad],
        ["sessions", "--label", bad],
        ["show", bad],
        ["usage", "--by", bad],
        [bad],
    ):
        code, out, err = run(capsys, [*argv, "--json"])
        assert code == 2
        envelope(out)
        out.encode("ascii")  # --json output is ASCII
        err.encode("utf-8")  # stderr text always encodes
    code, out, err = run(capsys, ["sessions", "--project", bad, "--json"])
    assert (code, out, err) == (0, "", "")  # a path may hold any bytes


def test_hostile_text_that_does_reach_a_message_is_sanitized_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing(ctx: Context, args: argparse.Namespace) -> int:
        raise CliError("internal", "bad \x1b[2J‮ thing", hint="hint \x1b[0m‮")

    command = Command(("boom",), "x", lambda p: None, lambda c, a: None, failing)
    code, out, err = run(capsys, ["boom", "--json"], commands=(command,))
    assert code == 1
    assert "\x1b" not in err and "‮" not in err
    assert "<U+001B>" in err and "<U+202E>" in err
    assert err.count("\n") == 2  # message and hint, one line each
    assert "\\u001b" in out and "\\u202e" in out  # --json is ASCII with escapes
    assert "\x1b" not in out


def test_multi_line_say_is_sanitized_line_by_line(capsys: pytest.CaptureFixture[str]) -> None:
    def say(ctx: Context, args: argparse.Namespace) -> int:
        ctx.say("one \x1b[2J\ntwo‮")
        return 0

    command = Command(("say",), "x", lambda p: None, lambda c, a: None, say)
    code, out, _ = run(capsys, ["say"], commands=(command,))
    assert (code, out) == (0, "one <U+001B>[2J\ntwo<U+202E>\n")


# the envelope on stdout ---------------------------------------------------------------------


def test_json_error_prints_only_the_envelope_and_the_same_message_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["sessions", "--limit", "0", "--json"])
    error = envelope(out)
    assert code == 2
    assert error["message"] in err
    assert "--limit" in err
    assert_golden({"error": error}, "error-usage-limit")


def test_without_json_nothing_is_printed_on_stdout_for_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["sessions", "--limit", "0"])
    assert (code, out) == (2, "")
    assert err.startswith("session-audit: error: number out of range (--limit)")


@pytest.mark.parametrize("sample", sorted(errors.EXIT_CODES))
def test_every_code_goes_through_one_report_with_its_exit(
    sample: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.test_errors import _samples

    error = _samples()[sample]

    def failing(ctx: Context, args: argparse.Namespace) -> int:
        raise error

    command = Command(("boom",), "x", lambda p: None, lambda c, a: None, failing)
    code, out, err = run(capsys, ["boom", "--json"], commands=(command,))
    assert code == error.exit_code
    assert json.loads(out) == error.envelope()
    validate(json.loads(out), ERROR_SCHEMA)
    assert err.startswith(f"session-audit: error: {error.message}")


def test_ambiguous_prints_its_matches_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    from tests.test_errors import _samples

    def failing(ctx: Context, args: argparse.Namespace) -> int:
        raise _samples()["ambiguous"]

    command = Command(("boom",), "x", lambda p: None, lambda c, a: None, failing)
    _, _, err = run(capsys, ["boom"], commands=(command,))
    assert "matches: a, b" in err


def test_an_unimplemented_command_validates_then_reports_an_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["validate", "--json"], commands=COMMANDS)
    assert code == 1
    assert envelope(out)["code"] == "internal"
    assert_golden(json.loads(out), "error-internal-not-implemented")
    code, out, _ = run(capsys, ["sessions", "--limit", "0", "--json"], commands=COMMANDS)
    assert code == 2  # flags are validated even before the implementation lands


def test_an_unexpected_exception_is_exit_1_without_its_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def crash(ctx: Context, args: argparse.Namespace) -> int:
        raise RuntimeError("secret detail \x1b[2J")

    command = Command(("boom",), "x", lambda p: None, lambda c, a: None, crash)
    code, out, err = run(capsys, ["boom", "--json"], commands=(command,))
    assert code == 1
    error = envelope(out)
    assert (error["code"], error["message"]) == ("internal", "internal error")
    assert "secret" not in out + err
    code, _, err = run(capsys, ["boom", "--verbose"], commands=(command,))
    assert code == 1
    assert "RuntimeError" in err and "secret detail <U+001B>[2J" in err
    assert "\x1b" not in err


def test_the_exit_code_of_run_is_the_exit_code_of_the_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = Command(("ok",), "x", lambda p: None, lambda c, a: None, lambda c, a: 7)
    assert run(capsys, ["ok"], commands=(command,))[0] == 7


# stdout failures ----------------------------------------------------------------------------


class _Broken(io.StringIO):
    def __init__(self, where: str, error: Exception) -> None:
        super().__init__()
        self.where, self.error = where, error

    def write(self, s: str) -> int:
        if self.where == "write":
            raise self.error
        return super().write(s)

    def flush(self) -> None:
        if self.where == "flush":
            raise self.error


@pytest.mark.parametrize("where", ["write", "flush"])
@pytest.mark.parametrize(
    "error",
    [BrokenPipeError(32, "Broken pipe"), OSError(9, "Bad file descriptor"), ValueError("closed")],
)
def test_a_failed_stdout_write_ends_with_exit_1(
    where: str, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _Broken(where, error)
    for argv in (["--version"], ["sessions", "--limit", "0", "--json"], ["sessions", "--help"]):
        code = main(argv, commands=STUBBED, stdout=lambda: stream)
        assert code == 1, argv
        assert "cannot write to standard output" in capsys.readouterr().err


def test_a_failed_stdout_write_wins_over_a_successful_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def prints(ctx: Context, args: argparse.Namespace) -> int:
        ctx.emit_json({"a": 1})
        return 0

    command = Command(("p",), "x", lambda p: None, lambda c, a: None, prints)
    stream = _Broken("write", BrokenPipeError())
    assert main(["p"], commands=(command,), stdout=lambda: stream) == 1
    good = io.StringIO()
    assert main(["p"], commands=(command,), stdout=lambda: good) == 0
    assert good.getvalue() == '{"a": 1}\n'


def test_a_closed_standard_output_object_is_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"], stdout=lambda: None) == 1
    closed = io.StringIO()
    closed.close()
    assert main(["--version"], stdout=lambda: closed) == 1


def test_output_degrades_instead_of_crashing_on_an_encoding_that_cannot_hold_the_text() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    Output(lambda: stream).write("café …\n")
    assert raw.getvalue() == b"caf? ?\n"


def test_output_wraps_every_failure() -> None:
    with pytest.raises(StdoutFailed):
        Output(lambda: _Broken("write", OSError(5, "io"))).write("x")


# signals --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)]
)
@pytest.mark.parametrize("json_mode", [False, True])
def test_an_interrupting_signal_exits_128_plus_n_and_prints_nothing(
    sig: signal.Signals, code: int, json_mode: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    cleaned: list[bool] = []

    def run_until_signaled(ctx: Context, args: argparse.Namespace) -> int:
        try:
            os.kill(os.getpid(), sig)
            return 0
        finally:
            cleaned.append(True)  # work is stopped cleanly on the way out

    command = Command(("w",), "x", lambda p: None, lambda c, a: None, run_until_signaled)
    before = {
        s: signal.getsignal(s)
        for s in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE)
    }
    argv = ["w", "--json"] if json_mode else ["w"]
    assert run(capsys, argv, commands=(command,)) == (code, "", "")
    assert cleaned == [True]
    after = {s: signal.getsignal(s) for s in before}
    assert after == before  # the handlers are put back


def test_a_keyboard_interrupt_raised_by_a_command_is_exit_130(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(ctx: Context, args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    command = Command(("w",), "x", lambda p: None, lambda c, a: None, interrupted)
    assert run(capsys, ["w", "--json"], commands=(command,)) == (130, "", "")


def test_the_exit_of_an_interrupt_is_not_an_internal_error_even_in_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def check(ctx: Context, args: argparse.Namespace) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    command = Command(("w",), "x", lambda p: None, check, lambda c, a: 0)
    assert run(capsys, ["w", "--json"], commands=(command,)) == (143, "", "")


def test_an_ignored_signal_stays_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    old = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        seen: list[Any] = []

        def look(ctx: Context, args: argparse.Namespace) -> int:
            seen.append(signal.getsignal(signal.SIGHUP))
            return 0

        command = Command(("w",), "x", lambda p: None, lambda c, a: None, look)
        run(capsys, ["w"], commands=(command,))
        assert seen == [signal.SIG_IGN]
    finally:
        signal.signal(signal.SIGHUP, old)


# real processes -------------------------------------------------------------------------------


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)  # PATH keeps the tripwire directory first
    env.update(extra)
    return env


def _module(*args: str | bytes) -> list[str | bytes]:
    return [sys.executable, "-m", "session_audit", *args]


def _run_process(
    args: list[str | bytes], *, env: dict[str, str] | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        args,
        env=env or _env(),
        stdin=subprocess.DEVNULL,
        capture_output=kwargs.pop("capture_output", True),
        timeout=60,
        **kwargs,
    )


def test_the_module_runs_and_prints_the_version() -> None:
    result = _run_process(_module("--version"))
    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout.decode() == f"session-audit {__version__}\n"


def test_the_console_script_target_is_main() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["session-audit"] == "session_audit.cli:main"


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["validate", "--json"],
        ["sessions", "--help"],
        ["sessions", "--limit", "0", "--json"],
    ],
)
def test_a_closed_pipe_on_stdout_ends_with_exit_1_and_no_traceback(argv: list[str]) -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody reads: every write is EPIPE
    try:
        result = _run_process(
            _module(*argv), stdout=write_end, capture_output=False, stderr=subprocess.PIPE
        )
    finally:
        os.close(write_end)
    assert result.returncode == 1, result.stderr
    stderr = result.stderr.decode()
    assert "Traceback" not in stderr and "Exception ignored" not in stderr
    assert "cannot write to standard output" in stderr


def test_a_closed_standard_output_descriptor_ends_with_exit_1() -> None:
    result = _run_process(
        ["sh", "-c", 'exec "$@" >&-', "sh", sys.executable, "-m", "session_audit", "--version"]
    )
    assert result.returncode == 1, result.stderr
    assert b"Traceback" not in result.stderr
    assert b"cannot write to standard output" in result.stderr


def test_a_closed_standard_error_descriptor_does_not_crash() -> None:
    result = _run_process(
        [
            "sh",
            "-c",
            'exec "$@" 2>&-',
            "sh",
            sys.executable,
            "-m",
            "session_audit",
            "sessions",
            "--limit",
            "0",
            "--json",
        ]
    )
    assert result.returncode == 2
    envelope(result.stdout.decode())


def test_sigpipe_default_disposition_in_the_parent_does_not_kill_the_run() -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = _run_process(
            _module("--version"),
            stdout=write_end,
            capture_output=False,
            stderr=subprocess.PIPE,
            restore_signals=True,
        )
    finally:
        os.close(write_end)
    assert result.returncode == 1  # not -13 (killed by SIGPIPE)


@pytest.mark.parametrize("locale", ["C", "POSIX", "C.UTF-8"])
def test_hostile_argv_bytes_under_the_c_locale(locale: str) -> None:
    result = _run_process(
        _module(b"sessions", b"--since", b"\xff\xfe\x1b[31m\x80", b"--json"),
        env=_env(LC_ALL=locale, LANG=locale, PYTHONUTF8="", PYTHONCOERCECLOCALE=""),
    )
    assert result.returncode == 2, result.stderr
    result.stdout.decode("ascii")
    error = envelope(result.stdout.decode("ascii"))
    assert error["details"] == {"field": "--since"}
    assert b"\x1b" not in result.stderr
    result.stderr.decode("utf-8")


def test_a_path_with_invalid_utf8_is_accepted_under_the_c_locale() -> None:
    result = _run_process(
        _module(b"sessions", b"--project", b"/p/\xff", b"--json"),
        env=_env(LC_ALL="C"),
    )
    # The command is a stub here: reaching it (exit 1, internal) means the argv was accepted.
    assert result.returncode == 1
    assert envelope(result.stdout.decode("ascii"))["code"] == "internal"


def test_an_ascii_only_stdout_and_stderr_still_print_everything() -> None:
    result = _run_process(
        _module("sessions", "--limit", "0"),
        env=_env(LC_ALL="C", PYTHONIOENCODING="ascii", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"),
    )
    assert result.returncode == 2
    assert b"error" in result.stderr


def test_a_real_sigterm_stops_a_running_command_with_exit_143(tmp_path: Path) -> None:
    script = tmp_path / "wait.py"
    script.write_text(
        "import sys, threading\n"
        "from session_audit.cli import main\n"
        "from session_audit.command import Command\n"
        "def run(ctx, args):\n"
        "    print('ready', file=sys.stderr, flush=True)\n"
        "    threading.Event().wait(60)\n"
        "    return 0\n"
        "c = Command(('w',), 'x', lambda p: None, lambda c, a: None, run)\n"
        "raise SystemExit(main(['w', '--json'], commands=(c,)))\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script)],
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        assert process.stderr.readline() == b"ready\n"
        process.send_signal(signal.SIGTERM)  # the exact PID we started
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert (process.returncode, out, err) == (143, b"", b"")


def test_the_envelope_is_the_same_document_in_a_real_process() -> None:
    result = _run_process(
        _module("retro", "run", "--since", "1d", "--mode", "interactive", "--json")
    )
    assert result.returncode == 2
    error = envelope(result.stdout.decode())
    assert error["details"] == {"reason": "mode_unsupported"}
    assert result.stderr.decode().startswith("session-audit: error:")


# the fixed text follows the kind of the argparse message, never the user's words -----------------


@pytest.mark.parametrize(
    ("argv", "message", "field"),
    [
        (["sessions", "--foo", "invalid choice"], "Unrecognized argument", None),
        (["sessions", "--bogus", "expected one argument"], "Unrecognized argument", None),
        (
            ["retro", "run", "--session", "11111111", "x ignored explicit argument"],
            "Unrecognized argument",
            None,
        ),
        (["sessions", "--bogus", "not allowed with argument"], "Unrecognized argument", None),
        (["sessions", "--since"], "An option is missing its value", "--since"),
        (["sessions", "--since", "--limit", "2"], "An option is missing its value", "--since"),
        (["sessions", "--entrypoint", "required"], "Invalid choice", "--entrypoint"),
        (["sessions", "--entrypoint", "expected one argument"], "Invalid choice", "--entrypoint"),
        (["sessions", "--json=ignored explicit argument"], "An option takes no value", "--json"),
        (["nope", "unrecognized arguments:"], "Invalid choice", None),
    ],
)
def test_the_usage_text_is_chosen_by_the_message_kind_not_by_user_words(
    argv: list[str], message: str, field: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, [*argv, "--json"])
    assert code == 2, (out, err)
    error = envelope(out)
    assert error["message"] == message
    assert error.get("details", {}).get("field") == field


@pytest.mark.parametrize("skill", ["My_Plugin:audit", "a.b:c", "A1:b-2", "x" * 64 + ":" + "y" * 63])
def test_a_skill_with_a_wider_plugin_part_is_accepted(
    skill: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, ["retro", "run", "--since", "1d", "--skill", skill])[0] == 0
    assert run(capsys, ["retro", "list", "--skill", skill])[0] == 0


@pytest.mark.parametrize("skill", ["a" * 64 + ":" + "b" * 65, "a" * 128 + ":b", "a:B", "_a:b"])
def test_a_skill_beyond_the_schema_grammar_is_refused(
    skill: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _usage(capsys, ["retro", "run", "--since", "1d", "--skill", skill])["details"] == {
        "field": "--skill"
    }
    assert _usage(capsys, ["retro", "list", "--skill", skill])["details"] == {"field": "--skill"}


@pytest.mark.parametrize("model", ["claude opus 4", "-x", "é" * 128, "a" * 128])
def test_a_model_is_any_text_without_controls_for_the_filters(
    model: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in (["sessions"], ["usage", "--by", "day"]):
        assert run(capsys, [*command, f"--model={model}"])[0] == 0


@pytest.mark.parametrize(
    "model", ["", "a" * 129, "a\x00b", "a\x1bb", "a\x7fb", "a\x85b", "a\ud800b"]
)
def test_a_model_with_controls_or_too_long_is_refused(
    model: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in (["sessions"], ["usage", "--by", "day"], ["retro", "run", "--since", "1d"]):
        assert _usage(capsys, [*command, "--model", model])["details"] == {"field": "--model"}


def test_retro_run_also_refuses_a_model_starting_with_a_dash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _usage(capsys, ["retro", "run", "--since", "1d", "--model", "-x"])["details"] == {
        "field": "--model"
    }
    assert run(capsys, ["retro", "run", "--since", "1d", "--model", "x -y"])[0] == 0


# the exit contract of main --------------------------------------------------------------------


def _with_run(fn: Any, check: Any = None) -> Command:
    return Command(("w",), "x", lambda p: None, check or (lambda c, a: None), fn)


@pytest.mark.parametrize("value", ["oops", 256, -1, 1000, None, 1.0, True, b"x", [1]])
def test_a_run_result_outside_0_to_255_is_an_internal_error(
    value: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    command = _with_run(lambda ctx, args: value)
    code, out, err = run(capsys, ["w", "--json"], commands=(command,))
    assert code == 1
    assert envelope(out)["code"] == "internal"


@pytest.mark.parametrize("value", [0, 1, 20, 255])
def test_a_run_result_in_range_is_the_exit_code(
    value: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, ["w"], commands=(_with_run(lambda ctx, args: value),))[0] == value


@pytest.mark.parametrize(
    ("raised", "code"), [(None, 0), (0, 0), (3, 3), (255, 255), ("oops", 1), (256, 1), (-1, 1)]
)
def test_system_exit_codes_are_validated(
    raised: Any, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    def fn(ctx: Context, args: argparse.Namespace) -> int:
        raise SystemExit(raised)

    got, out, err = run(capsys, ["w", "--json"], commands=(_with_run(fn),))
    assert got == code
    if code == 1:
        assert envelope(out)["code"] == "internal"
    else:
        assert out == ""


class _Custom(BaseException):
    pass


@pytest.mark.parametrize("raised", [GeneratorExit, _Custom, RuntimeError])
def test_any_other_base_exception_is_an_internal_error_without_a_traceback(
    raised: type[BaseException], capsys: pytest.CaptureFixture[str]
) -> None:
    def fn(ctx: Context, args: argparse.Namespace) -> int:
        raise raised("secret text")

    code, out, err = run(capsys, ["w", "--json"], commands=(_with_run(fn),))
    assert code == 1
    assert envelope(out)["code"] == "internal"
    assert "Traceback" not in err and "secret" not in err and "secret" not in out


def test_a_second_signal_while_the_first_unwinds_does_not_traceback(tmp_path: Path) -> None:
    script = tmp_path / "twice.py"
    script.write_text(
        "import sys, time\n"
        "from session_audit.cli import main\n"
        "from session_audit.command import Command\n"
        "def run(ctx, args):\n"
        "    print('ready', file=sys.stderr, flush=True)\n"
        "    try:\n"
        "        time.sleep(60)\n"
        "    finally:\n"
        "        time.sleep(1)\n"  # slow cleanup: the second signal lands while unwinding
        "    return 0\n"
        "c = Command(('w',), 'x', lambda p: None, lambda c, a: None, run)\n"
        "raise SystemExit(main(['w', '--json'], commands=(c,)))\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script)],
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        assert process.stderr.readline() == b"ready\n"
        process.send_signal(signal.SIGTERM)  # the exact PID we started
        time.sleep(0.3)
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert (process.returncode, out, err) == (143, b"", b"")


# a command's progress note on a broken stderr -------------------------------------------------


def _note_script(tmp_path: Path) -> Path:
    script = tmp_path / "note.py"
    script.write_text(
        "from session_audit.cli import main\n"
        "from session_audit.command import Command\n"
        "def run(ctx, args):\n"
        "    ctx.note('progress')\n"
        "    ctx.emit_json({'ok': True})\n"
        "    return 0\n"
        "c = Command(('w',), 'x', lambda p: None, lambda c, a: None, run)\n"
        "raise SystemExit(main(['w'], commands=(c,)))\n",
        encoding="utf-8",
    )
    return script


def test_a_note_on_a_closed_stderr_does_not_fail_the_command(tmp_path: Path) -> None:
    script = _note_script(tmp_path)
    result = _run_process(["sh", "-c", 'exec "$@" 2>&-', "sh", sys.executable, str(script)])
    assert (result.returncode, result.stdout) == (0, b'{"ok": true}\n')


def test_a_note_on_a_full_stderr_does_not_fail_the_command(tmp_path: Path) -> None:
    if not Path("/dev/full").exists():
        pytest.skip("no /dev/full")
    script = _note_script(tmp_path)
    with open("/dev/full", "wb") as full:
        result = _run_process(
            _module_script(script), stderr=full, capture_output=False, stdout=subprocess.PIPE
        )
    assert (result.returncode, result.stdout) == (0, b'{"ok": true}\n')


def _module_script(script: Path) -> list[str | bytes]:
    return [sys.executable, str(script)]
