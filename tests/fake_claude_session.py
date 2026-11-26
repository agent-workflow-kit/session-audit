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


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    """``document["a"]["b"] = value`` for ``path`` ``a.b`` (every parent must exist)."""
    *parents, last = path.split(".")
    target = document
    for part in parents:
        target = target[part]
    target[last] = value


def plan_doc(changes: dict[str, Any] | None = None) -> dict[str, Any]:
    """A valid ``start --dry-run --json`` plan of the retro launch: what claude-session prints for
    ``--kind audit --reads-third-party --no-implicit-done --tools Read Write Bash Skill ...`` with
    no config that changes a safety setting. ``changes`` maps a dotted path
    (``profile.env.inherit``, ``sandbox.mode``, ``origins.mcp``, ``tools``) to the value that
    replaces the default, so one test changes one safety setting. A path that does not exist
    yet (a new ``origins`` key) is added when its parent exists."""
    document: dict[str, Any] = {
        "dry_run": True,
        "csession_id": None,
        "run": 1,
        "fork": False,
        "forked_from": None,
        "launch_kind": "audit",
        "run_kind": "audit",
        "labels": ["kind=audit", "owner=session-audit", "retro=ra-aaaaaaaa"],
        "taints": True,
        "mode": "headless",
        "cwd": "/state/sessions/cs-aaaaaaaa/work",
        "repo": None,
        "skill": "session-audit:audit",
        "has_prompt": True,
        "input_files": ["digest.md", "manifest.json"],
        "implicit_done": False,
        "tools": ["Read", "Write", "Bash", "Skill"],
        "permission_profile": "file:0123456789abcdef",
        "timeout": 1200,
        "max_turns": 12,
        "actor": "auto:unknown",
        "override_escalation": False,
        "allow_tainted": False,
        "sandbox_lowered": False,
        "config_hash_changed": None,
        "profile": {
            "name": "audit",
            "builtin": True,
            "fallback": False,
            "permission_profile": "file:0123456789abcdef",
            "sandbox": "required",
            "sandbox_available": True,
            "sandbox_problems": [],
            "reads_third_party": True,
            "implicit_done": False,
            "setting_sources": [],
            "mcp": "none",
            "cwd": "session-dir",
            "env": {
                "inherit": False,
                "allow": ["PATH", "HOME", "USER", "LANG", "TERM"],
                "set_names": [],
                "path_prepend": [],
            },
            "required_env_names": ["PATH"],
            "path": "/usr/bin:/bin",
            "read_dirs": [],
            "write_dirs": [],
            "timeout": 1200,
            "interactive_timeout": None,
            "max_turns": 12,
            "auto_exit": True,
            "backstop": True,
            "notes_context": False,
            "model": None,
            "claude_args": [],
            "warnings": [],
        },
        "origins": {
            "sandbox": "flag-implied",
            "setting_sources": "flag-implied",
            "mcp": "flag-implied",
            "env.inherit": "flag-implied",
            "env.set_names": "builtin",
            "env.path_prepend": "builtin",
            "reads_third_party": "flag",
            "implicit_done": "flag",
            "tools": "flag",
            "permission_profile": "flag",
            "cwd": "flag",
            "notes_context": "builtin",
            "write_dirs": "builtin",
            "claude_args": "builtin",
            "model": "builtin",
            "timeout": "flag",
            "max_turns": "flag",
        },
        "settings": {},
        "claude_argv": ["claude", "--tools", "Read,Write,Bash,Skill"],
        "env_names": ["HOME", "LANG", "PATH", "TERM", "USER"],
        "plugin_dirs": [],
        "sandbox": {
            "mode": "required",
            "available": True,
            "problems": [],
            "allow_read": [],
            "allow_write": [],
            "deny_read": [],
            "deny_write": [],
        },
        "hooks": [],
        "would_refuse": None,
        "would_fail": None,
        "warnings": [],
    }
    for path, value in (changes or {}).items():
        _set_path(document, path, value)
    return document


def refusing_plan(reason: str = "live", **changes: Any) -> dict[str, Any]:
    """A plan whose launch would be refused (exit 20, ``would_refuse`` set)."""
    return plan_doc({"would_refuse": reason, **changes})


def failing_plan(end_reason: str = "sandbox_unavailable", **changes: Any) -> dict[str, Any]:
    """A plan whose launch would fail a validation (exit 12, ``would_fail`` set)."""
    return plan_doc({"would_fail": end_reason, **changes})


def list_doc(sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A valid ``list --json`` document (sessions must be complete records when given)."""
    return {"sessions": sessions or []}


# outcome -> (state, exit code, report status or None, a valid end reason): the pinned outcome rules
_OUTCOMES: dict[str, tuple[str, int, str | None, str]] = {
    "done": ("exited", 0, "done", "normal"),
    "blocked": ("exited", 13, "blocked", "normal"),
    "needs-input": ("exited", 13, "needs-input", "normal"),
    "failed": ("exited", 18, "failed", "normal"),
    "exited-unreported": ("exited-unreported", 14, None, "no_report"),
    "killed": ("killed", 15, None, "stopped"),
    "timeout": ("timeout", 16, None, "timeout"),
    "lost": ("lost", 17, None, "lost"),
}


def _report(outcome: str | None, *, artifact: bool = False) -> dict[str, Any] | None:
    status = _OUTCOMES[outcome][2] if outcome else None
    if status is None:
        return None
    return {
        "status": status,
        "implicit": False,
        "summary": None,
        "reason": None,
        "artifact": (
            {
                "name": "audit.json",
                "path": "/state/private/cs-aaaaaaab/runs/1/artifact/1/audit.json",
                "sha256": "0" * 64,
                "size": 2,
            }
            if artifact
            else None
        ),
        "reported_at": "2025-12-31T23:58:00Z",
    }


def _list_fields(
    csession_id: str,
    *,
    updated_at: str,
    launch_kind: str,
    labels: list[str],
    outcome: str | None,
    end_reason: str | None,
    repo: str | None,
) -> dict[str, Any]:
    """The fields ``list`` items and ``status`` share (all required by the pinned schemas)."""
    state = _OUTCOMES[outcome][0] if outcome else "working"
    final = outcome is not None
    return {
        "csession_id": csession_id,
        "created_at": "2025-12-31T23:00:00Z",
        "launch_kind": launch_kind,
        "run_kind": launch_kind,
        "state": state,
        "activity": None if final else "working",
        "needs_input_reason": None,
        "live": not final,
        "tainted": False,
        "tainted_at": None,
        "tainted_by_run": None,
        "forked_from": None,
        "labels": labels,
        "repo": repo,
        "mode": "headless",
        "current_run": 1,
        "pending": False,
        "expires_at": None,
        "updated_at": updated_at,
    }


def list_item(
    csession_id: str = "cs-aaaaaaab",
    *,
    updated_at: str = "2026-01-01T00:00:00Z",
    launch_kind: str = "work",
    labels: list[str] | None = None,
    outcome: str | None = "done",
    end_reason: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """One valid item of ``list --json`` (a headless run that ended with ``outcome``, or a live
    one when ``outcome`` is None)."""
    item = _list_fields(
        csession_id,
        updated_at=updated_at,
        launch_kind=launch_kind,
        labels=labels or [],
        outcome=outcome,
        end_reason=end_reason,
        repo=repo,
    )
    item.update(
        phase="session",
        last_outcome=outcome,
        last_end_reason=(end_reason or _OUTCOMES[outcome][3]) if outcome else None,
        exit_code=_OUTCOMES[outcome][1] if outcome else None,
        report_status=_OUTCOMES[outcome][2] if outcome else None,
    )
    return item


def status_doc(
    csession_id: str = "cs-aaaaaaab",
    *,
    updated_at: str = "2026-01-01T00:00:00Z",
    launch_kind: str = "work",
    labels: list[str] | None = None,
    claude_session_ids: list[str] | None = None,
    outcome: str | None = "done",
    end_reason: str | None = None,
    cost_usd: float | None = 0.5,
    num_turns: int | None = 3,
    skill: str | None = None,
    repo: str | None = None,
    sandbox_active: bool | None = False,
    artifact: bool = False,
) -> dict[str, Any]:
    """A valid ``status --json`` document. ``claude_session_ids`` are the Claude Code session
    ids the session recorded (the join keys)."""
    ids = claude_session_ids or []
    doc = _list_fields(
        csession_id,
        updated_at=updated_at,
        launch_kind=launch_kind,
        labels=labels or [],
        outcome=outcome,
        end_reason=end_reason,
        repo=repo,
    )
    final = outcome is not None
    doc.update(
        csession_dir=f"/state/sessions/{csession_id}",
        claude_session_id=ids[-1] if ids else None,
        claude_session_ids=[
            {
                "claude_session_id": cid,
                "run": 1,
                "source": "startup",
                "transcript_path": None,
                "seen_at": "2025-12-31T23:30:00Z",
            }
            for cid in ids
        ],
        runs=1,
        recent_events=[],
        warnings=[],
        run={
            "run": 1,
            "run_kind": launch_kind,
            "reads_third_party": False,
            "implicit_done": True,
            "mode": "headless",
            "cwd": None,
            "repo": repo,
            "model": None,
            "skill": skill,
            "has_prompt": True,
            "sandbox": "off",
            "sandbox_active": sandbox_active,
            "permission_profile": None,
            "decision_ids": [],
            "input_files": [],
            "pending": False,
            "expires_at": None,
            "started_at": "2025-12-31T23:00:00Z",
            "session_started_at": "2025-12-31T23:30:00Z",
            "ended_at": "2025-12-31T23:59:00Z" if final else None,
            "state": _OUTCOMES[outcome][0] if outcome else "working",
            "phase": "session",
            "message": None,
            "stop_requested_at": None,
            "warnings": [],
            "state_since": "2025-12-31T23:30:00Z",
            "wrapper_pid": None,
            "wrapper_alive": False,
            "child_pid": None,
            "child_alive": False,
            "backstop_used": False,
            "report": _report(outcome, artifact=artifact),
            "outcome": outcome,
            "end_reason": (end_reason or _OUTCOMES[outcome][3]) if outcome else None,
            "session_end_reason": None,
            "exit_code": _OUTCOMES[outcome][1] if outcome else None,
            "result_path": None,
            "cost_usd": cost_usd,
            "num_turns": num_turns,
            "is_error": None,
        },
    )
    return doc


def version_step(version: str = "0.45.3", *, exit: int = 0) -> dict[str, Any]:  # noqa: A002
    """A ``--version`` call printing what argparse prints for claude-session."""
    return step(f"claude-session {version}\n", exit=exit)


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


_LAUNCH_OUTCOMES = {"not-started": 12, "vetoed": 11, "escalated": 13}


def exec_doc(
    csession_id: str = "cs-aaaaaaab",
    outcome: str = "done",
    *,
    phase: str = "session",
    end_reason: str | None = None,
    run: int = 1,
    cost_usd: float | None = 0.5,
    num_turns: int | None = 3,
    artifact: bool = False,
) -> dict[str, Any]:
    """A valid ``exec --json`` outcome object. A session-phase ``outcome`` is one of done, blocked,
    needs-input, failed, exited-unreported, killed, timeout or lost; a launch-phase one is
    not-started, vetoed or escalated (``end_reason`` required)."""
    launch = phase == "launch"
    if launch:
        if end_reason is None:
            raise ValueError("a launch-phase outcome needs an end_reason")
        state, code, reason = "not-started", _LAUNCH_OUTCOMES[outcome], end_reason
        report = None
    else:
        state, code, _status, normal = _OUTCOMES[outcome]
        reason = end_reason or normal
        report = _report(outcome, artifact=artifact)
    return {
        "csession_id": csession_id,
        "run": run,
        "forked_from": None,
        "claude_session_id": None,
        "phase": phase,
        "outcome": outcome,
        "end_reason": reason,
        "exit_code": code,
        "state": state,
        "report": report,
        "session_started_at": None if launch else "2025-12-31T23:30:00Z",
        "cost_usd": None if launch else cost_usd,
        "num_turns": None if launch else num_turns,
        "message": None,
        "warnings": [],
    }


def stop_doc(csession_id: str = "cs-aaaaaaab", *, outcome: str = "killed") -> dict[str, Any]:
    """A valid ``stop --json`` document of a run that ended with ``outcome``."""
    state, code, _status, normal = _OUTCOMES[outcome]
    return {
        "csession_id": csession_id,
        "run": 1,
        "already_final": False,
        "canceled": False,
        "dry_run": False,
        "ended": True,
        "run_exit_code": code,
        "signals": [],
        "state": state,
        "outcome": outcome,
        "end_reason": "stopped" if outcome == "killed" else normal,
        "recorded_by_stop": False,
    }


def step(
    stdout: object = "",
    *,
    exit: int = 0,  # noqa: A002 (the key of the scenario format)
    stderr: str = "",
    sleep: float = 0.0,
    sleep_after: float = 0.0,
    outcome_file: str | None = None,
    ignore_term: bool = False,
    read_stdin: bool = False,
    pid_file: Path | None = None,
    schema: str | None = None,
    unvalidated: bool = False,
) -> dict[str, Any]:
    """One scripted call. ``schema`` overrides the schema chosen from the key. ``sleep_after``
    keeps the call alive after it printed; ``outcome_file`` is written to the path given after
    ``--outcome-file`` before anything is printed."""
    out: dict[str, Any] = {"stdout": stdout, "exit": exit}
    if stderr:
        out["stderr"] = stderr
    if sleep:
        out["sleep"] = sleep
    if sleep_after:
        out["sleep_after"] = sleep_after
    if outcome_file is not None:
        out["outcome_file"] = outcome_file
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


def _base_key(key: str) -> str:
    """``status cs-aaaaaaab`` (a step for one id) is a ``status`` step."""
    return "status" if key.startswith("status cs-") else key


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
        """Script the calls with ``key`` (see the fake's docstring), validating every step.
        ``status <csession id>`` scripts the calls of one id; plain ``status`` the others."""
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
        return SCHEMA_FOR_KEY[_base_key(key)]

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
        allowed = EXITS_FOR_KEY.get(_base_key(key))
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
            if code == "launch_failed" and _base_key(key) != "start":
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
    "exec_doc",
    "failing_plan",
    "forget_doc",
    "list_doc",
    "list_item",
    "outcome_doc",
    "plan_doc",
    "refusing_plan",
    "start_doc",
    "status_doc",
    "step",
    "stop_doc",
    "version_step",
]
