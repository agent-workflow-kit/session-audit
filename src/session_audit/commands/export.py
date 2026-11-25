"""``export``: print store rows as JSON lines (interface, Commands, ``export``).

TODO(#17): ``export sessions`` and ``export usage``. TODO(#31): ``export retros``.
"""

from __future__ import annotations

import argparse

from .. import args as a
from ..command import Command, Context

KINDS = ("sessions", "usage", "retros")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "kind", choices=KINDS, metavar="{sessions|usage|retros}", help="the rows to print"
    )
    a.add_time_filters(parser, until=False)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.since is not None:
        a.check_when(ctx, args.since, "--since")


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (Command(("export",), "print store rows as JSON lines", add_arguments, check, run),)
