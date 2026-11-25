"""The gateway boundary: nothing under ``src/session_audit`` except ``gateway.py`` may start a
process, open or list files outside the store, read the clock or reach the network.

The scan is syntactic (the AST of every module), so it is a guard against the obvious ways around
the gateway, not a sandbox: a module that imports ``subprocess`` or calls ``open`` fails the test
and has to go through ``gateway.py``. The store's own SQLite file is opened by ``sqlite3``, which
is allowed. Aliasing an import (``import time as t``) is not followed; reviewers look for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "session_audit"
GATEWAY = "gateway.py"

FORBIDDEN_MODULES = frozenset(
    {
        "subprocess",
        "pty",
        "multiprocessing",
        "socket",
        "http.client",
        "urllib.request",
        "requests",
        "httpx",
    }
)
FORBIDDEN_BUILTIN_CALLS = frozenset({"open"})
FORBIDDEN_OS_CALLS = frozenset(
    {"open", "fdopen", "scandir", "listdir", "walk", "system", "popen", "fork", "forkpty"}
)
FORBIDDEN_OS_PREFIXES = ("exec", "spawn", "posix_spawn")
FORBIDDEN_METHODS = frozenset({"open", "read_text", "read_bytes", "iterdir", "glob", "rglob"})
CLOCK_CALLS = {
    "time": frozenset({"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "sleep"}),
    "datetime": frozenset({"now", "utcnow", "today"}),
    "date": frozenset({"today"}),
}


def violations(source: str, filename: str) -> list[str]:
    """What in ``source`` reaches around the gateway, as ``file:line: what`` strings."""
    found: list[str] = []

    def add(node: ast.AST, what: str) -> None:
        found.append(f"{filename}:{getattr(node, 'lineno', 0)}: {what}")

    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES or alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    add(node, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module in FORBIDDEN_MODULES or node.module.split(".")[0] in FORBIDDEN_MODULES:
                add(node, f"from {node.module} import ...")
            for alias in node.names:
                if f"{node.module}.{alias.name}" in FORBIDDEN_MODULES:
                    add(node, f"from {node.module} import {alias.name}")
                if alias.name in CLOCK_CALLS.get(node.module, ()):
                    add(node, f"from {node.module} import {alias.name} (clock)")
                if node.module == "os" and (
                    alias.name in FORBIDDEN_OS_CALLS or alias.name.startswith(FORBIDDEN_OS_PREFIXES)
                ):
                    add(node, f"from os import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTIN_CALLS:
                add(node, f"{func.id}() call")
            elif isinstance(func, ast.Attribute):
                owner = func.value.id if isinstance(func.value, ast.Name) else None
                if owner == "os" and (
                    func.attr in FORBIDDEN_OS_CALLS or func.attr.startswith(FORBIDDEN_OS_PREFIXES)
                ):
                    add(node, f"os.{func.attr}() call")
                elif owner in CLOCK_CALLS and func.attr in CLOCK_CALLS[owner]:
                    add(node, f"{owner}.{func.attr}() reads the clock")
                elif func.attr in FORBIDDEN_METHODS:
                    add(node, f".{func.attr}() call")
    return found


def scan(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).as_posix() == GATEWAY:
            continue
        found.extend(
            violations(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        )
    return found


def test_the_package_reaches_the_outside_only_through_the_gateway() -> None:
    assert scan(SOURCE_ROOT) == []


def test_a_deliberate_subprocess_import_outside_the_gateway_fails(tmp_path: Path) -> None:
    (tmp_path / "gateway.py").write_text("import subprocess\n", encoding="utf-8")
    (tmp_path / "sneaky.py").write_text("import subprocess\n", encoding="utf-8")
    assert scan(tmp_path) == ["sneaky.py:1: import subprocess"]


def test_the_gateway_itself_is_exempt(tmp_path: Path) -> None:
    (tmp_path / "gateway.py").write_text("import subprocess\nopen('x')\n", encoding="utf-8")
    assert scan(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import urllib.request",
        "from urllib import request\nfrom urllib.request import urlopen",
        "from http import client\nfrom http.client import HTTPSConnection",
        "import socket",
        "import multiprocessing",
        "open('transcript.jsonl')",
        "from pathlib import Path\nPath('t.jsonl').open()",
        "from pathlib import Path\nPath('t.jsonl').read_text()",
        "from pathlib import Path\nPath('t.jsonl').read_bytes()",
        "from pathlib import Path\nlist(Path('.').iterdir())",
        "from pathlib import Path\nlist(Path('.').rglob('*.jsonl'))",
        "import os\nos.open('t', os.O_RDONLY)",
        "import os\nos.fdopen(3)",
        "import os\nos.scandir('.')",
        "import os\nos.listdir('.')",
        "import os\nos.walk('.')",
        "import os\nos.system('true')",
        "import os\nos.popen('true')",
        "import os\nos.execv('/bin/true', ['true'])",
        "import os\nos.spawnv(0, '/bin/true', ['true'])",
        "import os\nos.posix_spawn('/bin/true', ['true'], {})",
        "import os\nos.fork()",
        "from os import system",
        "import time\ntime.time()",
        "import time\ntime.monotonic()",
        "import time\ntime.sleep(1)",
        "from time import sleep",
        "import datetime\nfrom datetime import datetime\ndatetime.now()",
        "from datetime import datetime\ndatetime.utcnow()",
        "from datetime import date\ndate.today()",
    ],
)
def test_each_way_around_the_gateway_is_caught(source: str) -> None:
    assert violations(source, "m.py"), source


@pytest.mark.parametrize(
    "source",
    [
        "import sqlite3\nsqlite3.connect('state.db')",
        "import os\nos.environ.get('HOME')\nos.getpid()\nos.path.join('a', 'b')",
        "from datetime import UTC, datetime\nx = datetime(2026, 1, 1, tzinfo=UTC)",
        "from pathlib import Path\nPath('a') / 'b'",
        "import json, re, hashlib, argparse, sys",
        "from . import gateway",
    ],
)
def test_ordinary_code_is_not_flagged(source: str) -> None:
    assert violations(source, "m.py") == []
