"""``prune``: apply the retention rules (interface, Commands, ``prune`` and Retention).

The flow of a real run (``retention`` holds the rules):

1. The ingest lease is taken as ingest takes it. When it is held, session retention (sessions and
   the old ``pruned_sessions`` rows) is skipped with the warning ``ingest_busy`` and the rest runs.
   The lease is released as soon as session retention is done.
2. ``harness_runs``, final retros with their artifacts and digests, expired digests,
   ``retro_starts`` and ``retro_sessions``, each in its own transaction.
3. Old ``log.jsonl`` entries (rewritten under ``log.lock``), then this run's ``prune`` entry.
4. ``Store.checkpoint()``: a checkpoint that cannot finish is a retryable exit 12, never
   swallowed. It also runs when a step failed, so the bytes deleted so far leave the disk.

``--dry-run`` takes no lease and writes nothing: it plans the same rules over a read-only view of
the store (none when there is none).

Housekeeping (``retrorecovery``: recovery of retros left behind, cleanup retries, old temporary
files) runs after session retention and before the retro rules, so a recovered retro's cleanup is
settled before ``retro_sessions`` rows are judged; ``recoveries`` lists the retro ids recovered
and the warning ``recovery_deferred`` says something had to wait (no claude-session, or the 120 s
bound). The expired digests are the retention rule's. ``--dry-run`` only lists the retros it would
recover.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
from typing import Any

from .. import args as a
from .. import errors, retrorecovery
from ..command import Command, Context
from ..lease import IngestTarget, acquire
from ..retention import (
    PRUNE_TABLES,
    Report,
    Settings,
    TranscriptRoots,
    epoch_ms,
    harness_runs,
    ingest_is_running,
    log_entries,
    plan_sessions,
    retro_sessions,
    retro_starts,
    retros,
    session_retention,
)
from ..store import Store, open_read_only, state_dir


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_dry_run(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    return None


def _settings(ctx: Context) -> Settings:
    s = ctx.settings
    return Settings(
        s.retention.sessions,
        s.retention.retros,
        s.retention.digests,
        s.retention.log,
        s.transcript_roots,
        s.zone(),
    )


def build_report(report: Report, *, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "tables": {name: report.tables[name] for name in PRUNE_TABLES},
        "recoveries": list(report.recoveries),
        "warnings": list(report.warnings),
    }


def print_report(ctx: Context, report: Report, document: dict[str, Any]) -> None:
    for note in report.notes:
        ctx.note(f"prune: {note}")
    if ctx.json:
        ctx.emit_json(document)
        return
    if ctx.quiet:
        return
    ctx.say("prune (dry run): would delete" if document["dry_run"] else "prune: deleted")
    ctx.say(" ".join(f"{k}={v}" for k, v in document["tables"].items()))
    for text in document["warnings"]:
        ctx.say(f"warning: {text}")


def run(ctx: Context, args: argparse.Namespace) -> int:
    report = Report()
    if ctx.dry_run:
        _plan(ctx, report)
        print_report(ctx, report, build_report(report, dry_run=True))
        return 0
    store = ctx.store()
    try:
        _apply(ctx, store, report)
    except BaseException:
        # The deleted bytes should leave the disk even when a step failed; the failure wins.
        with contextlib.suppress(errors.CliError, sqlite3.Error):
            store.checkpoint()
        raise
    store.checkpoint()
    print_report(ctx, report, build_report(report, dry_run=False))
    return 0


def _now(ctx: Context) -> Any:
    return ctx.gateway.now()


def _plan(ctx: Context, report: Report) -> None:
    from ..config import home_dir  # lazy: config imports args, which imports command

    directory = ctx.state_directory or state_dir(os.environ, home_dir(os.environ))
    conn = open_read_only(ctx.gateway, directory)
    if conn is None:
        return  # no store, nothing to prune, and none is created
    settings = _settings(ctx)
    now = _now(ctx)
    now_ms = epoch_ms(now)
    roots = TranscriptRoots(ctx.gateway, settings.roots)
    from ..log import LogWriter

    try:
        if ingest_is_running(conn, ctx.gateway):
            report.warnings.append("ingest_busy")
        else:
            plan_sessions(conn, now_ms, settings, report)
        report.recoveries.extend(retrorecovery.planned(conn, ctx.gateway))
        harness_runs(conn, None, now, settings, report)
        retros(conn, None, ctx.gateway, directory, now_ms, settings, report)
        retro_starts(conn, now, settings, report, apply=False)
        retro_sessions(conn, None, roots, now_ms, report)
        log_entries(LogWriter(ctx.gateway, directory), now, settings, report, apply=False)
    finally:
        roots.close()
        conn.close()


def _apply(ctx: Context, store: Store, report: Report) -> None:
    settings = _settings(ctx)
    gateway = ctx.gateway

    def now_ms() -> int:
        return epoch_ms(gateway.now())

    lease = acquire(store, IngestTarget())
    if lease is None:
        report.warnings.append("ingest_busy")
    else:
        try:
            with lease:
                session_retention(store, lease, now_ms, settings, report)
        finally:
            with contextlib.suppress(errors.CliError):
                lease.release()
    housekeeping = retrorecovery.run_housekeeping(ctx, digests=False)
    report.recoveries.extend(housekeeping.recovered)
    for warning in housekeeping.warnings:
        if warning not in report.warnings:
            report.warnings.append(warning)
    now = gateway.now()
    harness_runs(store.conn, store.transaction, now, settings, report)
    retros(store.conn, store.transaction, gateway, store.directory, now_ms(), settings, report)
    roots = TranscriptRoots(gateway, settings.roots)
    try:
        with store.transaction() as conn:
            retro_starts(conn, now, settings, report, apply=True)
        retro_sessions(store.conn, store.transaction, roots, now_ms(), report)
    finally:
        roots.close()
    log_entries(store.log, now, settings, report, apply=True)
    assert ctx.actor is not None
    store.log.append(
        ctx.actor,
        "prune",
        tables={name: report.tables[name] for name in PRUNE_TABLES},
        recovered=len(report.recoveries),
    )


COMMANDS = (
    Command(
        ("prune",),
        "apply the retention rules",
        add_arguments,
        check,
        run,
        accepts_actor=True,
        refuses_unsafe_config=True,
    ),
)
