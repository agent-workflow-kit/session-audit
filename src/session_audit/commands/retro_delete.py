"""``retro delete``: delete retros (interface, Commands, ``retro delete``).

The flow, all or nothing:

1. Every id or prefix is resolved against a read-only view of the store; an unknown one is exit 10
   (ambiguous: 21) before anything changes, and no store is created.
2. The confirmation: ``--yes``, or a terminal (``Gateway.ask``).
3. One ``BEGIN IMMEDIATE`` transaction: every retro is checked again (gone meanwhile: exit 10; a
   live run: exit 20 ``in_use``), the digest directory and the retro's local files go through the
   shared removal rules (``retrofiles.remove_digest`` and ``delete_local``: the id is checked
   against the id grammar before any path is built, nothing is followed through a symlink) BEFORE
   the rows. A planning pass over ALL ids (``apply=False``) runs first: when any removal would not
   be complete, nothing is removed and every retro is kept (exit 12). Only then are the files
   removed (a failure after that point, a race or an I/O error, keeps the rows and may leave
   files already removed), and the artifact and retro rows go in one ``DELETE ... IN`` per page.
4. One ``retro_delete`` log entry per retro after the commit (every entry is attempted; a failed
   one is exit 12 naming the ids that were deleted), then the ``TRUNCATE`` checkpoint
   (a busy one is a retryable exit 12; it is attempted on every path, a failure included).

``retro_starts`` and ``retro_sessions`` rows are never touched: a pending cleanup of a deleted
retro goes on in the housekeeping of ``retro run`` and ``prune`` (``retrorecovery``).
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sqlite3
from typing import Any

from .. import args as a
from .. import errors, retrofiles
from ..command import Command, Context
from ..grammar import GrammarError, parse_retro_prefix
from ..lease import LeaseState, is_live
from ..retroquery import resolve_retros
from ..sessionquery import put, read_store
from ..store import Store

MAX_IDS = 1000
PAGE = 200
NON_FINAL = ("digesting", "running", "validating")
_KEY = re.compile(r"[0-9a-f]{64}", re.ASCII)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("retro_ids", nargs="+", metavar="RETRO_ID", help="a retro id or a prefix")
    a.add_yes(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    a.check_list_max(args.retro_ids, "RETRO_ID", MAX_IDS)
    try:
        args.retro_ids = [parse_retro_prefix(r, field="RETRO_ID") for r in args.retro_ids]
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    a.require_confirmation(ctx)


def _resolve(ctx: Context, prefixes: list[str]) -> list[str]:
    def read(conn: sqlite3.Connection | None) -> list[str]:
        try:
            return resolve_retros(conn, prefixes)
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None

    return read_store(ctx, read)


def _confirm(ctx: Context, ids: list[str]) -> None:
    if ctx.yes:
        return
    shown = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
    answer = ctx.gateway.ask(f"session-audit: delete {len(ids)} retro(s): {shown}? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise errors.usage(
            "the deletion was not confirmed",
            reason="confirmation_required",
            hint="Answer y, or pass --yes.",
        )


def _removal_failed() -> errors.CliError:
    return errors.CliError(
        "failed",
        "a digest or run file could not be removed completely; the retros are kept",
        details={"reason": "store_unwritable", "retryable": True},
    )


def _delete(ctx: Context, store: Store, ids: list[str]) -> list[str | None]:
    """The transaction: the keys (None when a stored key is not a hash) in the order of ``ids``."""
    gateway = ctx.gateway
    keys: list[str | None] = []
    with store.transaction() as conn:
        for retro_id in ids:
            row = conn.execute(
                "SELECT key, state, lease_pid, lease_start, lease_expires_at FROM retros "
                "WHERE id = ?",
                (retro_id,),
            ).fetchone()
            if row is None:
                raise errors.CliError(
                    "not_found", "no such retro", details={"id": retro_id}
                )  # deleted by someone else since the lookup: nothing is deleted
            key, state, pid, start, expires = row
            if state in NON_FINAL and is_live(gateway, LeaseState(pid, start, expires)):
                raise errors.CliError(
                    "refused",
                    "the retro's run is live",
                    details={"reason": "in_use", "retro_id": retro_id},
                )
            keys.append(key if isinstance(key, str) and _KEY.fullmatch(key) else None)
        for retro_id in ids:  # plan everything first: nothing is removed unless all can be
            if not retrofiles.remove_digest(gateway, store.directory, retro_id, apply=False):
                raise _removal_failed()
            if not retrofiles.delete_local(gateway, store.directory, retro_id, apply=False):
                raise _removal_failed()
        for retro_id in ids:
            if not retrofiles.remove_digest(gateway, store.directory, retro_id):
                raise _removal_failed()
            if not retrofiles.delete_local(gateway, store.directory, retro_id):
                raise _removal_failed()
        for start in range(0, len(ids), PAGE):
            page = ids[start : start + PAGE]
            marks = ",".join("?" * len(page))
            conn.execute(f"DELETE FROM retro_artifacts WHERE retro_id IN ({marks})", page)
            conn.execute(f"DELETE FROM retros WHERE id IN ({marks})", page)
    return keys


def _log(ctx: Context, store: Store, ids: list[str], keys: list[str | None]) -> None:
    """One log entry per deleted retro. The rows are gone: every entry is attempted, and a failed
    one is reported after the others, naming the ids that were deleted."""
    assert ctx.actor is not None
    failed = False
    for retro_id, key in zip(ids, keys, strict=True):
        try:
            store.log.append(ctx.actor, "retro_delete", retro_id=retro_id, key=key)
        except errors.CliError:
            failed = True
    if failed:
        raise errors.CliError(
            "failed",
            "the retros were deleted but some log entries could not be written",
            details={"reason": "store_unwritable", "retryable": False, "deleted": ids},
        )


def run(ctx: Context, args: argparse.Namespace) -> int:
    ids = _resolve(ctx, args.retro_ids)
    _confirm(ctx, ids)
    store = ctx.store()
    try:
        keys = _delete(ctx, store, ids)
        _log(ctx, store, ids, keys)
    except BaseException:
        with contextlib.suppress(errors.CliError, sqlite3.Error):
            store.checkpoint()  # the bytes deleted so far leave the disk; the failure wins
        raise
    store.checkpoint()
    document: dict[str, Any] = {"deleted": ids}
    if ctx.json:
        ctx.emit_json(document)
    elif not ctx.quiet:
        for retro_id in ids:
            put(ctx, f"deleted {retro_id}")
    return 0


COMMANDS = (
    Command(
        ("retro", "delete"),
        "delete stored retros",
        add_arguments,
        check,
        run,
        accepts_actor=True,
        refuses_unsafe_config=True,
    ),
)
