"""``show``: one stored session (interface, Commands, ``show``).

The prefix is resolved through the primary-key index (at most 21 ids read), the Session object,
at most 1000 agents and at most 10000 usage rows are read in one read transaction, all
constrained by the resolved session id. A session is shown when asked for by id even if its
snapshot labels it ``owner=self-improve`` (the default exclusion applies to listings).

TODO(#19): snapshots are read from ``harness_runs``; ``ingest`` fills it once #19 lands.
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import derive, errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_session_prefix
from ..sessionquery import (
    freshness,
    one_line,
    open_reader,
    resolve_session,
    session_json,
    snapshot_for,
    usage_rows,
)


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
    with open_reader(ctx) as conn:
        try:
            session_id = resolve_session(conn, args.session_id)
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
        assert conn is not None  # resolve_session raises not_found without a store
        values = derive.session_values(conn, ctx.settings, session_id)
        if values is None:  # removed between the lookup and the read: cannot happen in one read
            raise errors.from_grammar(
                GrammarError("not found", "SESSION_ID", details={"id": session_id})
            )
        snapshot = snapshot_for(conn, session_id)
        agents, agents_cut = derive.agent_values(conn, session_id)
        usage, usage_cut = usage_rows(conn, session_id, ctx.settings.zone())
        store = freshness(conn)
    document = {
        "session": session_json(values, snapshot),
        "agents": [x.to_json() for x in agents],
        "usage": usage,
        "truncated": agents_cut or usage_cut,
        "store": store,
    }
    if ctx.json:
        ctx.emit_json(document)
        return 0
    print_session(ctx, document)
    return 0


def print_session(ctx: Context, document: dict[str, Any]) -> None:
    """Human output: one fact per line, every untrusted text through the sanitizer."""
    s = document["session"]
    ctx.say(f"session {s['session_id']}")
    ctx.say(f"project: {one_line(s['project'] or '-')}")
    ctx.say(f"entrypoint: {s['entrypoint']}  version: {one_line(s['version'] or '-')}")
    ctx.say(f"started: {s['started_at'] or '-'}  ended: {s['ended_at'] or '-'}")
    ctx.say(f"models: {', '.join(one_line(m) for m in s['models']) or '-'}")
    t = s["tokens"]
    ctx.say(
        f"tokens: input {t['input']} output {t['output']} cache_read {t['cache_read']} "
        f"cache_write {t['cache_write']} subagents {t['subagents']}"
    )
    ctx.say(f"cost: ${s['cost']['estimated_usd']:.2f} (estimated)")
    ctx.say(
        f"tool calls {s['tool_calls']}  errors {s['tool_errors']}  denials {s['denials']}  "
        f"interruptions {s['interruptions']}"
    )
    if s["flags"]:
        ctx.say(f"flags: {', '.join(s['flags'])}")
    harness = s["harness"]
    if harness is not None:
        ctx.say(
            f"harness: {harness['csession_id']} kind {one_line(harness['kind'])} (labels are hints)"
        )
        for label in harness["labels"]:
            ctx.say(f"  label: {one_line(label)}")
    ctx.say(f"agents: {len(document['agents'])}")
    for agent in document["agents"]:
        ctx.say(
            f"  {one_line(agent['agent_id'])}  {one_line(agent['agent_type'] or '-')}  "
            f"{sum(agent['tokens'].values())} tokens"
        )
    ctx.say(f"usage rows: {len(document['usage'])}")
    for row in document["usage"]:
        who = one_line(row["agent_id"] or "main")
        model = one_line(row["model"] or "-")
        ctx.say(f"  {row['day'] or '-'}  {who}  {model}  {sum(row['tokens'].values())} tokens")
    if document["truncated"]:
        ctx.say("(output was cut at the documented caps)")
    ctx.say(f"last ingest: {document['store']['last_ingest_at'] or 'never'}")


COMMANDS = (Command(("show",), "show one stored session", add_arguments, check, run),)
