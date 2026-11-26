"""Helpers for the ``retro run`` tests: a gateway that answers ``claude-session --version`` and
sleeps on a fake clock, a corpus of old synthetic sessions, stored retro rows in the layout of
``retroview`` and a driver for the real CLI and for ``retro.prepare``."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import cli
from session_audit.command import Context
from session_audit.gateway import CallResult, CallStatus, SystemGateway
from session_audit.lease import identity
from session_audit.retroview import (
    encode_artifact,
    encode_resolved_scope,
    encode_session_map,
    scope_text,
)

from .digesthelp import OLD, make_ctx, prompt, say, write
from .fake_claude_session import FakeClaudeSession
from .ingesthelp import Corpus, Run, TickGateway, connect, ingest

KEY_A = "a" * 64
RETRO = "ra-abcdefgh"
VERSION_OK = "claude-session 0.45.3\n"


class RetroGateway(TickGateway):
    """``claude-session --version`` prints ``version`` (None: not installed); every other call is
    refused. ``sleep`` moves the fake clock and is recorded, never real."""

    def __init__(self) -> None:
        super().__init__()
        self.version: str | None = VERSION_OK
        self.sleeps: list[float] = []
        self.fake: FakeClaudeSession | None = None

    def run(self, argv: Any, **kwargs: Any) -> Any:
        self.runs.append(list(argv))
        if self.fake is not None:
            # Every claude-session call goes to the scripted fake program (validated documents).
            forwarded = {**kwargs, "env": self.fake.env()}
            return SystemGateway.run(self, [str(self.fake.program), *list(argv)[1:]], **forwarded)
        if list(argv)[1:] != ["--version"]:
            raise AssertionError(f"retro run (steps 1 to 10) must not call {list(argv)!r}")
        if self.version is None:
            return CallResult(CallStatus.SPAWN_FAILED, detail="FileNotFoundError: scripted")
        return CallResult(CallStatus.EXITED, 0, stdout=self.version)

    def sleep(self, seconds: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            time.sleep(0.001)  # a lease renewal thread: neither recorded nor moving the clock
            return
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


def seed(corpus: Corpus, session: str, text: str = "hello", project: str = "p1") -> Path:
    """An old (not live) session with a prompt and an answer."""
    return write(
        corpus,
        session,
        [prompt(text, 1, cwd=f"/work/{project}"), say(f"m-{session}", "answer", 2)],
        project=project,
    )


def retro(
    capsys: pytest.CaptureFixture[str],
    gw: TickGateway,
    *argv: str,
    tty: bool = False,
) -> Run:
    capsys.readouterr()
    code = cli.main(["retro", "run", "--json", *argv], gateway=gw, stdin_is_tty=tty)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def ingested(capsys: pytest.CaptureFixture[str], gw: TickGateway) -> None:
    # The harness capture of a plain ingest would call claude-session: make it missing for the run.
    saved = getattr(gw, "version", None)
    saved_fake = getattr(gw, "fake", None)
    if isinstance(gw, RetroGateway):
        gw.version = None
        gw.fake = None
    try:
        run = ingest(capsys, gw)
    finally:
        if isinstance(gw, RetroGateway):
            gw.version = saved
            gw.fake = saved_fake
    assert run.code == 0, run.err


def args_for(**given: Any) -> argparse.Namespace:
    """The namespace ``retro run`` hands ``prepare`` after its checks."""
    base: dict[str, Any] = {
        "session": None,
        "label": None,
        "project": None,
        "since": None,
        "until": None,
        "kind": None,
        "outcome": None,
        "include_self_improve": False,
        "skill": None,
        "model": None,
        "again": False,
    }
    base.update(given)
    return argparse.Namespace(**base)


def retro_ctx(
    gw: TickGateway,
    corpus: Corpus,
    *,
    dry_run: bool = False,
    retro_settings: dict[str, Any] | None = None,
    **digest: Any,
) -> Context:
    ctx = make_ctx(gw, corpus.home, corpus, **digest)
    ctx.actor = "auto:test"
    ctx.dry_run = dry_run
    ctx.quiet = True
    if retro_settings:
        ctx.settings = ctx.settings.model_copy(
            update={"retro": ctx.settings.retro.model_copy(update=retro_settings)}
        )
    return ctx


def add_retro(
    home: Path,
    key: str,
    *,
    retro_id: str = RETRO,
    state: str = "done",
    session: str = "11111111-1111-4111-8111-111111111111",
    created: int = 1_000,
    lease: tuple[Any, Any, Any] = (None, None, None),
    summary: str | None = "a summary",
    reason: str | None = None,
    artifact: str | None = None,
    **overrides: Any,
) -> None:
    """A stored retro in the layout of ``retroview`` (a ``done`` one by default)."""
    conn = connect(home)
    try:
        counts = {
            "sessions": 1,
            "skipped": 0,
            "over_cap": 0,
            "items": 2,
            "findings": 0,
            "candidates": 0,
        }
        coverage = {
            "digest_bytes": 10,
            "items_total": 2,
            "items_kept": 2,
            "sessions": [
                {
                    "ref": "s1",
                    "items_total": 2,
                    "items_kept": 2,
                    "dropped": {"session_cap": 0, "total_cap": 0},
                }
            ],
        }
        flags = {
            "labels": [],
            "project": None,
            "since": None,
            "until": None,
            "kind": None,
            "outcome": None,
        }
        values: dict[str, Any] = {
            "id": retro_id,
            "key": key,
            "state": state,
            "reason": reason,
            "scope": scope_text(flags),
            "resolved_scope": encode_resolved_scope(flags, [session], None, None),
            "skill": "session-audit:audit",
            "model": None,
            "created_at": created,
            "session_map": encode_session_map(
                [{"ref": "s1", "session_id": session, "csession_id": None}]
            ),
            "counts": json.dumps(counts),
            "dropped": json.dumps({"findings": 0, "candidates": 0}),
            "lease_pid": lease[0],
            "lease_start": lease[1],
            "lease_expires_at": lease[2],
        }
        values.update(overrides)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO retros ({names}) VALUES ({marks})", tuple(values.values()))
        if summary is not None:
            conn.execute(
                "INSERT INTO retro_artifacts (retro_id, artifact, summary) VALUES (?, ?, ?)",
                (
                    retro_id,
                    artifact
                    if artifact is not None
                    else encode_artifact(
                        {"findings": [], "candidates": []},
                        [],
                        coverage,
                        {"total.tool_calls": 0},
                    ),
                    summary,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def live_lease(gw: TickGateway, seconds: int = 120) -> tuple[int, str, int]:
    """A lease held by this very process, live for ``seconds`` of the fake clock."""
    holder = identity(gw)
    return holder.pid, holder.start, int(gw.now().timestamp() * 1000) + seconds * 1000


def mtime_of(gw: TickGateway, ago_seconds: int) -> int:
    return int(gw.now().timestamp()) - ago_seconds


def touch(path: Path, mtime: int = OLD) -> None:
    os.utime(path, (mtime, mtime))


def rid(n: int) -> str:
    """The n-th valid retro id (``ra-`` and eight base32 characters)."""
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return "ra-" + "".join(alphabet[(n >> (5 * i)) % 32] for i in range(8))


def ingest_entries(home: Path) -> int:
    """The ``ingest`` entries of ``log.jsonl`` (0 when there is no log)."""
    path = home / ".local" / "state" / "session-audit" / "log.jsonl"
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if json.loads(line).get("action") == "ingest")


def retro_rows(home: Path) -> int:
    conn = connect(home)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM retros").fetchone()[0])
    finally:
        conn.close()
