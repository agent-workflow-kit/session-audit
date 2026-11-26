"""The gateway boundary: nothing under ``src/session_audit`` except ``gateway.py`` may start a
process, open or list files outside the store, read the clock or reach the network.

The scan is syntactic (the AST of every module), so it is a guard against the obvious ways around
the gateway, not a sandbox: a module that imports ``subprocess`` or calls ``open`` fails the test
and has to go through ``gateway.py``. The store's own SQLite file is opened by ``sqlite3``, which
is allowed, and so are ``os.stat`` and ``Path.exists`` (reaching the store). Plain import aliases
(``import time as t``, ``from datetime import datetime as dt``) are resolved; an alias of an alias
(``a = time; a.time()``) is not, and reviewers look for it.
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
        "http.server",
        "urllib.request",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "smtplib",
        "ftplib",
        "ctypes",
    }
)
FORBIDDEN_OS_CALLS = frozenset(
    {"open", "fdopen", "scandir", "listdir", "walk", "system", "popen", "fork", "forkpty"}
)
FORBIDDEN_OS_PREFIXES = ("exec", "spawn", "posix_spawn")
SAFE_OPEN_USERS = frozenset({"commands/ingest.py"})
FORBIDDEN_METHODS = frozenset({"open", "read_text", "read_bytes", "iterdir", "glob", "rglob"})
# Dotted names (after import aliases are resolved) that are forbidden to call or to import.
FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "builtins.open",
        "io.open",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "concurrent.futures.ProcessPoolExecutor",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.datetime.fromtimestamp",
        "datetime.datetime.utcfromtimestamp",
        "datetime.date.today",
        "datetime.date.fromtimestamp",
        *(
            f"shutil.{name}"
            for name in ("copy", "copy2", "copyfile", "copyfileobj", "copytree", "move")
        ),
        *(
            f"time.{name}"
            for name in (
                "time",
                "time_ns",
                "monotonic",
                "monotonic_ns",
                "perf_counter",
                "perf_counter_ns",
                "process_time",
                "process_time_ns",
                "thread_time",
                "sleep",
                "localtime",
                "gmtime",
                "strftime",
                "ctime",
                "asctime",
            )
        ),
    }
)
DYNAMIC_IMPORTS = frozenset({"importlib.import_module", "importlib.__import__", "__import__"})


def _chain(node: ast.expr) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; None when the expression is not a plain dotted name."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        head = _chain(node.value)
        return None if head is None else [*head, node.attr]
    return None


def _aliases(tree: ast.AST) -> dict[str, list[str]]:
    """What each imported local name stands for (plain aliases only: ``import time as t``,
    ``from datetime import datetime as dt``; an alias of an alias is not followed)."""
    names: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name.split(".")
                else:
                    names[alias.name.split(".")[0]] = [alias.name.split(".")[0]]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = [*node.module.split("."), alias.name]
    return names


def _forbidden(dotted: list[str]) -> str | None:
    """Why the dotted name must not be used outside the gateway, or None."""
    for end in range(1, len(dotted) + 1):
        if ".".join(dotted[:end]) in FORBIDDEN_MODULES:
            return f"uses {'.'.join(dotted[:end])}"
    full = ".".join(dotted)
    if full in FORBIDDEN_NAMES:
        return f"{full}()"
    if len(dotted) == 2 and dotted[0] == "os":
        name = dotted[1]
        if name in FORBIDDEN_OS_CALLS or name.startswith(FORBIDDEN_OS_PREFIXES):
            return f"os.{name}()"
    return None


def violations(source: str, filename: str) -> list[str]:
    """What in ``source`` reaches around the gateway, as ``file:line: what`` strings."""
    found: list[str] = []
    tree = ast.parse(source, filename)
    aliases = _aliases(tree)

    def add(node: ast.AST, what: str) -> None:
        found.append(f"{filename}:{getattr(node, 'lineno', 0)}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES or alias.name in FORBIDDEN_MODULES:
                    add(node, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module in FORBIDDEN_MODULES or node.module.split(".")[0] in FORBIDDEN_MODULES:
                add(node, f"from {node.module} import ...")
            for alias in node.names:
                reason = _forbidden([*node.module.split("."), alias.name])
                if reason:
                    add(node, f"from {node.module} import {alias.name} ({reason})")
        elif isinstance(node, ast.Call):
            chain = _chain(node.func)
            if chain is not None:
                dotted = [*aliases.get(chain[0], [chain[0]]), *chain[1:]]
                reason = _forbidden(dotted)
                if reason:
                    add(node, reason)
                elif ".".join(dotted) in DYNAMIC_IMPORTS:
                    target = node.args[0] if node.args else None
                    if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                        add(node, f"{'.'.join(dotted)}() with a name that is not a literal")
                    elif _forbidden(target.value.split(".")):
                        add(node, f"{'.'.join(dotted)}({target.value!r})")
            # reader.Discovery.open, the safe open, is called only by the ingest command.
            sanctioned = chain == ["discovery", "open"] and filename in SAFE_OPEN_USERS
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in FORBIDDEN_METHODS
                and not sanctioned
            ):
                add(node, f".{node.func.attr}() call")
    return found


def python_files(root: Path) -> list[Path]:
    """The modules the scan covers: everything under ``root`` except the gateway."""
    return [p for p in sorted(root.rglob("*.py")) if p.relative_to(root).as_posix() != GATEWAY]


def scan(root: Path) -> list[str]:
    found: list[str] = []
    for path in python_files(root):
        found.extend(
            violations(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        )
    return found


def test_the_scan_covers_the_package() -> None:
    names = {p.name for p in python_files(SOURCE_ROOT)}
    assert {"cli.py", "__init__.py"} <= names and GATEWAY not in names


def test_the_package_reaches_the_outside_only_through_the_gateway() -> None:
    assert python_files(SOURCE_ROOT), "the scan found no module to check"
    assert scan(SOURCE_ROOT) == []


def test_a_deliberate_subprocess_import_outside_the_gateway_fails(tmp_path: Path) -> None:
    (tmp_path / "gateway.py").write_text("import subprocess\n", encoding="utf-8")
    (tmp_path / "sneaky.py").write_text("import subprocess\n", encoding="utf-8")
    assert scan(tmp_path) == ["sneaky.py:1: import subprocess"]


def test_the_safe_open_exemption_is_scoped_to_the_ingest_command(tmp_path: Path) -> None:
    call = "discovery.open(candidate)\n"
    assert violations(call, "commands/ingest.py") == []
    assert violations(call, "commands/other.py") == ["commands/other.py:1: .open() call"]
    assert violations(call, "elsewhere.py") == ["elsewhere.py:1: .open() call"]


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
        "import datetime\ndatetime.datetime.now()",
        "import datetime\ndatetime.datetime.utcnow()",
        "import datetime\ndatetime.date.today()",
        "import datetime\ndatetime.datetime.fromtimestamp(0)",
        "from datetime import datetime\ndatetime.fromtimestamp(0)",
        "from datetime import datetime as dt\ndt.now()",
        "import datetime as d\nd.datetime.now()",
        "import datetime as d\nd.date.today()",
        "import time\ntime.localtime()",
        "import time\ntime.strftime('%H')",
        "import time\ntime.process_time()",
        "import time as t\nt.time()",
        "import time as t\nt.sleep(1)",
        "from time import time as now",
        "import os as o\no.system('true')",
        "import os as o\no.listdir('.')",
        "from os import listdir as ls",
        "import subprocess as sp\nsp.run(['true'])",
        "import asyncio\nasyncio.create_subprocess_exec('true')",
        "import asyncio\nasyncio.create_subprocess_shell('true')",
        "import asyncio as a\na.create_subprocess_exec('true')",
        "from asyncio import create_subprocess_exec",
        "import importlib\nimportlib.import_module('subprocess')",
        "import importlib as il\nil.import_module('socket')",
        "from importlib import import_module\nimport_module('http.client')",
        "importlib = None\n__import__('subprocess')",
        "import importlib\nimportlib.import_module(name)",
        "import ctypes",
        "from ctypes import CDLL",
        "import urllib3",
        "import aiohttp",
        "import smtplib",
        "import ftplib",
        "import http.server",
        "from http import server",
        "import urllib\nurllib.request.urlopen('x')",
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
        "import shutil\nshutil.copy('a', 'b')",
        "import shutil\nshutil.copy2('a', 'b')",
        "import shutil\nshutil.copyfile('a', 'b')",
        "import shutil\nshutil.copytree('a', 'b')",
        "import shutil\nshutil.move('a', 'b')",
        "import shutil as sh\nsh.move('a', 'b')",
        "from shutil import copy",
        "import builtins\nbuiltins.open('x')",
        "import io\nio.open('x')",
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
        "import os\nos.stat('s')\nos.path.exists('s')\nos.lstat('s')",
        "from pathlib import Path\nPath('s').exists()\nPath('s').is_dir()\nPath('s').stat()",
        "import shutil\nshutil.which('git')",
        "import time as t\nt.struct_time",
        "import importlib\nimportlib.import_module('json')",
        "import datetime as d\nd.datetime(2026, 1, 1, tzinfo=d.UTC)",
        "import asyncio\nasyncio.run(main())",
    ],
)
def test_ordinary_code_is_not_flagged(source: str) -> None:
    assert violations(source, "m.py") == []


def test_discovery_open_is_the_one_sanctioned_open_method(tmp_path: Path) -> None:
    (tmp_path / "gateway.py").write_text("", encoding="utf-8")
    (tmp_path / "commands").mkdir()
    allowed = tmp_path / "commands" / "ingest.py"
    allowed.write_text("discovery.open(candidate)\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("other.open(candidate)\n", encoding="utf-8")
    assert scan(tmp_path) == ["bad.py:1: .open() call"]
