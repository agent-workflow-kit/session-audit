"""Leases: one liveness rule, compare-and-set takeover (interface.md, Retro lifecycle, Leases).

A lease is ``(holder_pid, holder_start, expires_at)`` on the object it protects: a ``retros`` row
(``RetroTarget``) or the ingest lease on ``meta`` (``IngestTarget``). The rule, and nothing
stronger::

    live(lease) = the holder PID runs with the recorded start time AND now < expires_at

A lease that is not live is taken over at once. Everything that changes a lease is a
compare-and-set (a single ``UPDATE ... WHERE`` whose ``rowcount`` decides):

* ``take_over``: ``WHERE`` the three fields still equal what was observed; rowcount 0 means
  another process won, the caller skips.
* ``renew``, ``release``, ``guarded_update`` (a retro state write) and ``check_still_holder``:
  ``WHERE`` the holder's own pid and start time still match. A stale holder therefore never
  extends, clears or writes under a new holder's lease; it finds out (rowcount 0) and stops with
  exit 1 (``lease_lost``).

The API::

    observed = observe(store, target)            # LeaseState | None (no such row); never writes
    lease = acquire(store, target)               # free or not live -> Lease, else None
    lease = take_over(store, target, observed)   # CAS on what was observed (recovery)
    with lease:                                  # starts the renewal thread, stops it on exit
        lease.raise_if_lost()                    # cheap, no database access
        with store.transaction() as conn:        # any state write of the holder:
            lease.check_still_holder(conn)       #   inside the transaction of the write, or
            lease.guarded_update(conn, "state = ?", ("running",))   # a retro row, as one CAS
    lease.release()                              # CAS clear; False when it was already lost

The renewal thread renews every ``RENEW_SECONDS`` in its own short transaction on its own
connection and keeps going while the holder waits on claude-session or a hook (no SQLite write
lock is held across either: every transaction here is one statement or two). It sleeps through the
gateway in short ticks so ``stop`` is prompt. A renewal that fails because the store is busy is
retried at the next interval (the lease outlives three missed renewals); a renewal that finds the
lease taken over ends the thread and makes ``raise_if_lost`` raise.

Signals are not handled here: ``#28`` owns them and calls ``close``/``release`` from its handlers.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from .errors import CliError
from .gateway import Gateway
from .store import Store

LEASE_SECONDS = 120.0
RENEW_SECONDS = 30.0
_TICK_SECONDS = 0.25
_JOIN_SECONDS = 40.0  # a renewal in flight may wait out the store's 30 s busy budget

__all__ = [
    "LEASE_SECONDS",
    "RENEW_SECONDS",
    "Holder",
    "IngestTarget",
    "Lease",
    "LeaseState",
    "RetroTarget",
    "acquire",
    "identity",
    "is_live",
    "lease_lost",
    "observe",
    "take_over",
]


@dataclass(frozen=True)
class Holder:
    """Who holds a lease: this process, as ``(pid, start-time token)``."""

    pid: int
    start: str


@dataclass(frozen=True)
class LeaseState:
    """A lease as stored. All three fields None is a free lease. ``raw`` is the stored text of a
    ``meta`` value (compared exactly by the takeover, so even a garbled value is taken over only
    if nobody changed it meanwhile)."""

    pid: int | None = None
    start: str | None = None
    expires_ms: int | None = None
    raw: str | None = field(default=None, compare=False)


def lease_lost() -> CliError:
    return CliError(
        "internal",
        "this process lost its lease to another process and stops",
        hint="Another run took the lease over (this one stopped renewing or was suspended); "
        "run the command again.",
    )


def identity(gateway: Gateway) -> Holder:
    """This process as a lease holder. A process whose start time cannot be read cannot hold a
    lease (nobody could tell it from a reused pid)."""
    pid = gateway.pid()
    start = gateway.process_start(pid)
    if not start:
        raise CliError("internal", "cannot read this process's start time to take a lease")
    return Holder(pid, start)


def _now_ms(gateway: Gateway) -> int:
    return int(gateway.now().timestamp() * 1000)


def is_live(gateway: Gateway, state: LeaseState | None) -> bool:
    """The single liveness rule. A lease with a missing field is free, so not live."""
    if state is None or state.pid is None or state.start is None or state.expires_ms is None:
        return False
    if _now_ms(gateway) >= state.expires_ms:
        return False
    return gateway.process_start(state.pid) == state.start


# --- targets: where a lease is stored --------------------------------------------------------


class Target(Protocol):
    def read(self, conn: sqlite3.Connection) -> LeaseState | None: ...
    def take(self, conn: sqlite3.Connection, old: LeaseState, new: LeaseState) -> bool: ...
    def renew(self, conn: sqlite3.Connection, holder: Holder, expires_ms: int) -> bool: ...
    def clear(self, conn: sqlite3.Connection, holder: Holder) -> bool: ...
    def holds(self, conn: sqlite3.Connection, holder: Holder) -> bool: ...


@dataclass(frozen=True)
class RetroTarget:
    """The lease on a ``retros`` row (its ``lease_*`` columns). A retro row that does not exist is
    never acquired (``observe`` gives None)."""

    retro_id: str

    def read(self, conn: sqlite3.Connection) -> LeaseState | None:
        row = conn.execute(
            "SELECT lease_pid, lease_start, lease_expires_at FROM retros WHERE id = ?",
            (self.retro_id,),
        ).fetchone()
        return None if row is None else LeaseState(row[0], row[1], row[2])

    def take(self, conn: sqlite3.Connection, old: LeaseState, new: LeaseState) -> bool:
        cursor = conn.execute(
            "UPDATE retros SET lease_pid = ?, lease_start = ?, lease_expires_at = ? "
            "WHERE id = ? AND lease_pid IS ? AND lease_start IS ? AND lease_expires_at IS ?",
            (
                new.pid,
                new.start,
                new.expires_ms,
                self.retro_id,
                old.pid,
                old.start,
                old.expires_ms,
            ),
        )
        return cursor.rowcount == 1

    def renew(self, conn: sqlite3.Connection, holder: Holder, expires_ms: int) -> bool:
        cursor = conn.execute(
            "UPDATE retros SET lease_expires_at = ? "
            "WHERE id = ? AND lease_pid = ? AND lease_start = ?",
            (expires_ms, self.retro_id, holder.pid, holder.start),
        )
        return cursor.rowcount == 1

    def clear(self, conn: sqlite3.Connection, holder: Holder) -> bool:
        cursor = conn.execute(
            "UPDATE retros SET lease_pid = NULL, lease_start = NULL, lease_expires_at = NULL "
            "WHERE id = ? AND lease_pid = ? AND lease_start = ?",
            (self.retro_id, holder.pid, holder.start),
        )
        return cursor.rowcount == 1

    def holds(self, conn: sqlite3.Connection, holder: Holder) -> bool:
        row = conn.execute(
            "SELECT 1 FROM retros WHERE id = ? AND lease_pid = ? AND lease_start = ?",
            (self.retro_id, holder.pid, holder.start),
        ).fetchone()
        return row is not None

    def guarded_update(
        self, conn: sqlite3.Connection, holder: Holder, assignments: str, params: tuple[object, ...]
    ) -> None:
        """``UPDATE retros SET <assignments> WHERE id = ? AND lease_pid = ? AND lease_start = ?``:
        a state write of the holder as one compare-and-set. ``assignments`` is SQL text written by
        the caller (never input); values go in ``params``. Raises ``lease_lost`` on rowcount 0."""
        cursor = conn.execute(
            f"UPDATE retros SET {assignments} WHERE id = ? AND lease_pid = ? AND lease_start = ?",
            (*params, self.retro_id, holder.pid, holder.start),
        )
        if cursor.rowcount != 1:
            raise lease_lost()


# ASCII digits only and a bounded length: str.isdigit() accepts '²' (int() then raises) and int()
# of a very long digit string raises too; either is a garbled value, not a crash.
_ASCII_NUMBER = re.compile(r"[0-9]{1,19}")


@dataclass(frozen=True)
class IngestTarget:
    """The ingest lease: one ``meta`` row ``ingest_lease`` holding ``''`` (free) or
    ``<pid>|<start>|<expires_ms>``. One value, so one ``UPDATE`` compares and swaps all three
    fields."""

    key: str = "ingest_lease"

    @staticmethod
    def _encode(pid: int, start: str, expires_ms: int) -> str:
        if "|" in start:
            raise ValueError("a start token cannot contain '|'")
        return f"{pid}|{start}|{expires_ms}"

    @staticmethod
    def _prefix(holder: Holder) -> str:
        return f"{holder.pid}|{holder.start}|"

    def read(self, conn: sqlite3.Connection) -> LeaseState | None:
        row = conn.execute(
            "SELECT CAST(value AS TEXT) FROM meta WHERE key = ?", (self.key,)
        ).fetchone()
        raw = None if row is None else row[0]
        if not raw:
            return LeaseState(raw=raw)
        parts = raw.split("|")
        if (
            len(parts) == 3
            and _ASCII_NUMBER.fullmatch(parts[0])
            and _ASCII_NUMBER.fullmatch(parts[2])
            and parts[1]
        ):
            return LeaseState(int(parts[0]), parts[1], int(parts[2]), raw=raw)
        # A garbled value is a lease nobody can renew: free to take over (by its exact text).
        return LeaseState(raw=raw)

    def take(self, conn: sqlite3.Connection, old: LeaseState, new: LeaseState) -> bool:
        if new.pid is None or new.start is None or new.expires_ms is None:
            raise ValueError("a lease is taken with all three fields")
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, '')", (self.key,))
        cursor = conn.execute(
            "UPDATE meta SET value = ? WHERE key = ? AND CAST(value AS TEXT) IS ?",
            (self._encode(new.pid, new.start, new.expires_ms), self.key, old.raw or ""),
        )
        return cursor.rowcount == 1

    def _guard(self, holder: Holder) -> tuple[str, str, int]:
        prefix = self._prefix(holder)
        return self.key, prefix, len(prefix)

    def renew(self, conn: sqlite3.Connection, holder: Holder, expires_ms: int) -> bool:
        key, prefix, size = self._guard(holder)
        cursor = conn.execute(
            "UPDATE meta SET value = ? WHERE key = ? AND substr(CAST(value AS TEXT), 1, ?) = ?",
            (self._encode(holder.pid, holder.start, expires_ms), key, size, prefix),
        )
        return cursor.rowcount == 1

    def clear(self, conn: sqlite3.Connection, holder: Holder) -> bool:
        key, prefix, size = self._guard(holder)
        cursor = conn.execute(
            "UPDATE meta SET value = '' WHERE key = ? AND substr(CAST(value AS TEXT), 1, ?) = ?",
            (key, size, prefix),
        )
        return cursor.rowcount == 1

    def holds(self, conn: sqlite3.Connection, holder: Holder) -> bool:
        key, prefix, size = self._guard(holder)
        row = conn.execute(
            "SELECT 1 FROM meta WHERE key = ? AND substr(CAST(value AS TEXT), 1, ?) = ?",
            (key, size, prefix),
        ).fetchone()
        return row is not None


# --- the operations --------------------------------------------------------------------------


def observe(store: Store, target: Target) -> LeaseState | None:
    """The lease as stored now (None when the object does not exist). Reads only; the caller
    judges it with ``is_live``. It may be stale by the time it is used, which is why
    ``take_over`` is a compare-and-set on exactly this value."""
    return target.read(store.conn)


def acquire(
    store: Store,
    target: Target,
    *,
    ttl: float = LEASE_SECONDS,
    renew_every: float = RENEW_SECONDS,
) -> Lease | None:
    """Take the lease when it is free or not live, in one write transaction. None when the holder
    is live, the object does not exist, or the compare-and-set lost (cannot happen inside the
    transaction, but the answer is the same)."""
    gateway = store.gateway
    holder = identity(gateway)
    with store.transaction() as conn:
        observed = target.read(conn)
        if observed is None or is_live(gateway, observed):
            return None
        expires = _now_ms(gateway) + int(ttl * 1000)
        if not target.take(conn, observed, LeaseState(holder.pid, holder.start, expires)):
            return None
    return Lease(store, target, holder, expires, ttl, renew_every)


def take_over(
    store: Store,
    target: Target,
    observed: LeaseState,
    *,
    ttl: float = LEASE_SECONDS,
    renew_every: float = RENEW_SECONDS,
) -> Lease | None:
    """Compare-and-set takeover of the lease as ``observed`` it: one ``UPDATE ... WHERE`` the three
    fields are unchanged. None (skip) when the observation was live, or when another process
    changed the lease first (rowcount 0)."""
    gateway = store.gateway
    if is_live(gateway, observed):
        return None
    holder = identity(gateway)
    expires = _now_ms(gateway) + int(ttl * 1000)
    with store.transaction() as conn:
        if not target.take(conn, observed, LeaseState(holder.pid, holder.start, expires)):
            return None
    return Lease(store, target, holder, expires, ttl, renew_every)


class Lease:
    """A lease this process holds. Use as a context manager around the work: it runs the renewal
    thread, and leaving the block stops it (the lease stays held; ``release`` gives it up).

    One ``Lease`` is used from one thread (plus its own renewal thread, which touches only the
    fields documented here and its own connection)."""

    def __init__(
        self,
        store: Store,
        target: Target,
        holder: Holder,
        expires_ms: int,
        ttl: float,
        renew_every: float,
    ) -> None:
        if not 0 < renew_every < ttl:
            raise ValueError("the renewal interval must be positive and shorter than the lease")
        self.store = store
        self.target = target
        self.holder = holder
        self.expires_ms = expires_ms
        self.ttl = ttl
        self.renew_every = renew_every
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: CliError | None = None  # the last renewal failure that is worth retrying
        self._fatal: BaseException | None = None

    # -- the holder's own writes -----------------------------------------------------------

    def check_still_holder(self, conn: sqlite3.Connection) -> None:
        """Inside the transaction of a state write: raise ``lease_lost`` (exit 1) unless this
        process still holds the lease. In an ``IMMEDIATE`` transaction nobody can change the lease
        between this check and the write, so it is as good as a compare-and-set."""
        if not self.target.holds(conn, self.holder):
            self._lost.set()
            raise lease_lost()

    def guarded_update(
        self, conn: sqlite3.Connection, assignments: str, params: tuple[object, ...] = ()
    ) -> None:
        """A retro state write as one compare-and-set on this holder's pid and start time."""
        if not isinstance(self.target, RetroTarget):
            raise TypeError("guarded_update is for a retro lease")
        try:
            self.target.guarded_update(conn, self.holder, assignments, params)
        except CliError:
            self._lost.set()
            raise

    def renew(self, store: Store | None = None) -> bool:
        """One renewal in its own short transaction (``store`` is the caller's connection, the
        renewal thread passes its own). False when the lease was taken over: nothing was
        extended."""
        use = self.store if store is None else store
        expires = _now_ms(use.gateway) + int(self.ttl * 1000)
        with use.transaction() as conn:
            ok = self.target.renew(conn, self.holder, expires)
        if ok:
            self.expires_ms = expires
        else:
            self._lost.set()
        return ok

    def release(self) -> bool:
        """Stop renewing and give the lease up (a compare-and-set: a stale holder clears nothing
        of a new holder's). False when it was no longer held."""
        self.close()
        with self.store.transaction() as conn:
            return self.target.clear(conn, self.holder)

    # -- the renewal thread ----------------------------------------------------------------

    def raise_if_lost(self) -> None:
        """Raise ``lease_lost`` when a renewal or a write found the lease taken over, and an
        internal error when the renewal thread died of something unexpected. No database
        access."""
        if self._fatal is not None:
            raise CliError("internal", "the lease renewal thread failed") from self._fatal
        if self._lost.is_set():
            raise lease_lost()

    @property
    def last_renewal_error(self) -> CliError | None:
        """The last renewal failure (a busy store), cleared by the next success."""
        return self._error

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._renew_loop, name="lease-renewal", daemon=True)
        self._thread = thread
        thread.start()

    def close(self) -> None:
        """Stop the renewal thread and wait for it. Idempotent; the lease stays held."""
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_JOIN_SECONDS)

    @property
    def renewing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> Lease:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _renew_loop(self) -> None:
        gateway = self.store.gateway
        tick = min(_TICK_SECONDS, self.renew_every / 4)
        own: Store | None = None
        due = gateway.monotonic() + self.renew_every
        try:
            while not self._stop.is_set():
                gateway.sleep(tick)
                if self._stop.is_set() or gateway.monotonic() < due:
                    continue
                due = gateway.monotonic() + self.renew_every
                try:
                    if own is None:
                        own = self.store.reopen()
                    ok = self.renew(own)
                except CliError as exc:  # a busy or unwritable store: retried at the next interval
                    self._error = exc
                    continue
                self._error = None
                if not ok:
                    return
        except BaseException as exc:  # noqa: BLE001 - reported by raise_if_lost, never swallowed
            self._fatal = exc
        finally:
            if own is not None:
                own.close()
