"""``cron install`` and ``cron uninstall``: the managed crontab block (interface, ``cron``).

TODO(#32): implement both ``run`` functions.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_ingest_every


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ingest-every", metavar="INTERVAL", help="the ingest interval (default 1h)"
    )
    parser.add_argument("--env-file", metavar="PATH", help="an environment file for the cron lines")
    a.add_dry_run(parser)


def check_install(ctx: Context, args: argparse.Namespace) -> None:
    if args.ingest_every is not None:
        try:
            args.ingest_every = parse_ingest_every(args.ingest_every, field="--ingest-every")
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
    if args.env_file is not None:
        a.check_path(args.env_file, "--env-file")


def add_uninstall_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_dry_run(parser)


def check_uninstall(ctx: Context, args: argparse.Namespace) -> None:
    return None


def run_install(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


def run_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (
    Command(
        ("cron", "install"),
        "install the managed cron block",
        add_install_arguments,
        check_install,
        run_install,
    ),
    Command(
        ("cron", "uninstall"),
        "remove the managed cron block",
        add_uninstall_arguments,
        check_uninstall,
        run_uninstall,
    ),
)
