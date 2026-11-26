"""The test helpers every later issue uses: schema validation, golden files, the environment
and process guards, and the fake gateway."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import ValidationError

from session_audit.gateway import CallResult, CallStatus

from .conftest import FAKEBIN, RealToolRefused, guard_argv
from .fake_gateway import EPOCH, FakeClock, FakeGateway
from .golden import assert_golden, schema_id_for
from .procs import wait_until_gone
from .schemas import OWN_SCHEMA_ROOT, PINNED_SCHEMA_ROOT, validate

GOOD_ERROR = {"error": {"code": "usage", "message": "unknown command", "details": {}}}


# -- schemas ---------------------------------------------------------------------------------


def test_validate_accepts_a_document_of_this_tool() -> None:
    validate(GOOD_ERROR, "session-audit/cli/error.json")  # also resolves ../defs.json


def test_validate_rejects_a_bad_document_of_this_tool() -> None:
    with pytest.raises(ValidationError):
        validate({"error": {"code": "nope", "message": "x"}}, "session-audit/cli/error.json")


def test_validate_knows_the_pinned_claude_session_schemas() -> None:
    with pytest.raises(ValidationError):
        validate({}, "claude-session/cli/start.json")


def test_every_pinned_schema_is_valid_and_has_a_pinned_id() -> None:
    from jsonschema import Draft202012Validator

    paths = sorted(PINNED_SCHEMA_ROOT.rglob("*.json"))
    assert paths
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)
        assert document["$id"].startswith("claude-session/cli/")


def test_format_checks_are_on() -> None:
    document = {
        "csession_id": "cs-aaaaaaab",
        "csession_dir": "/d",
        "argv": ["a", "b", "c", "d", "e", "f"],
        "run": 1,
        "forked_from": None,
        "expires_at": "not a date",
        "required_env_names": [],
        "env_inherit": False,
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        validate(document, "claude-session/cli/start.json")


# -- golden files ----------------------------------------------------------------------------


def test_schema_id_for_picks_the_longest_matching_schema() -> None:
    assert schema_id_for("error-usage") == "session-audit/cli/error.json"
    assert schema_id_for("hooks/pre-retro-basic") == "session-audit/hooks/pre-retro.json"
    with pytest.raises(LookupError):
        schema_id_for("no-such-command-case")


def test_golden_passes_on_equal_output(tmp_path: Path) -> None:
    (tmp_path / "error-usage.json").write_text(json.dumps(GOOD_ERROR), encoding="utf-8")
    assert_golden(GOOD_ERROR, "error-usage", root=tmp_path)


def test_golden_fails_on_different_output(tmp_path: Path) -> None:
    (tmp_path / "error-usage.json").write_text(json.dumps(GOOD_ERROR), encoding="utf-8")
    other = {"error": {"code": "usage", "message": "something else", "details": {}}}
    with pytest.raises(AssertionError):
        assert_golden(other, "error-usage", root=tmp_path)


def test_golden_validates_the_real_output_before_comparing(tmp_path: Path) -> None:
    bad = {"error": {"code": "nope", "message": "x"}}
    (tmp_path / "error-bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        assert_golden(bad, "error-bad", root=tmp_path)


def test_golden_normalizes_after_validation(tmp_path: Path) -> None:
    (tmp_path / "error-usage.json").write_text(json.dumps(GOOD_ERROR), encoding="utf-8")
    volatile = {"error": {"code": "usage", "message": "unknown command at 12:01", "details": {}}}
    assert_golden(volatile, "error-usage", root=tmp_path, normalize=lambda _: GOOD_ERROR)


def test_golden_update_rewrites_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    assert_golden(GOOD_ERROR, "error-new", root=tmp_path)
    assert json.loads((tmp_path / "error-new.json").read_text(encoding="utf-8")) == GOOD_ERROR


def test_golden_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        assert_golden(GOOD_ERROR, "error-missing", root=tmp_path)


def test_own_schemas_have_ids_under_session_audit() -> None:
    ids = [
        json.loads(p.read_text(encoding="utf-8"))["$id"] for p in OWN_SCHEMA_ROOT.rglob("*.json")
    ]
    assert ids and all(i.startswith("session-audit/") for i in ids)


# -- environment and process guards ----------------------------------------------------------


def test_home_and_xdg_dirs_are_private_to_the_test(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp().resolve()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(os.environ[name]).resolve().is_relative_to(base), name
    assert Path.home().resolve().is_relative_to(base)
    assert "XDG_RUNTIME_DIR" not in os.environ and "CLAUDE_CONFIG_DIR" not in os.environ


@pytest.mark.parametrize("name", ["crontab", "claude", "claude-session"])
def test_starting_a_real_protected_program_by_name_is_refused(name: str) -> None:
    with pytest.raises(RealToolRefused):
        subprocess.Popen([name])


@pytest.mark.parametrize(
    "path", ["/usr/bin/crontab", "/usr/local/bin/claude", "/opt/x/claude-session"]
)
def test_starting_a_protected_program_by_absolute_path_is_refused(path: str) -> None:
    with pytest.raises(RealToolRefused):
        subprocess.Popen([path])


def test_a_symlink_under_another_name_to_a_protected_program_is_refused(tmp_path: Path) -> None:
    # The guard refuses before anything is started, so the real crontab is never run (the link
    # may even dangle).
    alias = tmp_path / "harmless-name"
    os.symlink("/usr/bin/crontab", alias)
    with pytest.raises(RealToolRefused):
        guard_argv([str(alias), "-l"], {}, [tmp_path])
    with pytest.raises(RealToolRefused):
        subprocess.Popen([str(alias), "-l"])
    with pytest.raises(RealToolRefused):
        guard_argv(["sh", "-c", f"'{alias}' -l"], {"PATH": str(tmp_path)}, [tmp_path], [tmp_path])


def test_a_bare_name_that_resolves_to_a_protected_program_is_refused(tmp_path: Path) -> None:
    alias = tmp_path / "harmless-name"
    os.symlink("/usr/bin/crontab", alias)
    with pytest.raises(RealToolRefused):
        guard_argv(["harmless-name", "-l"], {"PATH": str(tmp_path)}, [tmp_path])
    with pytest.raises(RealToolRefused):
        guard_argv(
            ["sh", "-c", "harmless-name -l"], {"PATH": str(tmp_path)}, [tmp_path], [tmp_path]
        )


def test_every_absolute_form_of_the_real_crontab_is_refused_by_the_guard() -> None:
    for path in (
        "/usr/bin/crontab",
        "/bin/crontab",
        "/usr/bin/../bin/crontab",
        "//usr/bin/crontab",
    ):
        with pytest.raises(RealToolRefused):
            guard_argv([path, "-l"], {}, [FAKEBIN])
        with pytest.raises(RealToolRefused):
            subprocess.Popen([path, "-l"])


def test_the_fakes_under_fakebin_and_the_test_directory_are_allowed(tmp_path: Path) -> None:
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    guard_argv([str(FAKEBIN / "claude-session"), "--version"], {}, [FAKEBIN, tmp_path])
    guard_argv([str(fake)], {}, [FAKEBIN, tmp_path])
    assert subprocess.run([str(fake)], check=False).returncode == 0


def test_the_tripwire_directory_is_not_an_allowed_fake(tmp_path: Path) -> None:
    trip = tmp_path / "trip"
    trip.mkdir()
    (trip / "crontab").write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    with pytest.raises(RealToolRefused):
        guard_argv([str(trip / "crontab")], {}, [tmp_path], [trip])


def test_other_programs_are_not_guarded() -> None:
    guard_argv(["true"], {}, [])
    assert subprocess.run(["true"], check=False).returncode == 0


def test_a_protected_name_that_does_not_exist_is_left_to_fail_by_itself(tmp_path: Path) -> None:
    guard_argv(["claude"], {"PATH": str(tmp_path)}, [])


def test_the_tripwire_fails_loudly_if_a_script_gets_past_the_popen_guard(tmp_path: Path) -> None:
    # The guard cannot read a script file, so the script reaches the tripwire through PATH.
    script = tmp_path / "runner"
    script.write_text("#!/bin/sh\nclaude-session --version\n", encoding="utf-8")
    script.chmod(0o755)
    result = subprocess.run([str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 97 and "tripwire" in result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "claude-session --version"],
        ["sh", "-ec", "crontab -l"],
        ["bash", "-lc", "claude x"],
        ["bash", "-c", "true; crontab -l"],
        ["sh", "-c", "x=$(crontab -l); echo $x"],
        ["sh", "-c", "'/usr/bin/crontab' -l"],
        ["env", "crontab", "-l"],
        ["env", "FOO=1", "claude", "-p", "hi"],
        ["nice", "claude-session", "list"],
    ],
)
def test_a_protected_program_hidden_behind_another_is_refused(argv: list[str]) -> None:
    with pytest.raises(RealToolRefused):
        subprocess.Popen(argv)


def test_a_protected_name_in_a_shell_command_line_is_refused() -> None:
    with pytest.raises(RealToolRefused):
        subprocess.Popen("echo hi; crontab -l", shell=True)


def test_the_guard_looks_through_argv_and_shell_strings(tmp_path: Path) -> None:
    trip = tmp_path / "trip"
    trip.mkdir()
    (trip / "crontab").write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    (trip / "crontab").chmod(0o755)
    (trip / "claude-session").write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    (trip / "claude-session").chmod(0o755)
    with pytest.raises(RealToolRefused):
        guard_argv(["env", "crontab", "-l"], {"PATH": str(trip)}, [tmp_path], [trip])
    with pytest.raises(RealToolRefused):
        guard_argv(["sh", "-c", "claude-session --version"], {"PATH": str(trip)}, [FAKEBIN], [trip])


def test_a_shell_with_a_minimal_env_is_refused(tmp_path: Path) -> None:
    trip = tmp_path / "trip"
    trip.mkdir()
    for argv in (["sh", "-c", "true"], ["env", "true"], [sys.executable, "-c", "pass"]):
        with pytest.raises(RealToolRefused):
            guard_argv(argv, {"PATH": "/usr/bin"}, [FAKEBIN], [trip])
        with pytest.raises(RealToolRefused):
            guard_argv(argv, {}, [FAKEBIN], [trip])
    with pytest.raises(RealToolRefused):
        subprocess.Popen(["sh", "-c", "true"], env={"PATH": "/usr/bin:/bin"})
    with pytest.raises(RealToolRefused):
        subprocess.Popen(["sh", "-c", "true"], env={})


def test_a_shell_whose_env_keeps_the_tripwire_first_is_allowed() -> None:
    env = {"PATH": os.environ["PATH"]}
    assert subprocess.run(["sh", "-c", "true"], env=env, check=False).returncode == 0
    assert subprocess.run(["sh", "-c", "true"], check=False).returncode == 0  # env=None inherits


def test_a_process_left_running_fails_the_test_and_is_killed(pytester: pytest.Pytester) -> None:
    root = Path(__file__).resolve().parent.parent
    pytester.makeconftest(
        f"""
        import sys
        sys.path.insert(0, {str(root)!r})
        from tests.conftest import _private_environment, _process_guard  # noqa: F401
        """
    )
    pytester.makepyfile(
        f"""
        import subprocess, sys, pathlib

        def test_leaks():
            cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
            p = subprocess.Popen(cmd, process_group=0)
            pathlib.Path({str(pytester.path / "pid")!r}).write_text(str(p.pid))
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*a test left processes running*"])
    assert wait_until_gone(int((pytester.path / "pid").read_text(encoding="utf-8")))


# -- fake gateway ----------------------------------------------------------------------------


def test_fake_gateway_answers_scripted_calls_in_order_and_records_them() -> None:
    gw = FakeGateway()
    gw.on(
        ["claude-session", "list"],
        CallResult(CallStatus.EXITED, 0, "a"),
        CallResult(CallStatus.TIMEOUT),
    )
    assert gw.run(["claude-session", "list", "--json"], timeout=5).stdout == "a"
    assert gw.run(["claude-session", "list"], timeout=5).status is CallStatus.TIMEOUT
    assert (
        gw.run(["claude-session", "list"], timeout=5).status is CallStatus.TIMEOUT
    )  # last repeats
    assert [c["timeout"] for c in gw.calls] == [5, 5, 5]


def test_fake_gateway_refuses_an_unscripted_call() -> None:
    with pytest.raises(AssertionError, match="unscripted"):
        FakeGateway().run(["git", "status"], timeout=5)


def test_fake_gateway_clock_moves_only_when_told() -> None:
    gw = FakeGateway()
    assert gw.now() == EPOCH
    gw.clock.advance(90)
    assert gw.now() == EPOCH + timedelta(seconds=90)
    with pytest.raises(ValueError):
        gw.clock.advance(-1)
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001 (the point of the test)
    assert FakeClock().now().tzinfo is UTC


def test_fake_gateway_crontab_round_trips() -> None:
    gw = FakeGateway()
    gw.crontab_write("0 * * * * job\n")
    assert gw.crontab_read().stdout == "0 * * * * job\n"


# -- the live marker -------------------------------------------------------------------------


def test_live_tests_are_deselected_by_default_and_selected_with_m_live(
    pytester: pytest.Pytester,
) -> None:
    import tomllib

    root = Path(__file__).resolve().parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    # json.dumps writes valid TOML strings and arrays; only the marker and addopts are copied.
    pytester.makepyprojecttoml(
        "[tool.pytest.ini_options]\n"
        f"markers = {json.dumps(options['markers'])}\n"
        f"addopts = {json.dumps(options['addopts'])}\n"
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.live
        def test_needs_a_real_model():
            pass

        def test_runs_everywhere():
            pass
        """
    )
    default = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-v")
    default.assert_outcomes(passed=1, deselected=1)
    default.stdout.fnmatch_lines(["*test_runs_everywhere PASSED*"])
    live = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-v", "-m", "live")
    live.assert_outcomes(passed=1, deselected=1)
    live.stdout.fnmatch_lines(["*test_needs_a_real_model PASSED*"])
