"""``retro delete``: delete retros (interface, Commands, ``retro delete``).

TODO(#31): implement ``run`` (the prompt on a terminal, the transaction).
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_retro_prefix


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("retro_ids", nargs="+", metavar="RETRO_ID", help="a retro id or a prefix")
    a.add_yes(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    try:
        args.retro_ids = [parse_retro_prefix(r, field="RETRO_ID") for r in args.retro_ids]
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    a.require_confirmation(ctx)


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


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
