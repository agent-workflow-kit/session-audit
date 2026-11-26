"""The retro row: the insert of step 13 and every later state write (issue #26).

Step 13 runs in ONE ``BEGIN IMMEDIATE`` transaction: ``retro.lookup_key`` is repeated (a stored
``done`` retro is reused, a live run of the key is ``already_running``, ``again_of`` is read) and
the row is inserted with every column written as ``docs/design.md`` (Data model, ``retros``) says,
through the ``retroview.encode_*`` helpers. The lease is the holder's own (``lease.identity``),
and the returned ``Lease`` is used as ``with lease:`` for the whole run, so its renewal thread runs
while the tool waits for claude-session and for hooks. No write lock is held across either.

Every later write is ``Retro.write``: one transaction, one compare-and-set on the holder's pid and
start time (``Lease.guarded_update``; rowcount 0 is ``lease_lost``, exit 1), and one ``retro_state``
log entry after the commit (hash-only fields: ids, hashes, closed tokens and counts; never text).

States written here and what recovery (#27) must handle (a crash or an exit between two writes
leaves exactly one of them; the lease columns stay set until ``Retro.release``):

``digesting``   row inserted, no files, no session: delete_local, final ``failed/runner_lost``.
``running``     ``retro_starts`` row, ``run_token`` and ``digest_sha256`` stored, files written or
                being written, a claude-session session maybe launched: ``csession_id`` is set once
                ``start`` returned (``retro_sessions`` row ``pending`` in the same transaction);
                recovery stops the run by that id and cleans up, or just deletes the local files.
``validating``  the session ended and was collected; the artifact check (``retroartifact``) runs.
final states    ``done`` (the artifact check), ``failed``, ``escalated``, ``canceled``:
                ``finished_at`` set. The lease stays held (and renewed) until cleanup and the post
                hooks are done, then ``release`` clears it.

Daily limit (#28): ``check_daily_limit`` runs inside the step 13 transaction; the ``retro_starts``
row is inserted with the ``running`` write (``retrolaunch``) and deleted again only by the refund of
a run that never reached the model (``Launcher._end``).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import errors, hooks, lease, retro
from .command import Context
from .gateway import Gateway
from .grammar import format_timestamp, is_csession_id
from .retroview import encode_resolved_scope, encode_session_map, scope_text
from .store import Store

__all__ = [
    "Inserted",
    "Retro",
    "Reused",
    "check_daily_limit",
    "insert_retro",
    "new_retro_id",
    "scope_object",
]

FINAL_STATES = ("done", "failed", "escalated", "canceled")
_RETRO_ID_TRIES = 8


def new_retro_id(gateway: Gateway) -> str:
    """``ra-`` and eight lowercase base32 characters from 40 random bits of the gateway."""
    raw = bytes.fromhex(gateway.token_hex(5))
    return "ra-" + base64.b32encode(raw).decode("ascii").lower()


def _ms(gateway: Gateway) -> int:
    return int(gateway.now().timestamp() * 1000)


def check_daily_limit(ctx: Context, conn: sqlite3.Connection) -> None:
    """``reserved(today) >= limits.retros_per_day`` is exit 20 ``daily_limit``: no row, no hook
    (Retro lifecycle, Daily limit). Called inside the step 13 ``BEGIN IMMEDIATE`` transaction,
    before the insert, so two processes cannot both take the last place: the write lock orders
    them, and the second one sees the first one's ``digesting`` row (live lease) as reserved."""
    limit = ctx.settings.limits.retros_per_day
    if retro.reserved(ctx, conn, retro.local_day(ctx)) >= limit:
        raise errors.CliError(
            "refused",
            "the daily retro limit is reached",
            details={"reason": "daily_limit", "limit": limit},
            hint="Raise limits.retros_per_day or try again tomorrow.",
        )


@dataclass(frozen=True)
class Reused:
    """Step 13 repeated step 10 and a stored ``done`` retro with the key now exists."""

    retro: dict[str, Any]


class Retro:
    """A retro row this process holds the lease of."""

    def __init__(
        self,
        ctx: Context,
        store: Store,
        held: lease.Lease,
        retro_id: str,
        key: str,
        again_of: str | None,
    ) -> None:
        self.ctx = ctx
        self.store = store
        self.lease = held
        self.id = retro_id
        self.key = key
        self.again_of = again_of
        self.digest_sha256: str | None = None
        self.csession_id: str | None = None
        self.state = "digesting"
        self.reason: str | None = None
        self.cost_usd: float | None = None
        self.actor: str | None = None  # overrides ``ctx.actor`` in the log (recovery: auto:recover)

    # -- writes ---------------------------------------------------------------------------

    def write(
        self,
        state: str,
        reason: str | None = None,
        *,
        columns: Mapping[str, Any] | None = None,
        also: Any = None,
        decision_ids: tuple[str, ...] = (),
        output_ignored: int = 0,
        findings: int | None = None,
        candidates: int | None = None,
    ) -> None:
        """Set ``state`` and ``reason`` (and ``columns``) in one compare-and-set on the holder's
        lease; ``also(conn)`` runs inside the same transaction after the guard (the
        ``retro_starts`` insert or refund, the ``retro_sessions`` insert). The log entry follows
        the commit."""
        now = _ms(self.ctx.gateway)
        values: dict[str, Any] = {"state": state, "reason": reason, **(columns or {})}
        if state in FINAL_STATES:
            values["finished_at"] = now
        names = ", ".join(f"{name} = ?" for name in values)
        with self.store.transaction() as conn:
            self.lease.guarded_update(conn, names, tuple(values.values()))
            if also is not None:
                also(conn)
        self.state, self.reason = state, reason
        if "digest_sha256" in values:
            self.digest_sha256 = values["digest_sha256"]
        if "csession_id" in values:
            self.csession_id = values["csession_id"]
        if "cost_usd" in values:
            self.cost_usd = values["cost_usd"]
        self.log(
            decision_ids=decision_ids,
            output_ignored=output_ignored,
            findings=findings,
            candidates=candidates,
        )

    def log(
        self,
        *,
        decision_ids: tuple[str, ...] = (),
        output_ignored: int = 0,
        findings: int | None = None,
        candidates: int | None = None,
    ) -> None:
        self.store.log.append(
            self.actor or self.ctx.actor or "auto:unknown",
            "retro_state",
            retro_id=self.id,
            state=self.state,
            reason=self.reason,
            key=self.key,
            digest_sha256=self.digest_sha256,
            again_of=self.again_of,
            csession_id=self.csession_id,
            decision_ids=list(decision_ids),
            hook_output_ignored=output_ignored,
            findings=findings,
            candidates=candidates,
        )

    def set_columns(self, columns: Mapping[str, Any]) -> None:
        """Write columns that are not a state change (the collected cost, turns, sandbox flag and
        outcome): one compare-and-set, no log entry."""
        names = ", ".join(f"{name} = ?" for name in columns)
        with self.store.transaction() as conn:
            self.lease.guarded_update(conn, names, tuple(columns.values()))
        if "cost_usd" in columns:
            self.cost_usd = columns["cost_usd"]

    def post_hooks(
        self,
        state: str,
        reason: str | None,
        *,
        findings: int = 0,
        candidates: int = 0,
        cancel: threading.Event | None = None,
    ) -> None:
        """``post-retro`` once for the final state (and ``post-attention`` ``retro_failed`` for
        ``failed``), best effort: a failing entry is a stderr warning. Holds no write lock."""
        gw, settings = self.ctx.gateway, self.ctx.settings.hooks
        payload = hooks.post_retro_payload(
            gw,
            retro_id=self.id,
            state=state,
            reason=reason,
            findings=findings,
            candidates=candidates,
            cost_usd=self.cost_usd,
        )
        for line in hooks.warning_text(
            hooks.run_hooks(gw, settings, payload, cancel=cancel), hooks.POST_RETRO
        ):
            self.ctx.note(f"session-audit: {line}")
        if state == "failed":
            csid = self.csession_id
            failure = hooks.retro_failed_payload(
                gw,
                retro_id=self.id,
                failure_reason=reason or "",
                csession_id=csid if is_csession_id(csid) else None,
            )
            for line in hooks.warning_text(
                hooks.run_hooks(gw, settings, failure, cancel=cancel), hooks.POST_ATTENTION
            ):
                self.ctx.note(f"session-audit: {line}")

    def release(self) -> None:
        """Stop the renewal and clear the lease (a compare-and-set: a stale holder clears
        nothing of a new holder's). Never raises for a lost lease."""
        try:
            self.lease.release()
        except errors.CliError:
            pass


# --- step 13 ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Inserted:
    retro: Retro


def scope_object(args: Any, prepared: retro.Prepared) -> dict[str, Any]:
    """The retro scope object of the dry-run plan and of the ``pre-retro`` payload
    (``defs.json`` ``scope``): the flags as given, sessions resolved to full ids."""
    return {
        "sessions": list(prepared.explicit_sessions),
        "labels": list(dict.fromkeys(args.label or [])),
        "project": args.project,
        "since": args.since,
        "until": args.until,
        "kind": args.kind,
        "outcome": args.outcome,
    }


def _flags(args: Any) -> dict[str, Any]:
    return {
        "labels": list(args.label or []),
        "project": args.project,
        "since": args.since,
        "until": args.until,
        "kind": args.kind,
        "outcome": args.outcome,
    }


def insert_retro(
    ctx: Context, store: Store, args: Any, prepared: retro.Prepared
) -> Inserted | Reused:
    """Order of checks step 13: in one ``BEGIN IMMEDIATE``, repeat step 10 (the key lookup and
    ``again_of`` included: the outcomes are the same as at step 10), check the daily limit (seam)
    and insert the retro row (state ``digesting``, the holder's lease, the key and the counts).

    Raises ``already_running`` (exit 20) when a live run of the key appeared meanwhile and returns
    ``Reused`` when a stored ``done`` retro with the key appeared (and no ``--again``)."""
    gateway = ctx.gateway
    holder = lease.identity(gateway)
    flags = _flags(args)
    filters = prepared.filters
    since = None if filters is None or filters.since is None else format_timestamp(filters.since)
    until = None if filters is None or filters.until is None else format_timestamp(filters.until)
    session_map = encode_session_map(
        [
            {
                "ref": h.ref,
                "session_id": h.session_id,
                "csession_id": prepared.csession_ids.get(h.session_id),
            }
            for h in prepared.hashes
        ]
    )
    counts = {
        "sessions": prepared.kept,
        "skipped": len(prepared.skipped),
        "over_cap": prepared.over_cap,
        "items": prepared.digest.items_kept,
        "findings": 0,
        "candidates": 0,
    }
    expires = _ms(gateway) + int(lease.LEASE_SECONDS * 1000)
    with store.transaction() as conn:
        state = retro.lookup_key(ctx, conn, prepared.key)
        if state.reused is not None and not args.again:
            return Reused(state.reused)
        if state.running is not None:
            raise errors.CliError(
                "refused",
                "a retro with the same key is running",
                details={"reason": "already_running", "retro_id": state.running},
            )
        check_daily_limit(ctx, conn)
        retro_id = _free_id(gateway, conn)
        again_of = state.previous if args.again else None
        conn.execute(
            "INSERT INTO retros (id, key, state, reason, scope, resolved_scope, skill, model, "
            "created_at, session_map, again_of, lease_pid, lease_start, lease_expires_at, "
            "counts, dropped, redacted, warnings) "
            "VALUES (?, ?, 'digesting', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                retro_id,
                prepared.key,
                scope_text(flags),
                encode_resolved_scope(flags, prepared.explicit_sessions, since, until),
                prepared.skill,
                prepared.model,
                _ms(gateway),
                session_map,
                again_of,
                holder.pid,
                holder.start,
                expires,
                json.dumps(counts, separators=(",", ":")),
                json.dumps({"findings": 0, "candidates": 0}, separators=(",", ":")),
                json.dumps(_row_warnings(ctx, prepared), separators=(",", ":")),
            ),
        )
    held = lease.Lease(
        store,
        lease.RetroTarget(retro_id),
        holder,
        expires,
        lease.LEASE_SECONDS,
        lease.RENEW_SECONDS,
    )
    row = Retro(ctx, store, held, retro_id, prepared.key, again_of)
    try:
        row.log()
    except BaseException:
        row.release()  # the row is not stranded behind a lease nobody renews
        raise
    return Inserted(row)


def _row_warnings(ctx: Context, prepared: retro.Prepared) -> list[str]:
    """The closed tokens a row stores: the preparation's, plus ``transcripts_kept`` when
    ``retro.remove_run_transcripts`` is false (the retro's own transcripts stay)."""
    found = list(prepared.warnings)
    if not ctx.settings.retro.remove_run_transcripts and "transcripts_kept" not in found:
        found.append("transcripts_kept")
    return found


def _free_id(gateway: Gateway, conn: sqlite3.Connection) -> str:
    for _ in range(_RETRO_ID_TRIES):
        candidate = new_retro_id(gateway)
        if conn.execute("SELECT 1 FROM retros WHERE id = ?", (candidate,)).fetchone() is None:
            return candidate
    raise errors.CliError("internal", "no free retro id")
