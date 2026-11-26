"""Hooks: the three configured hook lists, their closed payloads and how an entry's result is read
(interface, Hooks).

``run_hooks`` runs the entries of one hook in config order (drop-ins appended by the config
loader). Each entry is ``/bin/sh -c <cmd>`` through ``Gateway.run_hook``: its own process group,
the payload on stdin, ``SESSION_AUDIT_HOOK``, ``SESSION_AUDIT_ID`` and ``SESSION_AUDIT_OBJECT_TYPE``
added to the inherited environment, the entry's timeout (SIGTERM to the group, 5 s, SIGKILL), and
the whole group ended after the leader exits (no grandchild is left running).

- ``pre-retro``: the first entry that does not exit 0 decides and later entries do not run: exit 13
  escalates, any other exit code, a timeout, a death by signal or a shell that cannot start vetoes.
  An entry that exits 0 or 13 may print a decision (``decision_id``); output that does not parse
  is ignored with a ``hook_output_ignored`` warning, never a veto.
- ``post-retro`` and ``post-attention`` cannot undo anything: every entry runs, a failure is a
  ``hook_failed`` warning and never changes the caller's outcome. An entry with ``async: true`` is
  started on its own thread beside the rest and is waited for before ``run_hooks`` returns, so the
  process never exits with a hook still running; its lifetime is bounded by the same timeout and
  group kill. (A hook may start a daemon with ``setsid`` that leaves the group: that cannot be
  ended and is a residual risk of running a user's command at all.)

Nothing here holds a SQLite write lock; the callers run hooks with none held. Payloads are built by
the ``*_payload`` functions below from ids, counts and enums only (``labels`` is always ``[]``, no
``repo``, no transcript or model text). The time and random source are the gateway's.
"""

from __future__ import annotations

import base64
import enum
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .config import HookSettings, PostHook, PreHook
from .gateway import HOOK_STDOUT_LIMIT, CallResult, CallStatus, Gateway
from .grammar import GrammarError, parse_decision_id
from .harness import Invalid, strict_loads
from .textsafe import dumps_json

PRE_RETRO: Final = "pre-retro"
POST_RETRO: Final = "post-retro"
POST_ATTENTION: Final = "post-attention"
HOOK_NAMES: Final = (PRE_RETRO, POST_RETRO, POST_ATTENTION)

ESCALATE_EXIT: Final = 13
MAX_DECISION_IDS: Final = 32
STDOUT_CAP: Final = HOOK_STDOUT_LIMIT - 1  # 64 KiB are read; one byte more is excess
TOOL: Final = "session-audit"

WARN_OUTPUT_IGNORED: Final = "hook_output_ignored"
WARN_FAILED: Final = "hook_failed"


class Decision(enum.Enum):
    ALLOW = "allow"
    VETO = "veto"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class HookPayload:
    """One event: the closed JSON document and the three values of the environment. ``text`` is
    what goes on stdin."""

    hook: str
    object_type: str
    object_id: str
    document: Mapping[str, Any]

    def text(self) -> str:
        return dumps_json(self.document) + "\n"

    def env(self) -> dict[str, str]:
        return {
            "SESSION_AUDIT_HOOK": self.hook,
            "SESSION_AUDIT_ID": self.object_id,
            "SESSION_AUDIT_OBJECT_TYPE": self.object_type,
        }


@dataclass(frozen=True)
class EntryResult:
    """What one entry did. ``status`` is ``ok`` (exit 0), ``escalate`` (exit 13 of a ``pre-retro``
    entry), ``exit`` (any other exit code), ``timeout``, ``signaled`` or ``spawn_failed``."""

    hook: str
    index: int
    status: str
    exit_code: int | None = None
    run_async: bool = False


@dataclass(frozen=True)
class HookOutcome:
    """The result of one ``run_hooks``. ``decision`` is ``ALLOW`` for every hook but ``pre-retro``
    (and for a ``pre-retro`` with no entries). ``decision_ids`` are the flag ids then the ids the
    hooks returned (no duplicates); ``output_ignored`` counts the entries whose stdout was ignored
    (the log entry's ``hook_output_ignored``); ``warnings`` holds the tokens
    ``hook_output_ignored`` and ``hook_failed`` (internal markers, not schema values), each at most
    once."""

    decision: Decision = Decision.ALLOW
    decision_ids: tuple[str, ...] = ()
    output_ignored: int = 0
    entries: tuple[EntryResult, ...] = ()
    warnings: tuple[str, ...] = ()


# --- payloads ---------------------------------------------------------------------------------


def new_event_id(gateway: Gateway) -> str:
    """``ev-`` and 26 lowercase base32 characters from 16 random bytes (128 bits)."""
    raw = bytes.fromhex(gateway.token_hex(16))
    return "ev-" + base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def _base(
    gateway: Gateway, hook: str, object_type: str, object_id: str, fields: Mapping[str, Any]
) -> HookPayload:
    document: dict[str, Any] = {
        "tool": TOOL,
        "hook": hook,
        "object_type": object_type,
        "object_id": object_id,
        "event_id": new_event_id(gateway),
        "labels": [],
        **fields,
    }
    return HookPayload(hook, object_type, object_id, document)


def pre_retro_payload(
    gateway: Gateway,
    *,
    retro_id: str,
    scope: Mapping[str, Any],
    sessions: int,
    digest_bytes: int,
    items: int,
    skill: str,
    model: str | None,
    key: str,
    max_turns: int,
    timeout_seconds: int,
) -> HookPayload:
    return _base(
        gateway,
        PRE_RETRO,
        "retro",
        retro_id,
        {
            "scope": scope,
            "sessions": sessions,
            "digest_bytes": digest_bytes,
            "items": items,
            "skill": skill,
            "model": model,
            "key": key,
            "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
        },
    )


def post_retro_payload(
    gateway: Gateway,
    *,
    retro_id: str,
    state: str,
    reason: str | None,
    findings: int,
    candidates: int,
    cost_usd: float | None,
) -> HookPayload:
    return _base(
        gateway,
        POST_RETRO,
        "retro",
        retro_id,
        {
            "retro_id": retro_id,
            "state": state,
            "reason": reason,
            "findings": findings,
            "candidates": candidates,
            "cost_usd": cost_usd,
        },
    )


def retro_failed_payload(
    gateway: Gateway, *, retro_id: str, failure_reason: str, csession_id: str | None
) -> HookPayload:
    return _base(
        gateway,
        POST_ATTENTION,
        "retro",
        retro_id,
        {
            "reason": "retro_failed",
            "details": {
                "retro_id": retro_id,
                "failure_reason": failure_reason,
                "csession_id": csession_id,
            },
        },
    )


def ingest_failing_payload(gateway: Gateway, details: Mapping[str, Any]) -> HookPayload:
    """``details`` is ``ingest_failing_due``'s result (``consecutive_failures``,
    ``first_failed_at``, ``last_failed_at``)."""
    return _base(
        gateway,
        POST_ATTENTION,
        "ingest",
        "ingest",
        {
            "reason": "ingest_failing",
            "details": {
                "consecutive_failures": details["consecutive_failures"],
                "first_failed_at": details["first_failed_at"],
                "last_failed_at": details["last_failed_at"],
            },
        },
    )


# --- reading a result -------------------------------------------------------------------------


def parse_decision_output(stdout: str) -> list[str] | None:
    """The decision ids a ``pre-retro`` entry printed: ``[]`` for empty output or an object
    without ``decision_id``; None when the output is to be ignored (over 64 KiB, not one strict
    JSON object, a ``decision_id`` that is not a string or a list of valid ids, or more than 32).
    Other keys of the object are not read."""
    if len(stdout.encode("utf-8", "surrogatepass")) > STDOUT_CAP:
        return None
    if not stdout.strip():
        return []
    try:
        document = strict_loads(stdout)
    except Invalid:
        return None
    if not isinstance(document, dict):
        return None
    if "decision_id" not in document:
        return []
    value = document["decision_id"]
    candidates = [value] if isinstance(value, str) else value
    if not isinstance(candidates, list) or len(candidates) > MAX_DECISION_IDS:
        return None
    found: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            return None
        try:
            found.append(parse_decision_id(candidate, field="decision_id"))
        except GrammarError:
            return None
    return found


def _classify(hook: str, index: int, result: CallResult, run_async: bool) -> EntryResult:
    if result.status is CallStatus.EXITED:
        code = result.returncode
        if code == 0:
            status = "ok"
        elif code == ESCALATE_EXIT and hook == PRE_RETRO:
            status = "escalate"
        else:
            status = "exit"
        return EntryResult(hook, index, status, code, run_async)
    if result.status is CallStatus.TIMEOUT:
        return EntryResult(hook, index, "timeout", None, run_async)
    if result.status is CallStatus.SPAWN_FAILED:
        return EntryResult(hook, index, "spawn_failed", None, run_async)
    return EntryResult(hook, index, "signaled", None, run_async)  # a signal, or output limit


def entries_for(settings: HookSettings, hook: str) -> tuple[PreHook | PostHook, ...]:
    if hook == PRE_RETRO:
        return settings.pre_retro
    if hook == POST_RETRO:
        return settings.post_retro
    if hook == POST_ATTENTION:
        return settings.post_attention
    raise ValueError("unknown hook")


def _call(
    gateway: Gateway,
    entry: PreHook | PostHook,
    payload: HookPayload,
    cancel: threading.Event | None = None,
) -> CallResult:
    try:
        return gateway.run_hook(
            entry.cmd,
            timeout=min(entry.timeout.total_seconds(), 600.0),
            stdin=payload.text(),
            env_extra=payload.env(),
            cancel=cancel,
        )
    except Exception as exc:  # noqa: BLE001 - a hook never takes the command down
        return CallResult(CallStatus.SPAWN_FAILED, detail=type(exc).__name__)


def run_hooks(
    gateway: Gateway,
    settings: HookSettings,
    payload: HookPayload,
    *,
    flag_decision_ids: Sequence[str] = (),
    cancel: threading.Event | None = None,
) -> HookOutcome:
    """Run the entries configured for ``payload.hook`` and return what they decided. Never raises
    for what a hook does; the caller must not call it in a dry run or with a write lock held.

    ``cancel`` (set by the first signal of a retro run, ``interrupt.Signals.watch``) ends the group
    of the entry that runs (SIGTERM, 5 s, SIGKILL) and every later entry is skipped; the caller
    reads the signal, not the outcome. A ``pre-retro`` entry ended that way vetoes, which the
    caller discards because the retro is canceled."""
    hook = payload.hook
    entries = entries_for(settings, hook)
    if hook == PRE_RETRO:
        return _run_pre(gateway, entries, payload, flag_decision_ids, cancel)
    return _run_post(gateway, entries, payload, cancel)


def _run_pre(
    gateway: Gateway,
    entries: Sequence[PreHook | PostHook],
    payload: HookPayload,
    flag_ids: Sequence[str],
    cancel: threading.Event | None = None,
) -> HookOutcome:
    ids: list[str] = list(dict.fromkeys(flag_ids))
    results: list[EntryResult] = []
    ignored = 0
    decision = Decision.ALLOW
    for index, entry in enumerate(entries):
        call = _call(gateway, entry, payload, cancel)
        result = _classify(PRE_RETRO, index, call, False)
        results.append(result)
        if result.status not in ("ok", "escalate"):
            decision = Decision.VETO
            break
        found = parse_decision_output(call.stdout)
        if found is not None:
            merged = [*ids, *(i for i in dict.fromkeys(found) if i not in ids)]
            if len(merged) > MAX_DECISION_IDS:
                found = None
            else:
                ids = merged
        if found is None:
            ignored += 1
        if result.status == "escalate":
            decision = Decision.ESCALATE
            break
    return HookOutcome(
        decision,
        tuple(ids),
        ignored,
        tuple(results),
        (WARN_OUTPUT_IGNORED,) if ignored else (),
    )


def _run_post(
    gateway: Gateway,
    entries: Sequence[PreHook | PostHook],
    payload: HookPayload,
    external: threading.Event | None = None,
) -> HookOutcome:
    slots: list[EntryResult | None] = [None] * len(entries)
    threads: list[threading.Thread] = []
    # set on an interrupt (an exception here, or the caller's event): every running hook group is
    # ended
    cancel = external if external is not None else threading.Event()

    def work(index: int, entry: PostHook) -> None:
        slots[index] = _classify(
            payload.hook, index, _call(gateway, entry, payload, cancel), entry.run_async
        )

    try:
        for index, entry in enumerate(entries):
            assert isinstance(entry, PostHook)
            if entry.run_async:
                thread = threading.Thread(target=work, args=(index, entry), name=f"hook-{index}")
                thread.start()
                threads.append(thread)
            else:
                work(index, entry)
        for thread in threads:
            thread.join()  # bounded by the entry's timeout and the group kill
    except BaseException:
        # An interrupt: no hook outlives the process. Each started thread ends its group (TERM,
        # 5 s, KILL) once it sees the event; the joins are bounded by that.
        cancel.set()
        for thread in threads:
            thread.join()
        raise
    results = tuple(r for r in slots if r is not None)
    failed = any(r.status != "ok" for r in results)
    return HookOutcome(entries=results, warnings=(WARN_FAILED,) if failed else ())


def warning_text(outcome: HookOutcome, hook: str) -> list[str]:
    """Fixed stderr lines for an outcome (names, indexes and enums only; never hook output)."""
    lines: list[str] = []
    for entry in outcome.entries:
        if hook != PRE_RETRO and entry.status != "ok":
            lines.append(f"warning: {hook} hook entry {entry.index + 1} failed ({entry.status})")
    if outcome.output_ignored:
        lines.append(
            f"warning: {hook} hook output was ignored for {outcome.output_ignored} entr"
            f"{'y' if outcome.output_ignored == 1 else 'ies'} ({WARN_OUTPUT_IGNORED})"
        )
    return lines
