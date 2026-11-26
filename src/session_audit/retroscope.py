"""Scope resolution and the refresh of ``retro run`` (Order of checks steps 6 to 8; issue #23).

* ``resolve_scope``: the explicit ``--session`` prefixes become full stored ids (exit 10 unknown,
  21 ambiguous; a FULL id the store does not know is first looked up on disk under the roots,
  through Discovery's safe paths only, and ingested), then the candidates are the sessions the scope
  matches by stored ``ended_at`` descending, null last, then id (``digest.candidates``). Nothing
  matched is ``empty_scope``; an explicit list over ``retro.max_sessions`` is ``scope_too_large``
  (counted over the sessions matched, before any skip).
* ``Refresher``: an ingest run over the files of some sessions only (``ingest.run_files``), under
  the ingest lease. The lease is waited for at most ``WAIT_SECONDS`` once for the whole retro (one
  shared budget, polled with ``Gateway.sleep``); ``ingest.budget`` is shared by all refreshes of
  one retro. A refresh that did not happen or did not finish is reported by ``busy`` (the warning
  ``ingest_busy``): the digest then reads the stored offsets.
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from . import errors
from .command import Context
from .digest import DigestScope, candidates
from .grammar import (
    GrammarError,
    PrefixNotFoundError,
)
from .incremental import run_deadline
from .lease import IngestTarget, Lease, acquire
from .reader import discover
from .sessionquery import Filters, build_filters, read_store, resolve_session

__all__ = ["WAIT_SECONDS", "Refresher", "Resolved", "refused", "resolve_scope", "scope_filters"]

WAIT_SECONDS = 60.0  # the ingest lease is waited for this long, once per retro
POLL_SECONDS = 1.0
FULL_ID_LENGTH = 36


def scope_filters(ctx: Context, args: argparse.Namespace) -> Filters:
    """The Session filters of ``retro run``: the scope flags and ``--include-self-improve``. The
    retro's own ``--model`` is not a session filter (``retro run`` has no ``--model`` or
    ``--entrypoint`` filter), so it is never handed to ``build_filters``."""
    view = argparse.Namespace(
        since=args.since,
        until=args.until,
        project=args.project,
        label=args.label,
        kind=args.kind,
        outcome=args.outcome,
        include_self_improve=bool(getattr(args, "include_self_improve", False)),
    )
    return build_filters(ctx, view)


class Refresher:
    """Refreshes sessions' files under the ingest lease (see the module docstring)."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.waited = 0.0
        self.deadline: float | None = None
        self.busy = False  # a refresh did not run or did not finish: warning ingest_busy

    def _acquire(self) -> Lease | None:
        gateway = self.ctx.gateway
        store = self.ctx.store()
        while True:
            lease = acquire(store, IngestTarget())
            if lease is not None:
                return lease
            left = WAIT_SECONDS - self.waited
            if left <= 0:
                return None  # the one wait of this retro is spent
            step = min(POLL_SECONDS, left)
            gateway.sleep(step)
            self.waited += step

    def refresh(self, session_ids: Sequence[str]) -> bool:
        """Ingest the files of ``session_ids``. True when the run finished within its budget."""
        wanted = frozenset(session_ids)
        if not wanted:
            return True
        if self.ctx.dry_run:
            return self._refresh_in_memory(wanted)
        lease = self._acquire()
        if lease is None:
            self.busy = True
            return False
        from .commands import ingest  # lazy: the commands package imports this module's users

        if self.deadline is None:
            self.deadline = run_deadline(self.ctx.gateway, self.ctx.settings.ingest.budget)
        try:
            with discover(self.ctx, ()) as discovery, lease:
                report = ingest.run_files(
                    self.ctx, self.ctx.store(), lease, discovery, self.deadline, wanted
                )
        finally:
            with contextlib.suppress(errors.CliError):
                lease.release()
        if not report["complete"]:
            self.busy = True
            return False
        return True

    def _refresh_in_memory(self, wanted: frozenset[str]) -> bool:
        """A dry run: the files are read to their current end into the private copy of the store
        (``Context.memory``), so the digest and the key are those of a real run over the same
        bytes. No lease (nothing is shared), nothing is written."""
        from .commands import ingest  # lazy, as in ``refresh``

        memory = self.ctx.memory
        if memory is None:
            raise RuntimeError("a dry run reads through the in-memory copy of the store")
        if self.deadline is None:
            self.deadline = run_deadline(self.ctx.gateway, self.ctx.settings.ingest.budget)
        with discover(self.ctx, ()) as discovery:
            complete = ingest.run_files_memory(self.ctx, memory, discovery, self.deadline, wanted)
        if not complete:
            self.busy = True
        return complete

    def on_disk(self, session_ids: Sequence[str]) -> frozenset[str]:
        """Which of ``session_ids`` have a transcript file under the roots (Discovery's safe
        paths only; nothing is read or stored)."""
        wanted = frozenset(session_ids)
        with discover(self.ctx, ()) as discovery:
            return frozenset(c.session_id for c in discovery.candidates() if c.session_id in wanted)


@dataclass(frozen=True)
class Resolved:
    """What step 6 and 7 produced: the explicit session ids (empty for a filter scope), the
    filters and the candidates in order."""

    explicit: tuple[str, ...]
    filters: Filters
    order: tuple[str, ...]


def _lookup(ctx: Context, prefixes: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(found full ids, unknown full ids)``. A prefix shorter than a full id that is unknown is
    exit 10, an ambiguous one exit 21, at once (nothing is ingested for a bad request)."""
    found: list[str] = []
    unknown: list[str] = []

    def work(conn: sqlite3.Connection | None) -> None:
        for prefix in prefixes:
            try:
                found.append(resolve_session(conn, prefix))
            except PrefixNotFoundError:
                if len(prefix) != FULL_ID_LENGTH:
                    raise
                unknown.append(prefix)

    try:
        read_store(ctx, work)
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    return found, unknown


def resolve_scope(
    ctx: Context, args: argparse.Namespace, refresher: Refresher, *, scope_max: int
) -> Resolved:
    filters = scope_filters(ctx, args)
    explicit: list[str] = []
    if args.session:
        prefixes = list(dict.fromkeys(args.session))
        found, unknown = _lookup(ctx, prefixes)
        if unknown:
            # A dry run finds the file on disk and reads it into its in-memory copy, as a real
            # run ingests it (``Refresher.refresh``); the second lookup reads that copy.
            ingested = refresher.refresh(unknown)
            again, still = _lookup(ctx, unknown)
            if still:
                if not ingested and refresher.on_disk(still):
                    # The file is there but the busy lease or the budget kept it out: a retry can
                    # work, so it is not exit 10.
                    raise errors.CliError(
                        "failed",
                        "the session is on disk but could not be ingested now",
                        details={"reason": "ingest_busy", "retryable": True, "id": still[0]},
                        hint="Another ingest holds the lease or the budget ended; retry.",
                    )
                raise errors.CliError("not_found", "session not found", details={"id": still[0]})
            found.extend(again)
        explicit = list(dict.fromkeys(found))
    scope = DigestScope(
        session_ids=tuple(explicit),
        filters=filters,
        max_sessions=scope_max,
        quiet_after=ctx.settings.retro.quiet_after,
    )
    matched = read_store(ctx, lambda conn: [] if conn is None else candidates(conn, ctx, scope))
    if not matched:
        raise refused("empty_scope")
    if explicit and len(matched) > scope_max:
        raise errors.CliError(
            "refused",
            "the explicit session list is longer than retro.max_sessions",
            details={"reason": "scope_too_large", "matched": len(matched), "limit": scope_max},
            hint="Name fewer sessions or raise retro.max_sessions.",
        )
    return Resolved(tuple(explicit), filters, tuple(sid for _, sid in matched))


def refused(reason: str) -> errors.CliError:
    """``empty_scope`` or ``live_sessions_only`` (exit 20)."""
    messages = {
        "empty_scope": "the scope matches no session",
        "live_sessions_only": "every matched session changed recently",
    }
    return errors.CliError("refused", messages[reason], details={"reason": reason})
