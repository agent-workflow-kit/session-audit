"""The store: ``state.db`` (SQLite, WAL) and the files beside it, opened safely and migrated.

What opening guarantees (interface.md, Files and Upgrades; design.md, Store):

* The state directory is ``$XDG_STATE_HOME/session-audit`` (``~/.local/state`` when the variable
  is unset, empty or not absolute). Missing directories are created with mode 0700, exactly,
  whatever the umask. An existing state directory, ``state.db``, ``state.db-wal`` and
  ``state.db-shm`` and ``state.db-journal`` must be real (no symlink, no FIFO), owned by this
  user, with exactly mode 0700 (directory) or 0600 (files; no group or other bit, no setuid,
  setgid or sticky bit) and a single link; anything else is refused as ``store_unwritable`` and
  nothing is changed, repaired or deleted. ``state.db`` is created 0600 before SQLite sees it.
* Every connection waits up to 30 s for a lock (``busy_timeout``; the switch to WAL mode, which
  SQLite refuses at once while another process holds the file, is retried inside the same
  budget), runs in WAL mode and has ``secure_delete`` on (a per-connection setting: every
  connection that writes must set it, and only ``Store.conn`` writes). ``checkpoint()`` is what
  ``retro delete`` and ``prune`` end with.
* Migrations (``migrations/``) are applied in number order, each in one ``BEGIN IMMEDIATE``
  transaction that also records it in the ``schema_migrations`` ledger, so a failure (disk full,
  a crash) leaves the store as it was and a concurrent opener applies each exactly once. A store
  whose ledger holds a number this code does not know is newer: ``open_store`` refuses it with
  ``store_newer`` (exit 20) and never touches it.
* ``inspect_store`` is the read-only view for ``validate``: it creates nothing, applies nothing
  and reports ``missing``, ``ok``, ``pending`` or ``newer``. It opens the database ``mode=ro``,
  and ``immutable=1`` as well when no ``-wal`` file exists, so SQLite creates no ``-wal`` or
  ``-shm`` file beside a cleanly closed store.

Every failure that is about the store being unusable (permissions, ownership, corruption, a lock
held too long, a full or read-only disk) is a ``CliError`` ``failed`` with reason
``store_unwritable``; the message is fixed and the hint names the cause without a path or any
database text.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat as statmod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from . import errors
from .gateway import Gateway
from .log import LogWriter
from .migrations import MIGRATIONS, Migration, validate_registry

PROGRAM_DIR = "session-audit"
DB_NAME = "state.db"
DIR_MODE = 0o700
FILE_MODE = 0o600
BUSY_TIMEOUT_MS = 30_000
LEDGER = "schema_migrations"
_SIDE_FILES = ("-wal", "-shm", "-journal")
_RETRY_SECONDS = 0.02

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DB_NAME",
    "Store",
    "StoreStatus",
    "inspect_store",
    "open_store",
    "state_dir",
]


def state_dir(env: Mapping[str, str], home: str) -> str:
    """``$XDG_STATE_HOME/session-audit`` when the variable is an absolute path, else
    ``<home>/.local/state/session-audit``."""
    xdg = env.get("XDG_STATE_HOME", "")
    base = xdg if xdg.startswith("/") and "\x00" not in xdg else f"{home}/.local/state"
    return os.path.normpath(f"{base}/{PROGRAM_DIR}")


# --- errors ---------------------------------------------------------------------------------


def unwritable(hint: str, *, retryable: bool = False) -> errors.CliError:
    return errors.CliError(
        "failed",
        "the state directory or state.db cannot be created or written",
        details={"reason": "store_unwritable", "retryable": retryable},
        hint=hint,
    )


def store_newer() -> errors.CliError:
    return errors.CliError(
        "refused",
        "state.db was written by a newer version of session-audit",
        details={"reason": "store_newer"},
        hint="Upgrade session-audit; the store is not changed (only validate can read it).",
    )


def storage_error(exc: BaseException) -> errors.CliError:
    """The error for an ``OSError`` or ``sqlite3.Error`` met while using the store. Only the cause
    class is reported, never the text of the exception (it may hold a path)."""
    if isinstance(exc, sqlite3.Error):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return unwritable("another process holds state.db; try again", retryable=True)
        if "full" in text:
            return unwritable("the disk is full")
        if "readonly" in text or "read-only" in text:
            return unwritable("state.db or its directory is read-only")
        if "not a database" in text or "malformed" in text or "corrupt" in text:
            return unwritable(
                "state.db is corrupt; move it aside to start a new store (history is lost)"
            )
        if "disk i/o" in text:
            return unwritable("state.db cannot be read or written (disk I/O error)")
        return unwritable("state.db cannot be opened or written")
    if isinstance(exc, OSError):
        if exc.errno in (28, 122):  # ENOSPC, EDQUOT
            return unwritable("the disk is full")
        if exc.errno == 30:  # EROFS
            return unwritable("the state directory is on a read-only file system")
        if exc.errno == 40:  # ELOOP: a final symlink
            return unwritable("a state file is a symlink")
        if exc.errno in (1, 13):  # EPERM, EACCES
            return unwritable("state.db is not writable")
        return unwritable("the state directory or state.db cannot be created or written")
    raise exc


# Primary result codes that mean the store (not the statement) is the problem: busy, locked,
# read-only, I/O error, corrupt, full, cannot open, not a database.
_STORAGE_CODES = frozenset({5, 6, 8, 10, 11, 13, 14, 26})
_STORAGE_WORDS = (
    "locked",
    "busy",
    "full",
    "readonly",
    "read-only",
    "disk i/o",
    "unable to open",
    "not a database",
    "malformed",
    "corrupt",
)


def is_storage_failure(exc: BaseException) -> bool:
    """True for the sqlite errors that mean the store is unusable (a lock held too long, a full,
    read-only, unreadable or corrupt database). A bad statement (no such table or column, a syntax
    error, a constraint) is a bug in the caller and is not one."""
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in _STORAGE_CODES
    text = str(exc).lower()
    return any(word in text for word in _STORAGE_WORDS)


def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in (5, 6)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _retry_busy[T](gateway: Gateway, budget_ms: int, action: Callable[[], T]) -> T:
    """Run ``action``, repeating it while SQLite reports busy or locked, until ``budget_ms`` has
    passed. Some statements (the switch to WAL mode among them) fail at once instead of waiting
    for ``busy_timeout``. ``action`` must be safe to repeat."""
    deadline = gateway.monotonic() + budget_ms / 1000
    while True:
        try:
            return action()
        except sqlite3.Error as exc:
            if not _is_busy(exc) or gateway.monotonic() >= deadline:
                raise
        gateway.sleep(_RETRY_SECONDS)


# --- the directory and the files ------------------------------------------------------------


def _check_dir(info: os.stat_result, uid: int) -> None:
    if statmod.S_ISLNK(info.st_mode):
        raise unwritable("the state directory is a symlink; it is not followed")
    if not statmod.S_ISDIR(info.st_mode):
        raise unwritable("the state directory path is not a directory")
    if info.st_uid != uid:
        raise unwritable("the state directory is not owned by this user")
    if info.st_mode & 0o7777 != DIR_MODE:
        raise unwritable("the state directory must have mode 0700; fix it with chmod 700")


def _check_file(info: os.stat_result, uid: int, name: str) -> None:
    if not statmod.S_ISREG(info.st_mode):
        raise unwritable(f"{name} is not a regular file")
    if info.st_uid != uid:
        raise unwritable(f"{name} is not owned by this user")
    if info.st_mode & 0o7777 != FILE_MODE:
        raise unwritable(f"{name} must have mode 0600; fix it with chmod 600")
    if info.st_nlink != 1:
        raise unwritable(f"{name} has more than one hard link")


def _prepare_directory(gateway: Gateway, directory: str, *, create: bool) -> bool:
    """Check the state directory; create it (and missing parents, 0700) when ``create``.
    Returns False when it does not exist and ``create`` is false."""
    try:
        info = gateway.lstat(directory)
    except FileNotFoundError:
        if not create:
            return False
        missing: list[str] = []
        probe = directory
        while True:
            try:
                gateway.lstat(probe)
                break
            except FileNotFoundError:
                missing.append(probe)
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
        for path in reversed(missing):
            try:
                gateway.mkdir(path, DIR_MODE)
            except FileExistsError:
                pass  # created meanwhile by another process; checked below
        info = gateway.lstat(directory)
    _check_dir(info, gateway.uid())
    return True


def _check_side_files(gateway: Gateway, db_path: str) -> None:
    for suffix in _SIDE_FILES:
        try:
            info = gateway.lstat(db_path + suffix)
        except FileNotFoundError:
            continue
        _check_file(info, gateway.uid(), DB_NAME + suffix)


def _check_db_file(
    gateway: Gateway, db_path: str, *, create: bool, read_only: bool = False
) -> bool:
    """Check ``state.db`` through an open descriptor (what is checked is what is opened); create it
    0600 when ``create`` and missing. False when it is missing and not created. A read-only caller
    (``inspect_store``) opens it read-only, so a file or file system it cannot write is still
    checked."""
    try:
        handle = gateway.open_private(db_path, create=create, read_only=read_only)
    except FileNotFoundError:
        return False
    except PermissionError:
        raise unwritable("state.db is not writable") from None
    try:
        _check_file(os.fstat(handle.fileno()), gateway.uid(), DB_NAME)
    finally:
        handle.close()
    return True


# --- the ledger and the migrations ----------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


def _read_ledger(conn: sqlite3.Connection) -> set[int] | None:
    """The applied numbers; None for a database with no ledger yet. A database that holds tables
    but no ledger is not a session-audit store."""
    tables = _tables(conn)
    if LEDGER not in tables:
        if tables:
            raise unwritable("state.db is not a session-audit store")
        return None
    numbers: set[int] = set()
    for (number,) in conn.execute(f"SELECT number FROM {LEDGER}"):
        if type(number) is not int:  # a malformed ledger: not ours (the value is not echoed)
            raise unwritable("state.db is not a session-audit store (its ledger is malformed)")
        numbers.add(number)
    return numbers


@dataclass(frozen=True)
class StoreStatus:
    """What ``inspect_store`` found. ``version`` is the highest applied number (0 when none)."""

    state: Literal["missing", "ok", "pending", "newer"]
    version: int = 0
    latest: int = 0
    pending: tuple[int, ...] = ()


def _classify(
    applied: set[int] | None, migrations: Sequence[Migration]
) -> tuple[set[int], list[Migration], bool]:
    known = {m.number for m in migrations}
    done = applied or set()
    pending = [m for m in sorted(migrations, key=lambda m: m.number) if m.number not in done]
    return done, pending, bool(done - known)


def _apply(
    conn: sqlite3.Connection, migration: Migration, migrations: Sequence[Migration], now_ms: int
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {LEDGER} (number INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL CHECK (length(name) <= 64), applied_at INTEGER NOT NULL)"
        )
        done, _, newer = _classify(_read_ledger(conn), migrations)
        if newer:
            raise store_newer()
        if migration.number not in done:  # a concurrent opener may have applied it
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {LEDGER} (number, name, applied_at) VALUES (?, ?, ?)",
                (migration.number, migration.name, now_ms),
            )
            if "meta" in _tables(conn):  # every real store has it (migration 800)
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = max(value, excluded.value)",
                    (migration.number,),
                )
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _epoch_ms(gateway: Gateway) -> int:
    return int(gateway.now().timestamp() * 1000)


# --- the store ------------------------------------------------------------------------------


class Store:
    """An open store. ``conn`` autocommits (``isolation_level`` None): a writer uses
    ``transaction()``. One ``Store`` is for one thread."""

    def __init__(
        self,
        gateway: Gateway,
        directory: str,
        conn: sqlite3.Connection,
        version: int,
    ) -> None:
        self.gateway = gateway
        self.directory = directory
        self.conn = conn
        self.version = version
        self.log = LogWriter(gateway, directory)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): commit on success, roll back on any
        exception. A storage failure (lock held too long, disk full, ...) is raised as the
        ``store_unwritable`` error."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            if is_storage_failure(exc):
                raise storage_error(exc) from None
            raise
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException as exc:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error) and is_storage_failure(exc):
                raise storage_error(exc) from None
            raise

    def checkpoint(self) -> None:
        """``PRAGMA wal_checkpoint(TRUNCATE)``: what ``retro delete`` and ``prune`` end with, so
        deleted rows do not stay readable in the write-ahead log or the database file. A checkpoint
        that cannot finish because a reader holds the log (the ``busy`` column of the result) leaves
        the deleted bytes on disk, so it is a retryable ``store_unwritable`` error."""
        try:
            row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            if is_storage_failure(exc):
                raise storage_error(exc) from None
            raise
        if row is None or row[0]:
            raise unwritable(
                "another connection is reading state.db, so deleted data is not yet removed "
                "from disk; try again",
                retryable=True,
            )

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_store(
    gateway: Gateway,
    directory: str,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> Store:
    """Create the state directory and ``state.db`` if needed, check them, open the database and
    apply the pending migrations. Raises ``CliError`` (``store_unwritable`` or ``store_newer``);
    a registry that is not unique and ascending is a programming error (``ValueError``)."""
    validate_registry(migrations)
    try:
        with gateway.umask(0o077):
            _prepare_directory(gateway, directory, create=True)
            db_path = f"{directory}/{DB_NAME}"
            _check_db_file(gateway, db_path, create=True)
            _check_side_files(gateway, db_path)
            conn = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000, isolation_level=None)
            try:
                version = _retry_busy(
                    gateway,
                    busy_timeout_ms,
                    lambda: _open_connection(gateway, conn, db_path, migrations),
                )
            except BaseException:
                conn.close()
                raise
    except (OSError, sqlite3.Error) as exc:
        raise storage_error(exc) from None
    return Store(gateway, directory, conn, version)


def _open_connection(
    gateway: Gateway, conn: sqlite3.Connection, db_path: str, migrations: Sequence[Migration]
) -> int:
    # A database that is already WAL is left alone (setting the mode is a write, and a newer
    # store must not be touched before it is refused).
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    done, pending, newer = _classify(_read_ledger(conn), migrations)
    if newer:
        raise store_newer()
    if str(mode).lower() != "wal":
        if str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
            raise unwritable("this file system does not support WAL mode")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    _check_side_files(gateway, db_path)
    for migration in pending:
        _apply(conn, migration, migrations, _epoch_ms(gateway))
        done = done | {migration.number}
    return max(done, default=0)


def inspect_store(
    gateway: Gateway, directory: str, *, migrations: Sequence[Migration] = MIGRATIONS
) -> StoreStatus:
    """The state of the store for ``validate``: nothing is created, applied or changed (the
    database is opened read-only). Raises ``CliError`` ``store_unwritable`` when the directory or
    a file is unsafe or the database cannot be read. With no ``-wal`` file the database is opened
    ``immutable`` as well, so SQLite creates no ``-wal`` or ``-shm`` file; with one it is opened
    plain read-only (it must see the log)."""
    validate_registry(migrations)
    try:
        if not _prepare_directory(gateway, directory, create=False):
            return StoreStatus("missing")
        db_path = f"{directory}/{DB_NAME}"
        if not _check_db_file(gateway, db_path, create=False, read_only=True):
            return StoreStatus("missing")
        _check_side_files(gateway, db_path)
        try:
            gateway.lstat(db_path + "-wal")
            options = "mode=ro"
        except FileNotFoundError:
            options = "mode=ro&immutable=1"
        conn = sqlite3.connect(
            f"file:{quote(db_path)}?{options}", uri=True, timeout=1.0, isolation_level=None
        )
        try:
            applied = _read_ledger(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise storage_error(exc) from None
    done, pending, newer = _classify(applied, migrations)
    latest = max((m.number for m in migrations), default=0)
    version = max(done, default=0)
    if newer:
        return StoreStatus("newer", version, latest)
    if pending:
        return StoreStatus("pending", version, latest, tuple(m.number for m in pending))
    return StoreStatus("ok", version, latest)


def column_names(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """``{table: [(column, declared type)]}`` of every table (for the schema pin test and the
    ``validate`` report)."""
    result: dict[str, list[tuple[str, str]]] = {}
    for table in sorted(_tables(conn)):
        rows: list[Any] = conn.execute(f"PRAGMA table_info({table})").fetchall()
        result[table] = [(r[1], r[2]) for r in rows]
    return result
