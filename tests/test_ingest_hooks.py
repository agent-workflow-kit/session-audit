"""``ingest`` and the ``post-attention`` hook ``ingest_failing``: the fail-count rule, the 24 h
repeat bound, partial runs, best effort and no lock held across the hook."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from session_audit.commands import ingest as ingest_command
from session_audit.gateway import CallResult, CallStatus

from .ingesthelp import Corpus, TickGateway, ingest, messages, meta, state_dir_of
from .schemas import validate

HOUR = 3600


class HookGateway(TickGateway):
    """The real file system, a scripted clock and scripted hooks: ``run_hook`` records what the
    hook would have received and answers with ``result`` (or raises ``error``)."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = home
        self.hook_calls: list[dict[str, Any]] = []
        self.result = CallResult(CallStatus.EXITED, 0)
        self.error: Exception | None = None
        self.lease_during_hook: list[object] = []
        self.write_lock_free: list[bool] = []
        self.during_hook: Callable[[], None] | None = None

    def run_hook(
        self,
        command: str,
        *,
        timeout: float,
        stdin: str,
        env_extra: Mapping[str, str],
        stdout_limit: int = 0,
        cancel: threading.Event | None = None,
    ) -> CallResult:
        self.hook_calls.append(
            {"command": command, "timeout": timeout, "payload": json.loads(stdin), "env": env_extra}
        )
        conn = sqlite3.connect(state_dir_of(self.home) / "state.db", timeout=0)
        try:
            self.lease_during_hook.append(
                dict(conn.execute("SELECT key, value FROM meta")).get("ingest_lease", "")
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                self.write_lock_free.append(True)
            except sqlite3.OperationalError:
                self.write_lock_free.append(False)
        finally:
            conn.close()
        reenter, self.during_hook = self.during_hook, None
        if reenter is not None:
            reenter()
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def home(_private_environment: Path) -> Path:
    return _private_environment


@pytest.fixture
def gw(home: Path) -> HookGateway:
    return HookGateway(home)


@pytest.fixture
def corpus(home: Path) -> Corpus:
    return Corpus(home)


def write_config(home: Path, text: str) -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


HOOK_CONFIG = "hooks:\n  post-attention:\n    - cmd: notify-me\n      timeout: 7\n"


def fail(capsys: pytest.CaptureFixture[str], gw: HookGateway, *flags: str) -> int:
    return ingest(capsys, gw, *flags).code


@pytest.fixture
def broken(corpus: Corpus) -> Corpus:
    """Every ingest fails with exit 12: the transcript root is gone."""
    corpus.root.rmdir()
    return corpus


def test_it_fires_after_the_threshold_with_the_documented_payload(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, HOOK_CONFIG)
    assert fail(capsys, gw) == 12
    gw.clock.advance(HOUR)
    assert fail(capsys, gw) == 12
    assert gw.hook_calls == []  # two failures: below the threshold of 3
    gw.clock.advance(HOUR)
    run = ingest(capsys, gw)
    assert run.code == 12  # the exit code and the error are the ingest's own
    assert json.loads(run.out)["error"]["code"] == "failed"
    [call] = gw.hook_calls
    assert call["command"] == "notify-me" and call["timeout"] == 7
    assert call["env"] == {
        "SESSION_AUDIT_HOOK": "post-attention",
        "SESSION_AUDIT_ID": "ingest",
        "SESSION_AUDIT_OBJECT_TYPE": "ingest",
    }
    payload = call["payload"]
    validate(payload, "session-audit/hooks/post-attention.json")
    assert payload["reason"] == "ingest_failing" and payload["labels"] == []
    assert payload["details"]["consecutive_failures"] == 3
    values = meta(home)
    assert int(values["last_fired_at"]) == int(values["last_failed_at"])
    assert "hook" not in run.err  # no warning: the hook succeeded


def test_it_does_not_fire_again_within_24_hours_and_fires_after(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    assert fail(capsys, gw) == 12
    assert len(gw.hook_calls) == 1
    gw.clock.advance(23 * HOUR)
    assert fail(capsys, gw) == 12
    assert len(gw.hook_calls) == 1
    gw.clock.advance(2 * HOUR)
    assert fail(capsys, gw) == 12
    assert len(gw.hook_calls) == 2
    ids = [c["payload"]["event_id"] for c in gw.hook_calls]
    assert len(set(ids)) == 2
    assert gw.hook_calls[1]["payload"]["details"]["consecutive_failures"] == 3


def test_a_partial_ingest_never_counts_or_fires(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    for flags in (["--rebuild", "--yes"], ["--since", "7d"], ["--path", str(broken.root)]):
        assert fail(capsys, gw, *flags) in (2, 12)
    assert gw.hook_calls == []
    assert "fail_count" not in meta(home)


def test_a_dry_run_never_runs_a_hook(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    fail(capsys, gw, "--dry-run")
    assert gw.hook_calls == []


def test_a_success_resets_the_counter_and_fires_nothing(
    gw: HookGateway, corpus: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 2\n" + HOOK_CONFIG)
    corpus.write_main("11111111-1111-4111-8111-111111111111", messages("a", 2))
    corpus.root.chmod(0o770)  # unusable root: exit 12
    assert fail(capsys, gw) == 12
    corpus.root.chmod(0o700)
    assert fail(capsys, gw) == 0
    corpus.root.chmod(0o770)
    assert fail(capsys, gw) == 12  # one failure again, not two
    assert gw.hook_calls == []


def test_without_a_configured_hook_nothing_is_claimed(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n")
    assert fail(capsys, gw) == 12
    assert gw.hook_calls == [] and "last_fired_at" not in meta(home)


def test_no_lease_and_no_write_lock_is_held_while_the_hook_runs(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    fail(capsys, gw)
    assert gw.lease_during_hook == [""]
    assert gw.write_lock_free == [True]


def test_the_due_check_and_the_claim_are_one_transaction_before_the_hook(
    gw: HookGateway,
    broken: Corpus,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second firing that starts while the first hook runs (another process, here a re-entrant
    call) must find the claim already made: exactly one hook call."""
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    original = ingest_command.fire_ingest_failing

    def spy(ctx: Any, store: Any) -> None:
        if gw.during_hook is None and not gw.hook_calls:
            gw.during_hook = lambda: original(ctx, store)
        original(ctx, store)

    monkeypatch.setattr(ingest_command, "fire_ingest_failing", spy)
    assert fail(capsys, gw) == 12
    assert len(gw.hook_calls) == 1


@pytest.mark.parametrize(
    "result",
    [
        CallResult(CallStatus.EXITED, 1),
        CallResult(CallStatus.EXITED, 13),
        CallResult(CallStatus.TIMEOUT),
        CallResult(CallStatus.SIGNALED, signal=9),
        CallResult(CallStatus.SPAWN_FAILED),
    ],
)
def test_a_failing_hook_is_a_stderr_warning_and_never_changes_the_result(
    gw: HookGateway,
    broken: Corpus,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    result: CallResult,
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    gw.result = result
    run = ingest(capsys, gw)
    assert run.code == 12 and json.loads(run.out)["error"]["code"] == "failed"
    assert "post-attention hook entry 1 failed" in run.err
    assert "notify-me" not in run.err


def test_a_hook_that_raises_never_changes_the_result(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    gw.error = RuntimeError("boom")
    run = ingest(capsys, gw)
    assert run.code == 12 and json.loads(run.out)["error"]["code"] == "failed"


def test_quiet_silences_the_warning(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "ingest:\n  fail_threshold: 1\n" + HOOK_CONFIG)
    gw.result = CallResult(CallStatus.EXITED, 1)
    run = ingest(capsys, gw, "--quiet")
    assert run.code == 12 and "hook" not in run.err


def test_every_post_attention_entry_runs_in_config_order(
    gw: HookGateway, broken: Corpus, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "ingest:\n  fail_threshold: 1\nhooks:\n  post-attention:\n    - cmd: one\n    - cmd: two\n"
    )
    write_config(home, text)
    fail(capsys, gw)
    assert [c["command"] for c in gw.hook_calls] == ["one", "two"]
    assert gw.hook_calls[0]["payload"] == gw.hook_calls[1]["payload"]


def test_a_real_process_receives_the_payload_on_stdin(
    home: Path, broken: Corpus, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "payload.json"
    write_config(
        home,
        f"ingest:\n  fail_threshold: 1\nhooks:\n  post-attention:\n    - cmd: cat > '{out}'\n",
    )
    gw = TickGateway()
    assert ingest(capsys, gw).code == 12
    payload = json.loads(out.read_text(encoding="utf-8"))
    validate(payload, "session-audit/hooks/post-attention.json")
    assert payload["details"]["consecutive_failures"] == 1
