"""Store-size and speed measurement of ``ingest`` on a synthetic corpus (design.md, Live
verification: store size). Not part of the default suite; run it by hand:

    uv run python -m tests.measure_store_size --files 120 --lines 5000 --dir <scratch-dir>

It writes a corpus under ``<dir>/home/.claude/projects`` (every id and token count synthetic), runs
the real ``ingest`` command in process with a private HOME, prints one JSON line (files, lines,
events, corpus bytes, ``state.db`` bytes after a checkpoint, bytes per event, seconds, runs needed
and the budget) and deletes what it created unless ``--keep`` is given. ``<dir>`` must not exist
or must be empty (anything else is refused, never deleted); only its ``home`` subdirectory is
removed, then ``<dir>`` itself when that leaves it empty. Nothing outside ``<dir>`` is read or
written.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from session_audit.cli import main

from .eventhelp import compaction, message, tool_error, tool_use, turn


def write_corpus(root: Path, files: int, lines: int) -> int:
    """``files`` main transcripts of about ``lines`` lines each (messages with a tool call,
    tool errors, turn durations and a compaction now and then). Returns the corpus bytes."""
    total = 0
    for f in range(files):
        project = root / f"-work-project-{f % 12}"
        project.mkdir(parents=True, mode=0o700, exist_ok=True)
        out: list[bytes] = []
        for i in range(lines):
            t = i % 3000
            kind = i % 10
            tag = f"{f}-{i}"
            if kind in (0, 1, 2, 3, 4):
                out.append(message(f"msg-{tag}", 20 + i % 200, t=t, inp=50 + i % 90))
            elif kind in (5, 6):
                out.append(tool_use(f"msg-{tag}", f"toolu_{tag}", "Bash", out=10 + i % 30, t=t))
            elif kind == 7:
                out.append(tool_error(f"toolu_{f}-{i - 2}", t=t, uuid=f"err-{tag}"))
            elif kind == 8:
                out.append(turn(1000 + i % 5000, t=t, uuid=f"turn-{tag}"))
            else:
                out.append(
                    compaction(f"cmp-{tag}", t=t) if i % 500 == 9 else message(f"m2-{tag}", 5, t=t)
                )
        data = b"".join(out)
        (project / f"{uuid.UUID(int=f + 1)}.jsonl").write_bytes(data)
        total += len(data)
    return total


def run(files: int, lines: int, directory: Path, budget: str, keep: bool) -> dict[str, object]:
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise SystemExit(f"--dir {directory} exists and is not an empty directory; refusing it")
    home = directory / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True, mode=0o700)
    for sub in (".config", ".local/state", ".local/share", ".cache"):
        (home / sub).mkdir(parents=True, mode=0o700, exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    try:
        corpus_bytes = write_corpus(projects, files, lines)
        started = time.monotonic()
        runs = 0
        doc: dict[str, object] = {}
        while True:
            runs += 1
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(["ingest", "--json", "--budget", budget], stdin_is_tty=False)
            if code != 0:
                raise SystemExit(f"ingest exited {code}: {buffer.getvalue()[:300]}")
            doc = json.loads(buffer.getvalue())
            if doc["complete"] or runs >= 20:
                break
        seconds = time.monotonic() - started
        db = home / ".local" / "state" / "session-audit" / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        counted = conn.execute("SELECT count(*) FROM events WHERE counted = 1").fetchone()[0]
        conn.close()
        size = db.stat().st_size
        return {
            "files": files,
            "lines": files * lines,
            "corpus_bytes": corpus_bytes,
            "events": events,
            "counted_events": counted,
            "db_bytes": size,
            "db_bytes_per_event": round(size / max(events, 1), 1),
            "db_to_corpus_ratio": round(size / corpus_bytes, 4),
            "seconds": round(seconds, 1),
            "runs": runs,
            "budget": budget,
            "complete": doc["complete"],
        }
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if not keep:
            shutil.rmtree(home, ignore_errors=True)
            with contextlib.suppress(OSError):
                directory.rmdir()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=50)
    parser.add_argument("--lines", type=int, default=2000)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--budget", default="60m")
    parser.add_argument("--keep", action="store_true")
    ns = parser.parse_args()
    print(json.dumps(run(ns.files, ns.lines, ns.dir, ns.budget, ns.keep)))


if __name__ == "__main__":
    sys.exit(cli())
