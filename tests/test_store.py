"""Opening the store: directories and file modes, migrations, the newer-store refusal and the
hostile states of the directory and the files (interface.md, Files and Upgrades)."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

import pytest

from session_audit import store as store_module
from session_audit.errors import CliError
from session_audit.migrations import MIGRATIONS, Migration, validate_registry
from session_audit.migrations.m800_initial_schema import MIGRATION as M800
from session_audit.store import inspect_store, open_store, state_dir

from .storehelp import V1, V2, V3, ClockedGateway, extra, foreign_gateway, ledger, state_path

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def mode(path: str | Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def refused(
    directory: str, code: str, reason: str, gateway: ClockedGateway | None = None
) -> CliError:
    with pytest.raises(CliError) as caught:
        open_store(gateway or ClockedGateway(), directory)
    assert caught.value.code == code
    assert caught.value.details["reason"] == reason
    return caught.value


# --- where the state directory is -----------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"XDG_STATE_HOME": "/x/state"}, "/x/state/session-audit"),
        ({}, "/h/.local/state/session-audit"),
        ({"XDG_STATE_HOME": ""}, "/h/.local/state/session-audit"),
        ({"XDG_STATE_HOME": "relative/dir"}, "/h/.local/state/session-audit"),
        ({"XDG_STATE_HOME": "/x/../y/"}, "/y/session-audit"),
        ({"XDG_STATE_HOME": "/x\x00y"}, "/h/.local/state/session-audit"),
    ],
)
def test_state_dir_follows_xdg(env: dict[str, str], expected: str) -> None:
    assert state_dir(env, "/h") == expected


# --- a fresh store --------------------------------------------------------------------------


def test_fresh_store_has_the_ledger_meta_and_pragmas(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    with open_store(ClockedGateway(), directory) as store:
        assert store.version == MIGRATIONS[-1].number
        conn = store.conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        assert meta == {"rules_version": 1, "schema_version": MIGRATIONS[-1].number}
        row = conn.execute("SELECT number, name, applied_at FROM schema_migrations").fetchone()
        assert row == (800, "initial_schema", 1767225600000)  # the fake clock's epoch, in ms
    assert ledger(directory) == [m.number for m in MIGRATIONS]


def test_every_table_of_the_data_model_exists(tmp_path: Path) -> None:
    with open_store(ClockedGateway(), state_path(tmp_path)) as store:
        names = {
            r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert names >= {
        "files", "sessions", "agents", "events", "pruned_sessions", "harness_runs", "retros",
        "retro_artifacts", "retro_starts", "retro_sessions", "meta",
    }  # fmt: skip


def test_reopening_changes_nothing(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory).close()
    gateway.clock.advance(3600)
    with open_store(gateway, directory) as store:
        assert store.conn.execute(
            "SELECT count(*), max(applied_at) FROM schema_migrations"
        ).fetchone() == (
            1,
            1767225600000,
        )


# --- file modes -----------------------------------------------------------------------------


@pytest.mark.parametrize("mask", [0o000, 0o002, 0o022, 0o077, 0o277])
def test_modes_are_exact_under_any_umask(tmp_path: Path, mask: int) -> None:
    directory = state_path(tmp_path)
    previous = os.umask(mask)
    try:
        with open_store(ClockedGateway(), directory) as store:
            store.conn.execute("CREATE TABLE IF NOT EXISTS scratch (a)")
            store.conn.execute("INSERT INTO scratch VALUES (1)")  # makes the -wal file
            assert os.umask(mask) == mask  # the umask is back as it was
            assert mode(tmp_path / "state") == 0o700  # a created parent is private too
            assert mode(directory) == 0o700
            assert mode(f"{directory}/state.db") == 0o600
            assert mode(f"{directory}/state.db-wal") == 0o600
            assert mode(f"{directory}/state.db-shm") == 0o600
    finally:
        os.umask(previous)


def test_a_pre_existing_parent_keeps_its_mode(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(mode=0o755)
    (tmp_path / "state").chmod(0o755)
    open_store(ClockedGateway(), state_path(tmp_path)).close()
    assert mode(tmp_path / "state") == 0o755


# --- migrations -----------------------------------------------------------------------------


def tables(directory: str) -> set[str]:
    conn = sqlite3.connect(f"{directory}/state.db")
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("first", "then"), [(None, V3), (V1, V2), (V1, V3), (V2, V3), (V3, V3), (None, V1)]
)
def test_migration_from_empty_and_from_each_earlier_version(
    tmp_path: Path, first: tuple[Migration, ...] | None, then: tuple[Migration, ...]
) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    if first is not None:
        with open_store(gateway, directory, migrations=first) as store:
            store.conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
    with open_store(gateway, directory, migrations=then) as store:
        assert store.version == max(m.number for m in then)
        assert store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == (max(m.number for m in then),)
        if first is not None:
            assert store.conn.execute("SELECT value FROM meta WHERE key='probe'").fetchone() == (
                "kept",
            )
    assert ledger(directory) == sorted(m.number for m in then)
    assert {m.name[4:] for m in then[1:]} <= tables(directory)


def test_a_migration_that_lands_late_is_still_applied(tmp_path: Path) -> None:
    """A store that took 1300 (a later branch) still gets 900 when that branch lands."""
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory, migrations=(MIGRATIONS[0], extra(1300, "t_thirteen"))).close()
    with open_store(gateway, directory, migrations=V3) as store:
        assert store.version == 1300
    assert ledger(directory) == [800, 900, 1300]
    assert {"t_nine", "t_thirteen"} <= tables(directory)


def test_a_failing_migration_leaves_the_store_as_it_was(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory, migrations=V1).close()
    broken = Migration(
        900,
        "broken",
        (
            "CREATE TABLE half_done (id INTEGER PRIMARY KEY)",
            "CREATE TABLE half_done (id INTEGER PRIMARY KEY)",  # fails: the table exists
        ),
    )
    with pytest.raises(CliError) as caught:
        open_store(gateway, directory, migrations=(*V1, broken))
    assert caught.value.details["reason"] == "store_unwritable"
    assert ledger(directory) == [800]
    assert "half_done" not in tables(directory)
    open_store(gateway, directory, migrations=V2).close()  # and a later good one still applies
    assert ledger(directory) == [800, 900]


def test_disk_full_during_a_migration_is_store_unwritable_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory, migrations=V1).close()
    real_connect = sqlite3.connect

    def tiny(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count={pages}")  # no room to grow: SQLITE_FULL
        return conn

    monkeypatch.setattr(store_module.sqlite3, "connect", tiny)
    error = refused_with(directory, V2, gateway)
    assert error.details["reason"] == "store_unwritable"
    assert error.hint == "the disk is full"
    monkeypatch.undo()
    assert ledger(directory) == [800]


def refused_with(
    directory: str, migrations: tuple[Migration, ...], gateway: ClockedGateway
) -> CliError:
    with pytest.raises(CliError) as caught:
        open_store(gateway, directory, migrations=migrations)
    return caught.value


def test_concurrent_first_opens_apply_each_migration_once(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            open_store(ClockedGateway(), directory, migrations=V3).close()
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert ledger(directory) == [800, 900, 1300]
    assert mode(f"{directory}/state.db") == 0o600


# --- a newer store --------------------------------------------------------------------------


def make_newer(directory: str) -> None:
    open_store(ClockedGateway(), directory, migrations=V3).close()


def test_a_newer_store_is_refused_and_not_touched(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    make_newer(directory)
    before = {n: (Path(directory) / n).read_bytes() for n in os.listdir(directory)}
    mtimes = {n: os.stat(f"{directory}/{n}").st_mtime_ns for n in before}
    # The default registry knows only 800: the stored 900 and 1300 are unknown to it.
    error = refused(directory, "refused", "store_newer")
    assert error.exit_code == 20
    assert {n: (Path(directory) / n).read_bytes() for n in os.listdir(directory)} == before
    assert {n: os.stat(f"{directory}/{n}").st_mtime_ns for n in before} == mtimes
    assert ledger(directory) == [800, 900, 1300]  # never downgraded


def test_newer_is_refused_even_with_a_pending_migration(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory, migrations=(extra(5000, "future"),)).close()
    with pytest.raises(CliError) as caught:
        open_store(gateway, directory, migrations=V2)  # it also lacks 800 and 900
    assert caught.value.details["reason"] == "store_newer"
    assert ledger(directory) == [5000]


# --- inspect (validate) ---------------------------------------------------------------------


def test_inspect_missing_creates_nothing(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    assert inspect_store(ClockedGateway(), directory).state == "missing"
    assert not (tmp_path / "state").exists()
    Path(directory).mkdir(parents=True, mode=0o700)
    assert inspect_store(ClockedGateway(), directory).state == "missing"
    assert os.listdir(directory) == []


def test_inspect_reports_pending_without_applying(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory, migrations=V1).close()
    status = inspect_store(ClockedGateway(), directory, migrations=V3)
    assert (status.state, status.version, status.latest, status.pending) == (
        "pending",
        800,
        1300,
        (900, 1300),
    )
    assert ledger(directory) == [800]


def test_inspect_ok_and_newer(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    make_newer(directory)
    assert inspect_store(ClockedGateway(), directory, migrations=V3).state == "ok"
    newer = inspect_store(ClockedGateway(), directory, migrations=V1)
    assert (newer.state, newer.version) == ("newer", 1300)


def test_inspect_is_read_only_on_an_open_store(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    with open_store(ClockedGateway(), directory):
        assert inspect_store(ClockedGateway(), directory).state == "ok"


# --- hostile directories and files ----------------------------------------------------------


@NOT_ROOT
@pytest.mark.parametrize(
    "bad", [0o777, 0o770, 0o750, 0o707, 0o755, 0o500, 0o600, 0o2700, 0o1700, 0o4700]
)
def test_an_existing_directory_with_a_wrong_mode_is_refused_and_not_repaired(
    tmp_path: Path, bad: int
) -> None:
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True)
    directory.chmod(bad)
    try:
        refused(str(directory), "failed", "store_unwritable")
        assert mode(directory) == bad
        assert os.listdir(directory) == []
    finally:
        directory.chmod(0o700)


def test_a_symlinked_state_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "state").mkdir()
    link = Path(state_path(tmp_path))
    link.symlink_to(real)
    refused(str(link), "failed", "store_unwritable")
    assert os.listdir(real) == []


def test_a_dangling_symlink_as_state_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    link = Path(state_path(tmp_path))
    link.symlink_to(tmp_path / "nowhere")
    refused(str(link), "failed", "store_unwritable")
    assert not (tmp_path / "nowhere").exists()


def test_a_state_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    Path(state_path(tmp_path)).write_text("x")
    refused(state_path(tmp_path), "failed", "store_unwritable")


def test_a_foreign_owned_directory_is_refused(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    refused(directory, "failed", "store_unwritable", foreign_gateway())


def test_a_foreign_owned_database_is_refused(tmp_path: Path) -> None:
    """The directory is the user's own (a fake user id that matches), the database is not."""
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.db").write_bytes(b"")
    (directory / "state.db").chmod(0o600)
    owner = os.lstat(directory).st_uid

    class DirectoryOnly(ClockedGateway):
        def uid(self) -> int:
            # asked for the directory first, then for the files: the files are "someone else's"
            self.asked = getattr(self, "asked", 0) + 1
            return owner if self.asked == 1 else owner + 1

    refused(str(directory), "failed", "store_unwritable", DirectoryOnly())
    assert (directory / "state.db").read_bytes() == b""


@NOT_ROOT
@pytest.mark.parametrize("bad", [0o644, 0o660, 0o606, 0o666, 0o400 | 0o040, 0o2600, 0o1600, 0o4600])
def test_a_database_with_a_wrong_mode_is_refused_and_not_repaired(tmp_path: Path, bad: int) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    os.chmod(f"{directory}/state.db", bad)
    before = Path(f"{directory}/state.db").read_bytes()
    refused(directory, "failed", "store_unwritable")
    assert mode(f"{directory}/state.db") == bad
    assert Path(f"{directory}/state.db").read_bytes() == before


def test_a_symlinked_database_is_refused(tmp_path: Path) -> None:
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    target.chmod(0o600)
    (directory / "state.db").symlink_to(target)
    refused(str(directory), "failed", "store_unwritable")
    assert target.read_bytes() == b""


def test_a_fifo_as_database_is_refused_without_blocking(tmp_path: Path) -> None:
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True, mode=0o700)
    os.mkfifo(directory / "state.db", 0o600)
    refused(str(directory), "failed", "store_unwritable")


def test_a_hard_linked_database_is_refused(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    os.link(f"{directory}/state.db", tmp_path / "alias.db")
    refused(directory, "failed", "store_unwritable")


def test_a_side_file_unlinked_during_the_check_counts_as_missing(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    wal = f"{directory}/state.db-wal"

    class Unlinking(ClockedGateway):
        def lstat(self, path):  # type: ignore[no-untyped-def]
            info = super().lstat(path)
            if str(path) == wal:
                # another connection's close unlinked the log: lstat reports no links left
                fields = list(info)
                fields[stat.ST_NLINK] = 0
                return os.stat_result(fields)
            return info

    Path(wal).write_bytes(b"")
    Path(wal).chmod(0o600)
    open_store(Unlinking(), directory).close()


@NOT_ROOT
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_side_file_with_a_wrong_mode_is_refused(tmp_path: Path, suffix: str) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    side = Path(f"{directory}/state.db{suffix}")
    side.write_bytes(b"")
    side.chmod(0o666)
    refused(directory, "failed", "store_unwritable")


@pytest.mark.parametrize(
    "content", [b"this is not a database at all" * 50, b"\x00" * 4096 + b"SQLite format 3\x00"]
)
def test_a_corrupt_database_is_refused_and_kept(tmp_path: Path, content: bytes) -> None:
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.db").write_bytes(content)
    (directory / "state.db").chmod(0o600)
    error = refused(str(directory), "failed", "store_unwritable")
    assert error.details["retryable"] is False
    assert (directory / "state.db").read_bytes() == content


def test_a_truncated_database_is_refused(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    path = Path(f"{directory}/state.db")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    refused(directory, "failed", "store_unwritable")


def test_a_foreign_sqlite_database_is_not_adopted(tmp_path: Path) -> None:
    directory = Path(state_path(tmp_path))
    directory.mkdir(parents=True, mode=0o700)
    conn = sqlite3.connect(directory / "state.db")
    conn.execute("CREATE TABLE someone_elses (a)")
    conn.commit()
    conn.close()
    (directory / "state.db").chmod(0o600)
    refused(str(directory), "failed", "store_unwritable")
    conn = sqlite3.connect(directory / "state.db")
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master")} == {"someone_elses"}
    conn.close()


@NOT_ROOT
def test_a_read_only_parent_is_store_unwritable(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir(mode=0o500)
    try:
        refused(str(parent / "session-audit"), "failed", "store_unwritable")
    finally:
        parent.chmod(0o700)


@NOT_ROOT
def test_a_read_only_database_file_is_store_unwritable(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    os.chmod(f"{directory}/state.db", 0o400)  # no group/other bits, but not writable by us
    try:
        error = refused(directory, "failed", "store_unwritable")
    finally:
        os.chmod(f"{directory}/state.db", 0o600)
    assert error.exit_code == 12


# --- locking --------------------------------------------------------------------------------


def test_a_lock_held_too_long_is_retryable_store_unwritable(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    gateway = ClockedGateway()
    open_store(gateway, directory, migrations=V1).close()
    holder = sqlite3.connect(f"{directory}/state.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(CliError) as caught:
            open_store(gateway, directory, migrations=V2, busy_timeout_ms=100)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert caught.value.details == {"reason": "store_unwritable", "retryable": True}
    assert ledger(directory) == [800]


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    with open_store(ClockedGateway(), state_path(tmp_path)) as store:
        with store.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('a', 1)")
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('b', 2)")
            raise RuntimeError
        keys = {r[0] for r in store.conn.execute("SELECT key FROM meta")}
    assert "a" in keys and "b" not in keys


def test_transaction_maps_a_busy_store(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    with open_store(ClockedGateway(), directory, busy_timeout_ms=100) as store:
        holder = sqlite3.connect(f"{directory}/state.db", isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(CliError) as caught, store.transaction():
                pass
        finally:
            holder.execute("ROLLBACK")
            holder.close()
    assert caught.value.details["retryable"] is True


# --- secure delete and the checkpoint -------------------------------------------------------


def test_checkpoint_truncates_the_wal_and_deleted_bytes_are_gone(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    secret = b"SECRET-MARKER-" + b"x" * 200
    with open_store(ClockedGateway(), directory) as store:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO retro_artifacts (retro_id, artifact, summary) VALUES ('r', ?, '')",
                (secret.decode(),),
            )
        assert os.path.getsize(f"{directory}/state.db-wal") > 0
        store.checkpoint()
        assert os.path.getsize(f"{directory}/state.db-wal") == 0
        assert secret in Path(f"{directory}/state.db").read_bytes()
        with store.transaction() as conn:
            conn.execute("DELETE FROM retro_artifacts")
        store.checkpoint()
        assert os.path.getsize(f"{directory}/state.db-wal") == 0
    assert secret not in Path(f"{directory}/state.db").read_bytes()
    assert b"SECRET-MARKER" not in Path(f"{directory}/state.db").read_bytes()


# --- the review round 1 items ---------------------------------------------------------------

CHILD_OPEN = """
import sys, time
from session_audit.gateway import SystemGateway
from session_audit.migrations import MIGRATIONS
from session_audit.store import open_store
directory, start = sys.argv[1], float(sys.argv[2])
while time.time() < start:
    pass
open_store(SystemGateway(), directory).close()
"""


def test_concurrent_first_opens_in_processes_all_succeed(tmp_path: Path) -> None:
    """The switch to WAL fails at once while another process holds the file: it is retried."""
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src}
    for round_ in range(4):
        directory = state_path(tmp_path / f"r{round_}")
        start = str(time.time() + 1.5)
        children = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD_OPEN, directory, start],
                env=env,
                stderr=subprocess.PIPE,
            )
            for _ in range(5)
        ]
        for child in children:
            code = child.wait(timeout=120)
            assert code == 0, child.stderr.read() if child.stderr else b""
        assert ledger(directory) == [800]


def test_a_wal_switch_that_stays_busy_gives_up_at_the_budget(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()  # now WAL, so use a rollback-mode database
    for name in os.listdir(directory):
        if name != "state.db":
            os.unlink(f"{directory}/{name}")
    raw = sqlite3.connect(f"{directory}/state.db", isolation_level=None)
    raw.execute("PRAGMA journal_mode=DELETE")
    raw.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(CliError) as caught:
            open_store(ClockedGateway(), directory, busy_timeout_ms=300)
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    assert caught.value.details == {"reason": "store_unwritable", "retryable": True}
    assert time.monotonic() - started < 10


class _BudgetGateway(ClockedGateway):
    """A gateway whose clock only moves when told to."""

    def __init__(self) -> None:
        super().__init__()
        self.now_s = 0.0

    def monotonic(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.now_s += seconds


def test_open_store_gives_each_attempt_only_the_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = state_path(tmp_path)
    gateway = _BudgetGateway()
    seen: list[int] = []
    real = store_module._open_connection

    def busy(gw, conn, db_path, migrations, left):  # type: ignore[no-untyped-def]
        seen.append(left)
        gateway.now_s += 0.1  # the attempt itself waited 100 ms
        if len(seen) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(gw, conn, db_path, migrations, left)

    monkeypatch.setattr(store_module, "_open_connection", busy)
    open_store(gateway, directory, busy_timeout_ms=1000).close()
    assert seen[0] == 1000 and len(seen) == 3
    assert seen[1] <= 1000 - 100 and seen[2] <= seen[1] - 100


def test_inspect_retries_plain_read_only_when_a_wal_appears_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    for name in os.listdir(directory):
        if name != "state.db":
            os.unlink(f"{directory}/{name}")
    wal = f"{directory}/state.db-wal"
    targets: list[str] = []
    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):  # type: ignore[no-untyped-def]
        targets.append(str(target))
        if "immutable=1" in str(target):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *args, **kwargs)

    class Racing(ClockedGateway):
        def __init__(self) -> None:
            super().__init__()
            self.wal_looks = 0

        def lstat(self, path):  # type: ignore[no-untyped-def]
            if str(path) == wal:
                self.wal_looks += 1
                if not targets:
                    raise FileNotFoundError(path)  # absent at the check, present afterwards
                return os.lstat(f"{directory}/state.db")
            return super().lstat(path)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    assert inspect_store(Racing(), directory).state == "ok"
    assert "immutable=1" in targets[0]
    assert "immutable" not in targets[1]


def test_checkpoint_with_a_reader_open_is_a_retryable_error(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    secret = "SECRET-MARKER-" + "x" * 200
    with open_store(ClockedGateway(), directory, busy_timeout_ms=100) as store:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO retro_artifacts (retro_id, artifact) VALUES ('r', ?)", (secret,)
            )
        store.checkpoint()
        reader = sqlite3.connect(f"{directory}/state.db", isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM meta").fetchall()
        with store.transaction() as conn:
            conn.execute("DELETE FROM retro_artifacts")
        try:
            with pytest.raises(CliError) as caught:
                store.checkpoint()
        finally:
            reader.execute("ROLLBACK")
            reader.close()
        assert caught.value.details == {"reason": "store_unwritable", "retryable": True}
        assert directory not in (caught.value.hint or "")
        store.checkpoint()  # the reader is gone: now it finishes
    assert b"SECRET-MARKER" not in Path(f"{directory}/state.db").read_bytes()


def test_inspect_of_a_cleanly_closed_store_creates_no_side_files(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    before = sorted(os.listdir(directory))
    assert before == ["state.db"]
    assert inspect_store(ClockedGateway(), directory).state == "ok"
    assert sorted(os.listdir(directory)) == before


def test_inspect_with_a_wal_present_still_sees_the_log(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    with open_store(ClockedGateway(), directory, migrations=V1) as store:
        with store.transaction() as conn:
            conn.execute("INSERT INTO schema_migrations VALUES (99999, 'future', 1)")
        assert os.path.exists(f"{directory}/state.db-wal")
        assert inspect_store(ClockedGateway(), directory, migrations=V1).state == "newer"


def make_foreign_ledger(directory: str, number: object) -> None:
    """A store whose ledger table is not ours: ``number`` can hold anything."""
    os.makedirs(directory, mode=0o700)
    conn = sqlite3.connect(f"{directory}/state.db", isolation_level=None)
    conn.execute("CREATE TABLE schema_migrations (number, name, applied_at)")
    conn.execute("INSERT INTO schema_migrations VALUES (?, 'x', 0)", (number,))
    conn.close()
    os.chmod(f"{directory}/state.db", 0o600)


@pytest.mark.parametrize("number", ["abc", None, 1.5, b"\x00"])
def test_a_malformed_ledger_is_store_unwritable_not_a_crash(tmp_path: Path, number: object) -> None:
    directory = state_path(tmp_path)
    make_foreign_ledger(directory, number)
    error = refused(directory, "failed", "store_unwritable")
    assert "abc" not in (error.hint or "")
    with pytest.raises(CliError) as caught:
        inspect_store(ClockedGateway(), directory)
    assert caught.value.details["reason"] == "store_unwritable"


def test_a_bad_statement_in_a_transaction_is_a_bug_not_a_store_error(tmp_path: Path) -> None:
    with open_store(ClockedGateway(), state_path(tmp_path)) as store:
        for sql in ("SELECT nosuchcol FROM files", "SELECT * FROM nosuchtable", "SELEC 1"):
            with pytest.raises(sqlite3.OperationalError) as caught, store.transaction() as conn:
                conn.execute(sql)
            assert not isinstance(caught.value, CliError)
        assert store.conn.in_transaction is False


def test_a_constraint_violation_is_not_a_store_error(tmp_path: Path) -> None:
    with open_store(ClockedGateway(), state_path(tmp_path)) as store:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO events (key, file_id, line_offset, kind) VALUES ('s', 1, 0, 'x')"
            )


@pytest.mark.parametrize("code", [5, 6, 8, 10, 11, 13, 14, 26, 5 | (1 << 8), 10 | (2 << 8)])
def test_storage_failure_codes_map_to_store_unwritable(code: int) -> None:
    exc = sqlite3.OperationalError("whatever")
    exc.sqlite_errorcode = code
    assert store_module.is_storage_failure(exc)


@pytest.mark.parametrize("code", [1, 19, 20, 21, 25])
def test_other_result_codes_are_not_storage_failures(code: int) -> None:
    exc = sqlite3.OperationalError("database is locked")  # the text must not decide
    exc.sqlite_errorcode = code
    assert not store_module.is_storage_failure(exc)


@pytest.mark.parametrize("mask", [0o777, 0o000])
def test_open_store_creates_the_directories_under_any_umask(tmp_path: Path, mask: int) -> None:
    before = os.umask(mask)
    try:
        directory = state_path(tmp_path)
        open_store(ClockedGateway(), directory).close()
    finally:
        os.umask(before)
    assert mode(directory) == 0o700 and mode(f"{directory}/state.db") == 0o600


def test_a_symlinked_journal_is_refused(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    os.symlink(target, f"{directory}/state.db-journal")
    refused(directory, "failed", "store_unwritable")
    with pytest.raises(CliError):
        inspect_store(ClockedGateway(), directory)
    assert target.read_bytes() == b""


def test_a_duplicate_migration_number_is_a_programming_error(tmp_path: Path) -> None:
    clash = (M800, extra(900, "t_a"), extra(900, "t_b"))
    with pytest.raises(ValueError, match="twice"):
        open_store(ClockedGateway(), state_path(tmp_path), migrations=clash)
    with pytest.raises(ValueError, match="twice"):
        inspect_store(ClockedGateway(), state_path(tmp_path), migrations=clash)
    assert not os.path.exists(state_path(tmp_path))  # nothing was created


def test_an_unsorted_registry_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ascending"):
        open_store(
            ClockedGateway(),
            state_path(tmp_path),
            migrations=(M800, extra(1300, "b"), extra(900, "a")),
        )


def test_the_real_registry_is_valid() -> None:
    validate_registry(MIGRATIONS)


class WriteDenied(ClockedGateway):
    """A file system where state.db can be read but not opened for writing (read-only mount)."""

    def open_private(
        self,
        path: str | os.PathLike[str],
        *,
        create: bool,
        append: bool = False,
        mode: int = 0o600,
        read_only: bool = False,
    ) -> BinaryIO:
        if not read_only:
            raise PermissionError(13, "denied", os.fspath(path))
        return super().open_private(path, create=create, append=append, mode=mode, read_only=True)


def test_inspect_reads_a_database_that_cannot_be_opened_for_writing(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    assert inspect_store(WriteDenied(), directory).state == "ok"


def test_open_gives_a_distinct_hint_when_state_db_is_not_writable(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    error = refused(directory, "failed", "store_unwritable", WriteDenied())
    assert error.hint == "state.db is not writable"
    assert error.details["retryable"] is False


@NOT_ROOT
def test_a_0400_database_is_refused_by_both_paths_without_a_crash(tmp_path: Path) -> None:
    directory = state_path(tmp_path)
    open_store(ClockedGateway(), directory).close()
    os.chmod(f"{directory}/state.db", 0o400)
    try:
        error = refused(directory, "failed", "store_unwritable")
        assert error.hint == "state.db is not writable"
        with pytest.raises(CliError) as caught:
            inspect_store(ClockedGateway(), directory)
        assert caught.value.details["reason"] == "store_unwritable"
    finally:
        os.chmod(f"{directory}/state.db", 0o600)
