"""``retro run``: run one retro (interface, Commands, ``retro run``).

``check`` holds the usage errors (Order of checks step 1). ``run`` does steps 2 to 10 through
``retro.prepare`` (the order, the skill preflight of step 4, the digest, the key, reuse and
``already_running``) and prints a reused retro; then step 11 (``launchplan.plan_check``) and, with
``--dry-run``, step 12 (the plan, never any digest text); then ``retrolaunch.run_launch`` (steps 13
to 16: the row, ``pre-retro``, the files, ``start``, the adopting ``exec``, the outcome mapping,
cleanup and the post hooks).
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from .. import args as a
from .. import errors, fencing, launchplan, plugins, retro, retrolaunch, retrorecovery, retrostate
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


class Steps(retro.NoSteps):
    """The real step 4 (the skill preflight) and step 5 (housekeeping)."""

    def housekeeping(self, ctx: Context) -> None:
        """Recovery of retros left behind, cleanup retries, expired digests and old temporary
        files (``retrorecovery``). Never changes the command's exit code."""
        result = retrorecovery.run_housekeeping(ctx)
        if result.recovered:
            ctx.note(f"session-audit: recovered {len(result.recovered)} retro(s)")

    def skill_preflight(self, ctx: Context, skill: str) -> plugins.PluginChoice:
        return plugins.skill_preflight(
            ctx.gateway,
            skill,
            plugin_dirs=ctx.settings.retro.plugin_dirs,
            env=os.environ,
            state_directory=ctx.state_path(),
        )


# The hook points of steps 4 and 5; tests replace them.
STEPS: retro.Steps = Steps()
MAX_SESSION_FLAGS = 1000
PLAN_LIST_MAX = 1000  # the most ids and skip rows of the dry-run plan (the schema's maxItems)
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


def plan_document(
    ctx: Context, args: argparse.Namespace, prepared: retro.Prepared, launch: launchplan.Launch
) -> dict[str, Any]:
    """The dry-run plan (``schemas/cli/retro-run.json``): ids, counts, enums and the flags; never
    digest text. ``digest_bytes`` is the size of the ``digest.md`` a real run writes (the digest is
    fenced here with a placeholder retro id and run token of the same widths, then dropped)."""
    result = prepared.digest
    fenced = fencing.finish_digest(
        result,
        launchplan.PLACEHOLDER_RETRO_ID,
        ctx.gateway.token_urlsafe(24),
        ctx.gateway,
        prepared.csession_ids,
    )
    return {
        "dry_run": True,
        "scope": retrostate.scope_object(args, prepared),
        "matched": [s.session_id for s in result.sections][:PLAN_LIST_MAX],
        "matched_total": prepared.kept,
        "skipped": [
            {"session_id": s.session_id, "reason": s.reason}
            for s in prepared.skipped[:PLAN_LIST_MAX]
        ],
        "over_cap": prepared.over_cap,
        "items": result.items_kept,
        "items_by_kind": result.items_by_kind,
        "digest_bytes": len(fenced.digest_bytes),
        "key": prepared.key,
        "skill": prepared.skill,
        "model": prepared.model,
        "launch_flags": launchplan.plan_flags(launch),
        "reused_retro": prepared.reused_retro,
        "running_retro": prepared.running_retro,
        "daily_remaining": prepared.daily_remaining,
    }


def _print_plan(ctx: Context, plan: dict[str, Any]) -> None:
    if ctx.json:
        ctx.emit_json(plan)
        return
    ctx.say(f"retro plan (dry run): key {plan['key']}")
    ctx.say(
        f"  sessions: {plan['matched_total']} digested, {len(plan['skipped'])} skipped, "
        f"{plan['over_cap']} over the cap"
    )
    ctx.say(f"  items: {plan['items']}, digest bytes: {plan['digest_bytes']}")
    ctx.say(f"  skill: {plan['skill']}, daily remaining: {plan['daily_remaining']}")
    if plan["reused_retro"] is not None:
        ctx.say(f"  a real run would reuse {plan['reused_retro']}")
    if plan["running_retro"] is not None:
        ctx.say(f"  a real run would be refused: {plan['running_retro']} is running")


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
    launch = launchplan.Launch.of(
        ctx.settings.retro,
        skill=prepared.skill,
        plugin_dirs=prepared.plugin_dirs,
        bundled=prepared.bundled_plugin,
        model=prepared.model,
        timeout=args.timeout,
        max_turns=args.max_turns,
    )
    plan = launchplan.plan_check(  # step 11: nothing is stored, nothing is spent
        ctx.gateway,
        ctx.settings.harness.claude_session_cmd,
        launch,
        state_directory=ctx.state_path(),
    )
    if ctx.dry_run:  # step 12
        _print_plan(ctx, plan_document(ctx, args, prepared, launch))
        return 0
    # Steps 13 to 16: the row, the hooks, the files, start, exec, the mapping and the cleanup.
    return retrolaunch.run_launch(
        ctx, args, prepared, launch, plan, on_reused=lambda found: _print_reused(ctx, found)
    )


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
