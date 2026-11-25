"""Helpers for the store tests: a real gateway on the real (temporary) file system with a scripted
clock and user id, and small migration lists."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from session_audit.gateway import SystemGateway
from session_audit.migrations import Migration
from session_audit.migrations.m800_initial_schema import MIGRATION as M800

from .fake_gateway import FakeClock


class ClockedGateway(SystemGateway):
    """The real gateway, with the clock of ``FakeClock`` and, when asked, another user id."""

    def __init__(self, *, uid: int | None = None) -> None:
        super().__init__()
        self.clock = FakeClock()
        self._uid = uid

    def now(self) -> datetime:
        return self.clock.now()

    def uid(self) -> int:
        return os.geteuid() if self._uid is None else self._uid


def foreign_gateway() -> ClockedGateway:
    """A gateway whose user is not the owner of anything in the temporary directory."""
    return ClockedGateway(uid=os.geteuid() + 1)


def extra(number: int, table: str) -> Migration:
    return Migration(number, f"add_{table}", (f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)",))


V1 = (M800,)
V2 = (M800, extra(900, "t_nine"))
V3 = (M800, extra(900, "t_nine"), extra(1300, "t_thirteen"))


def state_path(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "session-audit")


def ledger(path: str) -> list[int]:
    conn = sqlite3.connect(f"{path}/state.db")
    try:
        return [r[0] for r in conn.execute("SELECT number FROM schema_migrations ORDER BY 1")]
    finally:
        conn.close()
