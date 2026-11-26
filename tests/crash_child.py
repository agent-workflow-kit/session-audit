"""Child process of ``test_prune_crash``: session retention over a store, batch size 50.

``crash_child.py <state_dir> slow|fast``. ``slow`` pauses after every event batch so the parent can
kill the process in the middle of a session; ``fast`` runs to the end (the resume)."""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from session_audit import retention
from session_audit.gateway import SystemGateway
from session_audit.lease import IngestTarget, acquire
from session_audit.retention import Report, Settings, epoch_ms
from session_audit.store import open_store


def main(state_dir: str, mode: str) -> int:
    gateway = SystemGateway()
    store = open_store(gateway, state_dir)
    lease = acquire(store, IngestTarget())
    if lease is None:
        return 3
    if mode == "slow":
        real = retention.delete_session_batch

        def paused(conn: sqlite3.Connection, session_id: str, limit: int) -> tuple[int, bool]:
            result = real(conn, session_id, limit)
            time.sleep(0.02)
            return result

        retention.delete_session_batch = paused  # type: ignore[assignment]
    day = timedelta(days=1)
    settings = Settings(400 * day, 180 * day, 7 * day, 90 * day, (), ZoneInfo("UTC"))
    retention.session_retention(
        store, lease, lambda: epoch_ms(gateway.now()), settings, Report(), batch=50
    )
    lease.release()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
