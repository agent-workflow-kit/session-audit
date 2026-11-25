"""``show``: one stored session (interface, Commands, ``show``).

TODO(#16): implement ``run`` (prefix resolution against the store, the session document).
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_session_prefix


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "session_id", metavar="SESSION_ID", help="a session id or a prefix of 8+ characters"
    )


def check(ctx: Context, args: argparse.Namespace) -> None:
    try:
        args.session_id = parse_session_prefix(args.session_id, field="SESSION_ID")
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (Command(("show",), "show one stored session", add_arguments, check, run),)
