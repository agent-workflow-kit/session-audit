"""``prune``: apply the retention rules (interface, Commands, ``prune``).

TODO(#18): implement ``run``.
"""

from __future__ import annotations

import argparse

from .. import args as a
from ..command import Command, Context


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_dry_run(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    return None


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (
    Command(("prune",), "apply the retention rules", add_arguments, check, run, accepts_actor=True),
)
