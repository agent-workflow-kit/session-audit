"""``retro list``: list stored retros (interface, Commands, ``retro list``).

Reads the store only (the read-only path of ``sessionquery.read_store``), never checks liveness
and prints no model text: a row is built from columns (``retroview.list_row``).
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_session_prefix, parse_when
from ..retention import epoch_ms
from ..retroquery import ListFilters, list_rows
from ..sessionquery import one_line, put, read_store, resolve_session


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state", action="append", choices=a.RETRO_STATES, help="a retro state (repeatable)"
    )
    a.add_time_filters(parser, until=False)
    parser.add_argument("--key", metavar="K", help="a retro key")
    parser.add_argument("--skill", metavar="S", help="a plugin skill, plugin:skill")
    parser.add_argument("--session", metavar="ID", help="a session id or prefix")
    a.add_limit(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.since is not None:
        a.check_when(ctx, args.since, "--since")
    if args.key is not None:
        a.check_retro_key(args.key)
    if args.skill is not None:
        a.check_skill(args.skill)
    if args.session is not None:
        try:
            args.session = parse_session_prefix(args.session, field="--session")
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
    a.check_limit(args)


def _session_id(conn: sqlite3.Connection | None, prefix: str) -> str:
    """The full id a ``--session`` value names: a full id is taken as it is (the retro keeps the
    ids of sessions that were pruned since), a prefix is resolved against the stored sessions."""
    if len(prefix) == 36:
        return prefix
    try:
        return resolve_session(conn, prefix)
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def run(ctx: Context, args: argparse.Namespace) -> int:
    since_ms = None
    if args.since is not None:
        since = parse_when(
            args.since, field="--since", now=ctx.gateway.now(), time_zone=ctx.settings.zone()
        )
        since_ms = epoch_ms(since)

    unfit = [0]

    def read(conn: sqlite3.Connection | None) -> tuple[list[dict[str, Any]], bool]:
        unfit[0] = 0  # a retried read starts over
        session = None if args.session is None else _session_id(conn, args.session)
        if conn is None:
            return [], False
        filters = ListFilters(
            states=tuple(dict.fromkeys(args.state or ())),
            since_ms=since_ms,
            key=args.key,
            skill=args.skill,
            session_id=session,
        )
        return list_rows(conn, filters, args.limit, unfit)

    rows, truncated = read_store(ctx, read)
    if unfit[0]:
        ctx.note(f"session-audit: {unfit[0]} stored retro(s) did not fit and were not listed")
    if ctx.json:
        ctx.emit_json({"retros": rows, "truncated": truncated})
        return 0
    if ctx.quiet:
        return 0
    for row in rows:
        state = row["state"] if row["reason"] is None else f"{row['state']}/{row['reason']}"
        extra = " superseded" if row["superseded"] else ""
        put(
            ctx,
            f"{one_line(row['id'])} {one_line(state)} {one_line(row['created_at'])} "
            f"findings={row['findings']} candidates={row['candidates']} "
            f"key={one_line(row['key'][:12])}{extra}",
        )
    if not rows:
        ctx.say("no retros")
    if truncated:
        ctx.note("session-audit: more retros match; raise --limit")
    return 0


COMMANDS = (Command(("retro", "list"), "list stored retros", add_arguments, check, run),)
