"""``retro run``: the skill preflight, the launch plan check and the dry run, end to end through the
real CLI with the fake claude-session (issue #24; Order of checks steps 4, 11 and 12)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from session_audit import fencing, launchplan, retro
from session_audit.commands import retro_run

from .digesthelp import say
from .fake_claude_session import (
    FakeClaudeSession,
    plan_doc,
    refusing_plan,
    step,
    version_step,
)
from .ingesthelp import S1, S2, Corpus, connect, state_dir_of
from .retrohelp import (
    RetroGateway,
    add_retro,
    args_for,
    ingest_entries,
    ingested,
    live_lease,
    retro_ctx,
    retro_rows,
    rid,
    seed,
    touch,
)
from .retrohelp import retro as run_retro
from .schemas import validate


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    """The fake claude-session answers every call of the retro: a good version and a clean plan."""
    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gw.fake = fake
    return fake


def two(gw: RetroGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]) -> None:
    seed(corpus, S1, "first secret prompt")
    seed(corpus, S2, "second", project="p2")
    ingested(capsys, gw)


def keys(fake: FakeClaudeSession) -> list[str]:
    """The calls of claude-session other than the version check that every run makes."""
    return [c["key"] for c in fake.calls() if c["key"] != "--version"]


def write_config(home: Path, text: str) -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def state_snapshot(home: Path) -> dict[str, Any]:
    """Everything a dry run must leave as it was: the state directory's entries (size and mtime)
    and the rows that an ingest would have moved."""
    directory = state_dir_of(home)
    entries = {}
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            info = path.stat()
            entries[str(path.relative_to(directory))] = (info.st_size, info.st_mtime_ns)
    rows: list[Any] = []
    if (directory / "state.db").exists():
        conn = connect(home)
        try:
            rows = conn.execute("SELECT path, offset, size FROM files ORDER BY path").fetchall()
            rows.append(conn.execute("SELECT COUNT(*) FROM events").fetchone())
            rows.append(conn.execute("SELECT COUNT(*) FROM sessions").fetchall())
        finally:
            conn.close()
    return {"entries": entries, "rows": rows, "ingests": ingest_entries(home)}


def dry(capsys: pytest.CaptureFixture[str], gw: RetroGateway, *argv: str) -> Any:
    return run_retro(capsys, gw, *(argv or ("--project", "/work")), "--dry-run")


def error_of(result: Any, code: int, reason: str | None = None) -> dict[str, Any]:
    assert result.code == code, result.err
    document = json.loads(result.out)
    validate(document, "session-audit/cli/error.json")
    if reason is not None:
        assert document["error"]["details"]["reason"] == reason
    return document["error"]  # type: ignore[no-any-return]


# --- the dry run's plan ------------------------------------------------------------------------


def test_a_dry_run_prints_the_plan_validated_against_the_schema_and_no_digest_text(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    result = dry(
        capsys, gw, "--project", "/work", "--model", "opus", "--timeout", "5m", "--max-turns", "3"
    )
    assert result.code == 0, result.err
    plan = result.doc
    validate(plan, "session-audit/cli/retro-run.json")
    assert plan["dry_run"] is True and plan["skill"] == "session-audit:audit"
    assert sorted(plan["matched"]) == [S1, S2] and plan["matched_total"] == 2
    assert plan["skipped"] == [] and plan["over_cap"] == 0
    assert plan["items"] == 4 and sum(plan["items_by_kind"].values()) == 4
    assert plan["model"] == "opus" and plan["reused_retro"] is None
    assert plan["running_retro"] is None and plan["daily_remaining"] == 5
    assert plan["scope"] == {
        "sessions": [],
        "labels": [],
        "project": "/work",
        "since": None,
        "until": None,
        "kind": None,
        "outcome": None,
    }
    flags = plan["launch_flags"]
    assert "--prompt-file" not in flags and "--input-file" not in flags
    assert flags[flags.index("--model") + 1] == "opus"
    assert flags[flags.index("--timeout") + 1] == "300s"
    assert flags[flags.index("--max-turns") + 1] == "3"
    assert flags[:4] == ["--kind", "audit", "--reads-third-party", "--no-implicit-done"]
    assert "retro=<retro_id>" in flags
    # No transcript text anywhere in the output.
    assert "secret" not in result.out and "second" not in result.out and "answer" not in result.out
    assert keys(cs) == ["start --dry-run"]  # nothing launched, nothing forgotten
    cs.assert_outputs_valid()


def test_the_dry_run_digest_bytes_are_those_of_a_real_digest(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    plan = dry(capsys, gw).doc
    ctx = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work"), env={})
    finally:
        ctx.close()
    real = fencing.finish_digest(
        prepared.digest, "ra-abcdefgh", "r" * 32, gw, prepared.csession_ids
    )
    assert plan["digest_bytes"] == len(real.digest_bytes) > 0
    assert plan["key"] == prepared.key


def test_the_human_dry_run_output_has_no_session_text(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    from session_audit import cli

    two(gw, corpus, capsys)
    capsys.readouterr()
    code = cli.main(
        ["retro", "run", "--project", "/work", "--dry-run"], gateway=gw, stdin_is_tty=False
    )
    out = capsys.readouterr().out
    assert code == 0 and "key " in out and "secret" not in out


# --- the dry run reads to the current end, in memory, and writes nothing -----------------------


def test_the_dry_run_key_is_the_key_of_a_real_run_over_the_same_bytes(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        stale = retro.prepare(ctx, args_for(project="/work"), env={}).key  # the stored bytes
    finally:
        ctx.close()
    path = corpus.main_path(S1, "p1")
    corpus.append_main(S1, [say("m-more", "a later answer", 5)], project="p1")
    touch(path)  # old again: the session is not live
    before = state_snapshot(corpus.home)

    plan = dry(capsys, gw).doc

    assert (
        state_snapshot(corpus.home) == before
    )  # not a byte of state, no log line, no offset moved
    assert plan["key"] != stale  # it read the appended bytes
    assert plan["items"] == 5
    ctx = retro_ctx(gw, corpus)
    try:
        real = retro.prepare(ctx, args_for(project="/work"), env={})  # a real refresh, then the key
    finally:
        ctx.close()
    assert real.key == plan["key"]
    assert state_snapshot(corpus.home) != before  # the real run did write


def test_a_dry_run_sees_a_session_the_store_has_never_read(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1, "first")
    ingested(capsys, gw)
    seed(corpus, S2, "new", project="p2")  # on disk, not in the store
    plan = dry(capsys, gw, "--session", S2).doc
    assert plan["matched"] == [S2] and plan["scope"]["sessions"] == [S2]
    ctx = retro_ctx(gw, corpus)
    try:
        assert retro.prepare(ctx, args_for(session=[S2]), env={}).key == plan["key"]
    finally:
        ctx.close()


def test_a_full_id_on_disk_is_dry_run_on_a_fresh_home_and_leaves_no_state(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1, "first")
    result = dry(capsys, gw, "--session", S1)
    assert result.code == 0, result.err
    assert result.doc["matched"] == [S1]
    assert not state_dir_of(corpus.home).exists()  # not even the placeholder directory remains
    ctx = retro_ctx(gw, corpus)
    try:
        assert retro.prepare(ctx, args_for(session=[S1]), env={}).key == result.doc["key"]
    finally:
        ctx.close()


def test_a_full_id_on_no_disk_is_exit_10_in_a_dry_run_too(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1, "first")
    result = dry(capsys, gw, "--session", S2)
    error_of(result, 10)
    assert not state_dir_of(corpus.home).exists()
    assert keys(cs) == []


def test_the_dry_run_of_a_prefix_unknown_to_the_store_is_still_exit_10(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    error_of(dry(capsys, gw, "--session", "99999999"), 10)


def test_a_dry_run_never_takes_the_ingest_lease_or_waits_for_it(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    from session_audit.lease import IngestTarget, acquire
    from session_audit.store import open_store

    two(gw, corpus, capsys)
    store = open_store(gw, str(state_dir_of(corpus.home)))
    lease = acquire(store, IngestTarget())
    assert lease is not None
    try:
        result = dry(capsys, gw)
        assert result.code == 0, result.err
    finally:
        lease.release()
        store.close()
    assert gw.sleeps == []  # nothing waited for the lease


def test_a_dry_run_reports_what_a_real_run_would_reuse_or_refuse(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = dry(capsys, gw).doc["key"]
    add_retro(corpus.home, key, retro_id="ra-aaaaaaab", created=5)
    assert dry(capsys, gw).doc["reused_retro"] == "ra-aaaaaaab"
    add_retro(corpus.home, key, retro_id="ra-aaaaaaac", state="running", created=9,
              lease=live_lease(gw))  # fmt: skip
    plan = dry(capsys, gw).doc
    assert plan["running_retro"] == "ra-aaaaaaac" and plan["reused_retro"] == "ra-aaaaaaab"


def test_daily_remaining_counts_the_starts_of_today_and_the_live_digesting_retros(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    assert dry(capsys, gw).doc["daily_remaining"] == 5
    conn = connect(corpus.home)
    try:
        for n, day in enumerate(["2026-01-01", "2026-01-01", "2025-12-31"]):
            conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES (?, ?)", (day, rid(n)))
        conn.commit()
    finally:
        conn.close()
    assert dry(capsys, gw).doc["daily_remaining"] == 3  # yesterday's start does not count
    add_retro(corpus.home, "b" * 64, retro_id=rid(20), state="digesting", lease=live_lease(gw))
    assert dry(capsys, gw).doc["daily_remaining"] == 2
    add_retro(corpus.home, "c" * 64, retro_id=rid(21), state="digesting",
              lease=(999_999, "gone", 1))  # fmt: skip
    assert dry(capsys, gw).doc["daily_remaining"] == 2  # a dead or expired lease reserves nothing
    conn = connect(corpus.home)
    try:
        for n in range(30, 40):
            conn.execute(
                "INSERT INTO retro_starts (day, retro_id) VALUES ('2026-01-01', ?)", (rid(n),)
            )
        conn.commit()
    finally:
        conn.close()
    assert dry(capsys, gw).doc["daily_remaining"] == 0


# --- step 11: the plan check --------------------------------------------------------------------


def test_a_real_run_that_passes_the_plan_check_goes_on_to_start(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    result = run_retro(capsys, gw, "--project", "/work")
    # The fake has no step for ``start``: the launch is attempted and ends claude_session_error.
    assert result.code == 12
    assert json.loads(result.out)["error"]["details"]["reason"] == "claude_session_error"
    assert keys(cs) == ["start --dry-run", "start"]  # the plan check, then the real start
    assert retro_rows(corpus.home) == 1
    assert not (state_dir_of(corpus.home) / "tmp").exists()  # the placeholders are gone


@pytest.mark.parametrize("is_dry", [False, True])
def test_a_plan_with_a_changed_safety_setting_is_exit_12_and_stores_nothing(
    is_dry: bool,
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    two(gw, corpus, capsys)
    cs.script("start --dry-run", step(plan_doc({"profile.env.inherit": True})))
    before = state_snapshot(corpus.home)
    argv = ["--project", "/work", *(["--dry-run"] if is_dry else [])]
    error = error_of(run_retro(capsys, gw, *argv), 12, "launch_settings_mismatch")
    assert error["details"]["retryable"] is False
    assert retro_rows(corpus.home) == 0
    conn = connect(corpus.home)
    try:
        assert conn.execute("SELECT COUNT(*) FROM retro_starts").fetchone()[0] == 0
    finally:
        conn.close()
    if is_dry:  # a real run's refresh (step 8) is an ingest; a dry run leaves every byte
        assert state_snapshot(corpus.home) == before
    assert not (state_dir_of(corpus.home) / "tmp").exists()
    assert keys(cs) == ["start --dry-run"]


def test_an_unknown_origins_key_set_by_config_fails_the_retro_closed(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    cs.script("start --dry-run", step(plan_doc({"origins.a_new_safety_field": "config"})))
    error_of(dry(capsys, gw), 12, "launch_settings_mismatch")


def test_a_user_audit_config_shown_in_origins_is_a_mismatch_but_model_is_open(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    cs.script("start --dry-run", step(plan_doc({"origins.sandbox": "config"})))
    error_of(dry(capsys, gw), 12, "launch_settings_mismatch")
    cs.script(
        "start --dry-run", step(plan_doc({"origins.model": "config", "origins.timeout": "repo"}))
    )
    assert dry(capsys, gw).code == 0


@pytest.mark.parametrize("is_dry", [False, True])
def test_would_refuse_and_would_fail_are_exit_12_and_launch_nothing(
    is_dry: bool,
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    two(gw, corpus, capsys)
    argv = ["--project", "/work", *(["--dry-run"] if is_dry else [])]
    cs.script("start --dry-run", step(refusing_plan("tainted"), exit=20))
    error_of(run_retro(capsys, gw, *argv), 12, "claude_session_error")
    cs.script("start --dry-run", step(plan_doc({"would_fail": "sandbox_unavailable"}), exit=12))
    error = error_of(run_retro(capsys, gw, *argv), 12, "session_not_started")
    assert error["details"]["end_reason"] == "sandbox_unavailable"
    assert retro_rows(corpus.home) == 0
    assert set(keys(cs)) == {"start --dry-run"}


def test_a_hung_plan_check_is_a_failed_call(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    two(gw, corpus, capsys)
    monkeypatch.setattr(launchplan, "PLAN_TIMEOUT", 0.5)
    cs.script("start --dry-run", step(plan_doc(), sleep=30.0))
    error_of(dry(capsys, gw), 12, "claude_session_error")
    assert retro_rows(corpus.home) == 0


def test_a_session_directory_with_a_non_ascii_path_is_refused_before_anything_is_stored(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    cs.script("start --dry-run", step(plan_doc({"cwd": "/état/sessions/cs-aaaaaaaa/work"})))
    error_of(run_retro(capsys, gw, "--project", "/work"), 12, "launch_settings_mismatch")
    assert retro_rows(corpus.home) == 0


def test_the_plan_check_comes_after_every_refusal_of_steps_6_to_10(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    error_of(run_retro(capsys, gw, "--project", "/work"), 20, "empty_scope")
    assert keys(cs) == []
    two(gw, corpus, capsys)
    key = dry(capsys, gw).doc["key"]
    add_retro(corpus.home, key, retro_id="ra-aaaaaaad", state="running", lease=live_lease(gw))
    before = keys(cs)
    error_of(run_retro(capsys, gw, "--project", "/work"), 20, "already_running")
    assert keys(cs) == before  # refused at step 10: the plan check never ran


def test_a_reused_retro_never_runs_the_plan_check(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    add_retro(corpus.home, dry(capsys, gw).doc["key"])
    before = keys(cs)
    result = run_retro(capsys, gw, "--project", "/work")
    assert result.code == 0 and result.doc["reused"] is True
    assert keys(cs) == before


# --- step 4: the skill preflight ----------------------------------------------------------------


def make_plugin(path: Path, name: str, skill: str) -> Path:
    (path / ".claude-plugin").mkdir(parents=True, mode=0o755)
    (path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), "utf-8")
    (path / "skills" / skill).mkdir(parents=True, mode=0o755)
    (path / "skills" / skill / "SKILL.md").write_text("x", "utf-8")
    for entry in [path, *path.rglob("*")]:
        entry.chmod(0o755 if entry.is_dir() else 0o644)
    return path


def test_an_unknown_skill_is_exit_12_before_the_scope_and_creates_no_state(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    error = error_of(run_retro(capsys, gw, "--project", "/work", "--skill", "nobody:audit"), 12)
    assert error["details"] == {
        "reason": "skill_not_found",
        "retryable": False,
    }  # before empty_scope would be 20
    assert not state_dir_of(corpus.home).exists() and keys(cs) == []


@pytest.mark.parametrize("skill", ["audit", "session-audit:Audit", "session-audit:a/b", "x:.."])
def test_a_malformed_skill_is_exit_2_before_claude_session_is_called(
    skill: str, corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    result = run_retro(capsys, gw, "--project", "/work", "--skill", skill)
    assert error_of(result, 2)["details"]["field"] == "--skill"
    assert cs.calls() == []


def test_a_configured_directory_with_a_group_writable_entry_is_exit_12_unsafe_plugin_dir(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path / "custom", "custom", "review")
    write_config(corpus.home, f"retro:\n  plugin_dirs:\n    - {plugin}\n")
    (plugin / "skills" / "review" / "SKILL.md").chmod(0o664)
    result = run_retro(capsys, gw, "--project", "/work", "--skill", "custom:review")
    assert error_of(result, 12, "unsafe_plugin_dir")["details"]["retryable"] is False
    assert not state_dir_of(corpus.home).exists() and keys(cs) == []
    assert retro_rows(corpus.home) == 0 if state_dir_of(corpus.home).exists() else True


def test_the_bundled_plugin_and_a_configured_one_are_passed_with_plugin_dir(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    two(gw, corpus, capsys)
    plugin = make_plugin(tmp_path / "custom", "custom", "review")
    write_config(corpus.home, f"retro:\n  plugin_dirs:\n    - {plugin}\n")
    result = dry(capsys, gw, "--project", "/work", "--skill", "custom:review")
    assert result.code == 0, result.err
    flags = result.doc["launch_flags"]
    dirs = [flags[i + 1] for i, f in enumerate(flags) if f == "--plugin-dir"]
    bundled = Path(__file__).resolve().parent.parent / "plugin"
    assert dirs == [str(bundled), str(plugin.resolve())]
    assert result.doc["skill"] == "custom:review"
    (call,) = [c for c in cs.calls() if c["key"] == "start --dry-run"]
    argv = call["argv"]
    assert argv[argv.index("--skill") + 1] == "custom:review"


def test_the_default_bundled_skill_passes_the_preflight_of_this_repository(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    assert dry(capsys, gw).code == 0
    assert isinstance(retro_run.STEPS, retro_run.Steps)  # the real step 4 is wired


def test_a_lowered_sandbox_config_is_passed_and_checked_with_a_terminal(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    write_config(corpus.home, "retro:\n  sandbox: preferred\n")
    cs.script(
        "start --dry-run", step(plan_doc({"sandbox.mode": "preferred", "sandbox_lowered": True}))
    )
    result = run_retro(capsys, gw, "--project", "/work", "--dry-run", tty=True)
    assert result.code == 0, result.err
    (call,) = [c for c in cs.calls() if c["key"] == "start --dry-run"]
    assert call["argv"][call["argv"].index("--sandbox") + 1] == "preferred"
