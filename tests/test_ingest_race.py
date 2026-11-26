"""Concurrent ``ingest`` processes: one reads, the others exit 0 with ``already_running``. Real
processes, each recorded at launch and waited for; a hung one is killed by its exact pid only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from .ingesthelp import S1, S2, S3, Corpus, counted_tokens, messages, state_dir_of

HOLDER = textwrap.dedent(
    """
    import os, sys, time
    from session_audit.gateway import SystemGateway
    from session_audit.lease import IngestTarget, acquire
    from session_audit.store import open_store

    directory, ready, gate = sys.argv[1:4]
    store = open_store(SystemGateway(), directory)
    lease = acquire(store, IngestTarget())
    if lease is None:
        sys.exit(3)
    open(ready, "w").close()
    end = time.monotonic() + 60
    while not os.path.exists(gate) and time.monotonic() < end:
        time.sleep(0.02)
    lease.release()
    """
)


def launch_ingest() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-I", "-m", "session_audit", "ingest", "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def finish(proc: subprocess.Popen[str]) -> tuple[int, dict]:
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()  # the exact process this test started
        proc.communicate()
        raise
    assert proc.returncode == 0, err
    return proc.returncode, json.loads(out)


def test_processes_that_find_the_lease_held_report_already_running_then_one_reads(
    tmp_path: Path, _private_environment: Path
) -> None:
    corpus = Corpus(_private_environment)
    corpus.write_main(S1, messages("a", 30))
    directory = state_dir_of(_private_environment)
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    ready, gate = tmp_path / "ready", tmp_path / "gate"
    holder = subprocess.Popen(
        [sys.executable, "-I", str(script), str(directory), str(ready), str(gate)],
        env=dict(os.environ),
    )
    try:
        end = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert ready.exists(), "the holder did not take the lease"
        procs = [launch_ingest(), launch_ingest()]
        docs = [finish(p)[1] for p in procs]
        assert [d["already_running"] for d in docs] == [True, True]
        assert all(d["complete"] is False and d["files"]["new"] == 0 for d in docs)
        gate.write_text("go", encoding="utf-8")
        assert holder.wait(timeout=30) == 0
        _, doc = finish(launch_ingest())
        assert doc["already_running"] is False and doc["files"]["new"] == 1
    finally:
        gate.write_text("go", encoding="utf-8")
        if holder.poll() is None:
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()  # the exact process this test started
                holder.wait()


def test_three_simultaneous_ingests_read_every_line_once(_private_environment: Path) -> None:
    corpus = Corpus(_private_environment)
    for i, session in enumerate((S1, S2, S3)):
        corpus.write_main(session, messages(f"f{i}", 1500), project=f"p{i}")
    procs = [launch_ingest() for _ in range(3)]
    docs = [finish(p)[1] for p in procs]
    readers = [d for d in docs if not d["already_running"]]
    assert readers, "no process ran the ingest"
    for d in docs:
        if d["already_running"]:
            assert d["complete"] is False
            assert all(v == 0 for v in d["files"].values())
    assert sum(d["files"]["new"] for d in readers) == 3
    assert sum(d["lines"]["parsed"] for d in readers) == 4500
    assert counted_tokens(_private_environment) == 45_000
