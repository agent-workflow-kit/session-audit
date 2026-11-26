"""``export``: print store rows as JSON lines (interface, Commands, ``export``).

``export sessions`` and ``export usage`` stream JSON lines (a line is written as soon as it is
produced, oldest first). ``export retros`` prints the full Retro object (with ``superseded``) of
every stored retro; ``--include-self-improve`` is accepted and has no meaning there.
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
from collections.abc import Iterator

from .. import aggregate, errors
from .. import args as a
from ..command import Command, Context, stderr_line
from ..grammar import parse_when
from ..retention import epoch_ms
from ..retroquery import export_lines
from ..sessionquery import Filters, derive_day, read_stream
from ..textsafe import dumps_json

KINDS = ("sessions", "usage", "retros")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "kind", choices=KINDS, metavar="{sessions|usage|retros}", help="the rows to print"
    )
    a.add_time_filters(parser, until=False)
    a.add_owner_filter(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.since is not None:
        a.check_when(ctx, args.since, "--since")


def run(ctx: Context, args: argparse.Namespace) -> int:
    # Not build_filters: the positional ``kind`` here is the export type, not ``--kind``.
    since = None
    if args.since is not None:
        since = parse_when(
            args.since, field="--since", now=ctx.gateway.now(), time_zone=ctx.settings.zone()
        )
    filters = Filters(since=since, include_self_improve=args.include_self_improve)
    unfit = [0]
    if args.kind == "retros":
        since_ms = None if since is None else epoch_ms(since)
        stream = read_stream(ctx, lambda conn: retro_stream(conn, since_ms, unfit))
    elif args.kind == "sessions":
        stream = read_stream(ctx, lambda conn: aggregate.session_lines(conn, ctx, filters))
    else:
        stream = read_stream(ctx, lambda conn: usage_stream(conn, ctx, filters))
    produced = False
    try:
        with contextlib.closing(stream):
            for line in stream:
                ctx.out.write(dumps_json(line) + "\n")
                produced = True
    except errors.CliError as error:
        if not produced:
            raise  # nothing was written: the normal error path (envelope under --json)
        # The stream is truncated: stdout stays pure data lines, the message goes to stderr only.
        for text in errors.stderr_lines(error):
            stderr_line(text)
        return error.exit_code
    if unfit[0]:
        ctx.note(f"session-audit: {unfit[0]} stored retro(s) did not fit and were not exported")
    return 0


def retro_stream(
    conn: sqlite3.Connection | None, since_ms: int | None, unfit: list[int]
) -> Iterator[dict[str, object]]:
    if conn is None:
        return iter(())
    return export_lines(conn, since_ms, unfit)


def usage_stream(conn, ctx: Context, filters: Filters) -> Iterator[dict[str, object]]:  # noqa: ANN001
    zone = ctx.settings.zone()
    since_day, min_slot = derive_day(filters.since, zone)
    return aggregate.usage_lines(conn, ctx, filters, zone, since_day, min_slot)


COMMANDS = (Command(("export",), "print store rows as JSON lines", add_arguments, check, run),)
