"""``ingest``: read new transcript bytes into the store (interface, Commands, ``ingest``).

TODO(#15): implement ``run``. ``check`` already holds the usage errors of the interface.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import INGEST_BUDGET_MAX, GrammarError, parse_csession_id, parse_duration


def add_arguments(parser: argparse.ArgumentParser) -> None:
    a.add_time_filters(parser, until=False)
    parser.add_argument(
        "--path", action="append", metavar="PATH", help="only files under this root"
    )
    parser.add_argument("--session", metavar="CSESSION_ID", help="capture one claude-session run")
    parser.add_argument("--no-harness", action="store_true", help="skip claude-session")
    parser.add_argument(
        "--rebuild", action="store_true", help="re-read every transcript from the start"
    )
    parser.add_argument("--budget", metavar="DURATION", help="overall time budget")
    a.add_dry_run(parser)
    a.add_yes(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.since is not None:
        a.check_when(ctx, args.since, "--since")
    for path in args.path or []:
        a.check_path(path, "--path")
    try:
        if args.session is not None:
            parse_csession_id(args.session, field="--session")
        if args.budget is not None:
            args.budget = parse_duration(args.budget, field="--budget", maximum=INGEST_BUDGET_MAX)
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    if args.rebuild:
        for flag, given in (
            ("--path", args.path),
            ("--since", args.since),
            ("--session", args.session),
        ):
            if given:
                raise errors.usage(
                    "--rebuild cannot be combined with this flag",
                    field="--rebuild",
                    hint=f"Drop {flag} or --rebuild.",
                )
    if args.dry_run and args.session is not None:
        raise errors.usage(
            "--dry-run cannot be combined with --session",
            field="--dry-run",
            hint="Drop --dry-run or --session.",
        )
    if args.rebuild and not args.dry_run:
        a.require_confirmation(ctx)


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (
    Command(
        ("ingest",),
        "read new transcript bytes into the store",
        add_arguments,
        check,
        run,
        accepts_actor=True,
        refuses_unsafe_config=True,
    ),
)
