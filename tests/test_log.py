"""``log.jsonl``: closed hash-only entries, millisecond timestamps, appends under ``log.lock``."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from session_audit import log as log_module
from session_audit.errors import CliError
from session_audit.log import LogWriter, render, timestamp_ms

from .schemas import validate
from .storehelp import ClockedGateway

ACTOR = "human:alice"
SHA = "a" * 64
FILES = dict.fromkeys(
    [
        "seen",
        "new",
        "changed",
        "unchanged",
        "unsafe",
        "excluded",
        "vanished",
        "rewritten",
        "remaining",
    ],
    1,
)
LINES = {"parsed": 5, "errors": 0, "oversized": 0, "unknown": 2}
TABLES = dict.fromkeys(
    ["sessions", "agents", "events", "files", "harness_runs", "retros", "digests", "log_entries",
     "retro_starts", "retro_sessions", "pruned_sessions"], 0
)  # fmt: skip

INGEST: dict[str, Any] = {
    "complete": True, "files": FILES, "lines": LINES, "sessions": {"new": 1, "updated": 2},
}  # fmt: skip
RETRO_STATE: dict[str, Any] = {
    "retro_id": "ra-abcdefgh", "state": "done", "reason": None, "key": SHA, "digest_sha256": SHA,
    "again_of": None, "csession_id": "cs-abcdefgh", "decision_ids": ["d1", "a:b/c@d.e"],
    "hook_output_ignored": 0, "findings": 3, "candidates": None,
}  # fmt: skip
RETRO_DELETE: dict[str, Any] = {"retro_id": "ra-abcdefgh", "key": None}
PRUNE: dict[str, Any] = {"tables": TABLES, "recovered": 0}
GOOD = {
    "ingest": INGEST,
    "retro_state": RETRO_STATE,
    "retro_delete": RETRO_DELETE,
    "prune": PRUNE,
}


def entry(kind: str, /, **override: Any) -> dict[str, Any]:
    doc = {"ts": "2026-01-01T00:00:00.000Z", "actor": ACTOR, "action": kind}
    doc.update(copy.deepcopy(GOOD[kind]))
    doc.update(override)
    return doc


def accepted_by_both(doc: dict[str, Any]) -> tuple[bool, bool]:
    try:
        validate(doc, "session-audit/log.json")
        schema_ok = True
    except ValidationError:
        schema_ok = False
    try:
        render(doc)
        code_ok = True
    except ValueError:
        code_ok = False
    return schema_ok, code_ok


@pytest.fixture
def state(tmp_path: Path) -> str:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return str(directory)


def lines(directory: str) -> list[str]:
    return (Path(directory) / "log.jsonl").read_text(encoding="ascii").splitlines()


# --- the shape ------------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(GOOD))
def test_each_action_validates_against_the_schema(state: str, action: str) -> None:
    writer = LogWriter(ClockedGateway(), state)
    written = writer.append(ACTOR, action, **GOOD[action])
    document = json.loads(written)
    validate(document, "session-audit/log.json")
    assert document["action"] == action
    assert lines(state) == [written.rstrip("\n")]


def test_ts_is_millisecond_utc_from_the_gateway_clock(state: str) -> None:
    gateway = ClockedGateway()
    gateway.clock.advance(1.2345)
    document = json.loads(LogWriter(gateway, state).append(ACTOR, "prune", **PRUNE))
    assert document["ts"] == "2026-01-01T00:00:01.234Z"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 1, 1, 0, 0, 0, 999999, tzinfo=UTC), "2026-01-01T00:00:00.999Z"),
        (datetime(2026, 1, 1, 5, 30, tzinfo=UTC).astimezone(UTC), "2026-01-01T05:30:00.000Z"),
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(hours=-5), "2025-12-31T19:00:00.000Z"),
    ],
)
def test_timestamp_ms(moment: datetime, expected: str) -> None:
    assert timestamp_ms(moment) == expected


def test_timestamp_ms_refuses_a_naive_clock() -> None:
    with pytest.raises(ValueError):
        timestamp_ms(datetime(2026, 1, 1))


@pytest.mark.parametrize("action", sorted(GOOD))
def test_an_unknown_field_is_refused_by_code_and_schema(action: str) -> None:
    for key in ("text", "path", "prompt", "message", "tool", "version", "actor_source"):
        doc = entry(action, **{key: "x"})
        assert accepted_by_both(doc) == (False, False), key


def test_nested_unknown_fields_are_refused() -> None:
    doc = entry("ingest")
    doc["files"]["path"] = "/home/x/secret.jsonl"
    assert accepted_by_both(doc) == (False, False)
    doc = entry("prune")
    doc["tables"]["text"] = 1
    assert accepted_by_both(doc) == (False, False)


HOSTILE: list[dict[str, Any]] = [
    entry("ingest", complete=1),
    entry("ingest", complete="true"),
    entry("ingest", files={**FILES, "seen": -1}),
    entry("ingest", files={**FILES, "seen": True}),
    entry("ingest", files={**FILES, "seen": "1"}),
    entry("ingest", lines={"parsed": 1}),
    entry("ingest", actor="human:"),
    entry("ingest", actor="human:a\n"),
    entry("ingest", actor="robot:x"),
    entry("ingest", actor="human:" + "a" * 65),
    entry("ingest", actor="human:é"),
    entry("ingest", ts="2026-01-01T00:00:00Z"),
    entry("ingest", ts="2026-01-01T00:00:00.000Z\n"),
    entry("ingest", ts="2026-01-01T00:00:00.0000Z"),
    entry("ingest", action="other"),
    entry("retro_state", retro_id="ra-ABCDEFGH"),
    entry("retro_state", retro_id="ra-abcdefgh\n"),
    entry("retro_state", retro_id="ra-abcdefg1"),
    entry("retro_state", state="nope"),
    entry("retro_state", reason="a free text reason"),
    entry("retro_state", key="A" * 64),
    entry("retro_state", key="a" * 63),
    entry("retro_state", key=SHA + "\n"),
    entry("retro_state", csession_id="cs-abcdefgh\n"),
    entry("retro_state", decision_ids=["a b"]),
    entry("retro_state", decision_ids=["x"] * 33),
    entry("retro_state", decision_ids="x"),
    entry("retro_state", findings=51),
    entry("retro_state", candidates=21),
    entry("retro_state", hook_output_ignored=-1),
    entry("retro_delete", key="not a hash"),
    entry("prune", recovered=-1),
    entry("prune", tables={**TABLES, "extra": 0}),
]


@pytest.mark.parametrize("doc", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_entries_are_refused_by_code_and_schema(doc: dict[str, Any]) -> None:
    assert accepted_by_both(doc) == (False, False)


def test_the_code_is_stricter_than_the_schema_on_a_float_count() -> None:
    """JSON Schema calls 1.0 an integer; the writer never writes one."""
    doc = entry("ingest", files={**FILES, "seen": 1.0})
    assert accepted_by_both(doc) == (True, False)


BOUNDARY: list[dict[str, Any]] = [
    entry("retro_state", findings=50, candidates=20, decision_ids=["x"] * 32),
    entry("retro_state", findings=0, candidates=0, decision_ids=[]),
    entry("ingest", actor="auto:" + "a" * 64),
    entry("ingest", actor="auto:a.b@c/d-e_f"),
    entry("retro_state", reason="hook_vetoed"),
]


@pytest.mark.parametrize("doc", BOUNDARY, ids=range(len(BOUNDARY)))
def test_boundary_values_are_accepted_by_code_and_schema(doc: dict[str, Any]) -> None:
    assert accepted_by_both(doc) == (True, True)


def test_a_rejected_entry_writes_nothing(state: str) -> None:
    writer = LogWriter(ClockedGateway(), state)
    with pytest.raises(ValueError) as caught:
        writer.append(ACTOR, "prune", tables=TABLES, recovered=0, text="user said hello")
    assert "hello" not in str(caught.value)  # the value is never echoed
    with pytest.raises(ValueError):
        writer.append("not an actor", "prune", **PRUNE)
    assert not (Path(state) / "log.jsonl").exists() or lines(state) == []


# --- files and modes ------------------------------------------------------------------------


@pytest.mark.parametrize("mask", [0o000, 0o002, 0o277])
def test_log_files_are_0600_under_any_umask(state: str, mask: int) -> None:
    previous = os.umask(mask)
    try:
        LogWriter(ClockedGateway(), state).append(ACTOR, "prune", **PRUNE)
    finally:
        os.umask(previous)
    for name in ("log.jsonl", "log.lock"):
        assert stat.S_IMODE(os.lstat(f"{state}/{name}").st_mode) == 0o600


def test_appends_accumulate_one_line_each(state: str) -> None:
    writer = LogWriter(ClockedGateway(), state)
    for _ in range(3):
        writer.append(ACTOR, "retro_delete", **RETRO_DELETE)
    assert len(lines(state)) == 3


@pytest.mark.parametrize("name", ["log.jsonl", "log.lock"])
def test_a_symlinked_log_file_is_refused(state: str, tmp_path: Path, name: str) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    (Path(state) / name).symlink_to(target)
    with pytest.raises(CliError) as caught:
        LogWriter(ClockedGateway(), state).append(ACTOR, "prune", **PRUNE)
    assert caught.value.details["reason"] == "store_unwritable"
    assert target.read_bytes() == b""


@pytest.mark.parametrize("name", ["log.jsonl", "log.lock"])
@pytest.mark.skipif(os.geteuid() == 0, reason="root")
def test_a_log_file_with_a_wrong_mode_is_refused_not_repaired(state: str, name: str) -> None:
    path = Path(state) / name
    path.write_bytes(b"")
    path.chmod(0o644)
    with pytest.raises(CliError):
        LogWriter(ClockedGateway(), state).append(ACTOR, "prune", **PRUNE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and path.read_bytes() == b""


def test_a_foreign_owned_log_is_refused(state: str) -> None:
    LogWriter(ClockedGateway(), state).append(ACTOR, "prune", **PRUNE)
    from .storehelp import foreign_gateway

    with pytest.raises(CliError):
        LogWriter(foreign_gateway(), state).append(ACTOR, "prune", **PRUNE)


def test_a_missing_directory_is_store_unwritable(tmp_path: Path) -> None:
    with pytest.raises(CliError) as caught:
        LogWriter(ClockedGateway(), str(tmp_path / "none")).append(ACTOR, "prune", **PRUNE)
    assert caught.value.exit_code == 12


def test_a_torn_last_line_is_ended_so_the_new_entry_is_whole(state: str) -> None:
    path = Path(state) / "log.jsonl"
    path.write_bytes(b'{"ts": "torn')
    path.chmod(0o600)
    LogWriter(ClockedGateway(), state).append(ACTOR, "prune", **PRUNE)
    first, second = lines(state)
    assert first == '{"ts": "torn'
    validate(json.loads(second), "session-audit/log.json")


# --- locking and concurrency ----------------------------------------------------------------


def test_lock_wait_is_bounded_and_retryable(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = ClockedGateway()
    holder = gateway.open_private(f"{state}/log.lock", create=True)
    assert gateway.lock_exclusive(holder, 1.0)
    monkeypatch.setattr(log_module, "LOCK_TIMEOUT", 0.2)
    try:
        with pytest.raises(CliError) as caught:
            LogWriter(gateway, state).append(ACTOR, "prune", **PRUNE)
    finally:
        holder.close()
    assert caught.value.details == {"reason": "store_unwritable", "retryable": True}
    assert not (Path(state) / "log.jsonl").exists()
    LogWriter(gateway, state).append(ACTOR, "prune", **PRUNE)  # the lock ended with its holder


def test_the_lock_timeout_is_five_seconds() -> None:
    assert log_module.LOCK_TIMEOUT == 5.0


def test_concurrent_threads_never_interleave(state: str) -> None:
    barrier = threading.Barrier(8)
    failures: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            writer = LogWriter(ClockedGateway(), state)
            barrier.wait()
            for _ in range(40):
                writer.append(f"auto:w{n}", "ingest", **INGEST)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert failures == []
    written = lines(state)
    assert len(written) == 320
    for line in written:
        validate(json.loads(line), "session-audit/log.json")
    for n in range(8):
        assert sum(json.loads(line)["actor"] == f"auto:w{n}" for line in written) == 40


CHILD = """
import sys
from session_audit.gateway import SystemGateway
from session_audit.log import LogWriter
w = LogWriter(SystemGateway(), sys.argv[1])
names = ["seen", "new", "changed", "unchanged", "unsafe", "excluded", "vanished", "rewritten"]
files = dict.fromkeys([*names, "remaining"], 1)
lines = {"parsed": 1, "errors": 0, "oversized": 0, "unknown": 0}
for _ in range(60):
    w.append("auto:" + sys.argv[2], "ingest", complete=True, files=files, lines=lines,
             sessions={"new": 0, "updated": 0})
"""


def test_concurrent_processes_never_interleave(state: str) -> None:
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, state, name], env=env, stderr=subprocess.PIPE
        )
        for name in ("p1", "p2", "p3")
    ]
    pids = [c.pid for c in children]
    for child in children:
        assert child.wait(timeout=120) == 0, child.stderr.read() if child.stderr else b""
    assert len(set(pids)) == 3
    written = lines(state)
    assert len(written) == 180
    for line in written:
        validate(json.loads(line), "session-audit/log.json")
