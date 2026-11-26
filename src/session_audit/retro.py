"""``retro run``: the order of checks through the key and the reuse decision (steps 1 to 10; #23).

``prepare`` runs, in this order and with no later step after a failure:

1. flags and scope (``commands/retro_run.check`` runs before the config is read);
2. config: ``retro.sandbox`` below ``required`` without a terminal, the permission profile file
   (both ``config_unsafe``), then the store (``store_newer``). The unsafe-config refusal itself is
   ``cli._check_config``, which runs first;
3. claude-session present and at least MIN_CS (``csversion.check_version``);
4. ``Steps.skill_preflight`` (``plugins.skill_preflight``, wired in ``commands/retro_run``);
5. ``Steps.housekeeping``, not with ``--dry-run`` (``retrorecovery``, in ``commands/retro_run``);
6. the scope (``retroscope.resolve_scope``), 7. the candidates by stored ``ended_at``;
8. the refresh and the skips: the candidates are digested in growing prefixes of the order until
   ``retro.max_sessions`` are kept or none is left; each new prefix is refreshed first
   (``retroscope.Refresher``), then ``digest.build_digest`` skips ``live``, ``transcript_missing``,
   ``transcript_changed`` and ``empty``;
9. the digest of the LAST build is the one used (one read snapshot, one redactor), and the key is
   computed from its section hashes: ``fencing.section_hashes`` needs no nonce, retro id or run
   token, so the key is known here, before any row exists. ``fencing.finish_digest`` (nonce,
   fence, manifest) runs after step 13, when the retro id exists (``retrolaunch``);
10. reuse of a ``done`` retro with the key (no ``--again``), ``already_running`` for a non-final
    retro with the key and a live lease.

Nothing is written before step 13 of the Order of checks (the row insert, ``retrostate``) except the
refresh, which is an ingest run. A dry run writes nothing at all: it reads and ingests a private
in-memory copy of the store (``store.open_memory_copy``, ``Context.memory``) to the current end of
every file, so its key is the key of a real run over the same bytes. ``Prepared`` is what
``commands/retro_run`` (steps 11 and 12) and ``retrolaunch`` (steps 13 to 16) continue from.
"""

from __future__ import annotations

import argparse
import os
import re
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
from .plugins import PluginChoice
from .retrokey import retro_key
from .retroscope import Refresher, Resolved, refused, resolve_scope
from .retroview import load_retro
from .sessionquery import Filters, read_store, snapshot_index
from .store import open_memory_copy

__all__ = ["KeyState", "NoSteps", "Prepared", "Steps", "lookup_key", "prepare"]

LOOKUP_LIMIT = 50  # ``done`` rows read per key: a store full of one key stays bounded
LEASE_LOOKUP_LIMIT = 1000  # non-final rows with an unexpired lease examined per key
UNFIT_COUNT_LIMIT = 1000  # ``done`` rows of one key counted for the "did not fit" note
_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}", re.ASCII)
# The same shape as a SQL GLOB, so a forged id is filtered before the LIMIT, not after it.
_ID_GLOB = "ra-" + "[a-z2-7]" * 8
NON_FINAL = ("digesting", "running", "validating")
FINAL = ("done", "failed", "escalated", "canceled")


class Steps(Protocol):
    """The hook points of the steps other issues implement. The defaults do nothing."""

    def skill_preflight(self, ctx: Context, skill: str) -> PluginChoice | None:
        """Order of checks step 4 (``plugins.skill_preflight``): exit 2 for a malformed skill, 12
        for ``skill_not_found`` or ``unsafe_plugin_dir``. Returns the directories found."""

    def housekeeping(self, ctx: Context) -> None:
        """Order of checks step 5 (``retrorecovery``): recovery and cleanup retries. Not called with
        ``--dry-run``. Never changes the command's exit code."""


class NoSteps:
    def skill_preflight(self, ctx: Context, skill: str) -> PluginChoice | None:
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
    unfit: int = 0  # ``done`` rows with the key when none of them could be reused (0 otherwise)


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
    warnings: tuple[str, ...]  # closed tokens only (``ingest_busy``): what the row stores
    notes: tuple[str, ...]  # free text for stderr (digest discovery warnings), never stored
    unfit_retros: int  # stored ``done`` retros with this key that did not fit and were not reused
    projects: int
    again_of: str | None
    reused_retro: str | None  # an id, for the dry-run plan
    running_retro: str | None  # an id, for the dry-run plan
    reuse: dict[str, Any] | None  # set only when the run must print it (not dry, no --again)
    plugin_dirs: tuple[str, ...] = ()  # the ``--plugin-dir`` values of step 4
    bundled_plugin: str | None = None  # the bundled plugin directory (the default profile's home)
    daily_remaining: int = 0  # for the dry-run plan; the binding check is step 13's
    filters: Filters | None = None  # the resolved scope filters (``scope_resolved`` of the row)

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
    # A newer store is exit 20 for a real run too, and nothing is created: the first writer (a
    # refresh at step 6 or 8, the row of step 13) creates the store, after every check that can
    # still fail without storing anything.
    read_store(ctx, lambda conn: None)


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
    """The retros with ``key``. Rows whose id does not have the shape of a retro id are never
    looked at (filtered in SQL before every LIMIT, so forged or damaged rows cannot crowd out a
    real one). A ``done`` row that does not fit the stored layout is skipped (a damaged row is
    never reused); a lease with the wrong types or an expired lease is not live."""
    reused: dict[str, Any] | None = None
    for (retro_id,) in conn.execute(
        "SELECT id FROM retros WHERE key = ? AND state = 'done' AND reason IS NULL "
        "AND typeof(id) = 'text' AND id GLOB ? "
        "AND EXISTS (SELECT 1 FROM retro_artifacts a WHERE a.retro_id = retros.id "
        "AND typeof(a.summary) = 'text' AND length(a.summary) > 0) "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (key, _ID_GLOB, LOOKUP_LIMIT),
    ).fetchall():
        reused = load_retro(conn, retro_id)
        if reused is not None:
            break
    unfit = 0
    if reused is None:
        unfit = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM retros WHERE key = ? AND state = 'done' LIMIT ?)",
            (key, UNFIT_COUNT_LIMIT),
        ).fetchone()[0]
    running: str | None = None
    marks = ",".join("?" for _ in NON_FINAL)
    now = int(ctx.gateway.now().timestamp() * 1000)
    # Only a row with a typed, unexpired lease can be live; the newest expiry comes first.
    for retro_id, *lease in conn.execute(
        "SELECT id, lease_pid, lease_start, lease_expires_at FROM retros "
        f"WHERE key = ? AND state IN ({marks}) AND typeof(id) = 'text' AND id GLOB ? "
        "AND typeof(lease_pid) = 'integer' AND typeof(lease_start) = 'text' "
        "AND typeof(lease_expires_at) = 'integer' AND lease_expires_at > ? "
        "ORDER BY lease_expires_at DESC, created_at DESC, id DESC LIMIT ?",
        (key, *NON_FINAL, _ID_GLOB, now, LEASE_LOOKUP_LIMIT),
    ).fetchall():
        state = _lease_of(tuple(lease))
        if state is not None and _RETRO_ID.fullmatch(retro_id) and is_live(ctx.gateway, state):
            running = retro_id
            break
    marks = ",".join("?" for _ in FINAL)
    row = conn.execute(
        f"SELECT id FROM retros WHERE key = ? AND state IN ({marks}) "
        "AND typeof(id) = 'text' AND id GLOB ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (key, *FINAL, _ID_GLOB),
    ).fetchone()
    previous = row[0] if row is not None and _RETRO_ID.fullmatch(row[0]) else None
    return KeyState(reused, running, previous, unfit)


def daily_remaining(ctx: Context, conn: sqlite3.Connection | None) -> int:
    """``max(0, limits.retros_per_day - reserved(today))`` (Retro lifecycle, Daily limit):
    ``reserved(day)`` is the starts recorded for the day (``time_zone``) plus the ``digesting``
    retros whose lease is live. Read only; the binding check is step 13's (#26)."""
    limit = ctx.settings.limits.retros_per_day
    if conn is None:
        return limit
    now = ctx.gateway.now()
    today = now.astimezone(ctx.settings.zone()).strftime("%Y-%m-%d")
    reserved = conn.execute("SELECT COUNT(*) FROM retro_starts WHERE day = ?", (today,)).fetchone()[
        0
    ]
    stamp = int(now.timestamp() * 1000)
    for retro_id, *lease in conn.execute(
        "SELECT id, lease_pid, lease_start, lease_expires_at FROM retros "
        "WHERE state = 'digesting' AND typeof(id) = 'text' AND id GLOB ? "
        "AND typeof(lease_pid) = 'integer' AND typeof(lease_start) = 'text' "
        "AND typeof(lease_expires_at) = 'integer' AND lease_expires_at > ? LIMIT ?",
        (_ID_GLOB, stamp, LEASE_LOOKUP_LIMIT),
    ).fetchall():
        state = _lease_of(tuple(lease))
        if state is not None and _RETRO_ID.fullmatch(retro_id) and is_live(ctx.gateway, state):
            reserved += 1
    return max(0, limit - reserved)


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
    # Candidates are refreshed in batches in order: a batch is the deficit (or half again of what
    # was tried) and may reach past the last candidate that ends up kept. Those candidates count
    # in ``over_cap`` with the ones never offered (interface, Order of checks step 8).
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
    # Counted from the digest headers, which cut a long project name: two long names that share
    # their first characters count once. A stderr count only (R16: accepted, no behavior change).
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
    plugins = steps.skill_preflight(ctx, skill)  # step 4
    if not ctx.dry_run:
        steps.housekeeping(ctx)  # step 5
    else:
        # A dry run reads and ingests a private in-memory copy of the store (Freshness): to the
        # current end of every file, writing nothing. It is closed after step 10.
        ctx.memory = open_memory_copy(ctx.gateway, ctx.state_path())
    try:
        return _prepare_rest(ctx, args, env, skill, model, plugins)
    finally:
        if ctx.memory is not None:
            ctx.memory.close()
            ctx.memory = None


def _prepare_rest(
    ctx: Context,
    args: argparse.Namespace,
    env: Mapping[str, str],
    skill: str,
    model: str | None,
    plugins: PluginChoice | None,
) -> Prepared:
    settings = ctx.settings.retro
    refresher = Refresher(ctx)
    resolved = resolve_scope(ctx, args, refresher, scope_max=settings.max_sessions)  # 6, 7
    result, unreached = _digest(ctx, resolved, refresher, env, settings.max_sessions)  # 8
    if not result.sections:
        reasons = {s.reason for s in result.skipped}
        raise refused("live_sessions_only" if reasons == {"live"} else "empty_scope")
    hashes = section_hashes(result)  # 9
    key = retro_key(hashes, skill, model)
    warnings = ("ingest_busy",) if refresher.busy else ()

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
    remaining = read_store(ctx, lambda conn: daily_remaining(ctx, conn))
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
        notes=tuple(result.warnings),
        unfit_retros=state.unfit,
        projects=_projects(result),
        again_of=state.previous if args.again else None,
        reused_retro=None if state.reused is None else state.reused["id"],
        running_retro=state.running,
        reuse=reuse,
        plugin_dirs=() if plugins is None else plugins.dirs,
        bundled_plugin=None if plugins is None else plugins.bundled,
        daily_remaining=remaining,
        filters=resolved.filters,
    )
