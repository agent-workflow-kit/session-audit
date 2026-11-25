"""Drive the fake claude-session (``tests/fakebin/claude-session``) from a test.

    fake = FakeClaudeSession(tmp_path)
    fake.script("start", step(start_doc("cs-aaaaaaab")))
    result = SystemGateway().run([str(fake.program), "start", "--json"], env=fake.env(), timeout=10)
    assert fake.calls()[0]["argv"] == ["start", "--json"]

Every scripted document is validated against claude-session's pinned schemas
(``tests/schemas/claude-session``, see ``tests.schemas.PINNED_VERSION``) when the step is
written, and ``assert_outputs_valid`` validates what the fake actually printed. A step that is
deliberately not valid (a hostile or malformed output a test wants the code to refuse) must say
so with ``unvalidated=True``; plain text is only allowed for ``--version`` or then.

A step is also checked against what the real program can do (``_check_exit``): the exit must be
one the command ends with, an error ``code`` must come with its exit (usage 2, not_found 10,
refused 20, ambiguous 21, launch_failed, schema_newer and failed 12, internal 1), and an ``exec``
outcome's ``exit_code`` must equal the exit. The fake itself refuses calls that break claude-
session's argument grammar (see ``tests/fakebin/claude-session``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .conftest import FAKEBIN
from .schemas import PINNED_VERSION, validate

FAKE_PROGRAM = FAKEBIN / "claude-session"
UNSCRIPTED_EXIT = 99

# Which pinned schema the document printed for a key must satisfy. An exit with an
# ``{"error": ...}`` document is checked against the error envelope instead.
SCHEMA_FOR_KEY = {
    "start": "claude-session/cli/start.json",
    "start --dry-run": "claude-session/cli/exec-dry-run.json",
    "exec --dry-run": "claude-session/cli/exec-dry-run.json",
    "exec": "claude-session/cli/exec.json",
    "status": "claude-session/cli/status.json",
    "list": "claude-session/cli/list.json",
    "forget": "claude-session/cli/forget.json",
    "stop": "claude-session/cli/stop.json",
}
ERROR_SCHEMA = "claude-session/cli/error.json"

# The process exit of each error code (interface.md, Exit codes; error.json).
EXIT_FOR_CODE = {
    "usage": 2,
    "not_found": 10,
    "refused": 20,
    "ambiguous": 21,
    "launch_failed": 12,
    "schema_newer": 12,
    "failed": 12,
    "internal": 1,
}
# Exits that can come with an error envelope from any command.
_ANY_EXITS = frozenset({1, 2, 10, 20, 21})
# The exits each scripted command can end with (interface.md, Output by exit code).
EXITS_FOR_KEY = {
    "start": frozenset({0, 12}) | _ANY_EXITS,
    "start --dry-run": frozenset({0, 12, 20}) | _ANY_EXITS,
    "exec --dry-run": frozenset({0, 12, 20}) | _ANY_EXITS,
    "exec": frozenset({0, 11, 12, 13, 14, 15, 16, 17, 18}) | _ANY_EXITS,
    "status": frozenset({0, 12}) | _ANY_EXITS,
    "list": frozenset({0, 12}) | _ANY_EXITS,
    "forget": frozenset({0, 12}) | _ANY_EXITS,
    "stop": frozenset({0, 12}) | _ANY_EXITS,
}


def start_doc(csession_id: str = "cs-aaaaaaab", *, run: int = 1) -> dict[str, Any]:
    """A valid ``start --json`` document."""
    return {
        "csession_id": csession_id,
        "csession_dir": f"/state/sessions/{csession_id}",
        "argv": ["/usr/bin/claude-session", "exec", "--session-id", csession_id, "--run", str(run)],
        "run": run,
        "forked_from": None,
        "expires_at": "2026-01-01T00:10:00Z",
        "required_env_names": ["PATH"],
        "env_inherit": False,
        "warnings": [],
    }


def list_doc(sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A valid ``list --json`` document (sessions must be complete records when given)."""
    return {"sessions": sessions or []}


def forget_doc(*csession_ids: str, purged: bool = True) -> dict[str, Any]:
    """A valid ``forget --purge --json`` document."""
    return {
        "dry_run": False,
        "forgotten": [
            {
                "csession_id": cid,
                "forgotten_at": "2026-01-01T00:00:00Z",
                "purged": purged,
                "delete_after": "2026-01-31T00:00:00Z",
            }
            for cid in csession_ids
        ],
        "skipped": [],
    }


def error_doc(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A valid error envelope (``code`` one of the pinned schema's codes)."""
    return {"error": {"code": code, "message": message, **extra}}


def outcome_doc(
    csession_id: str = "cs-aaaaaaab", *, outcome: str = "timeout", exit_code: int = 16
) -> dict[str, Any]:
    """A valid ``exec --json`` outcome object for a run that timed out (exit 16). Other outcomes
    need their own state and report; pass them by editing the returned document."""
    return {
        "csession_id": csession_id,
        "run": 1,
        "forked_from": None,
        "claude_session_id": None,
        "phase": "session",
        "outcome": outcome,
        "end_reason": "timeout",
        "exit_code": exit_code,
        "state": "timeout",
        "report": None,
        "session_started_at": None,
        "cost_usd": None,
        "num_turns": None,
        "message": None,
        "warnings": [],
    }


def step(
    stdout: object = "",
    *,
    exit: int = 0,  # noqa: A002 (the key of the scenario format)
    stderr: str = "",
    sleep: float = 0.0,
    ignore_term: bool = False,
    read_stdin: bool = False,
    pid_file: Path | None = None,
    schema: str | None = None,
    unvalidated: bool = False,
) -> dict[str, Any]:
    """One scripted call. ``schema`` overrides the schema chosen from the key."""
    out: dict[str, Any] = {"stdout": stdout, "exit": exit}
    if stderr:
        out["stderr"] = stderr
    if sleep:
        out["sleep"] = sleep
    if ignore_term:
        out["ignore_term"] = True
    if read_stdin:
        out["stdin"] = True
    if pid_file is not None:
        out["pid_file"] = str(pid_file)
    if schema:
        out["schema"] = schema
    if unvalidated:
        out["unvalidated"] = True
    return out


class FakeClaudeSession:
    def __init__(self, directory: Path) -> None:
        self.program = FAKE_PROGRAM
        self.scenario_path = directory / "fake-claude-session.scenario.json"
        self.log_path = directory / "fake-claude-session.log"
        self._steps: dict[str, list[dict[str, Any]]] = {}
        self._write()

    def env(self) -> dict[str, str]:
        """The environment to give the fake: the test's own plus the two paths it needs."""
        return {
            **os.environ,
            "FAKE_CLAUDE_SESSION_SCENARIO": str(self.scenario_path),
            "FAKE_CLAUDE_SESSION_LOG": str(self.log_path),
        }

    def script(self, key: str, *steps: dict[str, Any]) -> None:
        """Script the calls with ``key`` (see the fake's docstring), validating every step."""
        for one in steps:
            self._check(key, one)
        self._steps[key] = list(steps)
        self._write()

    def calls(self) -> list[dict[str, Any]]:
        """The calls in the order they started. ``printed`` is None for a call that never got to
        print (killed while it slept); a call refused by the grammar has the key ``<usage>``."""
        if not self.log_path.exists():
            return []
        calls: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for line in self.log_path.read_text("utf-8").splitlines():
            record = json.loads(line)
            if "done" in record:
                by_id[record["done"]]["printed"] = record["printed"]
            elif "stdin_of" in record:
                by_id[record["stdin_of"]]["stdin"] = record["stdin"]
            else:
                calls.append(record)
                if "id" in record:
                    by_id[record["id"]] = record
        return calls

    def assert_outputs_valid(self) -> None:
        """Validate what the fake printed in every logged call against the pinned schemas."""
        counts: dict[str, int] = {}
        for call in self.calls():
            key = call["key"]
            index = counts.get(key, 0)
            counts[key] = index + 1
            steps = self._steps.get(key)
            if not steps:
                continue
            one = steps[min(index, len(steps) - 1)]
            if one.get("unvalidated") or key == "--version":
                continue
            printed = call["printed"]
            if printed is None:
                continue  # never got to print
            schema = self._schema_for(
                key, json.loads(printed["stdout"]), printed["exit"], one.get("schema")
            )
            validate(json.loads(printed["stdout"]), schema)

    # -- internals ---------------------------------------------------------------------

    def _write(self) -> None:
        scenario = {
            key: [
                {k: v for k, v in one.items() if k not in ("schema", "unvalidated")}
                for one in steps
            ]
            for key, steps in self._steps.items()
        }
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    @staticmethod
    def _schema_for(key: str, document: object, exit_code: int, override: str | None = None) -> str:
        if override:
            return override
        if isinstance(document, dict) and "error" in document and exit_code != 0:
            return ERROR_SCHEMA
        return SCHEMA_FOR_KEY[key]

    def _check(self, key: str, one: dict[str, Any]) -> None:
        if one.get("unvalidated") or key == "--version":
            return
        stdout = one.get("stdout")
        if isinstance(stdout, str):
            raise AssertionError(
                f"step for {key!r} prints text; pass unvalidated=True for a deliberate non-document"
            )
        validate(stdout, self._schema_for(key, stdout, one.get("exit", 0), one.get("schema")))
        self._check_exit(key, stdout, one.get("exit", 0))

    @staticmethod
    def _check_exit(key: str, stdout: object, exit_code: int) -> None:
        """Refuse a step the real program could not produce: an exit it never ends that command
        with, an error code with the wrong exit, an outcome whose ``exit_code`` is not the exit."""
        allowed = EXITS_FOR_KEY.get(key)
        if allowed is not None and exit_code not in allowed:
            raise AssertionError(
                f"step for {key!r}: exit {exit_code} is not one of {sorted(allowed)}"
            )
        if not isinstance(stdout, dict):
            return
        error = stdout.get("error")
        if isinstance(error, dict) and "code" in error:
            code = error["code"]
            if EXIT_FOR_CODE.get(code) != exit_code:
                raise AssertionError(
                    f"step for {key!r}: error code {code!r} goes with exit "
                    f"{EXIT_FOR_CODE.get(code)}, not {exit_code}"
                )
            if code == "launch_failed" and key != "start":
                raise AssertionError(f"step for {key!r}: launch_failed is only for start")
        elif key == "exec" and "exit_code" in stdout and stdout["exit_code"] != exit_code:
            raise AssertionError(
                f"step for 'exec': the outcome says exit_code {stdout['exit_code']} "
                f"but the step exits {exit_code}"
            )


__all__ = [
    "ERROR_SCHEMA",
    "FAKE_PROGRAM",
    "PINNED_VERSION",
    "FakeClaudeSession",
    "error_doc",
    "forget_doc",
    "list_doc",
    "outcome_doc",
    "start_doc",
    "step",
]
