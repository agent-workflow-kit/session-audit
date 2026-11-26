"""``prune`` through the real CLI: every retention rule at its boundary, the report, the dry run,
the ingest lease, the log entry, the checkpoint and what ingest does afterwards."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from session_audit import removal, retention
from session_audit.cli import main
from session_audit.errors import CliError
from session_audit.lease import IngestTarget, acquire
from session_audit.store import Store, open_store, unwritable

from .golden import assert_golden
from .ingesthelp import (
    S1,
    S2,
    Corpus,
    TickGateway,
    connect,
    ingest,
    messages,
    state_dir_of,
)
from .schemas import validate

DAY = timedelta(days=1)
NOW = datetime(2026, 1, 1, tzinfo=UTC)  # the fake clock's start
NOW_MS = int(NOW.timestamp() * 1000)
OLD_MTIME = 1_700_000_000  # 2023: after the 2021 timestamps, before the fake clock


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> TickGateway:
    gateway = TickGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def home(_private_environment: Path) -> Path:
    return _private_environment


@pytest.fixture
def corpus(home: Path) -> Corpus:
    return Corpus(home)


class Out:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.out)  # type: ignore[no-any-return]


def prune(
    capsys: pytest.CaptureFixture[str], gw: TickGateway, *argv: str, actor: bool = True
) -> Out:
    capsys.readouterr()
    extra = ["--actor", "human:tester"] if actor else []
    code = main(["prune", "--json", *extra, *argv], gateway=gw, stdin_is_tty=False)
    captured = capsys.readouterr()
    return Out(code, captured.out, captured.err)


def open_db(home: Path) -> sqlite3.Connection:
    """The store (created when missing) as a plain writable connection for seeding."""
    open_store(TickGateway(), str(state_dir_of(home))).close()
    conn = connect(home)
    conn.isolation_level = None
    return conn


def count(home: Path, table: str) -> int:
    conn = connect(home)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def add_retro(
    conn: sqlite3.Connection, rid: str, state: str, created_at: int, artifact: bool = True
) -> None:
    conn.execute(
        "INSERT INTO retros (id, key, state, created_at) VALUES (?, ?, ?, ?)",
        (rid, hashlib.sha256(rid.encode()).hexdigest(), state, created_at),
    )
    if artifact:
        conn.execute("INSERT INTO retro_artifacts (retro_id, artifact) VALUES (?, '{}')", (rid,))


def add_digest(home: Path, rid: str, *, mtime: int | None = None) -> Path:
    directory = state_dir_of(home) / "digests" / rid
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    (directory / "digest.md").write_text("digest", encoding="utf-8")
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    if mtime is not None:
        os.utime(directory, (mtime, mtime))
    return directory


def settings_file(home: Path, text: str) -> str:
    config = home / "cfg.yaml"
    config.write_text(text, encoding="utf-8")
    config.chmod(0o600)
    return str(config)


# --- the report -------------------------------------------------------------------------------


def test_an_empty_store_reports_zeros_validates_and_logs_the_prune(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    run = prune(capsys, gw)
    assert run.code == 0, run.err
    validate(run.doc, "session-audit/cli/prune.json")
    assert run.doc["dry_run"] is False
    assert run.doc["recoveries"] == [] and run.doc["warnings"] == []
    assert set(run.doc["tables"].values()) == {0}
    log = (state_dir_of(home) / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    entry = json.loads(log[0])
    validate(entry, "session-audit/log.json")
    assert (entry["action"], entry["actor"], entry["recovered"]) == ("prune", "human:tester", 0)
    assert entry["tables"] == run.doc["tables"]
    assert (state_dir_of(home) / "log.jsonl").stat().st_mode & 0o777 == 0o600


def test_the_actor_defaults_for_a_non_terminal(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    assert prune(capsys, gw, actor=False).code == 0
    entry = json.loads((state_dir_of(home) / "log.jsonl").read_text(encoding="utf-8"))
    assert entry["actor"] == "auto:unknown"


def test_the_human_report_names_every_table(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    code = main(["prune", "--actor", "human:t"], gateway=gw, stdin_is_tty=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "events=0" in out and "pruned_sessions=0" in out


# --- each rule at its boundary --------------------------------------------------------------


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def test_harness_runs_older_than_the_sessions_retention_go(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    cutoff = NOW - 400 * DAY
    for name, when in (
        ("cs-aaaaaaaa", stamp(cutoff)),
        ("cs-bbbbbbbb", stamp(cutoff - timedelta(milliseconds=1))),
        ("cs-cccccccc", "not a time"),
        ("cs-dddddddd", stamp(NOW)),
    ):
        conn.execute(
            "INSERT INTO harness_runs (csession_id, updated_at) VALUES (?, ?)", (name, when)
        )
    run = prune(capsys, gw)
    assert run.doc["tables"]["harness_runs"] == 1
    left = {r[0] for r in conn.execute("SELECT csession_id FROM harness_runs")}
    assert left == {"cs-aaaaaaaa", "cs-cccccccc", "cs-dddddddd"}


def test_final_retros_older_than_the_retention_go_with_artifact_and_digest(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    cutoff = NOW_MS - 180 * 86_400_000
    cases = {
        "ra-aaaaaaaa": ("done", cutoff - 1, True),
        "ra-bbbbbbbb": ("done", cutoff, False),  # exactly at the cutoff stays
        "ra-cccccccc": ("failed", cutoff - 1, True),
        "ra-dddddddd": ("escalated", cutoff - 1, False),
        "ra-eeeeeeee": ("canceled", cutoff - 1, False),
        "ra-ffffffff": ("running", cutoff - 1, True),  # not final: never
        "ra-gggggggg": ("digesting", cutoff - 1, False),
        "ra-hhhhhhhh": ("validating", cutoff - 1, False),
    }
    for rid, (state, created, _) in cases.items():
        add_retro(conn, rid, state, created)
    for rid, (_, _, has_digest) in cases.items():
        if has_digest:
            add_digest(home, rid, mtime=1)
    run = prune(capsys, gw)
    assert run.code == 0, run.err
    assert run.doc["tables"]["retros"] == 4
    assert run.doc["tables"]["digests"] == 2
    left = {r[0] for r in conn.execute("SELECT id FROM retros")}
    assert left == {"ra-bbbbbbbb", "ra-ffffffff", "ra-gggggggg", "ra-hhhhhhhh"}
    artifacts = {r[0] for r in conn.execute("SELECT retro_id FROM retro_artifacts")}
    assert artifacts == left
    digests = {p.name for p in (state_dir_of(home) / "digests").iterdir()}
    assert digests == {"ra-ffffffff"}


def test_expired_digests_go_by_the_retro_row_else_by_the_directory_time(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    cutoff = NOW_MS - 7 * 86_400_000
    add_retro(conn, "ra-aaaaaaaa", "done", cutoff - 1)  # expired digest, retro kept
    add_retro(conn, "ra-bbbbbbbb", "done", cutoff)  # at the cutoff: kept
    add_retro(conn, "ra-cccccccc", "running", 0)  # a live retro's digest is never expired
    for rid in ("ra-aaaaaaaa", "ra-bbbbbbbb", "ra-cccccccc"):
        add_digest(home, rid)
    cutoff_s = cutoff // 1000
    add_digest(home, "ra-dddddddd", mtime=cutoff_s - 1)  # no row: the directory's own time
    add_digest(home, "ra-eeeeeeee", mtime=cutoff_s + 1)
    stray = state_dir_of(home) / "digests" / "not-a-retro"
    stray.mkdir(mode=0o700)
    os.utime(stray, (1, 1))
    run = prune(capsys, gw)
    assert run.doc["tables"]["digests"] == 2
    assert run.doc["tables"]["retros"] == 0
    names = {p.name for p in (state_dir_of(home) / "digests").iterdir()}
    assert names == {"ra-bbbbbbbb", "ra-cccccccc", "ra-eeeeeeee", "not-a-retro"}


def test_a_symlink_inside_a_digest_is_unlinked_and_never_followed(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    add_retro(conn, "ra-aaaaaaaa", "done", 0)
    directory = add_digest(home, "ra-aaaaaaaa")
    outside = home / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (directory / "link").symlink_to(outside)
    prune(capsys, gw)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not directory.exists()


def test_a_digests_directory_that_is_a_symlink_is_not_entered(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    open_db(home).close()
    target = home / "elsewhere"
    (target / "ra-aaaaaaaa").mkdir(parents=True)
    (state_dir_of(home) / "digests").symlink_to(target)
    run = prune(capsys, gw)
    assert run.code == 0
    assert (target / "ra-aaaaaaaa").is_dir()


def test_retro_starts_keep_today_and_the_two_days_before(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    for day in ("2025-12-28", "2025-12-29", "2025-12-30", "2025-12-31", "2026-01-01"):
        conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES (?, ?)", (day, "ra-aaaaaaaa"))
    run = prune(capsys, gw)
    assert run.doc["tables"]["retro_starts"] == 2
    left = [r[0] for r in conn.execute("SELECT day FROM retro_starts ORDER BY day")]
    assert left == ["2025-12-30", "2025-12-31", "2026-01-01"]


def test_retro_starts_follow_the_configured_time_zone(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    for day in ("2025-12-29", "2025-12-30"):
        conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES (?, ?)", (day, "ra-aaaaaaaa"))
    # 2026-01-01 00:00Z is still 2025-12-31 in Honolulu: the cutoff day is 2025-12-29
    config = settings_file(home, "time_zone: Pacific/Honolulu\n")
    run = prune(capsys, gw, "--config", config)
    assert run.doc["tables"]["retro_starts"] == 0


# --- retro_sessions ---------------------------------------------------------------------------


def add_retro_session(
    conn: sqlite3.Connection, name: str, project: str, state: str, created_at: int
) -> None:
    conn.execute(
        "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, project_dir, "
        "cleanup_state, created_at) VALUES (?, 'ra-aaaaaaaa', '/x', ?, ?, ?)",
        (name, project, state, created_at),
    )


def left_sessions(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT csession_id FROM retro_sessions")}


def test_a_retro_session_row_survives_while_its_project_directory_exists(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    conn = open_db(home)
    (corpus.root / "-retro-here").mkdir()
    for i, state in enumerate(("done", "incomplete", "skipped")):
        add_retro_session(conn, f"cs-hhhhhhh{i}", "-retro-here", state, NOW_MS - 1000)
        add_retro_session(conn, f"cs-gone-{i}", "-retro-gone", state, NOW_MS - 1000)
    run = prune(capsys, gw)
    assert run.doc["tables"]["retro_sessions"] == 3
    assert left_sessions(conn) == {"cs-hhhhhhh0", "cs-hhhhhhh1", "cs-hhhhhhh2"}


def test_a_row_with_cleanup_pending_is_never_removed(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    conn = open_db(home)
    add_retro_session(conn, "cs-pppppppp", "-gone", "pending", 0)
    prune(capsys, gw)
    assert left_sessions(conn) == {"cs-pppppppp"}


def test_a_row_goes_after_400_days_even_when_its_directory_exists(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    conn = open_db(home)
    (corpus.root / "-here").mkdir()
    cutoff = NOW_MS - 400 * 86_400_000
    add_retro_session(conn, "cs-aaaaaaaa", "-here", "done", cutoff)
    add_retro_session(conn, "cs-bbbbbbbb", "-here", "done", cutoff - 1)
    add_retro_session(conn, "cs-cccccccc", "-here", "pending", cutoff - 1)
    prune(capsys, gw)
    assert left_sessions(conn) == {"cs-aaaaaaaa", "cs-cccccccc"}


def test_the_directory_is_matched_by_name_never_by_prefix(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    conn = open_db(home)
    (corpus.root / "-retro-abc-longer").mkdir()
    add_retro_session(conn, "cs-aaaaaaaa", "-retro-abc", "done", NOW_MS)
    prune(capsys, gw)
    assert left_sessions(conn) == set()


@pytest.mark.parametrize("name", ["..", ".", "a/b", "../projects", "/etc"])
def test_a_name_that_is_not_one_path_component_holds_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus, name: str
) -> None:
    conn = open_db(home)
    add_retro_session(conn, "cs-aaaaaaaa", name, "done", NOW_MS)
    prune(capsys, gw)
    assert left_sessions(conn) == set()


def test_a_directory_that_is_a_symlink_still_counts_as_existing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    conn = open_db(home)
    (corpus.root / "-link").symlink_to(home / "nowhere")  # lexists: dangling is still there
    add_retro_session(conn, "cs-aaaaaaaa", "-link", "done", NOW_MS)
    prune(capsys, gw)
    assert left_sessions(conn) == {"cs-aaaaaaaa"}


def test_any_configured_root_that_holds_the_directory_keeps_the_row(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    first, second = home / "root1", home / "root2"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    (second / "-there").mkdir()
    config = settings_file(home, f"transcript_roots:\n  - {first}\n  - {second}\n")
    add_retro_session(conn, "cs-aaaaaaaa", "-there", "done", NOW_MS)
    add_retro_session(conn, "cs-bbbbbbbb", "-nowhere", "done", NOW_MS)
    prune(capsys, gw, "--config", config)
    assert left_sessions(conn) == {"cs-aaaaaaaa"}


def test_an_unreadable_root_keeps_the_rows_it_might_hold(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    root = home / "root1"
    root.mkdir(mode=0o700)
    (root / "-there").mkdir()
    config = settings_file(home, f"transcript_roots:\n  - {root}\n")
    add_retro_session(conn, "cs-aaaaaaaa", "-there", "done", NOW_MS)
    root.chmod(0)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        prune(capsys, gw, "--config", config)
    finally:
        root.chmod(0o700)
    assert left_sessions(conn) == {"cs-aaaaaaaa"}


def test_a_missing_root_holds_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    config = settings_file(home, f"transcript_roots:\n  - {home}/absent\n")
    add_retro_session(conn, "cs-aaaaaaaa", "-there", "skipped", NOW_MS)
    prune(capsys, gw, "--config", config)
    assert left_sessions(conn) == set()


# --- the log ----------------------------------------------------------------------------------


def test_old_log_entries_go_and_the_run_is_logged_after_the_rewrite(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    open_db(home).close()
    log = state_dir_of(home) / "log.jsonl"
    cutoff = NOW - 90 * DAY
    lines = []
    for i, when in enumerate(
        (cutoff - timedelta(milliseconds=1), cutoff, cutoff + timedelta(milliseconds=1), NOW)
    ):
        lines.append(
            json.dumps(
                {
                    "ts": stamp(when),
                    "actor": "auto:x",
                    "action": "retro_delete",
                    "retro_id": f"ra-{i}",
                }
            )
        )
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.chmod(0o600)
    run = prune(capsys, gw)
    assert run.doc["tables"]["log_entries"] == 1
    kept = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["retro_id"] for e in kept[:3]] == ["ra-1", "ra-2", "ra-3"]
    assert kept[3]["action"] == "prune"  # the run's own entry is the newest and is not counted
    assert not (state_dir_of(home) / "log.jsonl.tmp").exists()


# --- the dry run ------------------------------------------------------------------------------


def snapshot(home: Path) -> dict[str, Any]:
    root = state_dir_of(home)
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in ("state.db-shm",):
            files[str(path.relative_to(root))] = path.read_bytes()
    return files


def seed_everything(
    home: Path, corpus: Corpus, capsys: pytest.CaptureFixture[str], gw: TickGateway
) -> None:
    corpus.write_main(S1, messages("a", 4), mtime=OLD_MTIME)
    corpus.write_agent(S1, "x1", messages("g", 2), mtime=OLD_MTIME)
    corpus.write_main(S2, messages("b", 3), project="proj-b", mtime=OLD_MTIME)
    assert ingest(capsys, gw).code == 0
    conn = connect(home)
    conn.isolation_level = None
    conn.execute(
        "INSERT INTO harness_runs (csession_id, updated_at) "
        "VALUES ('cs-aaaaaaaa', '2000-01-01T00:00:00Z')"
    )
    add_retro(conn, "ra-aaaaaaaa", "done", 0)
    conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES ('2000-01-01', 'ra-aaaaaaaa')")
    add_retro_session(conn, "cs-aaaaaaaa", "-gone", "done", NOW_MS)
    conn.execute("INSERT INTO pruned_sessions (session_id, pruned_at) VALUES ('old', 0)")
    conn.close()
    add_digest(home, "ra-aaaaaaaa")
    add_digest(home, "ra-bbbbbbbb", mtime=1)
    (state_dir_of(home) / "log.jsonl").write_text(
        json.dumps(
            {
                "ts": "2000-01-01T00:00:00.000Z",
                "actor": "auto:x",
                "action": "retro_delete",
                "retro_id": "ra-aaaaaaaa",
                "key": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir_of(home) / "log.jsonl").chmod(0o600)


def test_the_dry_run_reports_what_the_real_run_deletes_and_writes_nothing(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    seed_everything(home, corpus, capsys, gw)
    before = snapshot(home)
    dry = prune(capsys, gw, "--dry-run")
    assert dry.code == 0, dry.err
    validate(dry.doc, "session-audit/cli/prune.json")
    assert dry.doc["dry_run"] is True
    assert snapshot(home) == before
    real = prune(capsys, gw)
    assert real.doc["tables"] == dry.doc["tables"]
    assert dry.doc["tables"] == {
        "sessions": 2,
        "agents": 1,
        "events": 9,
        "files": 3,
        "harness_runs": 1,
        "retros": 1,
        "digests": 2,
        "log_entries": 1,
        "retro_starts": 1,
        "retro_sessions": 1,
        "pruned_sessions": 1,
    }
    assert_golden(dry.doc, "prune-seeded")


def test_the_dry_run_creates_no_store(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    run = prune(capsys, gw, "--dry-run")
    assert run.code == 0
    assert set(run.doc["tables"].values()) == {0}
    assert not state_dir_of(home).exists()


def test_the_dry_run_takes_no_lease_and_leaves_no_log_line(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    seed_everything(home, corpus, capsys, gw)
    lease_before = (
        connect(home).execute("SELECT value FROM meta WHERE key='ingest_lease'").fetchone()
    )
    log_before = (state_dir_of(home) / "log.jsonl").read_bytes()
    prune(capsys, gw, "--dry-run")
    lease_after = (
        connect(home).execute("SELECT value FROM meta WHERE key='ingest_lease'").fetchone()
    )
    assert lease_before == lease_after
    assert (state_dir_of(home) / "log.jsonl").read_bytes() == log_before


# --- the ingest lease -------------------------------------------------------------------------


def hold_ingest_lease(home: Path) -> Store:
    store = open_store(TickGateway(), str(state_dir_of(home)))
    assert acquire(store, IngestTarget()) is not None
    return store


def test_a_held_ingest_lease_skips_session_retention_and_nothing_else(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    seed_everything(home, corpus, capsys, gw)
    holder = hold_ingest_lease(home)
    try:
        run = prune(capsys, gw)
        assert run.code == 0, run.err
        validate(run.doc, "session-audit/cli/prune.json")
        assert run.doc["warnings"] == ["ingest_busy"]
        tables = run.doc["tables"]
        assert (tables["sessions"], tables["events"], tables["pruned_sessions"]) == (0, 0, 0)
        assert tables["harness_runs"] == 1 and tables["retros"] == 1  # the rest ran
        assert count(home, "sessions") == 2
        dry = prune(capsys, gw, "--dry-run")
        assert dry.doc["warnings"] == ["ingest_busy"]
    finally:
        holder.close()


def test_the_lease_is_free_again_after_the_run(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    assert prune(capsys, gw).code == 0
    store = open_store(TickGateway(), str(state_dir_of(home)))
    try:
        assert acquire(store, IngestTarget()) is not None
    finally:
        store.close()


# --- review round 1 ---------------------------------------------------------------------------


def letters_id(n: int) -> str:
    return "ra-" + "".join("abcdefgh"[(n >> (3 * k)) & 7] for k in range(8))


def test_a_planted_retro_id_with_a_path_never_leaves_the_digests_directory(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    add_retro(conn, "../victim12", "done", 0)
    add_digest(home, "ra-aaaaaaaa", mtime=NOW_MS // 1000)  # the digests directory exists
    victim = state_dir_of(home) / "victim12"
    victim.mkdir(mode=0o700)
    (victim / "f").write_text("x", encoding="utf-8")
    run = prune(capsys, gw)
    assert run.code == 0, run.err
    assert (victim / "f").exists()
    assert run.doc["tables"]["retros"] == 1 and run.doc["tables"]["digests"] == 0
    assert conn.execute("SELECT COUNT(*) FROM retros").fetchone()[0] == 0


def test_deleted_retros_leave_no_bytes_in_the_database_or_its_wal(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    conn.execute("PRAGMA secure_delete=ON")  # the planting itself must leave no stale copy
    conn.execute("BEGIN")
    for i in range(300):
        add_retro(conn, letters_id(i), "done", 0)
        conn.execute(
            "UPDATE retros SET scope = ? WHERE id = ?",
            (f"DISTINCTIVE-SCOPE-{i:04d}" * 8, letters_id(i)),
        )
    conn.execute("COMMIT")
    conn.close()
    run = prune(capsys, gw)
    assert run.doc["tables"]["retros"] == 300
    for name in ("state.db", "state.db-wal"):
        path = state_dir_of(home) / name
        if path.exists():
            assert b"DISTINCTIVE-SCOPE" not in path.read_bytes(), name


def add_deep_digest(home: Path, rid: str) -> None:
    deep = add_digest(home, rid)
    for _ in range(removal.MAX_DEPTH + 2):
        deep = deep / "d"
    deep.mkdir(parents=True)


def test_a_retro_whose_digest_cannot_be_removed_stays_and_is_noted_once(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    add_retro(conn, "ra-aaaaaaaa", "done", 0)  # old, final: both its row and its digest expired
    add_retro(conn, "ra-bbbbbbbb", "done", 0)
    add_deep_digest(home, "ra-aaaaaaaa")
    add_digest(home, "ra-bbbbbbbb")
    planned = prune(capsys, gw, "--dry-run")
    assert planned.err.count("ra-aaaaaaaa") == 1
    real = prune(capsys, gw)
    assert real.err.count("ra-aaaaaaaa") == 1
    assert real.doc["tables"]["retros"] == 1 and real.doc["tables"]["digests"] == 1
    assert planned.doc["tables"] == real.doc["tables"]
    left = {r[0] for r in conn.execute("SELECT id FROM retros")}
    assert left == {"ra-aaaaaaaa"}
    assert conn.execute("SELECT COUNT(*) FROM retro_artifacts").fetchone()[0] == 1
    assert {p.name for p in (state_dir_of(home) / "digests").iterdir()} == {"ra-aaaaaaaa"}


# --- ingest after a prune ---------------------------------------------------------------------


def test_ingest_does_not_resurrect_a_pruned_session_from_an_unchanged_file(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    corpus.write_main(S1, messages("a", 4), mtime=OLD_MTIME)
    first = ingest(capsys, gw)
    assert first.doc["sessions"] == {"new": 1, "updated": 0}
    assert prune(capsys, gw).doc["tables"]["sessions"] == 1
    assert count(home, "events") == 0 and count(home, "files") == 0
    again = ingest(capsys, gw)
    assert again.doc["files"]["unchanged"] == 1 and again.doc["files"]["new"] == 0
    assert again.doc["sessions"] == {"new": 0, "updated": 0}
    assert count(home, "sessions") == 0 and count(home, "events") == 0


def test_a_file_changed_after_the_prune_is_read_from_the_start_as_a_new_session(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    corpus.write_main(S1, messages("a", 4), mtime=OLD_MTIME)
    ingest(capsys, gw)
    prune(capsys, gw)
    pruned_at = connect(home).execute("SELECT pruned_at FROM pruned_sessions").fetchone()[0]
    assert pruned_at == NOW_MS
    corpus.append_main(S1, messages("z", 2))
    newer = pruned_at // 1000 + 3600
    os.utime(corpus.main_path(S1), (newer, newer))
    run = ingest(capsys, gw)
    assert run.doc["files"]["new"] == 1 and run.doc["sessions"] == {"new": 1, "updated": 0}
    assert count(home, "events") == 6  # the whole file, from the start


def test_a_file_whose_mtime_equals_the_prune_time_stays_skipped(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    corpus.write_main(S1, messages("a", 2), mtime=OLD_MTIME)
    ingest(capsys, gw)
    prune(capsys, gw)
    os.utime(corpus.main_path(S1), (NOW_MS // 1000, NOW_MS // 1000))
    assert ingest(capsys, gw).doc["files"]["new"] == 0
    os.utime(corpus.main_path(S1), ns=((NOW_MS + 1) * 1_000_000, (NOW_MS + 1) * 1_000_000))
    assert ingest(capsys, gw).doc["files"]["new"] == 1


class Crash(BaseException):
    pass


def crash_after_first_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prune stops (as a kill would) after the first event batch of the first session."""
    real = retention.delete_session_batch
    calls = {"n": 0}

    def maybe(conn: sqlite3.Connection, session_id: str, limit: int) -> tuple[int, bool]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise Crash
        return real(conn, session_id, limit)

    monkeypatch.setattr(retention, "delete_session_batch", maybe)
    monkeypatch.setattr(retention.session_retention, "__kwdefaults__", {"batch": 2})


def half_pruned(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1 is ingested, then its prune is killed after its first batch (marker left)."""
    corpus.write_main(S1, messages("a", 6), mtime=OLD_MTIME)
    ingest(capsys, gw)
    with monkeypatch.context() as patched:
        crash_after_first_batch(patched)
        assert prune(capsys, gw).code != 0  # the run ends where a kill would have


def test_a_resumed_prune_keeps_the_time_the_session_prune_started(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    half_pruned(gw, capsys, corpus, monkeypatch)
    conn = connect(home)
    assert conn.execute("SELECT value FROM meta WHERE key = 'prune_session'").fetchone()[0] == (
        f"{S1}|{NOW_MS}"
    )
    gw.clock.advance(7200)
    assert prune(capsys, gw).code == 0
    assert conn.execute("SELECT pruned_at FROM pruned_sessions").fetchone()[0] == NOW_MS
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'prune_session'").fetchone()[0] == 0


def test_a_line_appended_to_a_half_pruned_session_is_ingested_after_the_resume(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    half_pruned(gw, capsys, corpus, monkeypatch)
    corpus.append_main(S1, messages("z", 1))
    newer = NOW_MS // 1000 + 3600
    os.utime(corpus.main_path(S1), (newer, newer))
    gw.clock.advance(7200)  # the resume runs after the append
    prune(capsys, gw)
    run = ingest(capsys, gw)
    assert run.doc["files"]["new"] == 1 and run.doc["sessions"] == {"new": 1, "updated": 0}
    keys = connect(home).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert keys == 7  # the file from its start, the appended line included


def test_an_unchanged_file_stays_skipped_after_a_resumed_prune(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    half_pruned(gw, capsys, corpus, monkeypatch)
    gw.clock.advance(7200)
    prune(capsys, gw)
    assert ingest(capsys, gw).doc["files"]["new"] == 0


def test_a_resumed_prune_skips_a_file_whose_mtime_equals_the_start_and_takes_a_later_one(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    half_pruned(gw, capsys, corpus, monkeypatch)
    gw.clock.advance(7200)
    prune(capsys, gw)
    os.utime(corpus.main_path(S1), (NOW_MS // 1000, NOW_MS // 1000))
    assert ingest(capsys, gw).doc["files"]["new"] == 0
    os.utime(corpus.main_path(S1), ns=((NOW_MS + 1) * 1_000_000, (NOW_MS + 1) * 1_000_000))
    assert ingest(capsys, gw).doc["files"]["new"] == 1


def usage_totals(capsys: pytest.CaptureFixture[str], gw: TickGateway) -> dict[str, Any]:
    capsys.readouterr()
    assert main(["usage", "--by", "model", "--json"], gateway=gw, stdin_is_tty=False) == 0
    doc = json.loads(capsys.readouterr().out)
    return {k: v for k, v in doc["totals"].items() if k != "sessions"}  # type: ignore[no-any-return]


def test_the_counted_totals_survive_the_prune_of_the_session_that_owned_the_keys(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path, corpus: Corpus
) -> None:
    corpus.write_main(S1, messages("a", 4), mtime=OLD_MTIME)
    corpus.write_main(S2, messages("a", 4), project="proj-b", mtime=OLD_MTIME)  # the same keys
    ingest(capsys, gw)
    conn = connect(home)
    conn.isolation_level = None
    (owner,) = conn.execute("SELECT DISTINCT session_id FROM events WHERE counted = 1").fetchone()
    other = S2 if owner == S1 else S1
    conn.execute(  # the other session is young by its file, the owner old by its counted events
        "UPDATE files SET max_ts = ? WHERE id = (SELECT main_file_id FROM sessions "
        "WHERE session_id = ?)",
        (NOW_MS, other),
    )
    before = usage_totals(capsys, gw)
    assert before
    assert prune(capsys, gw).doc["tables"]["sessions"] == 1
    assert conn.execute("SELECT DISTINCT session_id FROM events WHERE counted = 1").fetchall() == [
        (other,)
    ]
    assert usage_totals(capsys, gw) == before


# --- the checkpoint and what stays on disk ----------------------------------------------------


def test_deleted_rows_leave_the_database_and_the_log_of_changes(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    conn = open_db(home)
    secret = "SECRET-RETRO-TEXT-" + "x" * 300
    add_retro(conn, "ra-aaaaaaaa", "done", 0, artifact=False)
    conn.execute(
        "INSERT INTO retro_artifacts (retro_id, artifact) VALUES ('ra-aaaaaaaa', ?)", (secret,)
    )
    conn.close()
    assert prune(capsys, gw).code == 0
    root = state_dir_of(home)
    assert b"SECRET-RETRO-TEXT" not in (root / "state.db").read_bytes()
    wal = root / "state.db-wal"
    assert (
        not wal.exists() or wal.stat().st_size == 0 or b"SECRET-RETRO-TEXT" not in wal.read_bytes()
    )


def test_a_busy_checkpoint_is_a_retryable_exit_12(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy(self: Store) -> None:
        raise unwritable("another connection is reading state.db", retryable=True)

    monkeypatch.setattr(Store, "checkpoint", busy)
    run = prune(capsys, gw)
    assert run.code == 12
    assert "store_unwritable" in run.err


def test_a_step_that_fails_still_attempts_the_checkpoint_and_wins_the_exit_code(
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit.commands import prune as prune_module

    calls: list[str] = []
    monkeypatch.setattr(Store, "checkpoint", lambda self: calls.append("checkpoint"))

    def boom(*args: object, **kwargs: object) -> None:
        raise CliError(
            "failed",
            "the log cannot be written",
            details={"reason": "store_unwritable", "retryable": True},
        )

    monkeypatch.setattr(prune_module, "log_entries", boom)
    run = prune(capsys, gw)
    assert run.code == 12
    assert calls == ["checkpoint"]


def test_an_unsafe_config_refuses_with_exit_20(
    gw: TickGateway, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    config = home / "cfg.yaml"
    config.write_text("time_zone: UTC\n", encoding="utf-8")
    config.chmod(0o666)
    assert prune(capsys, gw, "--config", str(config)).code == 20
    assert not state_dir_of(home).exists()
