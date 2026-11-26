"""The claude-session half of ``cleanup(rs)`` (Collect and cleanup; issue #26).

``forget_session`` is exactly the first block of the interface's ``cleanup``: ``forget --purge``
(with ``stop`` and a second ``forget`` on exit 20) and the forgotten time set only on exit 10 or
on a forget object (exit 0 or 12) whose ``forgotten`` list names the id; an error envelope, exit
20 or a timeout leave it unforgotten. It acts only on an id the caller validated and recorded
(``retrooutcome.validated_session``): the tool never runs ``forget`` or ``stop`` on any other id.

TODO(#27): the rest of ``cleanup(rs)``: the purge check of both claude-session directories, the
removal of the transcript project directory and the ``session-env`` entries, ``delete_local``
and the ``cleanup_state`` transition (``pending``, ``done``, ``incomplete``, ``skipped``), and the
retries by housekeeping. Until then the ``retro_sessions`` row stays ``pending``, which is what
#27's housekeeping retries (it skips the forget once ``forgotten_at`` is set).
"""

from __future__ import annotations

from .command import Context
from .gateway import CallResult
from .lease import Lease
from .retrooutcome import parse_forget
from .store import Store

__all__ = ["CALL_TIMEOUT", "OUTPUT_LIMIT", "call", "forget_session", "stop_session"]

CALL_TIMEOUT = 60.0  # list, status, forget and stop (Retro launch item 6)
OUTPUT_LIMIT = 1024 * 1024


def call(ctx: Context, *argv: str, timeout: float = CALL_TIMEOUT) -> CallResult:
    """One claude-session call: its own process group, a timeout, a capped output."""
    return ctx.gateway.run(
        [ctx.settings.harness.claude_session_cmd, *argv], timeout=timeout, stdout_limit=OUTPUT_LIMIT
    )


def stop_session(ctx: Context, csession_id: str) -> CallResult:
    return call(ctx, "stop", csession_id, "--grace", "5s", "--json")


def _forget(ctx: Context, csession_id: str) -> tuple[CallResult, bool]:
    result = call(ctx, "forget", csession_id, "--purge", "--json")
    gone = result.exit_is(10) or (
        result.exit_is(0, 12) and parse_forget(result.stdout, csession_id)
    )
    return result, gone


def forget_session(ctx: Context, store: Store, held: Lease, csession_id: str) -> bool:
    """Forget (and purge) the recorded session; True when it is now forgotten. Sets
    ``retro_sessions.forgotten_at`` (once) under the retro's lease."""
    result, gone = _forget(ctx, csession_id)
    if result.exit_is(20):  # still live: stop it, then forget again
        stop_session(ctx, csession_id)
        _result, gone = _forget(ctx, csession_id)
    if not gone:
        return False
    now = int(ctx.gateway.now().timestamp() * 1000)
    with store.transaction() as conn:
        held.check_still_holder(conn)
        conn.execute(
            "UPDATE retro_sessions SET forgotten_at = ? "
            "WHERE csession_id = ? AND forgotten_at IS NULL",
            (now, csession_id),
        )
    return True
