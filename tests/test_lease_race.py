"""Two real processes race to take over one lease: exactly one wins. Each process is recorded at
launch, waited for, and (only if it hangs) killed by its exact pid."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from session_audit.lease import LeaseState, RetroTarget, observe, take_over
from session_audit.store import open_store

from .storehelp import ClockedGateway, state_path

CHILD = textwrap.dedent(
    """
    import os, sys, time
    from session_audit.gateway import SystemGateway
    from session_audit.lease import IngestTarget, RetroTarget, acquire, observe, take_over
    from session_audit.store import open_store

    directory, target_name, mode, ready, gate = sys.argv[1:6]
    gateway = SystemGateway()
    store = open_store(gateway, directory)
    target = IngestTarget() if target_name == "ingest" else RetroTarget(target_name)
    observed = observe(store, target)
    open(ready, "w").close()
    end = time.monotonic() + 30
    while not os.path.exists(gate):
        if time.monotonic() > end:
            print("TIMEOUT")
            sys.exit(3)
        time.sleep(0.001)
    if mode == "take":
        held = take_over(store, target, observed)
    else:
        held = acquire(store, target)
    print("WON" if held is not None else "LOST", flush=True)
    time.sleep(0.3)  # stay alive, holding, while the other finishes
    """
)


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def seed(directory: str, retro_id: str | None) -> None:
    """A retro (or the ingest lease) held by a dead process, not yet expired."""
    store = open_store(ClockedGateway(), directory)
    expires = int(time.time() * 1000) + 3_600_000
    with store.transaction() as conn:
        if retro_id is None:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('ingest_lease', ?)",
                (f"{dead_pid()}|ghost|{expires}",),
            )
        else:
            conn.execute(
                "INSERT INTO retros (id, key, state, created_at, lease_pid, lease_start, "
                "lease_expires_at) VALUES (?, ?, 'running', 0, ?, 'ghost', ?)",
                (retro_id, "k" * 64, dead_pid(), expires),
            )
    store.close()


def race(tmp_path: Path, directory: str, target: str, mode: str, round_no: int) -> list[str]:
    script = tmp_path / "racer.py"
    script.write_text(CHILD, encoding="utf-8")
    gate = tmp_path / f"gate{round_no}"
    procs: list[subprocess.Popen[str]] = []
    readies = []
    try:
        for index in range(2):
            ready = tmp_path / f"ready{round_no}-{index}"
            readies.append(ready)
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(script),
                        directory,
                        target,
                        mode,
                        str(ready),
                        str(gate),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=dict(os.environ),
                )
            )
        end = time.monotonic() + 30
        while not all(r.exists() for r in readies) and time.monotonic() < end:
            time.sleep(0.01)
        assert all(r.exists() for r in readies), "a racer did not get ready"
        gate.write_text("go", encoding="utf-8")
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
            results.append(out.strip())
        return results
    finally:
        for proc in procs:  # only the exact processes started here
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.mark.parametrize("mode", ["take", "acquire"])
@pytest.mark.parametrize("target", ["ra-abcdefgh", "ingest"])
def test_exactly_one_of_two_processes_takes_the_lease_over(
    tmp_path: Path, mode: str, target: str
) -> None:
    directory = state_path(tmp_path)
    for round_no in range(3):
        name = target if target == "ingest" else f"ra-abcdefg{round_no}"
        seed(directory, None if target == "ingest" else name)
        assert sorted(race(tmp_path, directory, name, mode, round_no)) == ["LOST", "WON"]


def test_the_cas_alone_decides_when_both_observed_the_same_dead_lease(tmp_path: Path) -> None:
    """Unit-level counterpart: both observe, then both take over; one gets rowcount 1."""
    directory = state_path(tmp_path)
    seed(directory, "ra-abcdefgh")
    target = RetroTarget("ra-abcdefgh")
    first = open_store(ClockedGateway(), directory)
    second = open_store(ClockedGateway(), directory)
    seen: LeaseState | None = observe(first, target)
    assert seen is not None
    assert take_over(first, target, seen) is not None
    assert take_over(second, target, seen) is None
