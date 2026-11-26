"""``retro show``: one stored retro (interface, Commands, ``retro show``).

``--json`` prints the Retro object (the contract other tools read) with the optional
``superseded`` and, with ``--digest``, the stored digest (null once pruned). Human output is
readable text with the mapping from session refs to sessions. Every string that is not one of the
tool's constants (model text, the digest, names, labels) goes through the sanitizer.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any

from .. import errors
from ..command import Command, Context
from ..grammar import GrammarError, parse_retro_prefix
from ..retrofiles import read_digest
from ..retroquery import resolve_retro, with_superseded
from ..retroview import load_retro
from ..sessionquery import one_line, put, read_store
from ..textsafe import decode_utf8


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("retro_id", metavar="RETRO_ID", help="a retro id or a prefix")
    parser.add_argument("--digest", action="store_true", help="also print the stored digest")


def check(ctx: Context, args: argparse.Namespace) -> None:
    try:
        args.retro_id = parse_retro_prefix(args.retro_id, field="RETRO_ID")
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def not_fit(retro_id: str) -> errors.CliError:
    return errors.CliError(
        "not_found",
        "the stored retro does not fit the documented layout",
        details={"id": retro_id},
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    def read(conn: sqlite3.Connection | None) -> dict[str, Any]:
        try:
            retro_id = resolve_retro(conn, args.retro_id)
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None
        assert conn is not None
        found = load_retro(conn, retro_id)
        if found is None:
            raise not_fit(retro_id)
        return with_superseded(conn, found)

    retro = read_store(ctx, read)
    digest: str | None = None
    if args.digest:
        raw = read_digest(ctx.gateway, ctx.state_path(), retro["id"])
        digest = None if raw is None else decode_utf8(raw)[:1048576]
    if ctx.json:
        ctx.emit_json({**retro, "digest": digest} if args.digest else retro)
        return 0
    if ctx.quiet:
        return 0
    _print(ctx, retro)
    if args.digest:
        put(ctx, "digest:")
        if digest is None:
            put(ctx, "  (not stored: pruned)")
        else:
            ctx.say(digest)  # untrusted: each line is sanitized, the fence lines stay in place
    return 0


def _print(ctx: Context, r: dict[str, Any]) -> None:
    u = one_line
    state = r["state"] if r["reason"] is None else f"{r['state']} ({r['reason']})"
    put(ctx, f"retro {u(r['id'])}: {u(state)}" + (" superseded" if r["superseded"] else ""))
    put(ctx, f"key: {u(r['key'])}")
    put(ctx, f"skill: {u(r['skill'])} model: {u(r['model'] or '-')}")
    put(
        ctx,
        f"created: {u(r['created_at'])} updated: {u(r['updated_at'])} "
        f"finished: {u(r['finished_at'] or '-')}",
    )
    scope = r["scope"]
    words = [f"session={u(s)}" for s in scope["sessions"]]
    words += [f"label={u(x)}" for x in scope["labels"]]
    for name in ("project", "since", "until", "kind", "outcome"):
        if scope[name] is not None:
            words.append(f"{name}={u(scope[name])}")
    put(ctx, "scope: " + (" ".join(words) or "-"))
    counts = r["counts"]
    put(ctx, "counts: " + " ".join(f"{k}={counts[k]}" for k in counts))
    cost = "-" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
    put(
        ctx,
        f"run: cost_usd={cost} turns={u(r['num_turns'] if r['num_turns'] is not None else '-')} "
        f"outcome={u(r['outcome'] or '-')} cleanup={u(r['cleanup'] or '-')}",
    )
    if r["warnings"]:
        put(ctx, "warnings: " + " ".join(u(w) for w in r["warnings"]))
    if r["again_of"]:
        put(ctx, f"again of: {u(r['again_of'])}")
    put(ctx, "sessions:")
    for s in r["sessions"]:
        put(ctx, f"  {u(s['ref'])} = {u(s['session_id'])} csession={u(s['csession_id'] or '-')}")
    for s in r["skipped"]:
        put(ctx, f"  skipped {u(s['session_id'])}: {u(s['reason'])}")
    if r["summary"] is not None:
        put(ctx, "summary: " + u(r["summary"]))
    for f in r["findings"]:
        put(ctx, f"finding {u(f['id'])} [{u(f['category'])}/{u(f['severity'])}] {u(f['title'])}")
        put(ctx, f"  {u(f['detail'])}")
        put(ctx, "  evidence: " + " ".join(u(e["ref"]) for e in f["evidence"]))
    for c in r["candidates"]:
        put(
            ctx,
            f"candidate {u(c['id'])} [{u(c['target_class'])}/{u(c['risk'])}] {u(c['target_hint'])}",
        )
        put(ctx, f"  {u(c['proposal'])}")
        put(
            ctx,
            f"  findings: {' '.join(u(x) for x in c['findings'])} "
            f"sessions: {' '.join(u(x) for x in c['sessions'])}",
        )


COMMANDS = (Command(("retro", "show"), "show one stored retro", add_arguments, check, run),)
