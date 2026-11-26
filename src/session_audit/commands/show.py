"""``show``: one stored session (interface, Commands, ``show``).

The prefix is resolved through the primary-key index (at most 21 ids read), the Session object,
at most 1000 agents and at most 10000 usage rows are read in one read transaction, all
constrained by the resolved session id. A session is shown when asked for by id even if its
snapshot labels it ``owner=self-improve`` (the default exclusion applies to listings).

TODO(#19): snapshots are read from ``harness_runs``; ``ingest`` fills it once #19 lands.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any

from .. import derive, errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_session_prefix
from ..sessionquery import (
    freshness,
    one_line,
    put,
    read_store,
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
    def read(conn: sqlite3.Connection | None) -> dict[str, Any]:
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
        return {
            "session": session_json(values, snapshot),
            "agents": [x.to_json() for x in agents],
            "usage": usage,
            "truncated": agents_cut or usage_cut,
            "store": freshness(conn),
        }

    document = read_store(ctx, read)
    if ctx.json:
        ctx.emit_json(document)
        return 0
    print_session(ctx, document)
    return 0


def print_session(ctx: Context, document: dict[str, Any]) -> None:
    """Human output: one fact per line. Every value read from the store goes through ``one_line``
    (the sanitizer, once) and the line is printed with ``put``, so a field is one visible run of
    text and cannot start another line."""

    def say(text: str) -> None:
        put(ctx, text)

    o = one_line
    s = document["session"]
    say(f"session {o(s['session_id'])}")
    say(f"project: {o(s['project'] or '-')}")
    say(f"entrypoint: {o(s['entrypoint'])}  version: {o(s['version'] or '-')}")
    say(f"started: {o(s['started_at'] or '-')}  ended: {o(s['ended_at'] or '-')}")
    say(f"models: {', '.join(o(m) for m in s['models']) or '-'}")
    t = s["tokens"]
    say(
        f"tokens: input {t['input']} output {t['output']} cache_read {t['cache_read']} "
        f"cache_write {t['cache_write']} subagents {t['subagents']}"
    )
    say(f"cost: ${s['cost']['estimated_usd']:.2f} (estimated)")
    say(
        f"tool calls {s['tool_calls']}  errors {s['tool_errors']}  denials {s['denials']}  "
        f"interruptions {s['interruptions']}"
    )
    if s["flags"]:
        say(f"flags: {', '.join(o(f) for f in s['flags'])}")
    harness = s["harness"]
    if harness is not None:
        say(f"harness: {o(harness['csession_id'])} kind {o(harness['kind'])} (labels are hints)")
        for label in harness["labels"]:
            say(f"  label: {o(label)}")
    say(f"agents: {len(document['agents'])}")
    for agent in document["agents"]:
        say(
            f"  {o(agent['agent_id'])}  {o(agent['agent_type'] or '-')}  "
            f"{sum(agent['tokens'].values())} tokens"
        )
    say(f"usage rows: {len(document['usage'])}")
    for row in document["usage"]:
        who = o(row["agent_id"] or "main")
        model = o(row["model"] or "-")
        say(f"  {o(row['day'] or '-')}  {who}  {model}  {sum(row['tokens'].values())} tokens")
    if document["truncated"]:
        say("(output was cut at the documented caps)")
    say(f"last ingest: {o(document['store']['last_ingest_at'] or 'never')}")


COMMANDS = (Command(("show",), "show one stored session", add_arguments, check, run),)
