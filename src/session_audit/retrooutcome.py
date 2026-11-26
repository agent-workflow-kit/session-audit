"""What claude-session prints for a retro launch, parsed strictly, and the Outcome mapping
(interface, Retro launch items 3, 6 and 7, Outcome mapping; issue #26).

Everything claude-session prints is untrusted data. Documents are parsed with ``strict_loads``
(no duplicate keys, no ``NaN`` or ``Infinity``, a depth the parser bounds; the gateway already
caps the size), every field the tool reads is checked by type and grammar, and a document that
fails is ``Invalid``: the caller maps that to ``claude_session_error`` (never exit 1, never a
crash).
No text of a document (messages, hints, warnings) is kept: only ids, numbers and tokens.

``map_exec`` is the Outcome mapping table for the adopting ``exec``: the rows are tried in the
order of the interface and the first that matches wins. The two ``start`` rows are ``map_start``.
The pure functions here touch no process, no clock and no store.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

from . import errors
from .gateway import CallResult
from .grammar import is_csession_id
from .harness import Invalid, strict_loads
from .launchplan import project_dir_name, retryable

__all__ = [
    "CSID_TOKEN",
    "SIGNAL_EXITS",
    "Collected",
    "Mapped",
    "Outcome",
    "Started",
    "launch_failed_id",
    "map_exec",
    "map_start",
    "parse_forget",
    "parse_outcome",
    "parse_start",
    "parse_status",
    "single_csession_id",
    "validated_session",
]

INT64_MAX = (1 << 63) - 1
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}", re.ASCII)
_END_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_PROJECT_DIR = re.compile(r"[A-Za-z0-9-]{1,200}", re.ASCII)
CSID_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])cs-[a-z2-7]{8}(?![A-Za-z0-9_-])", re.ASCII)
SIGNAL_EXITS = (129, 130, 143)
_LAUNCH_EXITS = (11, 12, 13)
_SESSION_FAILED_OUTCOME = {14: "exited-unreported", 16: "timeout", 17: "lost", 18: "failed"}


# --- small checks -----------------------------------------------------------------------------


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Invalid("not an object")
    return value


def _need(document: dict[str, Any], key: str) -> Any:
    if key not in document:
        raise Invalid("missing field")
    return document[key]


def _integer(value: Any, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= INT64_MAX:
        raise Invalid("not an integer in range")
    return value


def _optional_integer(value: Any) -> int | None:
    return None if value is None else _integer(value)


def _optional_cost(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid("not a number")
    try:
        number = float(value)
    except OverflowError:
        raise Invalid("not a finite non-negative number") from None
    if not math.isfinite(number) or number < 0 or number >= 1e12:
        raise Invalid("not a finite non-negative number")
    return number


def _optional_token(value: Any, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Invalid("not a token")
    return value


# --- start ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Started:
    """The fields of ``start --json`` the tool acts on."""

    csession_id: str
    csession_dir: str
    run: int
    argv: tuple[str, ...]


def parse_start(text: str) -> Started:
    """The ``start --json`` document of exit 0, or ``Invalid``."""
    document = _object(strict_loads(text))
    csid = _need(document, "csession_id")
    directory = _need(document, "csession_dir")
    argv = _need(document, "argv")
    if not is_csession_id(csid) or not isinstance(directory, str):
        raise Invalid("bad id or directory")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise Invalid("bad argv")
    return Started(csid, directory, _integer(_need(document, "run"), 1), tuple(argv))


def launch_failed_id(text: str) -> tuple[str, str, int | None] | None:
    """``(csession_id, end_reason, run)`` of the ``launch_failed`` error envelope of ``start``
    (exit 12), or None for any other document (a different code, a bad shape, a bad id)."""
    try:
        document = _object(strict_loads(text))
        error = _object(_need(document, "error"))
        if error.get("code") != "launch_failed":
            return None
        csid = error.get("csession_id")
        end_reason = _optional_token(error.get("end_reason"), _END_REASON)
        run = error.get("run")
        if not is_csession_id(csid) or end_reason is None:
            return None
        assert isinstance(csid, str)
        return csid, end_reason, run if isinstance(run, int) and not isinstance(run, bool) else None
    except Invalid:
        return None


def single_csession_id(*texts: str) -> str | None:
    """The one distinct claude-session id token in ``texts`` (a ``start`` that failed in some
    other way may still have printed its id), or None when there is none or more than one."""
    found = {m for text in texts for m in CSID_TOKEN.findall(text)}
    return next(iter(found)) if len(found) == 1 else None


def validated_session(
    csid: str, csession_dir: str, sessions_root: str
) -> tuple[str, str, str] | None:
    """``(csid, csession_dir, project_dir)`` when the id, the directory and the transcript project
    directory name of the session are well formed, else None. Nothing is stored, forgotten or
    stopped unless this returns a value: the id has the claude-session grammar, the directory is
    exactly ``sessions_root/<id>`` (ASCII, no ``..``, so it lies under the root), and the project
    directory name of ``<dir>/work`` matches ``^[A-Za-z0-9-]{1,200}$``."""
    if not is_csession_id(csid) or not isinstance(csession_dir, str):
        return None
    if not csession_dir.isascii() or "\x00" in csession_dir or ".." in csession_dir.split("/"):
        return None
    if not sessions_root.startswith("/") or csession_dir != os.path.join(sessions_root, csid):
        return None
    project = project_dir_name(csession_dir + "/work")
    if not _PROJECT_DIR.fullmatch(project):
        return None
    return csid, csession_dir, project


# --- exec -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The fields of the outcome object of ``exec --json`` the tool acts on."""

    csession_id: str
    run: int
    phase: str
    outcome: str | None
    end_reason: str | None
    exit_code: int
    cost_usd: float | None
    num_turns: int | None


def parse_outcome(text: str, csid: str, run: int) -> Outcome:
    """The outcome of ``exec`` for session ``csid`` and run ``run``, or ``Invalid`` (also for an
    outcome of another session or run, or an exit code the pinned rules never pair with its
    outcome)."""
    document = _object(strict_loads(text))
    if _need(document, "csession_id") != csid or _need(document, "run") != run:
        raise Invalid("not this run")
    phase = _need(document, "phase")
    if phase not in ("launch", "session"):
        raise Invalid("bad phase")
    outcome = _optional_token(_need(document, "outcome"), _TOKEN)
    end_reason = _optional_token(_need(document, "end_reason"), _END_REASON)
    exit_code = _integer(_need(document, "exit_code"))
    if exit_code > 255:
        raise Invalid("bad exit code")
    return Outcome(
        csid,
        run,
        phase,
        outcome,
        end_reason,
        exit_code,
        _optional_cost(_need(document, "cost_usd")),
        _optional_integer(_need(document, "num_turns")),
    )


@dataclass(frozen=True)
class Collected:
    """What ``status --json`` gives the retro (Retro launch item 7). ``report`` is the run's
    report object as parsed (the artifact check, #29, reads it); never printed or stored here."""

    cost_usd: float | None
    num_turns: int | None
    sandbox_active: bool | None
    outcome: str | None
    report: dict[str, Any] | None


def parse_status(text: str, csid: str) -> Collected:
    """The ``run`` of a ``status --json`` document of ``csid``, or ``Invalid``."""
    document = _object(strict_loads(text))
    if _need(document, "csession_id") != csid:
        raise Invalid("not this session")
    run = _object(_need(document, "run"))
    active = _need(run, "sandbox_active")
    if active is not None and not isinstance(active, bool):
        raise Invalid("sandbox_active is not a boolean")
    report = run.get("report")
    if report is not None and not isinstance(report, dict):
        raise Invalid("report is not an object")
    return Collected(
        _optional_cost(_need(run, "cost_usd")),
        _optional_integer(_need(run, "num_turns")),
        active,
        _optional_token(_need(run, "outcome"), _TOKEN),
        report,
    )


def parse_forget(text: str, csid: str) -> bool:
    """True when the ``forget --json`` document lists ``csid`` as forgotten (a forget object, not
    an error envelope, whose ``forgotten`` array holds the id)."""
    try:
        document = _object(strict_loads(text))
        forgotten = _need(document, "forgotten")
        if not isinstance(forgotten, list):
            return False
        return any(isinstance(row, dict) and row.get("csession_id") == csid for row in forgotten)
    except Invalid:
        return False


# --- the Outcome mapping ----------------------------------------------------------------------


@dataclass(frozen=True)
class Mapped:
    """One row of the Outcome mapping: the retro's state and reason and the command's exit.
    ``state`` is ``done`` only for the row that hands the retro to the artifact check
    (``validate`` is then true; the check decides ``done`` or ``failed``)."""

    state: str
    reason: str | None
    exit_code: int
    outcome: str | None = None  # claude-session's outcome token (``details.outcome``)
    end_reason: str | None = None
    retryable: bool = False
    validate: bool = False

    def error(self, retro_id: str) -> errors.CliError | None:
        """The error the command ends with, or None for exit 0 and for a canceled retro (exit
        by signal: no envelope)."""
        if self.state in ("done", "canceled"):
            return None
        if self.state == "escalated":
            return errors.CliError(
                "needs_human",
                "the retro session needs a human",
                details={"reason": self.reason, "retro_id": retro_id},
            )
        if self.reason == "session_failed":
            return errors.CliError(
                "session_ended",
                "the retro session ended without a finished retro",
                exit_code=self.exit_code,
                details={"outcome": self.outcome, "retro_id": retro_id},
            )
        details: dict[str, Any] = {
            "reason": self.reason,
            "retryable": self.retryable,
            "retro_id": retro_id,
        }
        if self.end_reason is not None:
            details["end_reason"] = self.end_reason
        message = (
            "the retro session did not start"
            if self.reason == "session_not_started"
            else "a claude-session call failed"
        )
        return errors.CliError("failed", message, details=details)


def _call_error() -> Mapped:
    return Mapped("failed", "claude_session_error", 12)


def map_start(
    result: CallResult, *, launch_failed: tuple[str, str, int | None] | None, deadline: bool = False
) -> Mapped:
    """The ``start`` rows. ``launch_failed`` is ``launch_failed_id`` of the output of an exit 12.
    Any other result (exit 1, 2, 20, unparseable, a timeout, a signal) is ``claude_session_error``;
    exit 0 is not mapped here (the caller continues to the adopting ``exec``)."""
    if deadline:
        return _call_error()
    if result.exit_is(12) and launch_failed is not None:
        end_reason = launch_failed[1]
        return Mapped(
            "failed",
            "session_not_started",
            12,
            end_reason=end_reason,
            retryable=retryable(end_reason),
        )
    return _call_error()


def map_exec(
    result: CallResult,
    outcome: Outcome | None,
    *,
    deadline: bool = False,
    forwarded: int | None = None,
) -> Mapped:
    """The ``exec`` rows, in the order of the interface (the first match wins).

    ``deadline``: the tool's own deadline ended the call. ``forwarded``: the signal number the
    tool forwarded to the exec child (set by the signal handling of #28; None here today).
    ``outcome`` is None when the output (and the outcome file) did not parse."""
    if deadline:  # row 1
        return _call_error()
    code = result.returncode if result.exited else None
    parsed = outcome is not None and code is not None and outcome.exit_code == code
    if forwarded is not None and (
        code == 15
        or (parsed and outcome is not None and outcome.end_reason == "launch_interrupted")
    ):  # row 2
        return Mapped("canceled", "interrupted", 128 + forwarded)
    if not parsed or outcome is None or code is None:
        return _call_error()  # the last row: an exit or an outcome outside the table
    if outcome.phase == "launch" and code in _LAUNCH_EXITS:  # row 5
        end_reason = outcome.end_reason
        return Mapped(
            "failed",
            "session_not_started",
            12,
            end_reason=end_reason,
            retryable=end_reason is not None and retryable(end_reason),
        )
    if outcome.phase != "session":
        return _call_error()
    if code == 0 and outcome.outcome == "done":  # row 6
        return Mapped("done", None, 0, outcome="done", validate=True)
    if code == 13 and outcome.outcome in ("blocked", "needs-input"):  # row 7
        reason = "session_blocked" if outcome.outcome == "blocked" else "session_needs_input"
        return Mapped("escalated", reason, 13, outcome=outcome.outcome)
    if code in _SESSION_FAILED_OUTCOME and outcome.outcome == _SESSION_FAILED_OUTCOME[code]:
        return Mapped("failed", "session_failed", code, outcome=outcome.outcome)  # row 8
    if code == 15 and outcome.outcome == "killed":  # row 9
        return Mapped("failed", "session_failed", 15, outcome="killed")
    return _call_error()  # row 10
