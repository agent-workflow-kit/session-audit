"""Helpers for the event store tests: a store with the ingest lease held, synthetic transcript
lines, and a driver that turns a file into a ``Candidate`` and ``Opened`` the way discovery does."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from session_audit.eventstore import FileResult, read_file
from session_audit.incremental import load_progress
from session_audit.lease import IngestTarget, Lease, acquire
from session_audit.reader import AgentMeta, Candidate, Opened
from session_audit.store import Store, open_store

from .storehelp import ClockedGateway, state_path

SESSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
BUDGET = timedelta(seconds=60)


class EnvGateway(ClockedGateway):
    """The real file system, a scripted clock (also for ``monotonic``) and a scripted process."""

    def __init__(self) -> None:
        super().__init__()
        self.tick = 0.0  # seconds the monotonic clock moves at every reading
        self.fail_monotonic_after: int | None = None  # raise at this many readings
        self.readings = 0
        self.running = {100: "s100"}

    def monotonic(self) -> float:
        self.readings += 1
        if self.fail_monotonic_after is not None and self.readings > self.fail_monotonic_after:
            raise RuntimeError("simulated crash")
        now = self.clock.monotonic()
        if self.tick:
            self.clock.advance(self.tick)
        return now

    def pid(self) -> int:
        return 100

    def process_start(self, pid: int) -> str | None:
        return self.running.get(pid)


@dataclass
class Env:
    root: Path
    gateway: EnvGateway
    store: Store
    lease: Lease
    files: dict[str, Path] = field(default_factory=dict)

    @property
    def conn(self) -> sqlite3.Connection:
        return self.store.conn

    def path(self, name: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{name}.jsonl"

    def write(self, name: str, lines: Iterable[bytes]) -> Path:
        p = self.path(name)
        p.write_bytes(b"".join(lines))
        return p

    def append(self, name: str, lines: Iterable[bytes]) -> None:
        with open(self.path(name), "ab") as handle:
            handle.write(b"".join(lines))

    def ingest(
        self,
        name: str,
        *,
        session: str = SESSION_A,
        agent: str | None = None,
        project: bytes = b"proj",
        meta: AgentMeta | None = None,
        force: bool = False,
        run_deadline: float | None = None,
        file_budget: timedelta = BUDGET,
        **limits: int,
    ) -> FileResult:
        path = self.path(name)
        handle = open(path, "rb")  # noqa: SIM115 - closed below
        try:
            info = os.fstat(handle.fileno())
            cand = Candidate(
                root_index=0,
                rel=(project, path.name.encode()),
                path=bytes(path),
                kind="main" if agent is None else "subagent",
                session_id=session,
                agent_id=agent,
                workflow=None,
                project=project,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                dev=info.st_dev,
                ino=info.st_ino,
            )
            previous = load_progress(self.conn, cand.path)
            return read_file(
                self.store,
                self.lease,
                Opened(cand, handle, info),
                previous,
                rules_version=1,
                file_budget=file_budget,
                run_deadline=run_deadline,
                force=force,
                meta=meta,
                **limits,
            )
        finally:
            handle.close()

    def counted(self) -> list[tuple]:
        """The counted events, as what a query would aggregate, in a canonical order."""
        rows = self.conn.execute(
            "SELECT key, kind, ref, session_id, agent_id, model, tool, ts, slot, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, "
            "seconds, native FROM events WHERE counted = 1 ORDER BY key"
        ).fetchall()
        return [tuple(r) for r in rows]

    def stored(self) -> int:
        return self.conn.execute("SELECT count(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self.store.close()


def make_env(tmp_path: Path, name: str = "corpus") -> Env:
    gateway = EnvGateway()
    store = open_store(gateway, state_path(tmp_path))
    lease = acquire(store, IngestTarget())
    assert lease is not None
    return Env(tmp_path / name, gateway, store, lease)


# --- synthetic transcript lines ---------------------------------------------------------------


def ts(n: int) -> str:
    """A timestamp n seconds after 2021-05-06T07:00:00Z (n below 3600)."""
    return f"2021-05-06T07:{n // 60:02d}:{n % 60:02d}.000Z"


def raw(obj: object) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def message(
    mid: str | None,
    out: int,
    *,
    t: int = 0,
    model: str = "model-a",
    tools: tuple[tuple[str, str], ...] = (),
    inp: int = 1,
    session: str | None = None,
    agent: str | None = None,
    uuid: str | None = None,
) -> bytes:
    """One assistant line: one content block of a message (several lines share a message id)."""
    msg: dict = {
        "model": model,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 3},
        "content": [{"type": "tool_use", "id": i, "name": n} for i, n in tools],
    }
    if mid is not None:
        msg["id"] = mid
    obj: dict = {"type": "assistant", "timestamp": ts(t), "message": msg}
    if uuid is not None:
        obj["uuid"] = uuid
    if session is not None:
        obj["sessionId"] = session
    if agent is not None:
        obj["agentId"] = agent
    return raw(obj)


def tool_use(
    mid: str,
    tool_id: str,
    name: str = "Bash",
    *,
    out: int = 5,
    t: int = 0,
    session: str | None = None,
) -> bytes:
    obj: dict = {
        "type": "assistant",
        "timestamp": ts(t),
        "message": {
            "id": mid,
            "model": "model-a",
            "usage": {"output_tokens": out},
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
    }
    if session is not None:
        obj["sessionId"] = session
    return raw(obj)


def tool_error(tool_id: str, *, t: int = 0, uuid: str | None = "u-err") -> bytes:
    obj: dict = {
        "type": "user",
        "timestamp": ts(t),
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": True}]},
    }
    if uuid:
        obj["uuid"] = uuid
    return raw(obj)


def denial_and_interruption(*, t: int = 0, uuid: str = "u-den", tool_id: str = "toolu_1") -> bytes:
    return raw(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": ts(t),
            "toolDenialKind": "user-rejected",
            "interruptedMessageId": "m-x",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
        }
    )


def turn(ms: int, *, t: int = 0, uuid: str | None = None) -> bytes:
    obj: dict = {"type": "system", "subtype": "turn_duration", "durationMs": ms, "timestamp": ts(t)}
    if uuid is not None:
        obj["uuid"] = uuid
    return raw(obj)


def compaction(uuid: str | None = "u-compact", *, t: int = 0) -> bytes:
    obj: dict = {"type": "system", "subtype": "compact_boundary", "timestamp": ts(t)}
    if uuid is not None:
        obj["uuid"] = uuid
    return raw(obj)
