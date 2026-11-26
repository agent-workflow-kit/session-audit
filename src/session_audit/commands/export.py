"""``export``: print store rows as JSON lines (interface, Commands, ``export``).

``export sessions`` and ``export usage`` stream JSON lines (a line is written as soon as it is
produced, oldest first). TODO(#31): ``export retros``.
"""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Iterator

from .. import aggregate
from .. import args as a
from ..command import Command, Context
from ..grammar import parse_when
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
    if args.kind == "retros":
        raise a.not_implemented()  # TODO(#31): export retros
    # Not build_filters: the positional ``kind`` here is the export type, not ``--kind``.
    since = None
    if args.since is not None:
        since = parse_when(
            args.since, field="--since", now=ctx.gateway.now(), time_zone=ctx.settings.zone()
        )
    filters = Filters(since=since, include_self_improve=args.include_self_improve)
    if args.kind == "sessions":
        stream = read_stream(ctx, lambda conn: aggregate.session_lines(conn, ctx, filters))
    else:
        stream = read_stream(ctx, lambda conn: usage_stream(conn, ctx, filters))
    with contextlib.closing(stream):
        for line in stream:
            ctx.out.write(dumps_json(line) + "\n")
    return 0


def usage_stream(conn, ctx: Context, filters: Filters) -> Iterator[dict[str, object]]:  # noqa: ANN001
    zone = ctx.settings.zone()
    since_day, min_slot = derive_day(filters.since, zone)
    return aggregate.usage_lines(conn, ctx, filters, zone, since_day, min_slot)


COMMANDS = (Command(("export",), "print store rows as JSON lines", add_arguments, check, run),)
