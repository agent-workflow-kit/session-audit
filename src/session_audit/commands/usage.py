"""``usage``: deterministic analytics over counted events (interface, Commands, ``usage``).

Flow: the Session filters (without time bounds: ``--since`` and ``--until`` narrow events here)
select the sessions, ``aggregate.usage`` folds their counted events in chunks of session ids.
"""

from __future__ import annotations

import argparse
import dataclasses
import sqlite3

from .. import aggregate, errors
from .. import args as a
from ..command import Command, Context
from ..grammar import GrammarError, parse_label_key, parse_time_zone, parse_when
from ..sessionquery import build_filters, freshness, one_line, put, read_store

DIMENSIONS = ("day", "week", "month", "project", "model", "kind", "entrypoint", "tool", "session")
MAX_DIMENSIONS = 2


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--by", metavar="DIM[,DIM]", required=True, help="one or two dimensions")
    a.add_time_filters(parser)
    a.add_session_filters(parser)
    parser.add_argument(
        "--entrypoint", choices=("interactive", "headless"), help="interactive or headless"
    )
    parser.add_argument("--model", metavar="M", help="an exact model name")
    parser.add_argument("--tz", metavar="ZONE", help="a time zone for day, week and month")
    a.add_owner_filter(parser)
    a.add_limit(parser)


def parse_dimensions(value: str) -> list[str]:
    """The ``--by`` list: one or two distinct dimensions, ``label=<key>`` allowed."""
    hint = "Use one or two of: " + ", ".join(DIMENSIONS) + ", label=<key>."
    if len(value) > 512:
        raise errors.usage("invalid dimensions", field="--by", hint=hint)
    parts = value.split(",")
    if not 1 <= len(parts) <= MAX_DIMENSIONS:
        raise errors.usage("invalid dimensions", field="--by", hint=hint)
    for part in parts:
        if part in DIMENSIONS:
            continue
        name, sep, key = part.partition("=")
        if name == "label" and sep:
            try:
                parse_label_key(key, field="--by")
            except GrammarError as exc:
                raise errors.from_grammar(exc) from None
            continue
        raise errors.usage("invalid dimensions", field="--by", hint=hint)
    if len(set(parts)) != len(parts):
        raise errors.usage("invalid dimensions", field="--by", hint="Name each dimension once.")
    return parts


def check(ctx: Context, args: argparse.Namespace) -> None:
    args.by = parse_dimensions(args.by)
    a.check_filters(ctx, args)
    if args.model is not None:
        a.check_model(args.model, "--model")
    if args.tz is not None:
        try:
            parse_time_zone(args.tz, field="--tz")
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
    a.check_limit(args)


def run(ctx: Context, args: argparse.Namespace) -> int:
    filters = build_filters(ctx, args)
    zone = ctx.settings.zone()
    if args.tz is not None:
        try:
            zone = parse_time_zone(args.tz, field="--tz")
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
    now = ctx.gateway.now()

    def when(name: str):  # noqa: ANN202
        value = getattr(args, name)
        if value is None:
            return None
        return parse_when(value, field=f"--{name}", now=now, time_zone=ctx.settings.zone())

    since_ms, until_ms = aggregate.window_bounds(when("since"), when("until"))
    query = aggregate.UsageQuery(tuple(args.by), zone, since_ms, until_ms, args.limit)
    session_filters = dataclasses.replace(filters, since=None, until=None)

    def read(
        conn: sqlite3.Connection | None,
    ) -> tuple[aggregate.UsageResult, dict[str, str | None]]:
        return aggregate.usage(conn, ctx, session_filters, query), freshness(conn)

    result, store = read_store(ctx, read)
    if ctx.json:
        ctx.emit_json(
            {
                "by": args.by,
                "rows": result.rows,
                "totals": result.totals,
                "truncated": result.truncated,
                "store": store,
            }
        )
        return 0
    print_table(ctx, args.by, result, store)
    return 0


def _money(cost: object) -> str:
    if not isinstance(cost, dict):
        return "-"
    reported = cost["reported_usd"]
    shown = "-" if reported is None else f"${reported:.2f}"
    return f"est ${cost['estimated_usd']:.2f} reported {shown}"


def _line(label: str, row: dict[str, object]) -> str:
    tokens = row["tokens"]
    total = "-" if not isinstance(tokens, dict) else str(sum(tokens.values()))
    return (
        f"{label}  {row['sessions']} sessions  {total} tokens  {_money(row['cost'])}  "
        f"calls {row['tool_calls']} errors {row['tool_errors']} denials {row['denials']}"
    )


def print_table(
    ctx: Context, by: list[str], result: aggregate.UsageResult, store: dict[str, str | None]
) -> None:
    """One line per row; every key part is untrusted and goes through ``one_line`` once."""
    put(ctx, "by " + ", ".join(one_line(d) for d in by))
    for row in result.rows:
        keys = row["key"]
        assert isinstance(keys, list)
        label = " / ".join("(none)" if k is None else one_line(k) for k in keys)
        put(ctx, _line(label, row))
    more = " (more matched; raise --limit)" if result.truncated else ""
    put(ctx, f"{len(result.rows)} rows{more}")
    put(ctx, _line("total", result.totals))
    put(ctx, f"last ingest: {store['last_ingest_at'] or 'never'}")


COMMANDS = (Command(("usage",), "usage analytics over counted events", add_arguments, check, run),)
