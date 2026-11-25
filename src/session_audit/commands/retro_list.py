"""``retro list``: list stored retros (interface, Commands, ``retro list``).

TODO(#31): implement ``run``.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_session_prefix


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


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (Command(("retro", "list"), "list stored retros", add_arguments, check, run),)
