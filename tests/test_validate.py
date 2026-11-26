"""``validate`` (issue #33): every check and warning, the three exits, the read-only rule and
``--probe`` against the fake claude-session."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import plugins
from session_audit.commands import cron
from session_audit.commands import validate as validate_module
from session_audit.commands.validate import env_file_of
from session_audit.config import ScheduleRetro, ScheduleScope
from session_audit.gateway import CallResult, CallStatus
from session_audit.grammar import IngestEvery

from .fake_claude_session import FakeClaudeSession, error_doc, kinds_doc, step, version_step
from .golden import assert_golden
from .ingesthelp import state_dir_of
from .recoveryhelp import RECENT_MS, orphan, plant_session
from .schemas import validate as validate_schema
from .validatehelp import (
    ValidateGateway,
    check_of,
    hold_wal,
    install,
    listing,
    make_newer,
    make_pending,
    make_store,
    now_ms,
    reasons,
    set_meta,
    snapshot,
    validate,
    write_config,
)

SCHEMA = "session-audit/cli/validate.json"
PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

ALL_CHECKS = [
    "config",
    "config_ownership",
    "transcript_roots",
    "store",
    "price_table",
    "permission_profile",
    "plugin",
    "claude_session",
]


@pytest.fixture
def gw() -> ValidateGateway:
    return ValidateGateway()


@pytest.fixture
def home(_private_environment: Path) -> Path:
    install(_private_environment)
    return _private_environment


def ok_doc(doc: dict[str, Any]) -> None:
    assert doc["ok"] is True, doc
    assert doc["warnings"] == [], doc


def managed_block(env_file: str | None = None, interval: str = "1h") -> str:
    retro = ScheduleRetro(scope=ScheduleScope(since="7d"))
    block = cron.build_block(
        IngestEvery(60 if interval == "1h" else 1440), retro, "/usr/bin", env_file
    )
    return "MAILTO=x\n" + "\n".join(block) + "\n"


# --- the document ------------------------------------------------------------------------------


def test_a_clean_install_is_ok(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    run = validate(capsys, gw)
    assert run.code == 0, run.out + run.err
    assert [c["name"] for c in run.doc["checks"]] == ALL_CHECKS
    ok_doc(run.doc)
    assert_golden(run.doc, "validate-ok")


def test_nothing_is_written_for_a_clean_install(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    before = snapshot(home / ".local", home / ".config")
    assert validate(capsys, gw).code == 0
    assert snapshot(home / ".local", home / ".config") == before
    assert not state_dir_of(home).exists()  # a missing store is not created


def test_the_human_output_lists_every_check(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    run = validate(capsys, gw, json_output=False)
    assert run.code == 0
    assert "config: ok" in run.out and "claude_session: ok" in run.out


# --- config ------------------------------------------------------------------------------------


def test_an_invalid_config_is_exit_2_with_the_document(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "retro: {max_turns: SEKRET-VALUE}\n")
    run = validate(capsys, gw)
    assert run.code == 2
    assert "SEKRET" not in run.out + run.err
    assert reasons(run.doc) == {
        "config": "config_invalid",
        "transcript_roots": "config_invalid",
        "price_table": "config_invalid",
        "permission_profile": "config_invalid",
        "plugin": "config_invalid",
    }
    assert run.doc["ok"] is False
    assert_golden(run.doc, "validate-config_invalid")


def test_an_invalid_config_still_checks_the_store_and_claude_session_on_defaults(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "not: [valid\n")
    make_newer(home)
    gw.version = CallResult(CallStatus.EXITED, 0, stdout="claude-session 0.1.0\n")
    run = validate(capsys, gw)
    assert run.code == 2
    assert check_of(run.doc, "store")["reason"] == "store_newer"
    assert check_of(run.doc, "claude_session")["reason"] == "claude_session_too_old"


def test_an_unsafe_config_is_exit_12_config_unsafe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "limits: {retros_per_day: 3}\n", mode=0o666)
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"config_ownership": "config_unsafe"}
    assert_golden(run.doc, "validate-config_unsafe")


def test_an_invalid_and_unsafe_config_is_exit_2(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "not: [valid\n", mode=0o666)
    run = validate(capsys, gw)
    assert run.code == 2
    assert check_of(run.doc, "config_ownership")["reason"] == "config_unsafe"
    assert check_of(run.doc, "config")["reason"] == "config_invalid"


# --- the transcript roots ----------------------------------------------------------------------


def test_no_readable_root_fails(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, f"transcript_roots: ['{home}/nowhere']\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"transcript_roots": "no_readable_roots"}
    assert run.doc["warnings"] == []


def test_one_unusable_root_among_usable_ones_is_a_warning(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, f"transcript_roots: ['{home}/.claude/projects', '{home}/nowhere']\n")
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["root_unusable"]


def test_a_group_writable_root_is_unusable(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    os.chmod(home / ".claude" / "projects", 0o770)
    run = validate(capsys, gw)
    assert run.code == 12
    assert check_of(run.doc, "transcript_roots")["reason"] == "no_readable_roots"


# --- the store ---------------------------------------------------------------------------------


def test_a_corrupt_store_fails_store_unreadable(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    directory = state_dir_of(home)
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.db").write_bytes(b"this is not a database" * 100)
    os.chmod(directory / "state.db", 0o600)
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"store": "store_unreadable"}


def test_an_unsafe_state_directory_fails_store_unreadable(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    make_store(home)
    os.chmod(state_dir_of(home), 0o777)
    run = validate(capsys, gw)
    assert run.code == 12
    assert check_of(run.doc, "store")["reason"] == "store_unreadable"


def test_a_newer_store_fails_store_newer_and_is_not_touched(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    make_newer(home)
    before = snapshot(state_dir_of(home))
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"store": "store_newer"}
    assert snapshot(state_dir_of(home)) == before


def test_a_pending_migration_is_a_warning_and_is_never_applied(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    make_pending(home)
    before = snapshot(state_dir_of(home))
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["migration_pending"]
    assert snapshot(state_dir_of(home)) == before


def test_a_store_with_a_live_wal_is_left_byte_identical(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    close = hold_wal(home)
    try:
        directory = state_dir_of(home)
        assert (directory / "state.db-wal").exists()
        before = snapshot(directory)
        listing_before = listing(directory)
        wal_bytes = (directory / "state.db-wal").read_bytes()
        run = validate(capsys, gw)
        assert run.code == 0, run.out + run.err
        after = snapshot(directory)
        for name in ("state.db", "state.db-wal"):
            path = str(directory / name)
            assert after[path] == before[path], name
        assert (directory / "state.db-wal").read_bytes() == wal_bytes
        assert set(after) == set(before)
        assert listing(directory) == listing_before
        # an existing -shm is shared memory readers touch (inherent): only its content may move
    finally:
        close()


def test_a_wal_without_a_shm_fails_store_unreadable_and_creates_nothing(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    close = hold_wal(home)
    try:
        directory = state_dir_of(home)
        (directory / "state.db-shm").unlink()
        assert (directory / "state.db-wal").stat().st_size > 0
        before = listing(directory)
        db_bytes = (directory / "state.db").read_bytes()
        wal_bytes = (directory / "state.db-wal").read_bytes()
        run = validate(capsys, gw)
        assert run.code == 12, run.out + run.err
        assert reasons(run.doc) == {"store": "store_unreadable"}
        assert listing(directory) == before
        assert not (directory / "state.db-shm").exists()
        assert (directory / "state.db").read_bytes() == db_bytes
        assert (directory / "state.db-wal").read_bytes() == wal_bytes
    finally:
        close()


# --- the price table ---------------------------------------------------------------------------


def test_an_old_price_table_is_a_warning(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "prices: {as_of: '2025-01-01', models: {}}\n")
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["price_table_old"]


def test_a_price_table_exactly_180_days_old_is_not_old(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "prices: {as_of: '2025-07-05', models: {}}\n")  # fake clock: 2026-01-01
    assert validate(capsys, gw).doc["warnings"] == []
    write_config(home, "prices: {as_of: '2025-07-04', models: {}}\n")
    assert validate(capsys, gw).doc["warnings"] == ["price_table_old"]


# --- the permission profile --------------------------------------------------------------------


def test_a_missing_configured_profile_fails(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, f"retro: {{permission_profile: '{home}/profile.yaml'}}\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"permission_profile": "permission_profile_missing"}


def test_a_configured_profile_that_is_there_passes(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    profile = home / "profile.yaml"
    profile.write_text("x: 1\n")
    os.chmod(profile, 0o600)
    write_config(home, f"retro: {{permission_profile: '{profile}'}}\n")
    assert validate(capsys, gw).code == 0


@pytest.mark.parametrize("mode", [0o666, 0o664])
def test_an_unsafe_profile_fails_config_unsafe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, mode: int
) -> None:
    profile = home / "profile.yaml"
    profile.write_text("x: 1\n")
    os.chmod(profile, mode)
    write_config(home, f"retro: {{permission_profile: '{profile}'}}\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"permission_profile": "config_unsafe"}


@pytest.mark.parametrize("shape", ["under-a-file", "too-long"])
def test_a_profile_path_that_cannot_exist_is_missing_not_unsafe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, shape: str
) -> None:
    if shape == "under-a-file":
        (home / "plain").write_text("x\n")
        path = f"{home}/plain/profile.yaml"
    else:
        path = f"{home}/{'a' * 300}/profile.yaml"
    write_config(home, f"retro: {{permission_profile: '{path}'}}\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"permission_profile": "permission_profile_missing"}


def test_a_profile_symlink_fails_config_unsafe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    target = home / "real.yaml"
    target.write_text("x: 1\n")
    os.chmod(target, 0o600)
    link = home / "profile.yaml"
    link.symlink_to(target)
    write_config(home, f"retro: {{permission_profile: '{link}'}}\n")
    assert reasons(validate(capsys, gw).doc) == {"permission_profile": "config_unsafe"}


def test_the_bundled_profile_must_exist_when_none_is_configured(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy = tmp_path / "plugin"
    shutil.copytree(PLUGIN, copy)
    (copy / "profiles" / "audit-retro.yaml").unlink()
    monkeypatch.setattr(plugins, "bundled_plugin_dir", lambda *_a, **_k: str(copy))
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"permission_profile": "permission_profile_missing"}


# --- the plugin --------------------------------------------------------------------------------


def private_plugin(home: Path) -> Path:
    """``~/myplugin`` with the skill ``x`` (every directory 0700 whatever the umask)."""
    mine = home / "myplugin"
    for path in (mine, mine / "skills", mine / "skills" / "x"):
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    skill = mine / "skills" / "x" / "SKILL.md"
    skill.write_text("x\n")
    os.chmod(skill, 0o600)
    return mine


def test_an_unknown_skill_fails_plugin_missing(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "retro: {skill: 'nothing:here'}\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"plugin": "plugin_missing"}


def test_a_plugin_dir_that_is_home_fails_unsafe_plugin_dir(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    # (a skill the bundled plugin does not hold, so the configured directories are walked)
    write_config(home, f"retro: {{plugin_dirs: ['{home}'], skill: 'other:thing'}}\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"plugin": "unsafe_plugin_dir"}


def test_a_symlink_inside_a_plugin_dir_fails_unsafe_plugin_dir(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, tmp_path: Path
) -> None:
    mine = private_plugin(home)
    (mine / "link").symlink_to(tmp_path)
    write_config(home, f"retro: {{plugin_dirs: ['{mine}'], skill: 'myplugin:x'}}\n")
    assert reasons(validate(capsys, gw).doc) == {"plugin": "unsafe_plugin_dir"}


def test_a_configured_plugin_with_the_skill_passes(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    mine = private_plugin(home)
    write_config(home, f"retro: {{plugin_dirs: ['{mine}'], skill: 'myplugin:x'}}\n")
    run = validate(capsys, gw)
    assert run.code == 0, run.out


def test_a_bundled_marker_that_does_not_match_the_skill_fails(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy = tmp_path / "plugin"
    shutil.copytree(PLUGIN, copy)
    skill = copy / "skills" / "audit" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    monkeypatch.setattr(plugins, "bundled_plugin_dir", lambda *_a, **_k: str(copy))
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"plugin": "plugin_missing"}


def test_a_missing_bundled_plugin_fails(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugins, "bundled_plugin_dir", lambda *_a, **_k: None)
    run = validate(capsys, gw)
    assert run.code == 12
    assert check_of(run.doc, "plugin")["reason"] == "plugin_missing"
    assert check_of(run.doc, "permission_profile")["reason"] == "permission_profile_missing"


# --- claude-session ----------------------------------------------------------------------------


def test_a_missing_claude_session_passes_with_auto(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    gw.version = None
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == []


def test_a_missing_claude_session_fails_with_harness_on(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "harness: {enabled: 'on'}\n")
    gw.version = None
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"claude_session": "claude_session_missing"}


@pytest.mark.parametrize(
    "result",
    [
        CallResult(CallStatus.EXITED, 0, stdout="claude-session 0.45.1\n"),
        CallResult(CallStatus.EXITED, 0, stdout="something else 1.0.0\n"),
        CallResult(CallStatus.EXITED, 3, stderr="boom"),
        CallResult(CallStatus.TIMEOUT, detail="timed out"),
    ],
)
def test_an_unusable_claude_session_fails_too_old(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, result: CallResult
) -> None:
    gw.version = result
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"claude_session": "claude_session_too_old"}


def test_a_too_old_claude_session_fails_with_harness_on(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "harness: {enabled: 'on'}\n")
    gw.version = CallResult(CallStatus.EXITED, 0, stdout="claude-session 0.45.1\n")
    run = validate(capsys, gw)
    assert run.code == 12
    assert reasons(run.doc) == {"claude_session": "claude_session_too_old"}


def test_claude_session_0_100_0_is_new_enough(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    gw.version = CallResult(CallStatus.EXITED, 0, stdout="claude-session 0.100.0\n")
    assert validate(capsys, gw).code == 0


def test_a_scheduled_retro_with_auto_and_no_claude_session_warns(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "schedule: {retro: {scope: {since: 7d}}}\n")
    gw.version = None
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["claude_session_missing"]
    write_config(home, "schedule: {retro: {scope: {since: 7d}}}\nharness: {enabled: 'off'}\n")
    assert validate(capsys, gw).doc["warnings"] == []


# --- warnings from the store -------------------------------------------------------------------


def test_ingest_stale_needs_a_managed_block(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    make_store(home)  # never ingested
    assert validate(capsys, gw).doc["warnings"] == []
    gw.crontab = managed_block()
    assert validate(capsys, gw).doc["warnings"] == ["ingest_stale"]


def test_a_crontab_that_cannot_be_read_or_is_malformed_is_not_a_block(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    make_store(home)
    gw.crontab_result = CallResult(CallStatus.EXITED, 1, stderr="permission denied")
    assert validate(capsys, gw).doc["warnings"] == []
    gw.crontab_result = None
    gw.crontab = cron.START + "\n"
    assert validate(capsys, gw).doc["warnings"] == []


@pytest.mark.parametrize(
    ("interval", "age_ms", "stale"),
    [
        (None, 23 * HOUR_MS, False),  # no interval recorded: max(24 h, ...) = 24 h
        (None, 25 * HOUR_MS, True),
        ("1h", 25 * HOUR_MS, True),
        ("1h", 2 * HOUR_MS, False),
        ("24h", 30 * HOUR_MS, False),  # 2 x 24 h = 48 h
        ("24h", 49 * HOUR_MS, True),
        ("zzz", 25 * HOUR_MS, True),  # an unreadable interval counts as absent
    ],
)
def test_ingest_stale_uses_the_longer_of_a_day_and_twice_the_interval(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    interval: str | None,
    age_ms: int,
    stale: bool,
) -> None:
    gw.crontab = managed_block()
    set_meta(home, "last_complete_ingest_at", now_ms(gw) - age_ms)
    if interval is not None:
        set_meta(home, "ingest_interval", interval)
    warnings = validate(capsys, gw).doc["warnings"]
    assert warnings == (["ingest_stale"] if stale else [])


def test_a_last_complete_ingest_stored_as_digit_text_counts_like_a_number(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    gw.crontab = managed_block()
    set_meta(home, "last_complete_ingest_at", str(now_ms(gw)))
    assert validate(capsys, gw).doc["warnings"] == []


@pytest.mark.parametrize("value", ["yesterday", 1.5, None, "", "-5", "1.5"])
def test_a_last_complete_ingest_that_is_not_a_whole_number_counts_as_never(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, value: object
) -> None:
    gw.crontab = managed_block()
    set_meta(home, "last_complete_ingest_at", value)
    assert validate(capsys, gw).doc["warnings"] == ["ingest_stale"]


def test_an_unfinished_retro_without_a_live_lease_warns(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    orphan(home, state="running")
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["retros_unrecovered"]


def test_a_retro_with_a_live_lease_does_not_warn(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    pid = os.getpid()
    start = gw.process_start(pid)
    orphan(home, state="running", lease=(pid, start, now_ms(gw) + 60_000))
    assert validate(capsys, gw).doc["warnings"] == []


def test_a_final_retro_does_not_warn(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    orphan(home, state="failed", reason="timeout")
    assert validate(capsys, gw).doc["warnings"] == []


@pytest.mark.parametrize(
    "lease",
    [
        (os.getpid(), "x", "zz"),
        (b"\x00\x01", "x", 10**15),
        ("pid", "x", 10**15),
    ],
    ids=["text-expiry", "blob-pid", "text-pid"],
)
def test_a_corrupt_lease_row_is_store_unreadable_with_the_document(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, lease: tuple[Any, ...]
) -> None:
    orphan(home, state="running", lease=lease)
    run = validate(capsys, gw)
    assert run.code == 12, run.out + run.err
    assert reasons(run.doc) == {"store": "store_unreadable"}
    validate_schema(run.doc, SCHEMA)


@pytest.mark.parametrize("error", [PermissionError("proc"), FileNotFoundError("ps")])
def test_a_lease_check_that_cannot_answer_is_an_error_never_dead(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, error: OSError
) -> None:
    orphan(home, state="running", lease=(os.getpid(), "x", now_ms(gw) + 60_000))
    gw.process_start_error = error
    run = validate(capsys, gw)
    assert run.code == 12
    assert "retros_unrecovered" not in run.doc["warnings"]
    assert reasons(run.doc) == {"store": "store_unreadable"}


@pytest.mark.parametrize(
    ("state", "age_ms", "warns"),
    [
        ("pending", 2 * DAY_MS, True),
        ("incomplete", 2 * DAY_MS, True),
        ("pending", 23 * HOUR_MS, False),
        ("done", 2 * DAY_MS, False),
        ("skipped", 2 * DAY_MS, False),
    ],
)
def test_cleanup_incomplete_is_a_pending_row_older_than_a_day(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    state: str,
    age_ms: int,
    warns: bool,
) -> None:
    plant_session(home, state=state, created_at=now_ms(gw) - age_ms)
    assert validate(capsys, gw).doc["warnings"] == (["cleanup_incomplete"] if warns else [])


def test_the_retro_checks_are_skipped_on_a_pending_store_without_the_tables(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    import sqlite3

    make_store(home)
    conn = sqlite3.connect(state_dir_of(home) / "state.db")
    conn.execute("DROP TABLE retro_sessions")
    conn.execute(
        "DELETE FROM schema_migrations WHERE number = (SELECT MAX(number) FROM schema_migrations)"
    )
    conn.commit()
    conn.close()
    run = validate(capsys, gw)
    assert run.code == 0
    assert run.doc["warnings"] == ["migration_pending"]


# --- the other warnings ------------------------------------------------------------------------


def test_a_lowered_sandbox_warns(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "retro: {sandbox: preferred}\n")
    assert validate(capsys, gw).doc["warnings"] == ["sandbox_lowered"]


def test_an_unsafe_env_file_of_the_managed_block_warns(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    env_file = home / "it's env"
    env_file.write_text("A=1\n")
    os.chmod(env_file, 0o600)
    gw.crontab = managed_block(str(env_file))
    set_meta(home, "last_complete_ingest_at", now_ms(gw))
    assert validate(capsys, gw).doc["warnings"] == []
    os.chmod(env_file, 0o666)
    assert validate(capsys, gw).doc["warnings"] == ["env_file_unsafe"]
    os.chmod(env_file, 0o600)
    env_file.unlink()
    assert validate(capsys, gw).doc["warnings"] == ["env_file_unsafe"]


@pytest.mark.parametrize("path", ["/a && b/env", "/x/it'", "/x/'", "/x/a' && 'b"])
def test_env_file_of_reads_back_the_path_cron_install_quoted(path: str) -> None:
    block = cron.build_block(
        IngestEvery(60), ScheduleRetro(scope=ScheduleScope(since="7d")), "/usr/bin", path
    )
    found = [env_file_of(line) for line in block[1:-1]]
    assert path in found


def test_an_env_file_is_checked_without_a_store(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    env_file = home / "env"
    env_file.write_text("A=1\n")
    os.chmod(env_file, 0o666)
    gw.crontab = managed_block(str(env_file))
    assert set(validate(capsys, gw).doc["warnings"]) == {"env_file_unsafe", "ingest_stale"}


def test_every_warning_at_once_matches_its_golden(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    env_file = home / "env"
    env_file.write_text("A=1\n")
    os.chmod(env_file, 0o666)
    write_config(
        home,
        f"transcript_roots: ['{home}/.claude/projects', '{home}/nowhere']\n"
        "prices: {as_of: '2025-01-01', models: {}}\n"
        "retro: {sandbox: preferred}\n"
        "schedule: {retro: {scope: {since: 7d}}}\n",
    )
    gw.version = None
    gw.crontab = managed_block(str(env_file))
    orphan(home, state="running", created=RECENT_MS)
    plant_session(home, state="pending", created_at=now_ms(gw) - 3 * DAY_MS)
    make_pending(home)
    run = validate(capsys, gw)
    assert run.code == 0, run.out + run.err
    assert run.doc["ok"] is True
    assert_golden(run.doc, "validate-warnings")


# --- read-only ---------------------------------------------------------------------------------


def test_validate_changes_nothing_with_every_warning_present(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    orphan(home, state="running")
    plant_session(home, state="pending", created_at=1)
    gw.crontab = managed_block()
    before = snapshot(home / ".local", home / ".config", home / ".claude")
    run = validate(capsys, gw)
    assert run.code == 0
    assert {"retros_unrecovered", "cleanup_incomplete", "ingest_stale"} <= set(run.doc["warnings"])
    assert snapshot(home / ".local", home / ".config", home / ".claude") == before


def test_validate_does_not_prompt(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ask(*_a: object, **_k: object) -> str:
        raise AssertionError("validate must not prompt")

    monkeypatch.setattr(type(gw), "ask", ask)
    assert validate(capsys, gw).code == 0


# --- --probe -----------------------------------------------------------------------------------


@pytest.fixture
def fake(gw: ValidateGateway, tmp_path: Path) -> FakeClaudeSession:
    cs = FakeClaudeSession(tmp_path)
    cs.script("--version", version_ok())
    gw.fake = cs
    return cs


def version_ok() -> dict[str, Any]:
    return version_step()


def probe_calls(cs: FakeClaudeSession) -> list[list[str]]:
    return [c["argv"] for c in cs.calls() if c["key"] == "kinds"]


def test_without_probe_kinds_is_never_called(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    run = validate(capsys, gw)
    assert run.code == 0
    assert [c["name"] for c in run.doc["checks"]] == ALL_CHECKS
    assert probe_calls(fake) == []


def test_probe_runs_kinds_and_checks_the_sandbox(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc()))
    run = validate(capsys, gw, "--probe")
    assert run.code == 0, run.out + run.err
    assert [c["name"] for c in run.doc["checks"]] == [*ALL_CHECKS, "probe_kinds", "probe_sandbox"]
    ok_doc(run.doc)
    assert probe_calls(fake) == [["kinds", "--json"]]
    fake.assert_outputs_valid()
    assert_golden(run.doc, "validate-probe_ok")


def test_a_failing_kinds_call_fails_both_probes(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(error_doc("failed", "boom"), exit=12))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert reasons(run.doc) == {"probe_kinds": "probe_failed", "probe_sandbox": "probe_failed"}
    assert "boom" not in run.out + run.err
    assert_golden(run.doc, "validate-probe_failed")


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"kinds": 3}', '{"kinds": []}', '{"kinds": [{"name": "review"}]}', ""],
)
def test_a_kinds_document_without_the_audit_profile_fails(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    fake: FakeClaudeSession,
    text: str,
) -> None:
    fake.script("kinds", step(text, unvalidated=True))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert check_of(run.doc, "probe_kinds")["reason"] == "probe_failed"


def test_a_claude_that_kinds_cannot_find_fails_the_probe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script(
        "kinds",
        step(kinds_doc(claude_bin_resolved=None, claude_version=None, claude_version_ok=None)),
    )
    assert check_of(validate(capsys, gw, "--probe").doc, "probe_kinds")["reason"] == "probe_failed"
    fake.script("kinds", step(kinds_doc(claude_version_ok=False)))
    assert check_of(validate(capsys, gw, "--probe").doc, "probe_kinds")["reason"] == "probe_failed"


def test_an_unavailable_sandbox_fails_when_it_is_required(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(sandbox_available=False)))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert reasons(run.doc) == {"probe_sandbox": "sandbox_unavailable"}
    assert "helper" not in run.out + run.err  # claude-session's text is never shown


def test_an_unavailable_sandbox_passes_when_not_required(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(sandbox_available=False)))
    write_config(home, "retro: {sandbox: preferred}\n")
    run = validate(capsys, gw, "--probe")
    assert run.code == 0
    assert run.doc["warnings"] == ["sandbox_lowered"]
    write_config(home, "retro: {sandbox: 'off'}\n")
    assert validate(capsys, gw, "--probe").code == 0


def test_an_unknown_sandbox_state_fails_when_required(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(review_sandbox_available=None)))  # both null
    assert (
        check_of(validate(capsys, gw, "--probe").doc, "probe_sandbox")["reason"] == "probe_failed"
    )


def test_a_stock_audit_profile_with_the_sandbox_off_falls_back_to_the_review_profile(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(sandbox_available=None, review_sandbox_available=True)))
    run = validate(capsys, gw, "--probe")
    assert run.code == 0, run.out
    assert check_of(run.doc, "probe_sandbox")["ok"] is True


def test_a_null_audit_sandbox_and_an_unavailable_review_sandbox_fail_when_required(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(sandbox_available=None, review_sandbox_available=False)))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert reasons(run.doc) == {"probe_sandbox": "sandbox_unavailable"}


def test_a_null_audit_sandbox_without_a_review_profile_fails_probe_failed(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(names=("audit",))))
    run = validate(capsys, gw, "--probe")
    assert reasons(run.doc) == {"probe_sandbox": "probe_failed"}


def test_a_boolean_audit_sandbox_wins_over_the_review_profile(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(sandbox_available=True, review_sandbox_available=False)))
    assert validate(capsys, gw, "--probe").code == 0
    fake.script("kinds", step(kinds_doc(sandbox_available=False, review_sandbox_available=True)))
    assert reasons(validate(capsys, gw, "--probe").doc) == {"probe_sandbox": "sandbox_unavailable"}


def test_a_null_sandbox_everywhere_passes_when_not_required(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(review_sandbox_available=None)))
    write_config(home, "retro: {sandbox: preferred}\n")
    assert validate(capsys, gw, "--probe").code == 0


# --- probe gaps --------------------------------------------------------------------------------


def test_a_kinds_call_that_times_out_fails_and_leaves_no_orphan(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    fake: FakeClaudeSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "kinds.pid"
    fake.script("kinds", step(kinds_doc(), sleep=30.0, pid_file=pid_file))
    monkeypatch.setattr(validate_module, "PROBE_TIMEOUT", 1.0)
    run = validate(capsys, gw, "--probe")
    assert reasons(run.doc) == {"probe_kinds": "probe_failed", "probe_sandbox": "probe_failed"}
    pid = int(pid_file.read_text().strip())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the timed-out kinds process is still running")


def test_an_audit_profile_that_is_a_fallback_fails_the_probe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    document = kinds_doc()
    document["kinds"][0]["fallback"] = True
    fake.script("kinds", step(document, unvalidated=True))
    run = validate(capsys, gw, "--probe")
    assert check_of(run.doc, "probe_kinds")["reason"] == "probe_failed"


def test_a_kinds_output_over_the_limit_fails_the_probe(
    capsys: pytest.CaptureFixture[str],
    gw: ValidateGateway,
    home: Path,
    fake: FakeClaudeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.script("kinds", step(kinds_doc()))
    monkeypatch.setattr(validate_module, "PROBE_OUTPUT_LIMIT", 100)
    run = validate(capsys, gw, "--probe")
    assert reasons(run.doc) == {"probe_kinds": "probe_failed", "probe_sandbox": "probe_failed"}


def test_an_empty_claude_bin_resolved_fails_the_probe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc(claude_bin_resolved=""), unvalidated=True))
    assert check_of(validate(capsys, gw, "--probe").doc, "probe_kinds")["reason"] == "probe_failed"


def test_a_kinds_call_that_cannot_start_fails(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    gw.kinds = CallResult(CallStatus.SPAWN_FAILED, detail="scripted")
    run = validate(capsys, gw, "--probe")
    assert reasons(run.doc) == {"probe_kinds": "probe_failed", "probe_sandbox": "probe_failed"}


def test_probe_never_runs_a_hook_or_the_exec_bearing_config_of_an_unsafe_config(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "hook-ran"
    write_config(
        home,
        "harness: {claude_session_cmd: evil-cs}\n"
        f"hooks: {{pre-retro: [{{cmd: 'touch {marker}'}}]}}\n",
        mode=0o666,
    )
    gw.kinds = CallResult(CallStatus.EXITED, 0, stdout=json.dumps(kinds_doc()))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert reasons(run.doc) == {
        "config_ownership": "config_unsafe",
        "probe_kinds": "config_unsafe",
        "probe_sandbox": "config_unsafe",
    }
    assert not marker.exists()
    assert all(call[0] == "claude-session" for call in gw.runs)  # never the configured command
    assert ["claude-session", "kinds", "--json"] not in gw.runs


def test_an_unsafe_config_runs_nothing_for_plugin_dirs_and_the_profile(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    script = tmp_path / "profile.yaml"
    script.write_text(f"touch {marker}\n")
    os.chmod(script, 0o600)
    mine = tmp_path / "plugins"
    mine.mkdir()
    write_config(
        home,
        f"retro: {{plugin_dirs: ['{mine}'], permission_profile: '{script}'}}\n"
        f"hooks: {{pre-retro: [{{cmd: 'touch {marker}'}}]}}\n",
        mode=0o666,
    )
    gw.kinds = CallResult(CallStatus.EXITED, 0, stdout=json.dumps(kinds_doc()))
    run = validate(capsys, gw, "--probe")
    assert run.code == 12
    assert check_of(run.doc, "probe_kinds")["reason"] == "config_unsafe"
    assert check_of(run.doc, "probe_sandbox")["reason"] == "config_unsafe"
    assert gw.runs == [["claude-session", "--version"]]
    assert not marker.exists()


def test_an_unsafe_config_never_runs_its_claude_session_command_even_without_probe(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "harness: {claude_session_cmd: evil-cs}\n", mode=0o666)
    validate(capsys, gw)
    assert gw.runs == [["claude-session", "--version"]]


def test_a_safe_config_runs_its_own_claude_session_command(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "harness: {claude_session_cmd: my-cs}\n")
    assert validate(capsys, gw).code == 0
    assert gw.runs == [["my-cs", "--version"]]


def test_probe_with_an_invalid_config_is_config_invalid_and_runs_nothing(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, "not: [valid\n")
    run = validate(capsys, gw, "--probe")
    assert run.code == 2
    assert check_of(run.doc, "probe_kinds")["reason"] == "config_invalid"
    assert check_of(run.doc, "probe_sandbox")["reason"] == "config_invalid"
    assert all(call[1:] == ["--version"] for call in gw.runs)


def test_no_hook_runs_with_hooks_configured(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "hook-ran"
    write_config(home, f"hooks: {{pre-retro: [{{cmd: 'touch {marker}'}}]}}\n")
    assert validate(capsys, gw).code == 0  # run_hook raises in the test gateway
    assert not marker.exists()


def test_the_probe_is_read_only(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path, fake: FakeClaudeSession
) -> None:
    fake.script("kinds", step(kinds_doc()))
    make_store(home)
    before = snapshot(home / ".local", home / ".config")
    assert validate(capsys, gw, "--probe").code == 0
    assert snapshot(home / ".local", home / ".config") == before


# --- hostile text never reaches the output -----------------------------------------------------


def test_a_hostile_config_path_and_values_are_never_echoed(
    capsys: pytest.CaptureFixture[str], gw: ValidateGateway, home: Path
) -> None:
    write_config(home, 'retro: {skill: "x\\x1b[31mred:y"}\n')
    run = validate(capsys, gw, json_output=False)
    assert "\x1b" not in run.out + run.err
    assert run.code == 2
    validate_schema(json.loads(validate(capsys, gw).out), SCHEMA)
