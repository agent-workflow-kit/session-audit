"""``retro run`` steps 13 to 16: the row, the ``pre-retro`` hook, the files, ``start``, the adopting
``exec``, the outcome mapping, the final state, cleanup and ``post-retro`` (issue #26).

The flow (every state write is a compare-and-set on the lease, ``retrostate.Retro.write``):

13. ``retrostate.insert_retro``: one ``BEGIN IMMEDIATE`` repeats step 10 and inserts the row
    (``digesting``). From here the lease is renewed by a thread until the retro is final, cleaned
    up and its post hooks ran; no write lock is held across a claude-session call or a hook.
14. ``pre-retro``: allow continues, escalate is ``escalated/hook_escalated`` (exit 13), a veto is
    ``canceled/hook_vetoed`` (exit 11); both fire ``post-retro`` once.
15. ``retro_starts`` row and state ``running`` in one transaction, then the files
    (``retrofiles.write_run_files``). A file that cannot be written ends ``failed/
    session_not_started`` (exit 12 ``store_unwritable``) and refunds the start.
16. ``start --json`` (recorded before anything can write a transcript), the adopting ``exec`` with
    ``--outcome-file`` under the overall deadline, ``collect``, the Outcome mapping
    (``retrooutcome``), the sandbox rule, the artifact check (the seam of #29), the final state,
    ``cleanup`` of the recorded session (``retrocleanup``: forget, purge check, transcripts and
    local files), ``post-retro`` and, for ``failed``, ``post-attention``.

What claude-session printed is parsed strictly; anything outside the mapping is
``claude_session_error``. A malformed id or directory records nothing and calls neither ``forget``
nor ``stop``. Untrusted text never reaches argv or the environment: the prompt and the inputs are
files.

Signals are #28's: no handler matrix here. The PID of the exec child is kept in ``exec_pid``
(``Gateway.run(on_start=...)``) for it. A process that dies or is interrupted leaves the row in the
state of its last write; ``retrostate`` lists the states ``retrorecovery`` (#27) handles. On an
exception that is not a final state the lease is released so recovery can take the row at once.
"""

from __future__ import annotations

import os
import stat as statmod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import errors, fencing, hooks, launchplan, retro, retrofiles
from .command import Context
from .config import home_dir, profile_issues
from .fencing import FencedDigest
from .gateway import CallResult, CallStatus
from .harness import Invalid
from .retro import Prepared
from .retroartifact import validate_and_store_artifact
from .retrocleanup import OUTPUT_LIMIT, cleanup, collect, load_session_row, stop_session
from .retrofiles import FilesError, RunFiles
from .retrooutcome import (
    Collected,
    Mapped,
    Outcome,
    Started,
    launch_failed_id,
    map_exec,
    map_start,
    names_a_malformed_id,
    parse_outcome,
    parse_start,
    single_csession_id,
    validated_session,
)
from .retrostate import FINAL_STATES, Inserted, Retro, Reused, insert_retro, scope_object

__all__ = ["EXEC_SLACK", "START_TIMEOUT", "TERM_WAIT", "run_launch"]

START_TIMEOUT = 120.0  # ``start`` (Retro launch item 6)
EXEC_SLACK = 600.0  # the deadline is the exec spawn plus the retro timeout plus 10 minutes
TERM_WAIT = 30.0  # the exec child gets SIGTERM, then this long, then SIGKILL on its group
OUTCOME_FILE_LIMIT = 1024 * 1024


class _SessionTaken(Exception):
    """``start`` named a session id that another retro already recorded: not ours to touch."""


@dataclass
class _Session:
    csession_id: str
    csession_dir: str
    project_dir: str


def run_launch(
    ctx: Context,
    args: Any,
    prepared: Prepared,
    launch: launchplan.Launch,
    plan: launchplan.LaunchPlan,
    *,
    on_reused: Callable[[dict[str, Any]], None],
) -> int:
    """Steps 13 to 16. Returns the exit code (0, or an exit by signal) and raises ``CliError`` for
    every other exit. ``on_reused`` prints a stored retro that step 13 found."""
    profile = ctx.settings.retro.permission_profile
    if profile is not None and profile_issues(ctx.gateway, profile, home_dir(os.environ)):
        raise errors.CliError(
            "refused",
            "the permission profile file is unsafe",
            details={"reason": "config_unsafe", "field": "retro.permission_profile"},
            hint="Its file and directories must be yours and not group or other writable.",
        )
    store = ctx.store()
    inserted = insert_retro(ctx, store, args, prepared)
    if isinstance(inserted, Reused):
        on_reused(inserted.retro)
        return 0
    assert isinstance(inserted, Inserted)
    with inserted.retro.lease:
        try:
            launcher = Launcher(ctx, args, prepared, launch, plan, inserted.retro)
            return launcher.run()
        except BaseException:
            if inserted.retro.state not in FINAL_STATES:
                inserted.retro.release()  # housekeeping may take the row at once
            raise


class Launcher:
    def __init__(
        self,
        ctx: Context,
        args: Any,
        prepared: Prepared,
        launch: launchplan.Launch,
        plan: launchplan.LaunchPlan,
        row: Retro,
    ) -> None:
        self.ctx = ctx
        self.gw = ctx.gateway
        self.args = args
        self.prepared = prepared
        self.launch = launch
        self.plan = plan
        self.row = row
        self.state_dir = ctx.state_path()
        self.command = ctx.settings.harness.claude_session_cmd
        self.sessions_root = os.path.dirname(os.path.dirname(plan.cwd))
        self.exec_pid: int | None = None  # the exact exec child (for the signal handling of #28)
        self.session: _Session | None = None
        self.files: RunFiles | None = None
        self.hook = hooks.HookOutcome()

    # -- steps 14 and 15 ------------------------------------------------------------------

    def run(self) -> int:
        row, ctx, gw = self.row, self.ctx, self.gw
        run_token = gw.token_urlsafe(24)
        fenced = fencing.finish_digest(
            self.prepared.digest, row.id, run_token, gw, self.prepared.csession_ids
        )
        if fenced.section_hashes != self.prepared.hashes:
            raise errors.CliError("internal", "the digest sections changed after the key")
        retrofiles.check_digest_hash(fenced)
        self.hook = self._pre_retro(fenced)
        row.lease.raise_if_lost()
        if self.hook.decision is hooks.Decision.VETO:
            return self._end(Mapped("canceled", "hook_vetoed", 11), delete_files=True)
        if self.hook.decision is hooks.Decision.ESCALATE:
            return self._end(Mapped("escalated", "hook_escalated", 13), delete_files=True)
        self._mark_running(run_token, fenced)
        try:
            self.files = retrofiles.write_run_files(gw, self.state_dir, row.id, run_token, fenced)
        except FilesError as exc:
            ctx.note("session-audit: error: the retro's files cannot be written")
            return self._end(
                Mapped(
                    "failed",
                    "session_not_started",
                    12,
                    retryable=exc.retryable,
                    error_reason="store_unwritable",
                ),
                refund=True,
                # write_run_files removed what it created; a path it refused is not ours to delete
                delete_files=False,
            )
        row.lease.raise_if_lost()
        return self._launch()

    def _pre_retro(self, fenced: FencedDigest) -> hooks.HookOutcome:
        payload = hooks.pre_retro_payload(
            self.gw,
            retro_id=self.row.id,
            scope=scope_object(self.args, self.prepared),
            sessions=self.prepared.kept,
            digest_bytes=len(fenced.digest_bytes),
            items=self.prepared.digest.items_kept,
            skill=self.prepared.skill,
            model=self.prepared.model,
            key=self.prepared.key,
            max_turns=self.launch.max_turns,
            timeout_seconds=int(self.launch.timeout.total_seconds()),
        )
        outcome = hooks.run_hooks(
            self.gw,
            self.ctx.settings.hooks,
            payload,
            flag_decision_ids=tuple(self.args.decision_id or ()),
        )
        for line in hooks.warning_text(outcome, hooks.PRE_RETRO):
            self.ctx.note(f"session-audit: {line}")
        return outcome

    def _mark_running(self, run_token: str, fenced: FencedDigest) -> None:
        now = self.gw.now()
        day = retro.day_of(self.ctx, now)
        row = self.row

        def starts(conn: Any) -> None:
            conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES (?, ?)", (day, row.id))

        row.write(
            "running",
            columns={
                "started_at": int(now.timestamp() * 1000),
                "run_token": run_token,
                "digest_sha256": fenced.digest_sha256,
            },
            also=starts,
            decision_ids=self.hook.decision_ids,
            output_ignored=self.hook.output_ignored,
        )

    # -- step 16 --------------------------------------------------------------------------

    def _launch(self) -> int:
        assert self.files is not None
        files = self.files
        flags = launchplan.launch_flags(
            self.launch,
            self.row.id,
            prompt_file=files.prompt,
            input_files=[files.digest, files.manifest],
        )
        result = self.gw.run(
            [self.command, "start", *flags, "--json"],
            timeout=START_TIMEOUT,
            stdout_limit=OUTPUT_LIMIT,
        )
        started, mapped = self._after_start(result)
        if mapped is not None:
            return self._end(mapped, refund=True, delete_files=self.session is None)
        assert started is not None
        return self._exec(started)

    def _after_start(self, result: CallResult) -> tuple[Started | None, Mapped | None]:
        """Record the session ``start`` allocated (when its id, directory and project directory
        name are valid) and map a ``start`` that did not succeed. Nothing is recorded, forgotten
        or stopped for an id, directory or name that fails ``validated_session``."""
        error = Mapped("failed", "claude_session_error", 12)
        started: Started | None = None
        failed: tuple[str, str, int | None] | None = None
        if result.exit_is(0):
            try:
                started = parse_start(result.stdout)
            except Invalid:
                started = None  # the one id token of the output may still be cleaned up below
                if names_a_malformed_id(result.stdout):
                    self.ctx.note("session-audit: warning: claude-session printed no usable id")
                    return None, error
        elif result.exit_is(12):
            failed = launch_failed_id(result.stdout)
        # The document, else the launch_failed envelope, else the one distinct id token of the
        # output (a start that timed out, failed some other way or printed a document that does not
        # parse may still have printed its id: it is validated, recorded and forgotten like any).
        if started is not None:
            csid = started.csession_id
        elif failed is not None:
            csid = failed[0]
        else:
            csid = single_csession_id(result.stdout, result.stderr)
        if csid is None:
            if result.exit_is(0):
                self.ctx.note("session-audit: warning: claude-session printed no usable id")
            return None, error
        directory = (
            started.csession_dir if started is not None else os.path.join(self.sessions_root, csid)
        )
        checked = validated_session(csid, directory, self.sessions_root)
        if checked is None or not self._record(_Session(*checked)):
            self.ctx.note("session-audit: warning: claude-session printed an id that was refused")
            return None, error
        if started is not None:
            return (started, None) if self._adoptable(started) else (None, error)
        return None, map_start(result, launch_failed=failed)

    def _adoptable(self, started: Started) -> bool:
        """The ``argv`` of ``start`` must be exactly ``<path> exec --session-id <id> --run <n>``."""
        return list(started.argv[1:]) == [
            "exec",
            "--session-id",
            started.csession_id,
            "--run",
            str(started.run),
        ]

    def _record(self, session: _Session) -> bool:
        """One transaction: ``retros.csession_id`` and the ``retro_sessions`` row (cleanup
        ``pending``). False (and nothing written) when the id belongs to another retro."""
        now = int(self.gw.now().timestamp() * 1000)
        private = os.path.join(os.path.dirname(self.sessions_root), "private", session.csession_id)

        def insert(conn: Any) -> None:
            if conn.execute(
                "SELECT 1 FROM retro_sessions WHERE csession_id = ?", (session.csession_id,)
            ).fetchone():
                raise _SessionTaken
            conn.execute(
                "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, private_dir, "
                "project_dir, cleanup_state, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    session.csession_id,
                    self.row.id,
                    session.csession_dir,
                    private,
                    session.project_dir,
                    now,
                ),
            )

        try:
            self.row.write("running", columns={"csession_id": session.csession_id}, also=insert)
        except _SessionTaken:
            return False
        self.session = session
        return True

    def _exec(self, started: Started) -> int:
        assert self.session is not None and self.files is not None
        csid = self.session.csession_id
        deadline = self.launch.timeout.total_seconds() + EXEC_SLACK
        result = self.gw.run(
            [
                self.command,
                "exec",
                "--session-id",
                csid,
                "--run",
                str(started.run),
                "--outcome-file",
                self.files.outcome,
                "--json",
            ],
            timeout=deadline,
            stdout_limit=OUTPUT_LIMIT,
            kill_grace=TERM_WAIT,
            on_start=self._exec_started,
        )
        # todo(#28): signal the exec child through a spawn handle that refuses after reap
        # (on_exit before reap), not a bare pid: exec_pid can hold a reaped pid until this line.
        self.exec_pid = None
        self.row.lease.raise_if_lost()
        timed_out = result.status is CallStatus.TIMEOUT
        if timed_out:
            stop_session(self.ctx, csid)
        outcome = None if timed_out else self._read_outcome(result, csid, started.run)
        mapped = map_exec(result, outcome, deadline=timed_out)
        collected = None
        if timed_out or (outcome is not None and outcome.phase == "session"):
            collected = self._collect(csid)
        columns = self._collected_columns(collected, outcome)
        if (
            self.launch.settings.sandbox == "required"
            and collected is not None
            and mapped.state in ("done", "escalated")
            and collected.sandbox_active is not True
        ):
            # Retro launch item 7: a collected session-phase run (done, blocked, needs-input)
            # whose status does not show an active sandbox is failed; exits 14-18 keep their own
            # rows and the artifact is never read.
            mapped = Mapped("failed", "claude_session_error", 12)
        if mapped.validate:
            mapped = self._done(collected, columns)
        # A start is refunded only when the run never reached the model: the outcome says the
        # launch phase ended it and agrees with the process exit (11, 12 or 13). An outcome that
        # did not parse or disagrees proves nothing, so it is not.
        refund = (
            outcome is not None
            and outcome.phase == "launch"
            and result.exited
            and result.returncode == outcome.exit_code
            and outcome.exit_code in (11, 12, 13)
        )
        return self._end(mapped, columns=columns, refund=refund)

    def _exec_started(self, pid: int) -> None:
        self.exec_pid = pid

    def _read_outcome(self, result: CallResult, csid: str, run: int) -> Outcome | None:
        """The outcome from stdout, the outcome file when stdout is not a complete outcome."""
        if not result.exited:
            return None
        try:
            return parse_outcome(result.stdout, csid, run)
        except Invalid:
            pass
        assert self.files is not None
        try:
            content = self.gw.read_file(self.files.outcome, OUTCOME_FILE_LIMIT)
            info = content.stat
            if (
                content.truncated
                or not statmod.S_ISREG(info.st_mode)
                or info.st_uid != self.gw.uid()
                or info.st_nlink != 1
            ):
                return None
            return parse_outcome(content.data.decode("utf-8"), csid, run)
        except (Invalid, OSError, UnicodeDecodeError):
            return None

    def _collect(self, csid: str) -> Collected | None:
        """``status`` of the finished run (Collect and cleanup): None when the call or the
        document fails."""
        return collect(self.ctx, csid)

    @staticmethod
    def _collected_columns(collected: Collected | None, outcome: Outcome | None) -> dict[str, Any]:
        if collected is not None:
            sandbox = None if collected.sandbox_active is None else int(collected.sandbox_active)
            return {
                "cost_usd": collected.cost_usd,
                "turns": collected.num_turns,
                "sandbox": sandbox,
                "outcome": collected.outcome,
            }
        if outcome is not None:
            return {
                "cost_usd": outcome.cost_usd,
                "turns": outcome.num_turns,
                "outcome": outcome.outcome,
            }
        return {}

    def _done(self, collected: Collected | None, columns: dict[str, Any]) -> Mapped:
        """A run that ended ``done``: ``status`` must have answered (the sandbox rule already ran
        in ``_exec``), then the artifact check (#29) on a retro in state ``validating``."""
        if collected is None:
            return Mapped("failed", "claude_session_error", 12)
        self.row.write("validating", columns=columns)
        return validate_and_store_artifact(collected)

    # -- the end --------------------------------------------------------------------------

    def _end(
        self,
        mapped: Mapped,
        *,
        columns: dict[str, Any] | None = None,
        refund: bool = False,
        delete_files: bool = False,
    ) -> int:
        """Final state, cleanup, post hooks, release; then the exit (an error for every code but
        0 and an exit by signal)."""
        row = self.row
        running = row.digest_sha256 is not None

        def refunded(conn: Any) -> None:
            conn.execute("DELETE FROM retro_starts WHERE retro_id = ?", (row.id,))

        row.write(
            mapped.state,
            mapped.reason,
            columns=columns,
            also=refunded if refund else None,
            # A retro that ended at its pre-retro hook logs the hook's ids here (a running one
            # logged them when it started).
            decision_ids=() if running else self.hook.decision_ids,
            output_ignored=0 if running else self.hook.output_ignored,
        )
        if self.session is not None:
            self._cleanup(self.session.csession_id)
        if delete_files:
            retrofiles.delete_local(self.gw, self.state_dir, row.id)
            if row.digest_sha256 is not None:
                retrofiles.remove_digest(self.gw, self.state_dir, row.id)
        self._post_hooks(mapped)
        row.release()
        error = mapped.error(row.id)
        if error is not None:
            raise error
        return mapped.exit_code

    def _cleanup(self, csid: str) -> None:
        """The step list of ``cleanup`` for the recorded session, under this retro's lease. An
        exception other than a lost lease leaves the row ``pending`` for housekeeping."""
        row = self.row
        session = load_session_row(row.store, csid)
        if session is None:
            return
        try:
            cleanup(self.ctx, row.store, session, row.lease)
        except errors.CliError:
            row.lease.raise_if_lost()  # the lease was taken over: stop
            self.ctx.note("session-audit: warning: the cleanup did not finish")

    def _post_hooks(self, mapped: Mapped) -> None:
        self.row.post_hooks(mapped.state, mapped.reason)
