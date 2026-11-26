"""``retro run``: run one retro (interface, Commands, ``retro run``).

``check`` holds the usage errors (Order of checks step 1). ``run`` does steps 2 to 10 through
``retro.prepare`` (the order, the digest, the key, reuse and ``already_running``) and prints a
reused retro. TODO(#24): the plan check and the dry run's plan (steps 11 and 12).
TODO(#26): the launch (steps 13 to 16).
"""

from __future__ import annotations

import argparse

from .. import args as a
from .. import errors, retro
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
from ..sessionquery import one_line, put

# The hook points of steps 4 and 5 (TODO(#24), TODO(#27)); tests replace them.
STEPS: retro.Steps = retro.NoSteps()
MAX_SESSION_FLAGS = 1000
SCOPE_FLAGS = ("session", "label", "project", "since", "until", "kind", "outcome")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session", action="append", metavar="ID", help="a session id or prefix (repeatable)"
    )
    a.add_time_filters(parser)
    a.add_session_filters(parser)
    a.add_owner_filter(parser)
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


def _print_reused(ctx: Context, retro_object: dict[str, object]) -> None:
    if ctx.json:
        ctx.emit_json({**retro_object, "reused": True})
        return
    ctx.say(f"retro {retro_object['id']} is done (reused, key {retro_object['key']})")
    put(ctx, "summary: " + one_line(retro_object["summary"]))  # model text: one sanitized line


def run(ctx: Context, args: argparse.Namespace) -> int:
    prepared = retro.prepare(ctx, args, steps=STEPS)
    for warning in (*prepared.warnings, *prepared.notes):
        ctx.note(f"session-audit: warning: {warning}")
    if prepared.unfit_retros and not args.again:
        ctx.note(
            f"session-audit: {prepared.unfit_retros} stored retro(s) with this key did not fit "
            "and were not reused"
        )
    if prepared.reuse is not None:
        _print_reused(ctx, prepared.reuse)
        return 0
    ctx.note(
        f"retro: {prepared.kept} sessions kept, {len(prepared.skipped)} skipped, "
        f"{prepared.projects} projects"
    )
    # TODO(#24): steps 11 and 12 (the launch plan check and the dry run's plan).
    # TODO(#26): steps 13 to 16 (the row, the hooks, the launch) from ``prepared``.
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
