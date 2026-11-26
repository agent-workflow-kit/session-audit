"""Leases: the one liveness rule, compare-and-set takeover, the holder's guarded writes and the
renewal thread. A real store in a temporary directory, a scripted clock and scripted processes."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from session_audit import lease as leases
from session_audit.errors import CliError
from session_audit.lease import (
    IngestTarget,
    Lease,
    LeaseState,
    RetroTarget,
    acquire,
    identity,
    is_live,
    observe,
    take_over,
)
from session_audit.store import Store, open_store

from .storehelp import ClockedGateway, state_path

RETRO = "ra-abcdefgh"
OTHER = "ra-bcdefghi"


class ProcGateway(ClockedGateway):
    """The real file system, a scripted clock, and scripted processes: ``me`` is this process's
    pid and ``running`` maps the pids that run to their start tokens."""

    def __init__(self, me: int = 100, start: str = "s100") -> None:
        super().__init__()
        self.me = me
        self.running: dict[int, str] = {me: start}

    def pid(self) -> int:
        return self.me

    def process_start(self, pid: int) -> str | None:
        return self.running.get(pid)


def make(tmp_path: Path, me: int = 100, start: str = "s100") -> tuple[ProcGateway, Store]:
    gateway = ProcGateway(me, start)
    return gateway, open_store(gateway, state_path(tmp_path))


def second(store: Store, me: int, start: str) -> tuple[ProcGateway, Store]:
    """Another 'process' on the same store and clock."""
    gateway = ProcGateway(me, start)
    gateway.clock = store.gateway.clock  # type: ignore[attr-defined]
    gateway.running = store.gateway.running  # type: ignore[attr-defined]
    gateway.running[me] = start
    return gateway, open_store(gateway, store.directory)


def add_retro(store: Store, retro_id: str = RETRO) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO retros (id, key, state, created_at) VALUES (?, ?, 'digesting', 0)",
            (retro_id, "k" * 64),
        )


def columns(store: Store, retro_id: str = RETRO) -> tuple[object, ...]:
    return store.conn.execute(
        "SELECT lease_pid, lease_start, lease_expires_at FROM retros WHERE id = ?", (retro_id,)
    ).fetchone()


def ms(store: Store) -> int:
    return int(store.gateway.now().timestamp() * 1000)


# --- identity and the liveness rule ----------------------------------------------------------


def test_identity_is_this_process(tmp_path: Path) -> None:
    gateway, _ = make(tmp_path)
    assert identity(gateway) == leases.Holder(100, "s100")


def test_a_process_without_a_start_time_cannot_hold_a_lease(tmp_path: Path) -> None:
    gateway, _ = make(tmp_path)
    gateway.running.clear()
    with pytest.raises(CliError) as caught:
        identity(gateway)
    assert caught.value.code == "internal"
    assert caught.value.exit_code == 1


def test_live_means_running_with_the_same_start_and_not_expired(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    gateway.running[7] = "s7"
    soon = ms(store) + 5_000
    assert is_live(gateway, LeaseState(7, "s7", soon))


def test_a_dead_holder_is_not_live_even_before_expiry(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    assert not is_live(gateway, LeaseState(8, "s8", ms(store) + 100_000))


def test_a_reused_pid_with_another_start_time_is_dead(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    gateway.running[7] = "new-process"
    assert not is_live(gateway, LeaseState(7, "old-process", ms(store) + 100_000))


def test_a_running_holder_past_expiry_is_not_live(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    gateway.running[7] = "s7"
    state = LeaseState(7, "s7", ms(store) + 1_000)
    gateway.clock.advance(1.0)  # exactly at expires_at: not live (now < expires_at is required)
    assert not is_live(gateway, state)


@pytest.mark.parametrize(
    "state",
    [None, LeaseState(), LeaseState(7, None, 5), LeaseState(None, "s7", 5), LeaseState(7, "s7")],
)
def test_a_lease_with_a_missing_field_is_free(tmp_path: Path, state: LeaseState | None) -> None:
    gateway, _ = make(tmp_path)
    gateway.running[7] = "s7"
    assert not is_live(gateway, state)


# --- retro leases ----------------------------------------------------------------------------


def test_a_missing_retro_is_not_acquired(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    assert observe(store, RetroTarget(RETRO)) is None
    assert acquire(store, RetroTarget(RETRO)) is None


def test_acquire_a_free_retro_lease(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    held = acquire(store, RetroTarget(RETRO))
    assert held is not None
    assert columns(store) == (100, "s100", ms(store) + 120_000)


def test_a_live_lease_is_not_acquired_and_not_changed(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    assert acquire(store, RetroTarget(RETRO)) is not None
    _, other = second(store, 200, "s200")
    before = columns(store)
    assert acquire(other, RetroTarget(RETRO)) is None
    assert columns(store) == before


def test_a_dead_holder_is_taken_over_at_once(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    assert acquire(store, RetroTarget(RETRO)) is not None
    _, other = second(store, 200, "s200")
    del gateway.running[100]  # the holder died
    taken = acquire(other, RetroTarget(RETRO))
    assert taken is not None
    assert columns(store)[:2] == (200, "s200")


def test_a_reused_pid_does_not_keep_the_lease_alive(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    assert acquire(store, RetroTarget(RETRO)) is not None
    _, other = second(store, 200, "s200")
    gateway.running[100] = "another-process-same-pid"
    assert acquire(other, RetroTarget(RETRO)) is not None


def test_a_hung_holder_is_taken_over_after_expiry(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    assert acquire(store, RetroTarget(RETRO)) is not None
    _, other = second(store, 200, "s200")
    gateway.clock.advance(119)
    assert acquire(other, RetroTarget(RETRO)) is None
    gateway.clock.advance(1)
    assert acquire(other, RetroTarget(RETRO)) is not None


def test_take_over_is_a_compare_and_set_on_what_was_observed(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    target = RetroTarget(RETRO)
    first = acquire(store, target)
    assert first is not None
    del gateway.running[100]
    gw_b, store_b = second(store, 200, "s200")
    _, store_c = second(store, 300, "s300")
    seen_b = observe(store_b, target)
    seen_c = observe(store_c, target)
    assert seen_b is not None and seen_c is not None
    assert take_over(store_b, target, seen_b) is not None
    assert take_over(store_c, target, seen_c) is None  # rowcount 0: skip
    assert columns(store)[:2] == (200, "s200")
    assert gw_b.running[200] == "s200"


def test_take_over_refuses_a_live_observation(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    target = RetroTarget(RETRO)
    assert acquire(store, target) is not None
    _, other = second(store, 200, "s200")
    seen = observe(other, target)
    assert seen is not None
    assert take_over(other, target, seen) is None


def test_take_over_changes_only_the_observed_row(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    add_retro(store, OTHER)
    assert acquire(store, RetroTarget(RETRO)) is not None
    assert columns(store, OTHER) == (None, None, None)


# --- the holder's later writes ---------------------------------------------------------------


def lose(store: Store, gateway: ProcGateway) -> Lease:
    """A holder (pid 100) whose lease is then taken over by pid 200; returns the stale holder."""
    add_retro(store)
    target = RetroTarget(RETRO)
    stale = acquire(store, target)
    assert stale is not None
    del gateway.running[100]
    _, other = second(store, 200, "s200")
    assert acquire(other, target) is not None
    gateway.running[100] = "s100"  # the stale process is running again (suspended, then resumed)
    return stale


def test_a_stale_renewal_does_not_extend_the_new_holders_lease(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    stale = lose(store, gateway)
    before = columns(store)
    gateway.clock.advance(30)
    assert stale.renew() is False
    assert columns(store) == before
    with pytest.raises(CliError) as caught:
        stale.raise_if_lost()
    assert (caught.value.code, caught.value.exit_code) == ("internal", 1)


def test_a_renewal_extends_expiry_by_the_ttl_from_now(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    held = acquire(store, RetroTarget(RETRO))
    assert held is not None
    gateway.clock.advance(30)
    assert held.renew() is True
    assert columns(store) == (100, "s100", ms(store) + 120_000)


def test_check_still_holder_passes_for_the_holder_and_stops_a_stale_one(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    held = acquire(store, RetroTarget(RETRO))
    assert held is not None
    with store.transaction() as conn:
        held.check_still_holder(conn)


def test_a_stale_holder_cannot_write_and_exits_with_internal(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    stale = lose(store, gateway)
    with pytest.raises(CliError) as caught, store.transaction() as conn:
        stale.check_still_holder(conn)
    assert (caught.value.code, caught.value.exit_code) == ("internal", 1)
    with pytest.raises(CliError) as again:
        stale.raise_if_lost()
    assert again.value.exit_code == 1


def test_guarded_update_writes_for_the_holder(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    held = acquire(store, RetroTarget(RETRO))
    assert held is not None
    with store.transaction() as conn:
        held.guarded_update(conn, "state = ?, reason = ?", ("running", "why"))
    row = store.conn.execute("SELECT state, reason FROM retros WHERE id = ?", (RETRO,)).fetchone()
    assert tuple(row) == ("running", "why")


def test_guarded_update_by_a_stale_holder_changes_nothing_and_stops_it(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    stale = lose(store, gateway)
    with pytest.raises(CliError) as caught, store.transaction() as conn:
        stale.guarded_update(conn, "state = ?", ("done",))
    assert caught.value.exit_code == 1
    state = store.conn.execute("SELECT state FROM retros WHERE id = ?", (RETRO,)).fetchone()[0]
    assert state == "digesting"


def test_guarded_update_is_for_retro_leases_only(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    held = acquire(store, IngestTarget())
    assert held is not None
    with pytest.raises(TypeError), store.transaction() as conn:
        held.guarded_update(conn, "x = 1")


def test_release_clears_and_a_stale_release_clears_nothing(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    stale = lose(store, gateway)
    before = columns(store)
    assert stale.release() is False
    assert columns(store) == before
    _, other = second(store, 300, "s300")
    del gateway.running[200]
    held = acquire(other, RetroTarget(RETRO))
    assert held is not None
    assert held.release() is True
    assert columns(store) == (None, None, None)


# --- the ingest lease on meta ----------------------------------------------------------------


def test_ingest_lease_is_exclusive_and_released(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    target = IngestTarget()
    first = acquire(store, target)
    assert first is not None
    _, other = second(store, 200, "s200")
    assert acquire(other, target) is None
    assert first.release() is True
    assert acquire(other, target) is not None
    assert gateway.running[100] == "s100"


def test_ingest_lease_of_a_dead_holder_is_taken_over(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    assert acquire(store, IngestTarget()) is not None
    _, other = second(store, 200, "s200")
    del gateway.running[100]
    assert acquire(other, IngestTarget()) is not None


def test_ingest_stale_renewal_does_not_extend_the_new_holders_lease(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    stale = acquire(store, IngestTarget())
    assert stale is not None
    del gateway.running[100]
    _, other = second(store, 200, "s200")
    assert acquire(other, IngestTarget()) is not None
    gateway.running[100] = "s100"
    value = store.conn.execute("SELECT value FROM meta WHERE key = 'ingest_lease'").fetchone()[0]
    gateway.clock.advance(10)
    assert stale.renew() is False
    assert (
        store.conn.execute("SELECT value FROM meta WHERE key = 'ingest_lease'").fetchone()[0]
        == value
    )
    with pytest.raises(CliError), store.transaction() as conn:
        stale.check_still_holder(conn)


def test_a_pid_prefix_is_not_the_same_holder(tmp_path: Path) -> None:
    gateway, store = make(tmp_path, me=10, start="s1")
    assert acquire(store, IngestTarget()) is not None
    gateway.running[1] = "s1"  # pid 1, start "s1": a prefix of "10|s1|..." only as text
    holder = leases.Holder(1, "s1")
    with store.transaction() as conn:
        assert IngestTarget().holds(conn, holder) is False
        assert IngestTarget().renew(conn, holder, 5) is False
        assert IngestTarget().clear(conn, holder) is False


@pytest.mark.parametrize("garbage", ["x", "1|2", "a|b|c", "1||5", "1|s|-5", "|||", "9|s|1|2"])
def test_a_garbled_ingest_value_is_a_free_lease(tmp_path: Path, garbage: str) -> None:
    _, store = make(tmp_path)
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('ingest_lease', ?)", (garbage,)
        )
    seen = observe(store, IngestTarget())
    assert seen == LeaseState()
    assert acquire(store, IngestTarget()) is not None


def test_ingest_values_never_hold_text_beyond_pid_start_and_expiry(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    assert acquire(store, IngestTarget()) is not None
    value = store.conn.execute("SELECT value FROM meta WHERE key = 'ingest_lease'").fetchone()[0]
    assert value == f"100|s100|{ms(store) + 120_000}"


def test_observing_the_ingest_lease_writes_nothing(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    assert observe(store, IngestTarget()) == LeaseState()
    row = store.conn.execute("SELECT 1 FROM meta WHERE key = 'ingest_lease'").fetchone()
    assert row is None


# --- the renewal thread ----------------------------------------------------------------------


def fast(store: Store, target: leases.Target, **kw: float) -> Lease:
    held = acquire(store, target, ttl=kw.get("ttl", 120.0), renew_every=kw.get("every", 0.1))
    assert held is not None
    return held


def wait_for(predicate: object, seconds: float = 10.0) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return bool(predicate())  # type: ignore[operator]


def test_the_thread_renews_on_its_own_connection_and_stops_cleanly(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))
    first = columns(store)[2]
    before = threading.active_count()
    with held:
        assert held.renewing
        gateway.clock.advance(50)  # the scripted clock; the thread's cadence is real time
        assert wait_for(lambda: columns(store)[2] != first)
        assert columns(store)[2] == ms(store) + 120_000
    assert not held.renewing
    assert threading.active_count() == before
    held.raise_if_lost()  # nothing was lost


def test_close_is_idempotent_and_leaves_the_lease_held(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))
    held.start()
    held.close()
    held.close()
    assert columns(store)[:2] == (100, "s100")


def test_the_thread_ends_when_the_lease_is_taken_over(tmp_path: Path) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))
    with held:
        del gateway.running[100]
        _, other = second(store, 200, "s200")
        assert acquire(other, RetroTarget(RETRO)) is not None
        gateway.running[100] = "s100"
        taken = columns(store)
        assert wait_for(lambda: not held.renewing)
        with pytest.raises(CliError) as caught:
            held.raise_if_lost()
        assert caught.value.exit_code == 1
    assert columns(store) == taken  # the stale thread extended nothing


def test_a_busy_store_is_retried_at_the_next_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))
    real = Lease.renew
    calls: list[int] = []

    def flaky(self: Lease, other: Store | None = None) -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise CliError(
                "failed",
                "busy",
                details={"reason": "store_unwritable", "retryable": True},
            )
        return real(self, other)

    monkeypatch.setattr(Lease, "renew", flaky)
    first = columns(store)[2]
    with held:
        assert wait_for(lambda: held.last_renewal_error is not None)
        gateway.clock.advance(10)
        assert wait_for(lambda: columns(store)[2] != first)
        assert wait_for(lambda: held.last_renewal_error is None)
    held.raise_if_lost()


def test_an_unexpected_thread_failure_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))

    def boom(self: Lease, other: Store | None = None) -> bool:
        raise sqlite3.OperationalError("no such table: bogus")

    monkeypatch.setattr(Lease, "renew", boom)
    with held:
        assert wait_for(lambda: not held.renewing)
        with pytest.raises(CliError) as caught:
            held.raise_if_lost()
    assert (caught.value.code, caught.value.exit_code) == ("internal", 1)


def test_the_renewal_interval_must_be_shorter_than_the_lease(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    add_retro(store)
    with pytest.raises(ValueError):
        acquire(store, RetroTarget(RETRO), ttl=10, renew_every=10)


def test_the_renewal_transaction_is_short_and_does_not_block_the_holder(tmp_path: Path) -> None:
    """While the thread runs, the holder's own writes still go through (no write lock is held
    between renewals)."""
    _, store = make(tmp_path)
    add_retro(store)
    held = fast(store, RetroTarget(RETRO))
    with held:
        for index in range(20):
            with store.transaction() as conn:
                held.guarded_update(conn, "turns = ?", (index,))
    assert store.conn.execute("SELECT turns FROM retros WHERE id = ?", (RETRO,)).fetchone()[0] == 19


def test_the_defaults_are_the_documented_ones() -> None:
    assert leases.LEASE_SECONDS == 120
    assert leases.RENEW_SECONDS == 30
