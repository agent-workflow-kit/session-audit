"""The test helpers every later issue uses: schema validation, golden files, the environment
and process guards, and the fake gateway."""

from __future__ import annotations

import json
import os
import subprocess
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


def test_the_tripwire_fails_loudly_if_a_shell_script_gets_past_the_popen_guard() -> None:
    # Reach the tripwire the way the Popen guard cannot see: through a shell's PATH lookup.
    result = subprocess.run(
        ["sh", "-c", "claude-session --version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 97 and "tripwire" in result.stderr


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
