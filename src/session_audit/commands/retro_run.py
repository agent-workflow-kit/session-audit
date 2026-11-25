"""``retro run``: run one retro (interface, Commands, ``retro run``).

TODO(#23): the order of checks through reuse. TODO(#24): the dry run. TODO(#26): the launch.
``check`` already holds the usage errors; ``run`` is replaced by the issues above.
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors
from ..command import Command, Context
from ..grammar import (
    MAX_TURNS_RANGE,
    RETRO_TIMEOUT_MAX,
    RETRO_TIMEOUT_MIN,
    GrammarError,
    parse_decision_id,
    parse_duration,
    parse_int,
    parse_session_prefix,
)

MAX_SESSION_FLAGS = 1000
SCOPE_FLAGS = ("session", "label", "project", "since", "until", "kind", "outcome")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session", action="append", metavar="ID", help="a session id or prefix (repeatable)"
    )
    a.add_time_filters(parser)
    a.add_session_filters(parser)
    parser.add_argument("--skill", metavar="NAME", help="a plugin skill, plugin:skill")
    parser.add_argument("--mode", default="headless", metavar="MODE", help="only headless in 0.x")
    parser.add_argument("--model", metavar="M", help="the retro model")
    parser.add_argument("--timeout", metavar="DURATION", help="1 s to 2 h")
    parser.add_argument("--max-turns", metavar="N", help="1 to 1000")
    parser.add_argument(
        "--again", action="store_true", help="run even when a stored retro has the same key"
    )
    parser.add_argument(
        "--decision-id", action="append", metavar="ID", help="a hook decision id (repeatable)"
    )
    a.add_dry_run(parser)


def check(ctx: Context, args: argparse.Namespace) -> None:
    if args.mode == "interactive":
        raise errors.usage(
            "interactive mode is not supported",
            reason="mode_unsupported",
            hint="Only --mode headless is available in 0.x.",
        )
    if args.mode != "headless":
        raise errors.usage(
            "invalid mode", field="--mode", hint="Only --mode headless is available in 0.x."
        )
    a.check_list_max(args.session, "--session", MAX_SESSION_FLAGS)
    try:
        args.session = [parse_session_prefix(s, field="--session") for s in args.session or []]
        if args.timeout is not None:
            args.timeout = parse_duration(
                args.timeout,
                field="--timeout",
                minimum=RETRO_TIMEOUT_MIN,
                maximum=RETRO_TIMEOUT_MAX,
            )
        if args.max_turns is not None:
            args.max_turns = parse_int(
                args.max_turns,
                field="--max-turns",
                minimum=MAX_TURNS_RANGE[0],
                maximum=MAX_TURNS_RANGE[1],
            )
        for decision in args.decision_id or []:
            parse_decision_id(decision, field="--decision-id")
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None
    a.check_filters(ctx, args)
    if args.skill is not None:
        a.check_skill(args.skill)
    if args.model is not None:
        a.check_model(args.model, launch=True)
    if not any(getattr(args, name) for name in SCOPE_FLAGS):
        raise errors.usage(
            "a scope flag is required",
            hint="Give one of --session, --label, --project, --since, --until, --kind, --outcome.",
        )


def run(ctx: Context, args: argparse.Namespace) -> int:
    raise a.not_implemented()


COMMANDS = (
    Command(
        ("retro", "run"),
        "run one retro",
        add_arguments,
        check,
        run,
        accepts_actor=True,
        refuses_unsafe_config=True,
    ),
)
