"""``retro show``: one stored retro (interface, Commands, ``retro show``).

TODO(#31): implement ``run``.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_retro_prefix


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("retro_id", metavar="RETRO_ID", help="a retro id or a prefix")
    parser.add_argument("--digest", action="store_true", help="also print the stored digest")


def check(ctx: Context, args: argparse.Namespace) -> None:
    try:
        args.retro_id = parse_retro_prefix(args.retro_id, field="RETRO_ID")
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (Command(("retro", "show"), "show one stored retro", add_arguments, check, run),)
