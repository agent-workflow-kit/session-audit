"""Housekeeping: crash recovery and cleanup retries by recorded id (Retro lifecycle, Crash
recovery and cleanup retries; issue #27).

It runs at step 5 of ``retro run`` and in ``prune`` (``run_housekeeping``), never in ``ingest`` or
``validate``, and with ``--dry-run`` only lists (``planned``). The flow, for the 120 s total bound
(``BOUND``, the gateway's monotonic clock: after it no NEW item is started, ``deferred`` is set and
the rest waits for the next housekeeping)::

    for each non-final retro whose lease is not live (oldest first):
        take_over(lease) -- rowcount 0 means another process has it: skip -- then renew it
        csession id unset                  delete_local (and the digest) -> failed/runner_lost
        a session the tool did not record  nothing is touched, failed/runner_lost + recovery_skipped
        claude-session absent or too old   release the lease, defer (the retro stays as it was)
        status (60 s)                      not exit 0 or 10, or an unusable document: release, defer
        no owner/retro labels              its retro_sessions row is skipped, nothing is touched,
                                           failed/runner_lost with the warning recovery_skipped
        run live                           stop --grace 10s
        delete_local -> failed/runner_lost -> cleanup of the retro's sessions -> post-retro (and
        post-attention) -> release
    for each retro_sessions row pending or incomplete (claude-session usable): cleanup, under the
        lease of its retro when it has a row (a live lease or a non-final retro is skipped), with
        the label check before any forget
    expired digests and ``<state>/tmp`` entries older than 1 h (stale ``plan-*`` directories)

An exception in one item is a stderr warning and housekeeping goes on; it never changes the
command's exit code (``run_housekeeping`` raises nothing but ``KeyboardInterrupt`` and the like).
``forget`` never runs on a session but a recorded one of the tool's own retros (the row, then the
labels), and a session is never looked up by label.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import csversion, lease, retention, retrofiles, retrooutcome
from .command import Context
from .errors import CliError
from .gateway import Gateway
from .grammar import is_csession_id
from .harness import Invalid
from .removal import MAX_ENTRIES, remove_tree
from .retrocleanup import (
    ACTIVE,
    OWNER_LABEL,
    call,
    cleanup,
    load_session_row,
    note_failure,
    set_cleanup_state,
    stop_session,
)
from .retrostate import FINAL_STATES, Retro
from .store import Store, inspect_store

__all__ = [
    "BOUND",
    "NON_FINAL",
    "RECOVER_ACTOR",
    "TMP_MAX_AGE",
    "Result",
    "planned",
    "run_housekeeping",
]

BOUND = 120.0  # seconds: no new item is started after this long
TMP_MAX_AGE = 3600.0  # seconds: older ``<state>/tmp`` entries are removed
STOP_GRACE = "10s"
RECOVER_ACTOR = "auto:recover"
NON_FINAL = ("digesting", "running", "validating")
_PAGE = 200
WARN_DEFERRED = "recovery_deferred"
WARN_SKIPPED = "recovery_skipped"


@dataclass
class Result:
    """What housekeeping did: the retro ids recovered (it reached a final state), whether any item
    had to wait (``recovery_deferred``) and the retros whose session was not the tool's own."""

    recovered: list[str] = field(default_factory=list)
    deferred: bool = False
    skipped: list[str] = field(default_factory=list)
    cleaned: int = 0

    @property
    def warnings(self) -> list[str]:
        return [WARN_DEFERRED] if self.deferred else []


def planned(conn: sqlite3.Connection, gateway: Gateway) -> list[str]:
    """The retro ids a real housekeeping would try to recover now (non-final, lease not live),
    oldest first, at most one page. Reads only (``prune --dry-run``)."""
    marks = ",".join("?" * len(NON_FINAL))
    found: list[str] = []
    for retro_id, pid, start, expires in conn.execute(
        "SELECT id, lease_pid, lease_start, lease_expires_at FROM retros "
        f"WHERE state IN ({marks}) ORDER BY created_at, id",
        NON_FINAL,
    ):
        if not lease.is_live(gateway, lease.LeaseState(pid, start, expires)):
            found.append(retro_id)
            if len(found) >= _PAGE:
                break
    return found


def run_housekeeping(ctx: Context, *, digests: bool = True) -> Result:
    """Run housekeeping against the store in the state directory when there is one (a fresh
    install has nothing to recover and nothing is created). ``digests`` also deletes the expired
    digests (``prune`` does that itself)."""
    result = Result()
    gateway = ctx.gateway
    try:
        if inspect_store(gateway, ctx.state_path()).state not in ("ok", "pending"):
            return result  # no store (nothing to recover, nothing is created) or a newer one
        store = ctx.store()
    except CliError:
        return result
    run = _Run(ctx, store, result)
    run.recover_all()
    run.retry_cleanups()
    run.clear_stale_leases()
    run.expire(digests)
    return result


class _Run:
    def __init__(self, ctx: Context, store: Store, result: Result) -> None:
        self.ctx = ctx
        self.gw = ctx.gateway
        self.store = store
        self.result = result
        self.started = self.gw.monotonic()
        self._cs: bool | None = None

    # -- bounds and claude-session ---------------------------------------------------------

    def over(self) -> bool:
        return self.gw.monotonic() - self.started >= BOUND

    def defer(self) -> None:
        if not self.result.deferred:
            self.ctx.note(f"session-audit: warning: {WARN_DEFERRED}")
        self.result.deferred = True

    def cs_ok(self) -> bool:
        """claude-session is present and at least ``MIN_CS`` (checked once, when first needed;
        ``harness.enabled`` and ``--no-harness`` do not matter: it is the retro's own session)."""
        if self._cs is None:
            check = csversion.check_version(self.gw, self.ctx.settings.harness.claude_session_cmd)
            self._cs = check.ok
        return self._cs

    # -- recovery of non-final retros ------------------------------------------------------

    def recover_all(self) -> None:
        marks = ",".join("?" * len(NON_FINAL))
        ids = [
            r[0]
            for r in self.store.conn.execute(
                f"SELECT id FROM retros WHERE state IN ({marks}) ORDER BY created_at, id LIMIT ?",
                (*NON_FINAL, _PAGE),
            )
        ]
        for retro_id in ids:
            if self.over():
                self.defer()
                return
            try:
                self._recover(retro_id)
            except Exception as exc:  # noqa: BLE001 - one retro never stops the rest
                note_failure(self.ctx, "recovery", exc)

    def _recover(self, retro_id: str) -> None:
        target = lease.RetroTarget(retro_id)
        observed = lease.observe(self.store, target)
        if observed is None or lease.is_live(self.gw, observed):
            return
        held = lease.take_over(self.store, target, observed)
        if held is None:
            return  # another process recovers it
        try:
            with held:
                self._recover_held(retro_id, held)
        except BaseException:
            with contextlib.suppress(CliError):
                held.release()
            raise

    def _load(self, retro_id: str) -> tuple[Any, ...] | None:
        return self.store.conn.execute(
            "SELECT key, state, again_of, csession_id, digest_sha256, cost_usd, warnings "
            "FROM retros WHERE id = ?",
            (retro_id,),
        ).fetchone()

    def _recover_held(self, retro_id: str, held: lease.Lease) -> None:
        row = self._load(retro_id)  # read again under the lease
        if row is None or row[1] in FINAL_STATES:
            held.release()
            return
        key, state, again_of, csid, digest_sha256, cost_usd, _warnings = row
        retro = Retro(self.ctx, self.store, held, retro_id, key, again_of)
        retro.actor = RECOVER_ACTOR
        retro.state = state
        retro.digest_sha256 = digest_sha256
        retro.csession_id = csid if is_csession_id(csid) else None  # only a valid id is logged
        retro.cost_usd = cost_usd
        skipped = False
        if csid is not None:
            decision = self._check_session(retro, csid)
            if decision is None:
                retro.release()
                self.defer()
                return
            skipped = not decision
        self._finish(retro, row[4], row[6], skipped=skipped)

    def _check_session(self, retro: Retro, csid: str) -> bool | None:
        """True: the session is the retro's own (a live run is stopped, or it is gone). False: it
        is not (nothing is touched). None: claude-session cannot answer now."""
        session = load_session_row(self.store, csid) if is_csession_id(csid) else None
        if session is None or session.retro_id != retro.id:
            return False  # not a session this tool recorded for this retro
        if not self.cs_ok():
            return None
        result = call(self.ctx, "status", csid, "--json")
        if result.exit_is(10):
            return True  # already forgotten: nothing to stop; the cleanup will see it
        if not result.exit_is(0):
            return None
        try:
            status = retrooutcome.parse_recovery_status(result.stdout, csid)
        except Invalid:
            return None
        if OWNER_LABEL not in status.labels or f"retro={retro.id}" not in status.labels:
            set_cleanup_state(self.store, retro.lease, csid, "skipped")
            return False
        if status.live:
            stop_session(self.ctx, csid, STOP_GRACE)
        return True

    def _finish(
        self, retro: Retro, digest_sha256: str | None, warnings: str, *, skipped: bool
    ) -> None:
        gw, state_dir = self.gw, self.ctx.state_path()
        retrofiles.delete_local(gw, state_dir, retro.id)
        if retro.csession_id is None:
            retrofiles.remove_digest(gw, state_dir, retro.id)  # never launched: no use for it
        columns: dict[str, Any] = {}
        if skipped:
            columns["warnings"] = json.dumps(
                _with(json.loads(warnings), WARN_SKIPPED), separators=(",", ":")
            )
        retro.write("failed", "runner_lost", columns=columns)
        if skipped:
            self.result.skipped.append(retro.id)
        self._cleanup_rows_of(retro)
        retro.post_hooks("failed", "runner_lost")
        retro.release()
        self.result.recovered.append(retro.id)

    def _cleanup_rows_of(self, retro: Retro) -> None:
        marks = ",".join("?" * len(ACTIVE))
        ids = [
            r[0]
            for r in self.store.conn.execute(
                f"SELECT csession_id FROM retro_sessions WHERE retro_id = ? "
                f"AND cleanup_state IN ({marks})",
                (retro.id, *ACTIVE),
            )
        ]
        for csid in ids:
            session = load_session_row(self.store, csid)
            if session is None:
                continue
            try:
                state = cleanup(self.ctx, self.store, session, retro.lease)
            except CliError:
                retro.lease.raise_if_lost()
                self.ctx.note("session-audit: warning: the cleanup did not finish")
                continue
            self.result.cleaned += int(state == "done")

    # -- cleanup retries -------------------------------------------------------------------

    def retry_cleanups(self) -> None:
        marks = ",".join("?" * len(ACTIVE))
        ids = [
            r[0]
            for r in self.store.conn.execute(
                f"SELECT csession_id FROM retro_sessions WHERE cleanup_state IN ({marks}) "
                "ORDER BY created_at, csession_id LIMIT ?",
                (*ACTIVE, _PAGE),
            )
        ]
        if not ids:
            return
        if not self.cs_ok():
            self.defer()
            return
        for csid in ids:
            if self.over():
                self.defer()
                return
            try:
                self._retry(csid)
            except Exception as exc:  # noqa: BLE001 - one row never stops the rest
                note_failure(self.ctx, "cleanup", exc)

    def _retry(self, csid: str) -> None:
        session = load_session_row(self.store, csid)
        if session is None or session.cleanup_state not in ACTIVE:
            return
        target = lease.RetroTarget(session.retro_id)
        observed = lease.observe(self.store, target)
        if observed is None:  # the retro was deleted: its row outlives it, so the cleanup goes on
            self._cleanup_one(session, None)
            return
        state = self.store.conn.execute(
            "SELECT state FROM retros WHERE id = ?", (session.retro_id,)
        ).fetchone()[0]
        if state not in FINAL_STATES or lease.is_live(self.gw, observed):
            return  # recovery owns a non-final retro; a live holder cleans up itself
        held = lease.take_over(self.store, target, observed)
        if held is None:
            return
        try:
            with held:
                self._cleanup_one(session, held)
        finally:
            with contextlib.suppress(CliError):
                held.release()

    def _cleanup_one(self, session: Any, guard: lease.Lease | None) -> None:
        state = cleanup(self.ctx, self.store, session, guard, verify=True)
        if state == session.cleanup_state and state in ACTIVE and session.forgotten_at is None:
            self.defer()  # claude-session could not answer: retried next time
        self.result.cleaned += int(state == "done")

    def clear_stale_leases(self) -> None:
        """A final retro whose holder died before it released keeps a lease that ran out: clear
        it (one compare-and-set per row on the three stale fields, so a holder that is still
        alive and renewing is never touched)."""
        now = int(self.gw.now().timestamp() * 1000)
        marks = ",".join("?" * len(FINAL_STATES))
        try:
            rows = self.store.conn.execute(
                "SELECT id, lease_pid, lease_start, lease_expires_at FROM retros "
                f"WHERE state IN ({marks}) AND lease_pid IS NOT NULL AND lease_expires_at <= ? "
                "LIMIT ?",
                (*FINAL_STATES, now, _PAGE),
            ).fetchall()
            for retro_id, pid, start, expires in rows:
                observed = lease.LeaseState(pid, start, expires)
                if not lease.is_live(self.gw, observed):
                    held = lease.take_over(self.store, lease.RetroTarget(retro_id), observed)
                    if held is not None:
                        held.release()
        except Exception as exc:  # noqa: BLE001 - housekeeping never fails the command
            note_failure(self.ctx, "lease cleanup", exc)

    # -- local files -----------------------------------------------------------------------

    def expire(self, digests: bool) -> None:
        try:
            if digests:
                now_ms = int(self.gw.now().timestamp() * 1000)
                settings = retention.Settings(
                    self.ctx.settings.retention.sessions,
                    self.ctx.settings.retention.retros,
                    self.ctx.settings.retention.digests,
                    self.ctx.settings.retention.log,
                    self.ctx.settings.transcript_roots,
                    self.ctx.settings.zone(),
                )
                report = retention.Report()
                retention.expire_digests(
                    self.store.conn, self.gw, self.store.directory, now_ms, settings, report
                )
        except Exception as exc:  # noqa: BLE001
            note_failure(self.ctx, "digest expiry", exc)
        try:
            self._clean_tmp()
        except Exception as exc:  # noqa: BLE001
            note_failure(self.ctx, "tmp cleanup", exc)

    def _clean_tmp(self) -> None:
        """Remove ``<state>/tmp`` entries older than ``TMP_MAX_AGE`` (stale ``plan-*``
        directories included), each by its own name through descriptors."""
        try:
            root = self.gw.open_root(self.store.directory)
        except OSError:
            return
        try:
            try:
                tmp = root.open_dir(b"tmp")  # O_NOFOLLOW: a symlinked tmp is never entered
            except OSError:
                return
        finally:
            root.close()
        try:
            cutoff = self.gw.now().timestamp() - TMP_MAX_AGE
            names, _more = tmp.entries(MAX_ENTRIES)
            for name in names:
                try:
                    if tmp.lstat(name).st_mtime >= cutoff:
                        continue
                    remove_tree(self.gw, tmp, name)
                except OSError:
                    continue
        finally:
            tmp.close()


def _with(warnings: Any, token: str) -> list[str]:
    found = [w for w in warnings if isinstance(w, str)] if isinstance(warnings, list) else []
    if token not in found:
        found.append(token)
    return found
