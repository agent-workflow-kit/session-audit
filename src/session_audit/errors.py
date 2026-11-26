"""The exit-code table and the closed error envelope (``schemas/cli/error.json``).

Every failure a command reports is a ``CliError``. Its ``code`` is the closed enum of the
envelope, its exit code comes from one table (``EXIT_CODES``), its ``message`` is a fixed string
of the tool (never input, transcript or model text) and its ``details`` is the closed object of
the schema. A ``CliError`` that would break the schema is a programming error and raises
``ValueError`` when it is built, so no invalid envelope is ever printed.

``hint`` is not part of the envelope: it goes to stderr only.
"""

from __future__ import annotations

import re
from typing import Any

from .gateway import CallResult
from .grammar import GrammarError

__all__ = [
    "DETAIL_KEYS",
    "EXIT_CODES",
    "REASONS",
    "CliError",
    "claude_session_failed",
    "from_grammar",
    "interrupt_exit",
    "usage",
]

# code -> the exit codes it may carry (the first is the default).
EXIT_CODES: dict[str, tuple[int, ...]] = {
    "internal": (1,),
    "usage": (2,),
    "config_invalid": (2,),
    "not_found": (10,),
    "vetoed": (11,),
    "failed": (12,),
    "needs_human": (13,),
    "session_ended": (14, 15, 16, 17, 18),  # passed through: never defaulted
    "refused": (20,),
    "ambiguous": (21,),
    "artifact_invalid": (22,),
}

DETAIL_KEYS = {
    "reason",
    "retryable",
    "matched",
    "limit",
    "retro_id",
    "id",
    "matches",
    "deleted",
    "field",
    "hook",
    "outcome",
    "end_reason",
}
_HOOKS = {"pre-retro", "post-retro", "post-attention"}

# code -> the reasons it may carry (a code that is absent has none, except usage, below).
REASONS: dict[str, set[str]] = {
    "usage": {"mode_unsupported", "confirmation_required"},
    "refused": {
        "scope_too_large",
        "daily_limit",
        "config_unsafe",
        "live_sessions_only",
        "empty_scope",
        "store_newer",
        "already_running",
        "in_use",
        "cron_block_malformed",
    },
    "failed": {
        "claude_session_missing",
        "claude_session_too_old",
        "store_unwritable",
        "no_readable_roots",
        "skill_not_found",
        "unsafe_plugin_dir",
        "launch_settings_mismatch",
        "session_not_started",
        "claude_session_error",
        "crontab_unavailable",
        "ingest_busy",
    },
    "artifact_invalid": {
        "no_artifact",
        "artifact_invalid",
        "identity_mismatch",
        "marker_mismatch",
        "no_valid_findings",
    },
    "vetoed": {"hook_vetoed"},
    "needs_human": {"hook_escalated", "session_blocked", "session_needs_input"},
}

# code -> the keys its details must hold.
REQUIRED: dict[str, set[str]] = {
    "refused": {"reason"},
    "failed": {"reason", "retryable"},
    "vetoed": {"reason"},
    "needs_human": {"reason"},
    "artifact_invalid": {"reason"},
    "session_ended": {"outcome", "retro_id"},
    "ambiguous": {"matches"},
    "not_found": {"id"},
}

_MESSAGE_MAX = 2048
_MATCHES_MAX = 20
_DELETED_MAX = 1000
_STRING_KEYS = {"retro_id", "id", "field", "outcome", "end_reason"}
_ID_MAX = 128
# The patterns of schemas/defs.json (error.details, retro_id, token); ``fullmatch`` ends at the
# true end of the string, as the schema's ``(?![\s\S])`` does (``$`` would allow a newline).
_PATTERNS: dict[str, re.Pattern[str]] = {
    "field": re.compile(r"--[a-z][a-z0-9-]{0,63}|[A-Z][A-Z0-9_]{0,63}|[a-z_][a-z0-9_.-]{0,63}"),
    "retro_id": re.compile(r"ra-[a-z2-7]{8}"),
    "outcome": re.compile(r"[a-z][a-z0-9_-]{0,63}"),
    "end_reason": re.compile(r"[a-z][a-z0-9_]{0,63}"),
}


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_details(code: str, details: dict[str, Any]) -> None:
    unknown = set(details) - DETAIL_KEYS
    if unknown:
        raise ValueError(f"details: keys outside the closed set: {sorted(unknown)}")
    missing = REQUIRED.get(code, set()) - set(details)
    if missing:
        raise ValueError(f"details: {code} requires {sorted(missing)}")
    if code == "session_ended" and "reason" in details:
        raise ValueError("details: session_ended carries no reason")
    if "reason" in details and details["reason"] not in REASONS.get(code, set()):
        raise ValueError(f"details: reason is not one of the reasons of {code}")
    for key in _STRING_KEYS & set(details):
        if not isinstance(details[key], str) or not details[key]:
            raise ValueError(f"details: {key} must be a non-empty string")
    for key, pattern in _PATTERNS.items():
        if key in details and not pattern.fullmatch(details[key]):
            raise ValueError(f"details: {key} does not match the schema pattern")
    if "id" in details and len(details["id"]) > _ID_MAX:
        raise ValueError("details: id is longer than 128 characters")
    if "retryable" in details and not isinstance(details["retryable"], bool):
        raise ValueError("details: retryable must be a boolean")
    for key in ("matched", "limit"):
        if key in details and not _is_count(details[key]):
            raise ValueError(f"details: {key} must be a non-negative integer")
    if "matches" in details:
        matches = details["matches"]
        if (
            not isinstance(matches, list)
            or len(matches) > _MATCHES_MAX
            or len(set(matches)) != len(matches)
            or not all(isinstance(m, str) and 1 <= len(m) <= _ID_MAX for m in matches)
        ):
            raise ValueError(
                "details: matches must be at most 20 distinct strings of 1 to 128 characters"
            )
    if "deleted" in details:
        deleted = details["deleted"]
        if (
            not isinstance(deleted, list)
            or len(deleted) > _DELETED_MAX
            or len(set(map(str, deleted))) != len(deleted)
            or not all(isinstance(d, str) and _PATTERNS["retro_id"].fullmatch(d) for d in deleted)
        ):
            raise ValueError("details: deleted must be at most 1000 distinct retro ids")
    if "hook" in details and details["hook"] not in _HOOKS:
        raise ValueError("details: hook is not a hook name")


def stderr_lines(error: CliError, *, prog: str = "session-audit") -> list[str]:
    """The lines an error prints on stderr (the envelope is separate, stdout only)."""
    head = f"{prog}: error: {error.message}"
    for key in ("field", "reason"):
        if key in error.details:
            head += f" ({error.details[key]})"
    lines = [head]
    matches = error.details.get("matches")
    if matches:
        lines.append("  matches: " + ", ".join(matches))
    if error.hint:
        lines.append(f"  hint: {error.hint}")
    return lines


class CliError(Exception):
    """A failure with its envelope. ``message`` must be a fixed string, never input text."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        if code not in EXIT_CODES:
            raise ValueError(f"code: unknown error code {code!r}")
        allowed = EXIT_CODES[code]
        if exit_code is None:
            if len(allowed) != 1:
                raise ValueError(f"exit: {code} needs an explicit exit code from {allowed}")
            exit_code = allowed[0]
        elif exit_code not in allowed:
            raise ValueError(f"exit: {exit_code} is not an exit code of {code}")
        if not isinstance(message, str) or not 1 <= len(message) <= _MESSAGE_MAX:
            raise ValueError("message: must be 1 to 2048 characters")
        details = {} if details is None else dict(details)
        _check_details(code, details)
        self.code = code
        self.exit_code = exit_code
        self.message = message
        self.details = details
        self.hint = hint

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def usage(
    message: str,
    *,
    field: str | None = None,
    reason: str | None = None,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
) -> CliError:
    """A usage error (exit 2) naming the flag at fault (``field``) or a named ``reason``."""
    merged: dict[str, Any] = dict(details or {})
    if field is not None:
        merged["field"] = field
    if reason is not None:
        merged["reason"] = reason
    return CliError("usage", message, details=merged, hint=hint)


def from_grammar(error: GrammarError) -> CliError:
    """Map a grammar failure straight into the envelope. Grammar messages are fixed text; a detail
    key outside the closed set is dropped, never printed."""
    details = {k: v for k, v in error.details.items() if k in DETAIL_KEYS}
    return CliError(
        error.code, error.message, exit_code=error.exit_code, details=details, hint=error.hint
    )


def claude_session_failed(result: CallResult) -> CliError:
    """A claude-session call that did not succeed, outside any row of the outcome mapping: exit 12
    ``claude_session_error``. A failed call (timeout, output limit, signal, spawn failure) has no
    exit code, so it can never be read as 0 (success) or 10 (not found); an exit code that is not
    mapped here is not trusted either."""
    if result.ok:
        raise ValueError("the call succeeded; there is nothing to map")
    return CliError(
        "failed",
        "a claude-session call failed",
        details={"reason": "claude_session_error", "retryable": False},
    )


def interrupt_exit(signum: int) -> int:
    """The exit code of a process ended by SIGHUP (129), SIGINT (130) or SIGTERM (143)."""
    return 128 + signum
