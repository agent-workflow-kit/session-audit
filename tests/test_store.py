"""Opening the store: directories and file modes, migrations, the newer-store refusal and the
hostile states of the directory and the files (interface.md, Files and Upgrades)."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from session_audit import store as store_module
from session_audit.errors import CliError
from session_audit.migrations import MIGRATIONS, Migration
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
@pytest.mark.parametrize("bad", [0o777, 0o770, 0o750, 0o707, 0o755, 0o500, 0o600])
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
@pytest.mark.parametrize("bad", [0o644, 0o660, 0o606, 0o666, 0o400 | 0o040])
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


@NOT_ROOT
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
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
