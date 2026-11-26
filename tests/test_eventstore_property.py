"""The owner rule as a property (issue #13): whatever the ingest order, growth stages, chunk sizes,
rewrite of a file, fork copies, partial copy of a streaming message or rebuild, the counted events
equal an independent reference computed from the final files alone.

Seeded and deterministic (no hypothesis: it is not a dependency, and the design says a new one
needs a justification). Every scenario has at most 5 files, so a key has at most 5 copies and no
copy is dropped (the 8-copy cap is tested in ``test_eventstore.py``)."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from session_audit.eventstore import dedupe_id, event_key, event_ref
from session_audit.parser import Event, FileFacts, Kind, ParseContext, parse_line

from .eventhelp import (
    SESSION_A,
    SESSION_B,
    SESSION_C,
    Env,
    compaction,
    denial_and_interruption,
    make_env,
    message,
    raw,
    tool_error,
    tool_use,
    turn,
)

SESSIONS = [SESSION_A, SESSION_B, SESSION_C]
SEEDS = range(30)
MTIME_NS = time.time_ns()


@dataclass
class Spec:
    name: str
    session: str
    agent: str | None
    lines: list[bytes]


def block_lines(rng: random.Random, i: int) -> list[bytes]:
    """The lines of streaming message ``i``: one per content block, output rising; also the calls
    and errors that hang off it."""
    steps = sorted(rng.sample(range(1, 200), rng.randint(1, 3)))
    t0 = rng.randint(0, 2000)
    out: list[bytes] = []
    for j, o in enumerate(steps):
        t = t0 + j if rng.random() > 0.15 else None
        if t is None:
            out.append(
                raw(
                    {
                        "type": "assistant",
                        "message": {"id": f"m{i}", "model": "mod", "usage": {"output_tokens": o}},
                    }
                )
            )
        else:
            out.append(message(f"m{i}", o, t=t, model=rng.choice(["mod-a", "mod-b"])))
    if rng.random() < 0.5:
        out.append(
            tool_use(f"m{i}", f"toolu_{i}", rng.choice(["Bash", "Read"]), out=steps[-1], t=t0 + 5)
        )
        if rng.random() < 0.6:
            out.append(
                tool_error(f"toolu_{i}", t=t0 + 6, uuid=f"err{i}" if rng.random() < 0.7 else None)
            )
    if rng.random() < 0.3:
        out.append(denial_and_interruption(uuid=f"den{i}", tool_id=f"toolu_{i}", t=t0 + 7))
    if rng.random() < 0.3:
        out.append(
            turn(rng.randint(1, 5000), t=t0 + 8, uuid=f"turn{i}" if rng.random() < 0.5 else None)
        )
    if rng.random() < 0.2:
        out.append(compaction(f"cmp{i}" if rng.random() < 0.5 else None, t=t0 + 9))
    return out


def scenario(seed: int, files: int | None = None) -> list[Spec]:
    rng = random.Random(seed)
    universe = [block_lines(rng, i) for i in range(rng.randint(3, 8))]
    specs: list[Spec] = []
    for n in range(files or rng.randint(2, 5)):
        session = rng.choice(SESSIONS)
        agent = rng.choice([None, None, "agent-x"])
        lines: list[bytes] = []
        for group in rng.sample(universe, rng.randint(1, len(universe))):
            chosen = (
                group if rng.random() < 0.6 else group[: rng.randint(1, len(group))]
            )  # partial copy
            for line in chosen:
                if rng.random() < 0.5:  # copied history: names a session, maybe another one
                    line = line.replace(
                        b'"timestamp"',
                        f'"sessionId": "{rng.choice(SESSIONS)}", "timestamp"'.encode(),
                        1,
                    )
                lines.append(line)
        lines.append(raw({"type": "zzz-unknown"}))
        lines.append(b"corrupt line\n")
        specs.append(Spec(f"f{rng.randint(0, 9)}{n}", session, agent, lines))
    return specs


def reference(env: Env, specs: list[Spec]) -> list[tuple]:
    """The counted events of the final files, from the parser and the owner order alone."""
    ids = dict(env.conn.execute("SELECT path, id FROM files"))
    best: dict[str, tuple[tuple, tuple]] = {}
    for spec in specs:
        path = bytes(env.path(spec.name))
        file_id = ids[path]
        facts = FileFacts()
        ctx = ParseContext(mtime_ns=MTIME_NS, main=spec.agent is None)
        merged: dict[str, tuple[int, Event, bool]] = {}
        offset = 0
        for line in spec.lines:
            result = parse_line(line, ctx)
            for ev in facts.add(result):
                key = event_key(ev.kind, dedupe_id(ev, file_id, offset))
                native = ev.line_session_id is None or ev.line_session_id == spec.session
                cur = merged.get(key)
                if cur is None:
                    merged[key] = (offset, ev, native)
                elif ev.output_tokens > cur[1].output_tokens:
                    merged[key] = (cur[0], ev, native)
            offset += len(line)
        for key, (off, ev, native) in merged.items():
            order = (-ev.output_tokens, ev.ts is None, ev.ts or 0, not native, path, off)
            ref = (
                event_ref(ev.ref_id)
                if ev.ref_id and ev.kind in (Kind.TOOL_ERROR, Kind.DENIAL)
                else None
            )
            row = (
                key,
                ev.kind.value,
                ref,
                spec.session,
                spec.agent,
                ev.model,
                ev.tool,
                ev.ts,
                ev.slot,
                ev.input_tokens,
                ev.output_tokens,
                ev.cache_read_tokens,
                ev.cache_write_5m_tokens,
                ev.cache_write_1h_tokens,
                ev.seconds,
                int(native),
            )
            if key not in best or order < best[key][0]:
                best[key] = (order, row)
    return sorted((row for _, row in best.values()), key=lambda r: r[0])


def check(env: Env, specs: list[Spec], *, dropped: bool = False) -> None:
    assert env.counted() == reference(env, specs)
    assert (
        env.conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM events GROUP BY key HAVING sum(counted) <> 1)"
        ).fetchone()[0]
        == 0
    )
    assert (
        env.conn.execute(
            "SELECT max(c) FROM (SELECT count(*) c FROM events GROUP BY key)"
        ).fetchone()[0]
        <= 8
    )
    assert env.conn.execute("SELECT sum(rereading) FROM files").fetchone()[0] == 0
    total = env.conn.execute("SELECT sum(copies_dropped) FROM files").fetchone()[0]
    assert dropped or total == 0  # with more than 8 files a copy may legitimately be dropped


def write_final(env: Env, specs: list[Spec]) -> None:
    for s in specs:
        env.write(s.name, s.lines)


def ingest(env: Env, spec: Spec, rng: random.Random, **kw: object) -> None:
    env.ingest(
        spec.name,
        session=spec.session,
        agent=spec.agent,
        chunk_lines=rng.randint(1, 5),
        chunk_events=rng.randint(1, 6),
        delete_batch_size=rng.randint(1, 4),
        **kw,  # type: ignore[arg-type]
    )


def staged_ingest(env: Env, specs: list[Spec], rng: random.Random) -> None:
    """Grow every file in 1 to 3 stages (a stage may end inside a line) and ingest the files in a
    random interleaving, ending with every file complete."""
    data = {s.name: b"".join(s.lines) for s in specs}
    cuts = {
        s.name: [
            *sorted(rng.randint(0, len(data[s.name])) for _ in range(rng.randint(0, 2))),
            len(data[s.name]),
        ]
        for s in specs
    }
    queue = {name: list(ends) for name, ends in cuts.items()}
    by_name = {s.name: s for s in specs}
    while any(queue.values()):
        name = rng.choice([n for n in queue if queue[n]])
        env.path(name).write_bytes(data[name][: queue[name].pop(0)])
        ingest(env, by_name[name], rng)


@pytest.mark.parametrize("seed", SEEDS)
def test_any_order_growth_and_chunking_give_the_reference(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed * 7 + 1)
    specs = scenario(seed)
    env = make_env(tmp_path)
    try:
        staged_ingest(env, specs, rng)
        check(env, specs)
    finally:
        env.close()


@pytest.mark.parametrize("seed", SEEDS)
def test_a_rewritten_file_and_a_rebuild_give_the_same_counts(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed * 11 + 3)
    specs = scenario(seed)
    env = make_env(tmp_path)
    try:
        # First the files hold other content (as if they were rewritten later), then the real one.
        for spec in rng.sample(specs, len(specs)):
            junk = [
                message(f"old{seed}{spec.name}{i}", 500 + i, t=i) for i in range(rng.randint(1, 6))
            ]
            junk.append(
                message("m0", 10_000, t=1, session=spec.session)
            )  # a bogus winner of a real key
            env.write(spec.name, junk)
            ingest(env, spec, rng)
        write_final(env, specs)
        for spec in rng.sample(specs, len(specs)):
            ingest(env, spec, rng)
        check(env, specs)
        # A rebuild of every file, in another order, with the reader's own change detection off.
        for spec in rng.sample(specs, len(specs)):
            ingest(env, spec, rng, force=True)
        check(env, specs)
    finally:
        env.close()


@pytest.mark.parametrize("seed", range(12))
def test_more_than_eight_copies_still_count_the_best_whatever_the_order(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed * 17 + 9)
    specs = scenario(seed, files=12)
    env = make_env(tmp_path)
    try:
        write_final(env, specs)
        for spec in rng.sample(specs, len(specs)):
            ingest(env, spec, rng)
        check(env, specs, dropped=True)
    finally:
        env.close()
