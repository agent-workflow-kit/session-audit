"""``usage``: deterministic analytics over counted events (interface, Commands, ``usage``).

TODO(#17): implement ``run`` (aggregation). ``check`` already holds the usage errors.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_label_key, parse_time_zone

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
    raise a.not_implemented()


COMMANDS = (Command(("usage",), "usage analytics over counted events", add_arguments, check, run),)
