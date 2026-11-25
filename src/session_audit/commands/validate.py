"""``validate``: check the installation (interface, Commands, ``validate``).

TODO(#33): implement ``run``. It must print its document on exits 0, 2 and 12 and never read a
broken config as fatal before it reports it.
"""

from __future__ import annotations

import argparse

from .. import args as a
from ..command import Command, Context


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--probe", action="store_true", help="also probe claude-session and the sandbox"
    )


def check(ctx: Context, args: argparse.Namespace) -> None:
    return None


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (
    Command(
        ("validate",), "check config, store, roots and claude-session", add_arguments, check, run
    ),
)
