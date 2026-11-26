"""Helpers for the recovery and cleanup tests (issue #27): planted ``retro_sessions`` rows,
transcripts of a retro's own session, stored retros left in a non-final state, scripted
claude-session answers for recovery and a driver for ``retrorecovery.run_housekeeping``."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from session_audit import retrorecovery
from session_audit.gateway import SystemGateway
from session_audit.retrocleanup import SessionRow
from session_audit.store import open_store

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    forget_doc,
    status_doc,
    step,
    stop_doc,
)
from .ingesthelp import Corpus, TickGateway, connect, state_dir_of
from .retrohelp import KEY_A, RETRO, add_retro, retro_ctx

CSID = "cs-aaaaaaab"
OTHER = "cs-aaaaaaac"
PROJECT = "-state-sessions-cs-aaaaaaab-work"
RECENT_MS = 1_767_225_000_000  # shortly before the fake clock starts (2026-01-01)
UUIDS = ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
__all__ = ["KEY_A", "RETRO"]  # re-exported for the tests


def ensure_store(home: Path) -> None:
    """The store exists (created and migrated when it did not)."""
    open_store(SystemGateway(), str(state_dir_of(home))).close()


def plant_session(
    home: Path,
    csid: str = CSID,
    retro_id: str = RETRO,
    *,
    project: str = PROJECT,
    state: str = "pending",
    forgotten_at: int | None = None,
    csession_dir: str | None = None,
    private_dir: str | None = None,
    created_at: int = 1,
) -> None:
    ensure_store(home)
    conn = connect(home)
    try:
        conn.execute(
            "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, private_dir, "
            "project_dir, cleanup_state, forgotten_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                csid,
                retro_id,
                csession_dir or f"/nonexistent/sessions/{csid}",
                private_dir or f"/nonexistent/private/{csid}",
                project,
                state,
                forgotten_at,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def plant_transcripts(
    corpus: Corpus, project: str = PROJECT, ids: tuple[str, ...] = UUIDS, *, env: bool = True
) -> dict[str, Path]:
    """The transcripts Claude Code leaves for a retro session: ``<uuid>.jsonl``, a ``<uuid>/``
    directory with a subagent file, ``memory/`` and the ``session-env/<uuid>`` entries."""
    directory = corpus.root / project
    directory.mkdir(mode=0o700, exist_ok=True)
    made = {"project": directory}
    for i, session in enumerate(ids):
        (directory / f"{session}.jsonl").write_text("{}\n")
        sub = directory / session / "subagents"
        sub.mkdir(parents=True, mode=0o700, exist_ok=True)
        (sub / "agent-a.jsonl").write_text("{}\n")
        if env:
            entry = corpus.home / ".claude" / "session-env" / session
            entry.mkdir(parents=True, mode=0o700, exist_ok=True)
            (entry / "state").write_text("x")
            made[f"env{i}"] = entry
    (directory / "memory").mkdir(mode=0o700, exist_ok=True)
    (directory / "memory" / "MEMORY.md").write_text("x")
    return made


def session_rows(home: Path) -> dict[str, sqlite3.Row]:
    conn = connect(home)
    conn.row_factory = sqlite3.Row
    try:
        return {r["csession_id"]: r for r in conn.execute("SELECT * FROM retro_sessions")}
    finally:
        conn.close()


def retro_rows(home: Path) -> dict[str, sqlite3.Row]:
    conn = connect(home)
    conn.row_factory = sqlite3.Row
    try:
        return {r["id"]: r for r in conn.execute("SELECT * FROM retros")}
    finally:
        conn.close()


def orphan(
    home: Path,
    retro_id: str = RETRO,
    *,
    state: str = "running",
    csid: str | None = None,
    created: int = RECENT_MS,
    lease: tuple[Any, Any, Any] = (None, None, None),
    key: str = KEY_A,
    digest: bool = False,
    **overrides: Any,
) -> None:
    """A retro a dead process left behind (no live lease), with its local files when asked."""
    ensure_store(home)
    extra: dict[str, Any] = {"csession_id": csid, **overrides}
    if digest or state != "digesting":
        extra.setdefault("digest_sha256", "d" * 64)
        extra.setdefault("run_token", "t" * 32)
    add_retro(
        home,
        key,
        retro_id=retro_id,
        state=state,
        created=created,
        lease=lease,
        summary=None,
        **extra,
    )


def plant_local_files(home: Path, retro_id: str = RETRO) -> dict[str, Path]:
    """The prompt, outcome file and digest of a retro that started."""
    state = state_dir_of(home)
    paths = {
        "prompt": state / "prompts" / retro_id / "prompt.md",
        "outcome": state / "outcomes" / f"{retro_id}.json",
        "digest": state / "digests" / retro_id / "digest.md",
    }
    for kind, path in paths.items():
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_text("x")
        if kind != "outcome":
            path.parent.chmod(0o700)
    return paths


def script_status(
    cs: FakeClaudeSession,
    retro_id: str = RETRO,
    csid: str = CSID,
    *,
    live: bool = False,
    labels: list[str] | None = None,
) -> None:
    """``status`` of the recorded session: live or ended, with the tool's labels by default."""
    given = labels if labels is not None else ["owner=session-audit", f"retro={retro_id}"]
    doc = status_doc(csid, outcome=None if live else "done", labels=given, sandbox_active=True)
    cs.script("status", step(doc))


def script_cleanup(
    cs: FakeClaudeSession,
    csid: str = CSID,
    *,
    purge: list[Path | str] | None = None,
    purged: bool = True,
) -> None:
    """``forget --purge`` that purges (``purge`` are removed like claude-session's directories),
    and ``stop`` that ends the run."""
    cs.script("forget", step(forget_doc(csid, purged=purged), remove_paths=purge))
    cs.script("stop", step(stop_doc(csid)))


def not_found() -> dict[str, Any]:
    return step(error_doc("not_found", "gone"), exit=10)


def hook_settings(tmp_path: Path) -> tuple[Any, Path, Path]:
    """``HookSettings`` whose post-retro and post-attention entries append their stdin payload (one
    line) to files; returns them and the two file paths."""
    from session_audit.config import HookSettings

    retro_log, attention_log = tmp_path / "post-retro.log", tmp_path / "post-attention.log"
    settings = HookSettings.model_validate(
        {
            "post-retro": [{"cmd": f"tr -d '\\n' >> {retro_log}; echo >> {retro_log}"}],
            "post-attention": [{"cmd": f"tr -d '\\n' >> {attention_log}; echo >> {attention_log}"}],
        }
    )
    return settings, retro_log, attention_log


def payloads(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def housekeep(
    gw: TickGateway,
    corpus: Corpus,
    *,
    hooks: Any = None,
    harness_enabled: str | None = None,
    quiet: bool = True,
    **settings: Any,
) -> retrorecovery.Result:
    ctx = retro_ctx(gw, corpus, retro_settings=settings or None)
    ctx.quiet = quiet
    update: dict[str, Any] = {}
    if hooks is not None:
        update["hooks"] = hooks
    if harness_enabled is not None:
        update["harness"] = ctx.settings.harness.model_copy(update={"enabled": harness_enabled})
    if update:
        ctx.settings = ctx.settings.model_copy(update=update)
    try:
        return retrorecovery.run_housekeeping(ctx)
    finally:
        ctx.close()


def keys(cs: FakeClaudeSession) -> list[str]:
    return [c["key"] for c in cs.calls() if c["key"] != "--version"]


def row_of(
    csid: str = CSID,
    retro_id: str = RETRO,
    *,
    project: str = PROJECT,
    state: str = "pending",
    forgotten_at: int | None = None,
    csession_dir: str = "/nonexistent/s",
    private_dir: str | None = "/nonexistent/p",
) -> SessionRow:
    return SessionRow(csid, retro_id, csession_dir, private_dir, project, state, forgotten_at, 1)


def log_states(home: Path, retro_id: str = RETRO) -> list[dict[str, Any]]:
    path = state_dir_of(home) / "log.jsonl"
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in found if e["action"] == "retro_state" and e["retro_id"] == retro_id]


def expire_lease(home: Path) -> None:
    """A holder that died: its lease runs out (the pid may still exist: this very process)."""
    conn = connect(home)
    try:
        conn.execute("UPDATE retros SET lease_expires_at = 0 WHERE lease_pid IS NOT NULL")
        conn.commit()
    finally:
        conn.close()


def touch_old(path: Path, seconds_ago: float) -> None:
    moment = path.lstat().st_mtime - seconds_ago
    os.utime(path, (moment, moment), follow_symlinks=False)
