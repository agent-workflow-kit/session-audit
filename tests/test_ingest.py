"""``ingest``: the run, its report, the lease, the dry run, rebuild, budgets, upgrades and the meta
counters, through the real CLI on a synthetic corpus under the private home."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from session_audit.lease import IngestTarget, acquire
from session_audit.store import open_store

from .eventhelp import message
from .golden import assert_golden
from .ingesthelp import (
    S1,
    S2,
    S3,
    Corpus,
    TickGateway,
    bad_line,
    connect,
    counted_tokens,
    ingest,
    messages,
    meta,
    scalar,
    state_dir_of,
    unknown_line,
)
from .schemas import validate


@pytest.fixture
def gw() -> TickGateway:
    return TickGateway()


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


def seed_basic(corpus: Corpus) -> None:
    corpus.write_main(S1, [*messages("a", 3), bad_line(), unknown_line()])
    corpus.write_agent(
        S1, "x1", messages("ag", 2), meta={"agentType": "Explore", "toolUseId": "toolu_1"}
    )
    corpus.write_main(S2, messages("b", 4), project="proj-b")


# --- the report -------------------------------------------------------------------------------


def test_first_run_reports_every_count_and_matches_its_schema(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    run = ingest(capsys, gw)
    assert run.code == 0
    doc = run.doc
    validate(doc, "session-audit/cli/ingest.json")
    assert doc["files"] == {
        "seen": 3,
        "new": 3,
        "changed": 0,
        "unchanged": 0,
        "unsafe": 0,
        "excluded": 0,
        "vanished": 0,
        "rewritten": 0,
        "remaining": 0,
    }
    assert doc["lines"] == {"parsed": 11, "errors": 1, "oversized": 0, "unknown": 1}
    assert doc["sessions"] == {"new": 2, "updated": 0}
    assert (doc["complete"], doc["already_running"], doc["dry_run"]) == (True, False, False)
    assert doc["harness"] == {"enabled": False, "matched": 0, "captured": 0, "errors": 0}
    assert doc["warnings"] == []
    assert_golden(doc, "ingest-first-run")
    assert counted_tokens(_private_environment) == 90
    assert scalar(_private_environment, "SELECT agent_type FROM agents") == "Explore"


def test_second_run_changes_nothing_and_writes_no_row(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    before = scalar(_private_environment, "SELECT max(last_ingest_at) FROM sessions")
    gw.clock.advance(60)
    run = ingest(capsys, gw)
    doc = run.doc
    assert doc["files"]["unchanged"] == 3
    assert doc["files"]["new"] == doc["files"]["changed"] == doc["files"]["rewritten"] == 0
    assert doc["lines"]["parsed"] == 0
    assert doc["sessions"] == {"new": 0, "updated": 0}
    assert scalar(_private_environment, "SELECT max(last_ingest_at) FROM sessions") == before
    assert counted_tokens(_private_environment) == 90


def test_appended_bytes_are_read_once(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    corpus.append_main(S1, [message("late", 7, t=5)])
    doc = ingest(capsys, gw).doc
    assert doc["files"]["changed"] == 1 and doc["files"]["unchanged"] == 2
    assert doc["lines"]["parsed"] == 1
    assert doc["sessions"] == {"new": 0, "updated": 1}
    assert counted_tokens(_private_environment) == 97
    assert ingest(capsys, gw).doc["files"]["unchanged"] == 3


def test_a_partial_tail_is_not_unchanged_and_not_read(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 2))
    ingest(capsys, gw)
    with open(corpus.main_path(S1), "ab") as handle:
        handle.write(b'{"type": "assis')  # no LF yet: waits for the next run
    doc = ingest(capsys, gw).doc
    assert doc["files"]["changed"] == 1 and doc["files"]["unchanged"] == 0
    assert doc["lines"]["parsed"] == 0
    assert counted_tokens(_private_environment) == 20


def test_a_rewritten_file_is_reread_and_not_double_counted(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 3))
    ingest(capsys, gw)
    corpus.write_main(S1, messages("z", 2, out=50))  # shorter: the size is below the offset
    doc = ingest(capsys, gw).doc
    assert doc["files"]["rewritten"] == 1 and doc["files"]["changed"] == 0
    assert counted_tokens(_private_environment) == 100


def test_unsafe_vanished_and_warnings_are_counted_and_sanitized(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corpus.write_main(S1, messages("a", 1))
    (corpus.root / "linked").symlink_to(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "gone\x1b[31m\nname"))
    run = ingest(capsys, gw)
    assert run.code == 0
    doc = run.doc
    validate(doc, "session-audit/cli/ingest.json")
    assert doc["files"]["unsafe"] == 1
    assert doc["complete"] is True
    [warning] = doc["warnings"]
    assert warning.startswith("root_unusable: ") and "(missing)" in warning
    assert "\x1b" not in warning and "\n" not in warning


def test_no_readable_root_is_exit_12(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    corpus.root.rmdir()
    run = ingest(capsys, gw)
    assert run.code == 12
    assert json.loads(run.out)["error"]["details"]["reason"] == "no_readable_roots"


def test_human_output_is_sanitized_text(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus.write_main(S1, messages("a", 1))
    run = ingest(capsys, gw, json_output=False)
    assert run.code == 0
    assert run.out.splitlines()[0] == "ingest: complete"
    assert "files.new=1" in run.out


# --- the lease --------------------------------------------------------------------------------


def hold_lease(gw: TickGateway, home: Path):  # type: ignore[no-untyped-def]
    store = open_store(gw, str(state_dir_of(home)))
    lease = acquire(store, IngestTarget())
    assert lease is not None
    return store, lease


def test_a_held_lease_reports_already_running_and_writes_nothing(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 3))
    store, lease = hold_lease(gw, _private_environment)
    try:
        run = ingest(capsys, gw)
    finally:
        lease.release()
        store.close()
    assert run.code == 0
    doc = run.doc
    validate(doc, "session-audit/cli/ingest.json")
    assert doc["already_running"] is True and doc["complete"] is False
    assert all(v == 0 for v in doc["files"].values())
    assert all(v == 0 for v in doc["lines"].values()) and doc["sessions"] == {
        "new": 0,
        "updated": 0,
    }
    assert doc["warnings"] == []
    assert scalar(_private_environment, "SELECT count(*) FROM events") == 0
    assert not (state_dir_of(_private_environment) / "log.jsonl").exists()
    assert "last_ingest_at" not in meta(_private_environment)
    assert_golden(doc, "ingest-already-running")


def test_rebuild_under_a_held_lease_warns_naming_rebuild(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 3))
    store, lease = hold_lease(gw, _private_environment)
    try:
        run = ingest(capsys, gw, "--rebuild", "--yes")
    finally:
        lease.release()
        store.close()
    assert run.code == 0
    doc = run.doc
    assert doc["already_running"] is True
    assert any("--rebuild" in w for w in doc["warnings"])
    validate(doc, "session-audit/cli/ingest.json")


def test_the_lease_is_released_after_a_run_and_after_a_failure(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus.write_main(S1, messages("a", 3))
    ingest(capsys, gw)
    assert meta(_private_environment)["ingest_lease"] == ""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated")

    monkeypatch.setattr("session_audit.commands.ingest.read_file", boom)
    corpus.append_main(S1, [message("late", 7, t=5)])
    run = ingest(capsys, gw)
    assert run.code == 1
    assert meta(_private_environment)["ingest_lease"] == ""
    assert meta(_private_environment)["fail_count"] == 1


def test_the_lease_is_released_on_keyboard_interrupt(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus.write_main(S1, messages("a", 3))

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("session_audit.commands.ingest.read_file", interrupt)
    run = ingest(capsys, gw)
    assert run.code == 130
    assert meta(_private_environment)["ingest_lease"] == ""
    assert "fail_count" not in meta(_private_environment)  # an interrupt is not a failure


# --- dry run ----------------------------------------------------------------------------------


def snapshot(home: Path) -> dict[str, str]:
    """The sha256 of every file in the state directory (empty when it does not exist)."""
    found: dict[str, str] = {}
    directory = state_dir_of(home)
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                found[str(path.relative_to(directory))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return found


def test_dry_run_with_no_store_creates_nothing(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    run = ingest(capsys, gw, "--dry-run")
    assert run.code == 0
    doc = run.doc
    validate(doc, "session-audit/cli/ingest.json")
    assert doc["dry_run"] is True and doc["complete"] is True
    assert doc["files"]["new"] == 3 and doc["lines"]["parsed"] == 11
    assert doc["lines"]["errors"] == 1 and doc["lines"]["unknown"] == 1
    assert doc["sessions"] == {"new": 2, "updated": 0}
    assert not state_dir_of(_private_environment).exists()
    assert gw.runs == []


def test_dry_run_with_a_store_writes_nothing_and_reports_what_would_change(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    corpus.append_main(S1, [message("late", 7, t=5)])
    corpus.write_main(S3, messages("c", 2), project="proj-c")
    before = snapshot(_private_environment)
    doc = ingest(capsys, gw, "--dry-run").doc
    assert snapshot(_private_environment) == before
    assert doc["files"]["new"] == 1 and doc["files"]["changed"] == 1
    assert doc["files"]["unchanged"] == 2
    assert doc["lines"]["parsed"] == 3
    assert doc["sessions"] == {"new": 1, "updated": 1}


def test_dry_run_ignores_a_held_lease_and_takes_none(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 3))
    store, lease = hold_lease(gw, _private_environment)
    try:
        doc = ingest(capsys, gw, "--dry-run").doc
    finally:
        lease.release()
        store.close()
    assert doc["already_running"] is False and doc["files"]["new"] == 1


def test_dry_run_with_the_session_flag_is_a_usage_error(
    gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    run = ingest(capsys, gw, "--dry-run", "--session", "cs-abcdefgh")
    assert run.code == 2 and run.doc["error"]["details"]["field"] == "--dry-run"
    assert gw.runs == []


def test_dry_run_leaves_a_pending_migration_pending(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 1))
    ingest(capsys, gw)
    conn = connect(_private_environment)
    conn.execute(
        "DELETE FROM schema_migrations WHERE number = (SELECT max(number) FROM schema_migrations)"
    )
    conn.commit()
    conn.close()
    before = snapshot(_private_environment)
    assert ingest(capsys, gw, "--dry-run").code == 0
    assert snapshot(_private_environment) == before


# --- rebuild ----------------------------------------------------------------------------------


def test_rebuild_rereads_every_file_and_keeps_rows_of_files_that_are_gone(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    corpus.main_path(S2, "proj-b").unlink()
    run = ingest(capsys, gw, "--rebuild", "--yes")
    doc = run.doc
    assert run.code == 0 and doc["complete"] is True
    assert doc["files"]["rewritten"] == 2 and doc["files"]["changed"] == 0
    assert doc["files"]["new"] == 0
    # The vanished file's rows and events stay; the rebuilt ones are not counted twice.
    assert counted_tokens(_private_environment) == 90
    assert scalar(_private_environment, "SELECT count(*) FROM sessions") == 2
    assert scalar(_private_environment, "SELECT count(*) FROM files") == 3
    assert scalar(_private_environment, "SELECT count(*) FROM files WHERE rules_version = 1") == 3


def test_rebuild_dry_run_reports_rewritten_without_confirmation_and_writes_nothing(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    before = snapshot(_private_environment)
    run = ingest(capsys, gw, "--rebuild", "--dry-run")
    assert run.code == 0
    assert run.doc["files"]["rewritten"] == 3 and run.doc["files"]["unchanged"] == 0
    assert snapshot(_private_environment) == before


def test_rebuild_without_yes_and_without_a_terminal_is_a_usage_error(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    run = ingest(capsys, gw, "--rebuild")
    assert run.code == 2
    assert run.doc["error"]["details"]["reason"] == "confirmation_required"


def test_an_interrupted_rebuild_is_finished_by_the_next_plain_run(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    for i, session in enumerate((S1, S2, S3)):
        corpus.write_main(session, messages(f"f{i}", 20), project=f"p{i}")
    ingest(capsys, gw)
    gw.tick = 1.0
    first = ingest(capsys, gw, "--rebuild", "--yes", "--budget", "30s").doc
    gw.tick = 0.0
    assert first["complete"] is False and first["files"]["remaining"] >= 1
    second = ingest(capsys, gw).doc
    assert second["complete"] is True and second["files"]["remaining"] == 0
    assert first["files"]["rewritten"] + second["files"]["rewritten"] >= 3
    assert counted_tokens(_private_environment) == 600
    assert scalar(_private_environment, "SELECT count(*) FROM files WHERE rules_version < 1") == 0


# --- budgets ----------------------------------------------------------------------------------


def test_a_run_stopped_by_its_budget_exits_0_and_the_next_run_resumes_without_loss(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    sessions = (S1, S2, S3)
    for i, session in enumerate(sessions):
        corpus.write_main(session, messages(f"f{i}", 40), project=f"p{i}")
    gw.tick = 1.0
    complete_after = None
    parsed = 0
    for n in range(1, 6):
        run = ingest(capsys, gw, "--budget", "50s")
        assert run.code == 0
        validate(run.doc, "session-audit/cli/ingest.json")
        parsed += run.doc["lines"]["parsed"]
        if n == 1:
            assert run.doc["complete"] is False and run.doc["files"]["remaining"] >= 1
            assert "last_complete_ingest_at" not in meta(_private_environment)
            assert "last_ingest_at" in meta(_private_environment)
        if run.doc["complete"]:
            complete_after = n
            break
    assert complete_after is not None and complete_after > 1
    assert parsed == 120  # every line read exactly once across the runs
    assert counted_tokens(_private_environment) == 1200
    assert "last_complete_ingest_at" in meta(_private_environment)


def test_the_file_budget_stops_one_file_and_the_run_goes_on(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("big", 200))
    corpus.write_main(S2, messages("small", 2), project="p2")
    gw.tick = 1.0
    gw.clock.advance(0)
    # ingest.file_budget 60 s: the big file stops after about 60 readings, the small one is read.
    doc = ingest(capsys, gw, "--budget", "10m").doc
    assert doc["complete"] is False
    assert doc["files"]["remaining"] == 1
    assert doc["files"]["new"] == 2


# --- upgrades ---------------------------------------------------------------------------------


def test_a_bumped_rules_version_rereads_every_file_and_clears_old_rules(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    conn = connect(_private_environment)
    conn.execute("UPDATE meta SET value = 2 WHERE key = 'rules_version'")
    conn.commit()
    conn.close()
    doc = ingest(capsys, gw).doc
    assert doc["files"]["rewritten"] == 3 and doc["files"]["unchanged"] == 0
    assert scalar(_private_environment, "SELECT count(*) FROM files WHERE rules_version = 2") == 3
    assert counted_tokens(_private_environment) == 90
    assert ingest(capsys, gw).doc["files"]["unchanged"] == 3


def test_a_gone_file_below_the_current_rules_keeps_its_old_version(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    corpus.main_path(S2, "proj-b").unlink()
    conn = connect(_private_environment)
    conn.execute("UPDATE meta SET value = 2 WHERE key = 'rules_version'")
    conn.commit()
    conn.close()
    ingest(capsys, gw)
    # The gone file's row is untouched: derive reads it as old_rules.
    assert scalar(_private_environment, "SELECT count(*) FROM files WHERE rules_version = 1") == 1


# --- --since and --path -----------------------------------------------------------------------


def test_since_considers_every_file_of_a_session_with_a_recent_one(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    old = 1_600_000_000
    recent = int(gw.now().timestamp()) + 10
    corpus.write_main(S1, messages("a", 2), mtime=old)
    corpus.write_agent(S1, "x1", messages("ag", 2), mtime=recent)  # the agent is recent
    corpus.write_main(S2, messages("b", 2), project="proj-b", mtime=old)
    doc = ingest(capsys, gw, "--since", "2021-01-01").doc
    assert doc["files"]["new"] == 2  # S1's main and agent; S2 is out
    assert doc["files"]["seen"] == 2
    assert scalar(_private_environment, "SELECT count(*) FROM sessions") == 1
    assert "last_complete_ingest_at" not in meta(_private_environment)


def test_path_restricts_the_run_and_a_path_outside_the_roots_is_exit_2(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    corpus.write_main(S1, messages("a", 2), project="proj-a")
    corpus.write_main(S2, messages("b", 2), project="proj-b")
    doc = ingest(capsys, gw, "--path", str(corpus.root / "proj-b")).doc
    assert doc["files"]["new"] == 1
    run = ingest(capsys, gw, "--path", str(tmp_path))
    assert run.code == 2 and run.doc["error"]["details"]["field"] == "--path"


def test_a_dry_run_starts_no_process(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus.write_main(S1, messages("a", 2))
    assert ingest(capsys, gw, "--dry-run").code == 0 and gw.runs == []


# --- meta and log -----------------------------------------------------------------------------


def test_a_completed_run_sets_the_freshness_meta_and_appends_one_log_entry(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw, "--actor", "auto:test")
    values = meta(_private_environment)
    assert values["last_ingest_at"] == values["last_complete_ingest_at"]
    assert values["fail_count"] == 0 and "first_failed_at" not in values
    [line] = (state_dir_of(_private_environment) / "log.jsonl").read_text().splitlines()
    entry = json.loads(line)
    validate(entry, "session-audit/log.json")
    assert entry["action"] == "ingest" and entry["actor"] == "auto:test"
    assert entry["complete"] is True and entry["files"]["new"] == 3
    assert str(_private_environment) not in line


def test_a_path_run_logs_but_does_not_claim_a_complete_ingest(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 2))
    ingest(capsys, gw, "--path", str(corpus.root))
    values = meta(_private_environment)
    assert "last_ingest_at" in values and "last_complete_ingest_at" not in values
    assert "fail_count" not in values  # not a full ingest: the counters are untouched


def test_full_ingest_failures_count_up_and_a_success_resets_them(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    root = corpus.root
    root.rmdir()
    assert ingest(capsys, gw).code == 12
    first = meta(_private_environment)
    assert first["fail_count"] == 1 and first["first_failed_at"] == first["last_failed_at"]
    gw.clock.advance(60)
    assert ingest(capsys, gw).code == 12
    second = meta(_private_environment)
    assert second["fail_count"] == 2
    assert second["first_failed_at"] == first["first_failed_at"]
    assert second["last_failed_at"] > first["last_failed_at"]
    # Runs that are not a full ingest never count: a dry run, a rebuild.
    assert ingest(capsys, gw, "--rebuild", "--yes").code == 12
    assert meta(_private_environment)["fail_count"] == 2
    root.mkdir(mode=0o700)
    Corpus(_private_environment).write_main(S1, messages("a", 1))
    assert ingest(capsys, gw).code == 0
    third = meta(_private_environment)
    assert third["fail_count"] == 0 and "first_failed_at" not in third


def test_a_partial_full_ingest_also_resets_the_failure_counters(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 40))
    corpus.root.chmod(0o770)  # unusable root: every ingest fails with exit 12
    assert ingest(capsys, gw).code == 12
    corpus.root.chmod(0o700)
    gw.tick = 1.0
    run = ingest(capsys, gw, "--budget", "10s")
    assert run.code == 0 and run.doc["complete"] is False
    assert meta(_private_environment)["fail_count"] == 0


def test_already_running_never_counts_as_a_failure(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 1))
    store, lease = hold_lease(gw, _private_environment)
    try:
        assert ingest(capsys, gw).code == 0
    finally:
        lease.release()
        store.close()
    assert "fail_count" not in meta(_private_environment)


def test_ingest_failing_is_due_at_the_threshold_and_quiet_for_24_hours() -> None:
    import sqlite3

    from session_audit.commands.ingest import ingest_failing_due

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value)")
    now = 1_700_000_000_000

    def put(key: str, value: object) -> None:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    assert ingest_failing_due(conn, 3, now) is None
    put("fail_count", 2)
    assert ingest_failing_due(conn, 3, now) is None
    put("fail_count", 3)
    put("first_failed_at", now - 7_200_000)
    put("last_failed_at", now - 1000)
    due = ingest_failing_due(conn, 3, now)
    assert due is not None and due["consecutive_failures"] == 3
    validate(
        {
            "tool": "session-audit",
            "hook": "post-attention",
            "object_type": "ingest",
            "object_id": "ingest",
            "event_id": "ev-" + "a" * 26,
            "labels": [],
            "reason": "ingest_failing",
            "details": due,
        },
        "session-audit/hooks/post-attention.json",
    )
    put("last_fired_at", now - 3_600_000)
    assert ingest_failing_due(conn, 3, now) is None
    put("last_fired_at", now - 86_400_001)
    assert ingest_failing_due(conn, 3, now) is not None
    put("fail_count", b"garbled")
    assert ingest_failing_due(conn, 3, now) is None


def test_ids_at_or_above_2_to_the_63_compare_signed() -> None:
    import sqlite3

    from session_audit.commands.ingest import _probably_unchanged
    from session_audit.reader import Candidate

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE files (path BLOB, dev INTEGER, inode INTEGER, offset INTEGER, "
        "rules_version INTEGER, rereading INTEGER)"
    )
    big = 2**63 + 5
    conn.execute("INSERT INTO files VALUES (?, ?, ?, 10, 1, 0)", (b"/p", big - 2**64, big - 2**64))
    cand = Candidate(0, (b"p",), b"/p", "main", S1, None, None, b"p", 10, 0, big, big)
    assert _probably_unchanged(conn, cand, 1, False) is True
    assert _probably_unchanged(conn, cand, 2, False) is False
    assert _probably_unchanged(conn, cand, 1, True) is False
    _ = os


# --- live facts on a synthetic corpus ---------------------------------------------------------


def test_a_compaction_boundary_is_one_compaction_and_a_resumed_session_stays_one_session(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    from .eventhelp import compaction

    # A resumed session appends to the same file; lines may carry another sessionId (never used
    # to join), and one boundary line marks the compaction.
    corpus.write_main(S1, [message("m1", 5, t=1), compaction("cmp-1", t=2)])
    ingest(capsys, gw)
    corpus.append_main(S1, [message("m2", 6, t=3, session=S3), message("m3", 7, t=4)])
    doc = ingest(capsys, gw).doc
    assert doc["files"]["changed"] == 1
    assert scalar(_private_environment, "SELECT count(*) FROM sessions") == 1
    assert (
        scalar(
            _private_environment,
            "SELECT count(*) FROM events WHERE kind = 'compaction' AND counted = 1",
        )
        == 1
    )
    assert scalar(_private_environment, "SELECT count(DISTINCT session_id) FROM events") == 1
    assert scalar(_private_environment, "SELECT session_id FROM events LIMIT 1") == S1


def test_store_size_on_a_scaled_down_synthetic_corpus_stays_bounded(
    gw: TickGateway, tmp_path: Path, _private_environment: Path
) -> None:
    # The full measurement is tests/measure_store_size.py (design.md, Live verification); this is
    # the regression guard: the store holds a few hundred bytes per event, never the lines.
    from .measure_store_size import write_corpus

    corpus = Corpus(_private_environment)
    corpus_bytes = write_corpus(corpus.root, 4, 500)
    gw.clock.advance(0)
    from session_audit.cli import main

    assert main(["ingest", "--json", "--budget", "10m"], gateway=gw, stdin_is_tty=False) == 0
    conn = connect(_private_environment)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()
    size = (state_dir_of(_private_environment) / "state.db").stat().st_size
    assert events == 2400
    assert size / events < 600
    assert size < 3 * corpus_bytes


# --- review round 1 ---------------------------------------------------------------------------

S4 = "44444444-4444-4444-8444-444444444444"


def test_a_dry_run_holds_no_connection_to_the_store_during_the_scan(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from session_audit.commands import ingest as ingest_module

    seed_basic(corpus)
    ingest(capsys, gw)
    opened: list[sqlite3.Connection] = []
    real_open = ingest_module.open_read_only
    real_scan = ingest_module.scan

    def tracking(*args: object, **kwargs: object) -> sqlite3.Connection | None:
        conn = real_open(*args, **kwargs)  # type: ignore[arg-type]
        assert conn is not None
        opened.append(conn)
        return conn

    def checking(ctx, args, discovery, conn, *rest, **kwargs):  # type: ignore[no-untyped-def]
        assert len(opened) == 1 and conn is not opened[0]
        with pytest.raises(sqlite3.ProgrammingError):  # the file connection is closed
            opened[0].execute("SELECT 1")
        return real_scan(ctx, args, discovery, conn, *rest, **kwargs)

    monkeypatch.setattr(ingest_module, "open_read_only", tracking)
    monkeypatch.setattr(ingest_module, "scan", checking)
    corpus.append_main(S1, [message("late", 7, t=5)])
    doc = ingest(capsys, gw, "--dry-run").doc
    assert doc["files"]["changed"] == 1 and doc["files"]["unchanged"] == 2


def test_a_lease_lost_during_the_run_writes_no_log_line_and_counts_one_failure(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit.commands import ingest as ingest_module

    seed_basic(corpus)
    real_scan = ingest_module.scan

    def stealing(*args: object, **kwargs: object) -> object:
        tally = real_scan(*args, **kwargs)  # type: ignore[arg-type]
        conn = connect(_private_environment)
        conn.execute("UPDATE meta SET value = '' WHERE key = 'ingest_lease'")
        conn.commit()
        conn.close()
        return tally

    monkeypatch.setattr(ingest_module, "scan", stealing)
    run = ingest(capsys, gw)
    assert run.code == 1
    assert not (state_dir_of(_private_environment) / "log.jsonl").exists()
    values = meta(_private_environment)
    assert values["fail_count"] == 1
    assert "last_ingest_at" not in values


def test_a_budget_stop_with_only_unchanged_files_left_is_complete(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    seed_basic(corpus)
    ingest(capsys, gw)
    gw.clock.advance(60)
    before = meta(_private_environment)["last_complete_ingest_at"]
    gw.tick = 1.0
    doc = ingest(capsys, gw, "--budget", "1s").doc
    assert doc["complete"] is True and doc["files"]["remaining"] == 0
    assert meta(_private_environment)["last_complete_ingest_at"] > before


def test_excluded_and_pruned_files_are_not_remaining_after_a_budget_stop(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    corpus.write_main(S1, messages("a", 2), project="proj-a")
    ingest(capsys, gw)
    corpus.write_main(S2, messages("b", 2), project="proj-b")  # excluded
    corpus.write_main(S3, messages("c", 2), project="proj-c")  # pruned
    corpus.write_main(S4, messages("d", 2), project="proj-d")  # really remaining
    conn = connect(_private_environment)
    conn.execute(
        "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, project_dir, created_at)"
        " VALUES ('cs-aaaaaaaa', 'ra-x', '/x', 'proj-b', 1)"
    )
    conn.execute("INSERT INTO pruned_sessions (session_id, pruned_at) VALUES (?, ?)", (S3, 2**50))
    conn.commit()
    conn.close()
    gw.tick = 1.0
    doc = ingest(capsys, gw, "--budget", "1s").doc
    assert doc["complete"] is False
    assert doc["files"]["remaining"] == 1


def test_dry_run_counts_equal_live_counts_with_an_unsafe_meta_file(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = corpus.write_agent(
        S1, "x1", messages("ag", 2), meta={"agentType": "Explore", "toolUseId": "toolu_1"}
    )
    corpus.write_main(S1, messages("a", 2))
    side = agent.with_name("agent-x1.meta.json")
    side.unlink()
    side.symlink_to(agent)  # a symlinked meta file is unsafe
    dry = ingest(capsys, gw, "--dry-run").doc
    live = ingest(capsys, gw).doc
    assert live["files"]["unsafe"] >= 1
    assert dry["files"] == live["files"]


def test_since_still_counts_unsafe_entries_it_cannot_attribute_to_a_session(
    gw: TickGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    corpus.write_main(S1, messages("a", 2), mtime=1_600_000_000)
    (corpus.root / "linked").symlink_to(tmp_path)
    doc = ingest(capsys, gw, "--since", "2021-01-01").doc
    assert doc["files"]["new"] == 0
    assert doc["files"]["unsafe"] == 1


def test_a_root_warning_is_sanitized_once(
    gw: TickGateway,
    corpus: Corpus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corpus.write_main(S1, messages("a", 1))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "gone\x01a<b"))
    [warning] = ingest(capsys, gw).doc["warnings"]
    assert "gone<U+0001>a<b" in warning
    assert "<U+003C>" not in warning


def test_ingest_failing_due_reads_a_real_meta_row(
    gw: TickGateway, _private_environment: Path
) -> None:
    from session_audit.commands.ingest import ingest_failing_due, record_failure

    store = open_store(gw, str(state_dir_of(_private_environment)))
    try:
        now = 1_700_000_000_000
        for _ in range(3):
            record_failure(store, now)
        due = ingest_failing_due(store.conn, 3, now + 1000)
        assert due is not None and due["consecutive_failures"] == 3
        assert ingest_failing_due(store.conn, 4, now + 1000) is None
    finally:
        store.close()


def test_measure_store_size_refuses_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    from .measure_store_size import run

    keep = tmp_path / "precious"
    keep.mkdir()
    (keep / "data.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit):
        run(1, 5, keep, "1m", False)
    assert (keep / "data.txt").read_text(encoding="utf-8") == "keep me"
