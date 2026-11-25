"""``sessions``: list stored sessions (interface, Commands, ``sessions``).

TODO(#16): implement ``run`` (the store read, filters and ordering).
"""

from __future__ import annotations

import argparse

from .. import args as a
from ..command import Command, Context

ENTRYPOINTS = ("interactive", "headless")
SORTS = ("start", "tokens", "cost", "active")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_time_filters(parser)
    a.add_session_filters(parser)
    parser.add_argument("--entrypoint", choices=ENTRYPOINTS, help="interactive or headless")
    parser.add_argument("--model", metavar="M", help="an exact model name")
    parser.add_argument("--sort", choices=SORTS, default="start", help="the order of the rows")
    a.add_limit(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    a.check_filters(ctx, args)
    if args.model is not None:
        a.check_model(args.model, "--model")
    a.check_limit(args)


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (Command(("sessions",), "list stored sessions", add_arguments, check, run),)
