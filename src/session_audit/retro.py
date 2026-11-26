"""``retro run``: the order of checks through the key and the reuse decision (steps 1 to 10; #23).

``prepare`` runs, in this order and with no later step after a failure:

1. flags and scope (``commands/retro_run.check`` runs before the config is read);
2. config: ``retro.sandbox`` below ``required`` without a terminal, the permission profile file
   (both ``config_unsafe``), then the store (``store_newer``). The unsafe-config refusal itself is
   ``cli._check_config``, which runs first;
3. claude-session present and at least MIN_CS (``csversion.check_version``);
4. ``Steps.skill_preflight`` (TODO(#24));
5. ``Steps.housekeeping``, not with ``--dry-run`` (TODO(#27));
6. the scope (``retroscope.resolve_scope``), 7. the candidates by stored ``ended_at``;
8. the refresh and the skips: the candidates are digested in growing prefixes of the order until
   ``retro.max_sessions`` are kept or none is left; each new prefix is refreshed first
   (``retroscope.Refresher``), then ``digest.build_digest`` skips ``live``, ``transcript_missing``,
   ``transcript_changed`` and ``empty``;
9. the digest of the LAST build is the one used (one read snapshot, one redactor), and the key is
   computed from its section hashes: ``fencing.section_hashes`` needs no nonce, retro id or run
   token, so the key is known here, before any row exists. ``fencing.finish_digest`` (nonce,
   fence, manifest) runs after step 10, when the retro id exists (TODO(#24), TODO(#26));
10. reuse of a ``done`` retro with the key (no ``--again``), ``already_running`` for a non-final
    retro with the key and a live lease.

Nothing is written before step 13 of the Order of checks (the row insert, #24/#26) except the
refresh, which is an ingest run. ``Prepared`` is what #24 and #26 continue from.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import errors, harness
from .command import Context
from .config import home_dir, profile_issues
from .csversion import check_version
from .digest import DigestResult, DigestScope, Skipped, build_digest
from .fencing import SectionHash, section_hashes
from .grammar import is_csession_id
from .lease import LeaseState, is_live
from .retrokey import retro_key
from .retroscope import Refresher, Resolved, refused, resolve_scope
from .retroview import load_retro
from .sessionquery import read_store, snapshot_index

__all__ = ["KeyState", "NoSteps", "Prepared", "Steps", "lookup_key", "prepare"]

LOOKUP_LIMIT = 50  # rows of one state read per key: a store full of one key stays bounded
NON_FINAL = ("digesting", "running", "validating")
FINAL = ("done", "failed", "escalated", "canceled")


class Steps(Protocol):
    """The hook points of the steps other issues implement. The defaults do nothing."""

    def skill_preflight(self, ctx: Context, skill: str) -> None:
        """Order of checks step 4 (TODO(#24)): exit 2 for a malformed skill, 12 for
        ``skill_not_found`` or ``unsafe_plugin_dir``."""

    def housekeeping(self, ctx: Context) -> None:
        """Order of checks step 5 (TODO(#27)): recovery and cleanup retries. Not called with
        ``--dry-run``. Never changes the command's exit code."""


class NoSteps:
    def skill_preflight(self, ctx: Context, skill: str) -> None:
        return None

    def housekeeping(self, ctx: Context) -> None:
        return None


@dataclass(frozen=True)
class KeyState:
    """What the store holds for a key (step 10, repeated at step 13): ``reused`` is the Retro
    object of the newest valid ``done`` retro, ``running`` the id of a non-final retro whose lease
    is live, ``previous`` the newest final retro (what ``--again`` records as ``again_of``)."""

    reused: dict[str, Any] | None
    running: str | None
    previous: str | None


@dataclass(frozen=True)
class Prepared:
    """The result of steps 1 to 10 when the run goes on (steps 11 and later: #24, #26)."""

    key: str
    skill: str
    model: str | None
    digest: DigestResult
    hashes: tuple[SectionHash, ...]
    csession_ids: Mapping[str, str | None]
    explicit_sessions: tuple[str, ...]
    matched_total: int
    over_cap: int
    skipped: tuple[Skipped, ...]
    warnings: tuple[str, ...]
    projects: int
    again_of: str | None
    reused_retro: str | None  # an id, for the dry-run plan
    running_retro: str | None  # an id, for the dry-run plan
    reuse: dict[str, Any] | None  # set only when the run must print it (not dry, no --again)

    @property
    def kept(self) -> int:
        return len(self.digest.sections)


# --- step 2 and 3 --------------------------------------------------------------------------------


def check_config(ctx: Context, env: Mapping[str, str]) -> None:
    if ctx.settings.retro.sandbox != "required" and not ctx.stdin_is_tty:
        raise errors.CliError(
            "refused",
            "retro.sandbox is below required and there is no terminal",
            details={"reason": "config_unsafe", "field": "retro.sandbox"},
            hint="Set retro.sandbox to required, or run from a terminal.",
        )
    profile = ctx.settings.retro.permission_profile
    if profile is not None and profile_issues(ctx.gateway, profile, home_dir(env)):
        raise errors.CliError(
            "refused",
            "the permission profile file is unsafe",
            details={"reason": "config_unsafe", "field": "retro.permission_profile"},
            hint="Its file and directories must be yours and not group or other writable.",
        )
    if ctx.dry_run:
        read_store(ctx, lambda conn: None)  # a newer store is exit 20; nothing is created
    else:
        ctx.store()  # opens (and migrates) the store: store_newer, store_unwritable


def check_claude_session(ctx: Context) -> None:
    check = check_version(ctx.gateway, ctx.settings.harness.claude_session_cmd)
    if not check.ok:
        raise harness.unavailable(check)


# --- step 10 -------------------------------------------------------------------------------------


def _lease_of(row: tuple[Any, ...]) -> LeaseState | None:
    pid, start, expires = row
    ok = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and isinstance(start, str)
        and isinstance(expires, int)
        and not isinstance(expires, bool)
    )
    return LeaseState(pid, start, expires) if ok else None


def lookup_key(ctx: Context, conn: sqlite3.Connection, key: str) -> KeyState:
    """The retros with ``key``. A ``done`` row that does not fit the stored layout is skipped (a
    damaged row is never reused); a lease with the wrong types is not live."""
    reused: dict[str, Any] | None = None
    for (retro_id,) in conn.execute(
        "SELECT id FROM retros WHERE key = ? AND state = 'done' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (key, LOOKUP_LIMIT),
    ).fetchall():
        reused = load_retro(conn, retro_id)
        if reused is not None:
            break
    running: str | None = None
    marks = ",".join("?" for _ in NON_FINAL)
    for retro_id, *lease in conn.execute(
        "SELECT id, lease_pid, lease_start, lease_expires_at FROM retros "
        f"WHERE key = ? AND state IN ({marks}) ORDER BY created_at DESC, id DESC LIMIT ?",
        (key, *NON_FINAL, LOOKUP_LIMIT),
    ).fetchall():
        state = _lease_of(tuple(lease))
        if state is not None and is_live(ctx.gateway, state):
            running = retro_id
            break
    marks = ",".join("?" for _ in FINAL)
    row = conn.execute(
        f"SELECT id FROM retros WHERE key = ? AND state IN ({marks}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (key, *FINAL),
    ).fetchone()
    return KeyState(reused, running, None if row is None else row[0])


# --- steps 8 and 9 -------------------------------------------------------------------------------


def _digest(
    ctx: Context,
    resolved: Resolved,
    refresher: Refresher,
    env: Mapping[str, str],
    scope_max: int,
) -> tuple[DigestResult, int]:
    """The digest of the candidates, refreshed in growing prefixes of the order until the cap is
    filled. Returns the last build and how many candidates were never put in front of it."""
    order = resolved.order
    attempt = min(len(order), scope_max)
    refreshed = 0
    while True:
        refresher.refresh(order[refreshed:attempt])
        refreshed = attempt
        scope = DigestScope(
            session_ids=order[:attempt],
            filters=resolved.filters,
            max_sessions=scope_max,
            quiet_after=ctx.settings.retro.quiet_after,
        )
        result = build_digest(ctx, scope, env=env)
        kept = len(result.sections)
        if kept >= scope_max or attempt >= len(order):
            return result, len(order) - attempt
        attempt = min(len(order), attempt + max(scope_max - kept, attempt // 2))


def _projects(result: DigestResult) -> int:
    found = set()
    for section in result.sections:
        for line in section.header:
            if line.startswith("project: ") and line != "project: -":
                found.add(line)
    return len(found)


def prepare(
    ctx: Context,
    args: argparse.Namespace,
    *,
    steps: Steps | None = None,
    env: Mapping[str, str] | None = None,
) -> Prepared:
    steps = steps or NoSteps()
    env = os.environ if env is None else env
    check_config(ctx, env)  # step 2
    check_claude_session(ctx)  # step 3
    settings = ctx.settings.retro
    skill = args.skill or settings.skill
    model = args.model or settings.model
    steps.skill_preflight(ctx, skill)  # step 4
    if not ctx.dry_run:
        steps.housekeeping(ctx)  # step 5
    refresher = Refresher(ctx)
    resolved = resolve_scope(ctx, args, refresher, scope_max=settings.max_sessions)  # 6, 7
    result, unreached = _digest(ctx, resolved, refresher, env, settings.max_sessions)  # 8
    if not result.sections:
        reasons = {s.reason for s in result.skipped}
        raise refused("live_sessions_only" if reasons == {"live"} else "empty_scope")
    hashes = section_hashes(result)  # 9
    key = retro_key(hashes, skill, model)
    warnings = (*result.warnings, *(("ingest_busy",) if refresher.busy else ()))

    def snapshots(conn: sqlite3.Connection | None) -> dict[str, str | None]:
        index = {} if conn is None else snapshot_index(conn)
        found: dict[str, str | None] = {}
        for section in result.sections:
            snap = index.get(section.session_id)
            found[section.session_id] = (
                snap.csession_id if snap is not None and is_csession_id(snap.csession_id) else None
            )
        return found

    csessions = read_store(ctx, snapshots)
    state = read_store(
        ctx, lambda conn: KeyState(None, None, None) if conn is None else lookup_key(ctx, conn, key)
    )  # 10
    reuse = state.reused if not ctx.dry_run and not args.again else None
    if reuse is None and state.running is not None and not ctx.dry_run:
        raise errors.CliError(
            "refused",
            "a retro with the same key is running",
            details={"reason": "already_running", "retro_id": state.running},
        )
    return Prepared(
        key=key,
        skill=skill,
        model=model,
        digest=result,
        hashes=hashes,
        csession_ids=csessions,
        explicit_sessions=resolved.explicit,
        matched_total=len(resolved.order),
        over_cap=result.over_cap + unreached,
        skipped=result.skipped,
        warnings=warnings,
        projects=_projects(result),
        again_of=state.previous if args.again else None,
        reused_retro=None if state.reused is None else state.reused["id"],
        running_retro=state.running,
        reuse=reuse,
    )
