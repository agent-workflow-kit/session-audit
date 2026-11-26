"""``collect`` and ``cleanup(rs)`` (Collect and cleanup; issues #26 and #27).

``collect(ctx, csid)`` is the ``status`` call after the adopting ``exec`` returned (never in
recovery): ``Collected`` or None when the call or the document fails.

``cleanup(ctx, store, row, guard)`` is the interface's idempotent step list over one
``retro_sessions`` row (``SessionRow``); housekeeping repeats it until it is ``done``. The steps,
in order (a crash between two of them is repeated from the top; every step is observed, never
assumed)::

    0 check   the row's csession id, project directory name and retro id are well formed, the
              project directory ends with ``-<id>-work``, the session directory is named by the
              id and the retro's own row (when it has one) records this id, else the row is
              ``skipped`` and nothing is touched (a planted or damaged row)
    1 verify  (housekeeping only, forgotten time unset) ``status``: a session without the tool's
              owner and retro labels is ``skipped`` and never forgotten; a call that fails defers
    2 forget  ``forget --purge`` (``stop`` and a second ``forget`` on exit 20); the forgotten time
              is set ONLY on exit 10 or a forget object naming the id; anything else stays pending
    3 purge   neither claude-session directory exists any more (observed with ``lstat``; never
              removed here: they are claude-session's)
    4 files   (``retro.remove_run_transcripts``) per transcript root, the retro's project
              directory found by its EXACT name: the ``session-env/<id>`` entries of the Claude
              Code session ids it holds, then the project directory itself, both with
              ``removal.remove_tree`` (descriptors, never a symlink followed, another owner or
              device left in place, 32 levels at most)
    5 local   ``retrofiles.delete_local`` (prompt and outcome file of the retro)
    6 state   ``pending`` when something could not be removed, ``done`` when claude-session's
              directories are gone too, else ``incomplete`` (claude-session's ``reconcile``
              finishes the purge later; a repeat ``forget`` answering 10, or a ``forget`` that
              reports ``purged: false``, never turns this into ``done``)

The session-env entries go before the project directory: the ids are read from the project
directory, so removing it first would lose them to a crash in between.

``forget`` and ``stop`` run only on an id the tool recorded for its own retro (the row), and
housekeeping checks the labels first: the tool never forgets any other session.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat as statmod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from . import retrofiles
from .command import Context
from .config import home_dir
from .gateway import CallResult, DirHandle, Gateway
from .grammar import is_csession_id
from .harness import Invalid
from .reader import open_root_checked
from .removal import MAX_ENTRIES, is_component, remove_tree
from .retrooutcome import Collected, parse_forget, parse_recovery_status, parse_status
from .store import Store

__all__ = [
    "CALL_TIMEOUT",
    "OUTPUT_LIMIT",
    "Guard",
    "SessionRow",
    "call",
    "cleanup",
    "collect",
    "forget_session",
    "load_session_row",
    "stop_session",
]

CALL_TIMEOUT = 60.0  # list, status, forget and stop (Retro launch item 6)
OUTPUT_LIMIT = 1024 * 1024
PROJECT_DIR = re.compile(r"[A-Za-z0-9-]{1,200}", re.ASCII)
RETRO_ID = retrofiles.RETRO_ID
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)
OWNER_LABEL = "owner=session-audit"
ACTIVE = ("pending", "incomplete")


class Guard(Protocol):
    """Whoever protects a state write: a retro ``Lease`` (``check_still_holder`` raises when it was
    taken over)."""

    def check_still_holder(self, conn: sqlite3.Connection) -> None: ...


@dataclass(frozen=True)
class SessionRow:
    """A ``retro_sessions`` row as stored (data: every field is validated before use)."""

    csession_id: str
    retro_id: str
    csession_dir: str
    private_dir: str | None
    project_dir: str
    cleanup_state: str
    forgotten_at: int | None
    created_at: int


def load_session_row(store: Store, csession_id: str) -> SessionRow | None:
    row = store.conn.execute(
        "SELECT csession_id, retro_id, csession_dir, private_dir, project_dir, cleanup_state, "
        "forgotten_at, created_at FROM retro_sessions WHERE csession_id = ?",
        (csession_id,),
    ).fetchone()
    return None if row is None else SessionRow(*row)


def call(ctx: Context, *argv: str, timeout: float | None = None) -> CallResult:
    """One claude-session call: its own process group, a timeout (``CALL_TIMEOUT``), a capped
    output."""
    return ctx.gateway.run(
        [ctx.settings.harness.claude_session_cmd, *argv],
        timeout=CALL_TIMEOUT if timeout is None else timeout,
        stdout_limit=OUTPUT_LIMIT,
    )


def stop_session(ctx: Context, csession_id: str, grace: str = "5s") -> CallResult:
    return call(ctx, "stop", csession_id, "--grace", grace, "--json")


def collect(ctx: Context, csid: str) -> Collected | None:
    """``status`` of the finished run (Collect and cleanup): None when the call or the document
    fails (the caller maps that to ``claude_session_error``)."""
    result = call(ctx, "status", csid, "--json")
    if not result.exit_is(0):
        return None
    try:
        return parse_status(result.stdout, csid)
    except Invalid:
        return None


# --- step 2: forget -------------------------------------------------------------------------


def _forget(ctx: Context, csession_id: str) -> tuple[CallResult, bool]:
    result = call(ctx, "forget", csession_id, "--purge", "--json")
    gone = result.exit_is(10) or (
        result.exit_is(0, 12) and parse_forget(result.stdout, csession_id)
    )
    return result, gone


def forget_session(ctx: Context, store: Store, held: Guard | None, csession_id: str) -> bool:
    """Forget (and purge) the recorded session; True when it is now forgotten. Sets
    ``retro_sessions.forgotten_at`` (once) under the retro's lease (``held``, when there is one)."""
    result, gone = _forget(ctx, csession_id)
    if result.exit_is(20):  # still live: stop it, then forget again
        stop_session(ctx, csession_id)
        _result, gone = _forget(ctx, csession_id)
    if not gone:
        return False
    now = int(ctx.gateway.now().timestamp() * 1000)
    with store.transaction() as conn:
        if held is not None:
            held.check_still_holder(conn)
        conn.execute(
            "UPDATE retro_sessions SET forgotten_at = ? "
            "WHERE csession_id = ? AND forgotten_at IS NULL",
            (now, csession_id),
        )
    return True


# --- step 1: verify -------------------------------------------------------------------------


def verify_owned(ctx: Context, row: SessionRow) -> bool | None:
    """True when ``status`` shows the tool's own labels for this retro, False for a session
    without them (or one whose document names another id), None when claude-session could not
    answer (the caller defers). A session that no longer exists (exit 10) is True: ``forget``
    will find nothing to forget."""
    result = call(ctx, "status", row.csession_id, "--json")
    if result.exit_is(10):
        return True
    if not result.exit_is(0):
        return None
    try:
        status = parse_recovery_status(result.stdout, row.csession_id)
    except Invalid:
        return None
    return OWNER_LABEL in status.labels and f"retro={row.retro_id}" in status.labels


# --- steps 3 and 4: what is observed and removed ---------------------------------------------


def lexists(gateway: Gateway, path: str | None) -> bool:
    """Whether ``path`` exists (a link counts). A path that cannot be examined (a NUL, an error
    other than not found) counts as present, so a purge is never assumed."""
    if path is None:
        return False
    try:
        gateway.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return True
    return True


def _transcript_ids(handle: DirHandle) -> list[str]:
    """The Claude Code session ids a project directory holds: ``<uuid>.jsonl`` files and
    ``<uuid>`` directories, by name only."""
    names, _more = handle.entries(MAX_ENTRIES)
    found: list[str] = []
    for raw in names:
        try:
            name = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        stem = name[:-6] if name.endswith(".jsonl") else name
        if _UUID.fullmatch(stem):
            found.append(stem)
    return sorted(set(found))


def _config_dir(env: Mapping[str, str]) -> str:
    custom = env.get("CLAUDE_CONFIG_DIR", "")
    if custom.startswith("/") and "\x00" not in custom:
        return os.path.normpath(custom)
    return os.path.join(home_dir(env), ".claude")


def _remove_session_env(gateway: Gateway, config_dir: str, ids: list[str]) -> bool:
    """``<config dir>/session-env/<id>`` for each id. True when all are gone. The config directory
    and ``session-env`` must be the user's own directories (no mode rule: the walk checks every
    entry it removes, and a symlink at either name is never entered)."""
    try:
        root = gateway.open_root(config_dir)
    except FileNotFoundError:
        return True  # no config directory: nothing of ours is in it
    except OSError:
        return False
    try:
        if root.stat().st_uid != gateway.uid():
            return False
        try:
            env_dir = root.open_dir(b"session-env")
        except FileNotFoundError:
            return True
        except OSError:
            return False  # a symlink or a file there: never entered
        try:
            if env_dir.stat().st_uid != gateway.uid():
                return False
            clean = True
            for session in ids:
                try:
                    clean &= remove_tree(gateway, env_dir, session.encode("ascii"))
                except OSError:
                    clean = False
            return clean
        finally:
            env_dir.close()
    finally:
        root.close()


def remove_run_transcripts(ctx: Context, project_dir: str) -> bool:
    """Remove the project directory ``project_dir`` (validated by the caller) of every transcript
    root and the ``session-env`` entries of the sessions it holds. True when nothing is left; a
    root that cannot be examined (other than missing) or an entry that cannot be removed is False
    (the cleanup stays ``pending``)."""
    gateway = ctx.gateway
    if not PROJECT_DIR.fullmatch(project_dir) or not is_component(project_dir.encode("ascii")):
        return False
    name = project_dir.encode("ascii")
    config_dir = _config_dir(os.environ)
    ok = True
    seen: set[tuple[int, int]] = set()
    for configured in ctx.settings.transcript_roots:
        root = open_root_checked(gateway, configured, seen)
        if root.handle is None:
            if root.reason not in ("missing", "duplicate"):
                ok = False
            continue
        try:
            try:
                info = root.handle.lstat(name)
            except FileNotFoundError:
                continue
            except OSError:
                ok = False
                continue
            ids: list[str] = []
            if statmod.S_ISDIR(info.st_mode):
                try:
                    project = root.handle.open_dir(name)
                except FileNotFoundError:
                    continue
                except OSError:
                    ok = False
                    continue
                try:
                    ids = _transcript_ids(project)
                finally:
                    project.close()
            if ids and not _remove_session_env(gateway, config_dir, ids):
                ok = False
                continue  # keep the project directory: the ids are still needed
            try:
                ok &= remove_tree(gateway, root.handle, name)
            except OSError:
                ok = False
        finally:
            root.handle.close()
    return ok


# --- the step list --------------------------------------------------------------------------


def _row_is_well_formed(store: Store, row: SessionRow) -> bool:
    """The row binds its own paths: the project directory is the encoding of
    ``<sessions root>/<id>/work`` (so it ends with ``-<id>-work``), the session directory's last
    component is the id, and the retro it names (when it still has a row) records this id."""
    if not (
        is_csession_id(row.csession_id)
        and bool(PROJECT_DIR.fullmatch(row.project_dir))
        and bool(RETRO_ID.fullmatch(row.retro_id))
    ):
        return False
    if not row.project_dir.endswith(f"-{row.csession_id}-work"):
        return False
    if os.path.basename(row.csession_dir) != row.csession_id:
        return False
    found = store.conn.execute(
        "SELECT csession_id FROM retros WHERE id = ?", (row.retro_id,)
    ).fetchone()
    return found is None or found[0] == row.csession_id  # a deleted retro has no row


def set_cleanup_state(store: Store, guard: Guard | None, csession_id: str, state: str) -> None:
    """``pending`` or ``incomplete`` to ``state`` (a ``skipped`` or ``done`` row is never
    changed), under the retro's lease when there is one."""
    with store.transaction() as conn:
        if guard is not None:
            guard.check_still_holder(conn)
        conn.execute(
            "UPDATE retro_sessions SET cleanup_state = ? "
            "WHERE csession_id = ? AND cleanup_state IN ('pending', 'incomplete')",
            (state, csession_id),
        )


def cleanup(
    ctx: Context,
    store: Store,
    row: SessionRow,
    guard: Guard | None,
    *,
    verify: bool = False,
) -> str:
    """The step list above. Returns the row's cleanup state afterwards (a deferred step leaves it
    as it was). Raises only what a store write or the guard raises (``lease_lost``)."""
    if row.cleanup_state not in ACTIVE:
        return row.cleanup_state
    if not _row_is_well_formed(store, row):
        set_cleanup_state(store, guard, row.csession_id, "skipped")
        return "skipped"
    forgotten = row.forgotten_at is not None
    if not forgotten:
        if verify:
            owned = verify_owned(ctx, row)
            if owned is None:
                return row.cleanup_state
            if not owned:
                set_cleanup_state(store, guard, row.csession_id, "skipped")
                return "skipped"
        if not forget_session(ctx, store, guard, row.csession_id):
            return row.cleanup_state  # stays pending
    gateway = ctx.gateway
    purged = not lexists(gateway, row.csession_dir) and not lexists(gateway, row.private_dir)
    files_ok = True
    if ctx.settings.retro.remove_run_transcripts:
        files_ok = remove_run_transcripts(ctx, row.project_dir)
    files_ok &= retrofiles.delete_local(gateway, ctx.state_path(), row.retro_id)
    state = "pending" if not files_ok else ("done" if purged else "incomplete")
    set_cleanup_state(store, guard, row.csession_id, state)
    return state


def note_failure(ctx: Context, what: str, exc: BaseException) -> None:
    """A stderr line for an exception housekeeping survived: its type only, never its text."""
    ctx.note(f"session-audit: warning: {what} failed ({type(exc).__name__})")
