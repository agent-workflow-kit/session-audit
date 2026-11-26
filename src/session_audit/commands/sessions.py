"""``sessions``: list stored sessions (interface, Commands, ``sessions``).

Flow: a read-only view of the store (none yet: an empty list), the filters as one predicate
(``sessionquery.Filters``), the sessions derived in batches and kept in a bounded heap of the
``limit + 1`` best by the sort key (so memory never grows with the corpus), the Session objects
with their harness snapshot, and the store freshness.

TODO(#19): snapshots are read from ``harness_runs``; ``ingest`` fills it once #19 lands.
"""

from __future__ import annotations

import argparse
import heapq
import sqlite3
from collections.abc import Callable
from typing import Any

from .. import args as a
from .. import derive
from ..command import Command, Context
from ..derive import SessionValues
from ..sessionquery import (
    Snapshot,
    build_filters,
    freshness,
    one_line,
    put,
    read_store,
    selected,
    session_json,
)

ENTRYPOINTS = ("interactive", "headless")
SORTS = ("start", "tokens", "cost", "active")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_time_filters(parser)
    a.add_session_filters(parser)
    parser.add_argument("--entrypoint", choices=ENTRYPOINTS, help="interactive or headless")
    parser.add_argument("--model", metavar="M", help="an exact model name")
    parser.add_argument("--sort", choices=SORTS, default="start", help="the order of the rows")
    a.add_owner_filter(parser)
    a.add_limit(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    a.check_filters(ctx, args)
    if args.model is not None:
        a.check_model(args.model, "--model")
    a.check_limit(args)


Row = tuple[SessionValues, Snapshot | None]


def sort_key(sort: str) -> Callable[[Row], tuple[Any, ...]]:
    """Ascending keys that give the documented order: descending by the value, nulls last,
    ties by session id ascending."""
    if sort == "tokens":
        return lambda r: (-(r[0].tokens.total + r[0].subagent_tokens), r[0].session_id)
    if sort == "cost":
        return lambda r: (-derive.usd(r[0].cost.estimated), r[0].session_id)
    if sort == "active":
        return lambda r: (r[0].active_ms is None, -(r[0].active_ms or 0), r[0].session_id)
    return lambda r: (r[0].started_ms is None, -(r[0].started_ms or 0), r[0].session_id)


def run(ctx: Context, args: argparse.Namespace) -> int:
    filters = build_filters(ctx, args)

    def read(conn: sqlite3.Connection | None) -> tuple[list[Row], dict[str, str | None]]:
        found: list[Row] = []
        if conn is not None:
            found = heapq.nsmallest(
                args.limit + 1, selected(conn, ctx, filters), key=sort_key(args.sort)
            )
        return found, freshness(conn)

    rows, store = read_store(ctx, read)
    truncated = len(rows) > args.limit
    rows = rows[: args.limit]
    if ctx.json:
        ctx.emit_json(
            {
                "sessions": [session_json(v, s) for v, s in rows],
                "truncated": truncated,
                "store": store,
            }
        )
        return 0
    print_table(ctx, rows, truncated, store)
    return 0


def print_table(
    ctx: Context, rows: list[Row], truncated: bool, store: dict[str, str | None]
) -> None:
    """One line per session; every stored text goes through ``one_line`` (the sanitizer, once)
    and the line is printed with ``put``."""
    for values, _snapshot in rows:
        started = values.to_json()["started_at"] or "-"
        tokens = values.tokens.total + values.subagent_tokens
        cost = derive.usd_json(values.cost.estimated)
        project = one_line(values.project or "-")
        put(
            ctx,
            f"{one_line(values.session_id)}  {started}  {tokens} tokens  ${cost:.2f}  {project}",
        )
    noun = "session" if len(rows) == 1 else "sessions"
    more = " (more matched; raise --limit)" if truncated else ""
    put(ctx, f"{len(rows)} {noun}{more}")
    put(ctx, f"last ingest: {store['last_ingest_at'] or 'never'}")


COMMANDS = (Command(("sessions",), "list stored sessions", add_arguments, check, run),)
