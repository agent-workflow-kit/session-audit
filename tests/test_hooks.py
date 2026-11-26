"""Hooks (issue #25): payloads, entry order, the pre-retro decision, output parsing, post hooks."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from session_audit import hooks
from session_audit.config import HookSettings
from session_audit.gateway import CallResult, CallStatus, SystemGateway
from session_audit.hooks import Decision, run_hooks

from .fake_gateway import FakeGateway
from .procs import gone, wait_until_gone
from .schemas import validate

RETRO = "ra-abcdefg2"
KEY = "a" * 64
SCOPE: dict[str, Any] = {
    "sessions": [],
    "labels": [],
    "project": None,
    "since": "7d",
    "until": None,
    "kind": None,
    "outcome": None,
}
PY = sys.executable


def sh(command: str) -> list[str]:
    return ["/bin/sh", "-c", command]


def exited(code: int, stdout: str = "") -> CallResult:
    return CallResult(CallStatus.EXITED, code, stdout=stdout)


def settings(**lists: list[dict[str, Any]]) -> HookSettings:
    return HookSettings.model_validate({k.replace("_", "-"): v for k, v in lists.items()})


def pre_payload(gw: FakeGateway) -> hooks.HookPayload:
    return hooks.pre_retro_payload(
        gw,
        retro_id=RETRO,
        scope=SCOPE,
        sessions=3,
        digest_bytes=1200,
        items=40,
        skill="plug:retro",
        model=None,
        key=KEY,
        max_turns=30,
        timeout_seconds=1800,
    )


def run_pre(gw: FakeGateway, cmds: list[str], flags: tuple[str, ...] = ()) -> hooks.HookOutcome:
    cfg = settings(pre_retro=[{"cmd": c, "timeout": 5} for c in cmds])
    return run_hooks(gw, cfg, pre_payload(gw), flag_decision_ids=flags)


# -- payloads ---------------------------------------------------------------------------------


def test_the_event_id_follows_the_grammar_and_is_unique_per_event() -> None:
    gw = SystemGateway()
    ids = {hooks.new_event_id(gw) for _ in range(50)}
    assert len(ids) == 50
    for event in ids:
        assert len(event) == 29 and event.startswith("ev-")
        assert set(event[3:]) <= set("abcdefghijklmnopqrstuvwxyz234567")


def test_pre_retro_payload_validates_and_is_closed() -> None:
    gw = FakeGateway()
    payload = pre_payload(gw)
    validate(payload.document, "session-audit/hooks/pre-retro.json")
    assert payload.document["labels"] == []
    assert "repo" not in payload.document
    assert payload.env() == {
        "SESSION_AUDIT_HOOK": "pre-retro",
        "SESSION_AUDIT_ID": RETRO,
        "SESSION_AUDIT_OBJECT_TYPE": "retro",
    }
    with pytest.raises(ValidationError):
        validate({**payload.document, "repo": "x"}, "session-audit/hooks/pre-retro.json")
    with pytest.raises(ValidationError):
        validate({**payload.document, "labels": {}}, "session-audit/hooks/pre-retro.json")
    with pytest.raises(ValidationError):
        validate({**payload.document, "labels": ["a=b"]}, "session-audit/hooks/pre-retro.json")


@pytest.mark.parametrize(
    ("state", "reason", "cost"),
    [
        ("done", None, 0.5),
        ("failed", "no_artifact", None),
        ("escalated", "hook_escalated", None),
        ("canceled", "hook_vetoed", None),
    ],
)
def test_post_retro_payload_validates(state: str, reason: str | None, cost: float | None) -> None:
    payload = hooks.post_retro_payload(
        FakeGateway(),
        retro_id=RETRO,
        state=state,
        reason=reason,
        findings=2,
        candidates=1,
        cost_usd=cost,
    )
    validate(payload.document, "session-audit/hooks/post-retro.json")
    assert payload.object_type == "retro" and payload.object_id == RETRO


def test_post_attention_payloads_validate() -> None:
    gw = FakeGateway()
    failed = hooks.retro_failed_payload(
        gw, retro_id=RETRO, failure_reason="session_failed", csession_id=None
    )
    validate(failed.document, "session-audit/hooks/post-attention.json")
    assert failed.env()["SESSION_AUDIT_OBJECT_TYPE"] == "retro"
    details = {
        "consecutive_failures": 3,
        "first_failed_at": "2026-01-01T00:00:00.000Z",
        "last_failed_at": "2026-01-01T03:00:00.000Z",
    }
    ingest = hooks.ingest_failing_payload(gw, details)
    validate(ingest.document, "session-audit/hooks/post-attention.json")
    assert ingest.env()["SESSION_AUDIT_ID"] == "ingest"
    assert ingest.env()["SESSION_AUDIT_OBJECT_TYPE"] == "ingest"
    assert ingest.document["details"] == details


def test_the_payload_text_is_one_ascii_json_line() -> None:
    gw = FakeGateway()
    text = pre_payload(gw).text()
    assert text.endswith("\n") and text.count("\n") == 1 and text.isascii()
    assert json.loads(text)["hook"] == "pre-retro"


# -- pre-retro --------------------------------------------------------------------------------


def test_no_entries_allows() -> None:
    gw = FakeGateway()
    outcome = run_hooks(gw, settings(), pre_payload(gw), flag_decision_ids=["d1"])
    assert outcome.decision is Decision.ALLOW and outcome.decision_ids == ("d1",)
    assert gw.calls == []


def test_exit_zero_allows_and_the_env_and_stdin_are_passed() -> None:
    gw = FakeGateway()
    gw.on(sh("ok"), exited(0))
    outcome = run_pre(gw, ["ok"])
    assert outcome.decision is Decision.ALLOW
    call = gw.calls[0]
    assert call["hook"] is True and call["timeout"] == 5
    assert call["env_extra"] == {
        "SESSION_AUDIT_HOOK": "pre-retro",
        "SESSION_AUDIT_ID": RETRO,
        "SESSION_AUDIT_OBJECT_TYPE": "retro",
    }
    validate(json.loads(str(call["stdin"])), "session-audit/hooks/pre-retro.json")


def test_exit_13_escalates() -> None:
    gw = FakeGateway()
    gw.on(sh("e"), exited(13))
    outcome = run_pre(gw, ["e"])
    assert outcome.decision is Decision.ESCALATE
    assert outcome.entries[0].status == "escalate"


@pytest.mark.parametrize("code", [1, 2, 12, 14, 127, 255])
def test_any_other_non_zero_exit_vetoes(code: int) -> None:
    gw = FakeGateway()
    gw.on(sh("v"), exited(code))
    assert run_pre(gw, ["v"]).decision is Decision.VETO


@pytest.mark.parametrize(
    "result",
    [
        CallResult(CallStatus.TIMEOUT),
        CallResult(CallStatus.SIGNALED, signal=9),
        CallResult(CallStatus.SPAWN_FAILED, detail="OSError"),
        CallResult(CallStatus.OUTPUT_LIMIT),
    ],
)
def test_a_timeout_a_signal_or_a_failed_start_vetoes(result: CallResult) -> None:
    gw = FakeGateway()
    gw.on(sh("v"), result)
    assert run_pre(gw, ["v"]).decision is Decision.VETO


def test_a_gateway_that_raises_vetoes_instead_of_crashing() -> None:
    gw = FakeGateway()  # nothing scripted: run_hook raises AssertionError
    assert run_pre(gw, ["unscripted"]).decision is Decision.VETO


def test_the_first_non_zero_entry_decides_and_later_entries_do_not_run() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0))
    gw.on(sh("b"), exited(13))
    gw.on(sh("c"), exited(1))
    outcome = run_pre(gw, ["a", "b", "c"])
    assert outcome.decision is Decision.ESCALATE
    assert [c["argv"][2] for c in gw.calls] == ["a", "b"]  # type: ignore[index]
    assert [e.index for e in outcome.entries] == [0, 1]


def test_entries_run_in_config_order_and_all_run_when_all_allow() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0))
    gw.on(sh("b"), exited(0))
    outcome = run_pre(gw, ["a", "b"])
    assert outcome.decision is Decision.ALLOW
    assert [c["argv"][2] for c in gw.calls] == ["a", "b"]  # type: ignore[index]


def test_the_timeout_of_an_entry_is_passed_and_capped_at_600_seconds() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0))
    cfg = settings(pre_retro=[{"cmd": "a", "timeout": 600}])
    run_hooks(gw, cfg, pre_payload(gw))
    assert gw.calls[0]["timeout"] == 600
    gw2 = FakeGateway()
    gw2.on(sh("a"), exited(0))
    run_hooks(gw2, settings(pre_retro=[{"cmd": "a"}]), pre_payload(gw2))
    assert gw2.calls[0]["timeout"] == 30


# -- pre-retro output -------------------------------------------------------------------------


def test_decision_ids_a_string_or_a_list_flags_first() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0, '{"decision_id": "hook-1"}'))
    gw.on(sh("b"), exited(0, '{"decision_id": ["hook-2", "flag-1", "hook-2"]}\n'))
    outcome = run_pre(gw, ["a", "b"], flags=("flag-1", "flag-2"))
    assert outcome.decision_ids == ("flag-1", "flag-2", "hook-1", "hook-2")
    assert outcome.output_ignored == 0 and outcome.warnings == ()


@pytest.mark.parametrize("text", ["", "   \n", "{}", '{"other": [1, 2], "decision_id": []}'])
def test_empty_output_or_an_object_without_ids_is_fine(text: str) -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0, text))
    outcome = run_pre(gw, ["a"])
    assert outcome.decision is Decision.ALLOW and outcome.decision_ids == ()
    assert outcome.output_ignored == 0


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '"x"',
        "null",
        "42",
        '{"decision_id": 5}',
        '{"decision_id": null}',
        '{"decision_id": [1]}',
        '{"decision_id": ["ok", "bad id"]}',
        '{"decision_id": ""}',
        '{"decision_id": "' + "x" * 129 + '"}',
        '{"decision_id": "a", "decision_id": "b"}',
        '{"decision_id": NaN}',
        '{"n": Infinity, "decision_id": "a"}',
        "{" * 5000,
        '{"decision_id": "a"} trailing',
        '{"decision_id": "a"}{"decision_id": "b"}',
        '{"decision_id": "\ud800"}',
        '{"decision_id": "tab\\there"}',
        '{"decision_id": ' + json.dumps([f"i{i}" for i in range(33)]) + "}",
        '{"decision_id": "' + "x" * 70000 + '"}',
    ],
)
def test_malformed_output_is_ignored_with_a_warning_and_never_a_veto(text: str) -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0, text))
    outcome = run_pre(gw, ["a"], flags=("keep",))
    assert outcome.decision is Decision.ALLOW
    assert outcome.decision_ids == ("keep",)  # no ids from the ignored output
    assert outcome.output_ignored == 1
    assert outcome.warnings == ("hook_output_ignored",)


def test_32_ids_from_a_hook_are_accepted() -> None:
    gw = FakeGateway()
    ids = [f"i{i}" for i in range(32)]
    gw.on(sh("a"), exited(0, json.dumps({"decision_id": ids})))
    outcome = run_pre(gw, ["a"])
    assert outcome.decision_ids == tuple(ids) and outcome.output_ignored == 0


def test_flag_and_hook_ids_together_over_32_ignore_the_hook_ids() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0, json.dumps({"decision_id": ["h1", "h2"]})))
    flags = tuple(f"f{i}" for i in range(31))
    outcome = run_pre(gw, ["a"], flags=flags)
    assert outcome.decision_ids == flags and outcome.output_ignored == 1
    assert outcome.decision is Decision.ALLOW


def test_ids_that_repeat_a_flag_id_do_not_count_twice() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0, json.dumps({"decision_id": ["f0", "h1"]})))
    flags = tuple(f"f{i}" for i in range(31))
    outcome = run_pre(gw, ["a"], flags=flags)
    assert outcome.decision_ids == (*flags, "h1") and outcome.output_ignored == 0


def test_exactly_64_kib_is_read_and_one_byte_more_is_ignored() -> None:
    pad = " " * (hooks.STDOUT_CAP - len('{"decision_id": "a"}'))
    at_cap = '{"decision_id": "a"}' + pad
    assert len(at_cap) == hooks.STDOUT_CAP
    assert hooks.parse_decision_output(at_cap) == ["a"]
    assert hooks.parse_decision_output(at_cap + " ") is None


def test_exit_13_with_garbage_still_escalates_and_counts_the_ignored_output() -> None:
    gw = FakeGateway()
    gw.on(sh("e"), exited(13, "\x00\x01 garbage {"))
    outcome = run_pre(gw, ["e"])
    assert outcome.decision is Decision.ESCALATE
    assert outcome.output_ignored == 1


def test_exit_13_with_valid_ids_keeps_them() -> None:
    gw = FakeGateway()
    gw.on(sh("e"), exited(13, '{"decision_id": "why-1"}'))
    outcome = run_pre(gw, ["e"])
    assert outcome.decision is Decision.ESCALATE and outcome.decision_ids == ("why-1",)


def test_a_veto_takes_no_ids_from_its_output() -> None:
    gw = FakeGateway()
    gw.on(sh("v"), exited(2, '{"decision_id": "no"}'))
    outcome = run_pre(gw, ["v"], flags=("f",))
    assert outcome.decision is Decision.VETO and outcome.decision_ids == ("f",)


# -- post hooks -------------------------------------------------------------------------------


def post_payload(gw: FakeGateway) -> hooks.HookPayload:
    return hooks.post_retro_payload(
        gw, retro_id=RETRO, state="done", reason=None, findings=1, candidates=0, cost_usd=None
    )


def test_every_post_entry_runs_even_after_a_failure_and_the_outcome_is_a_warning() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(1))
    gw.on(sh("b"), CallResult(CallStatus.TIMEOUT))
    gw.on(sh("c"), exited(0))
    cfg = settings(post_retro=[{"cmd": c} for c in "abc"])
    outcome = run_hooks(gw, cfg, post_payload(gw))
    assert [e.status for e in outcome.entries] == ["exit", "timeout", "ok"]
    assert outcome.decision is Decision.ALLOW
    assert outcome.warnings == ("hook_failed",)
    assert [c["argv"][2] for c in gw.calls] == ["a", "b", "c"]  # type: ignore[index]


def test_post_exit_13_is_a_plain_failure_and_stdout_is_never_read() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(13, '{"decision_id": "x"}'))
    outcome = run_hooks(gw, settings(post_retro=[{"cmd": "a"}]), post_payload(gw))
    assert outcome.entries[0].status == "exit" and outcome.decision_ids == ()
    assert outcome.output_ignored == 0


def test_post_hooks_that_succeed_have_no_warning() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(0))
    outcome = run_hooks(
        gw, settings(post_attention=[{"cmd": "a"}]), hooks.ingest_failing_payload(gw, DETAILS)
    )
    assert outcome.warnings == () and outcome.entries[0].status == "ok"


DETAILS = {
    "consecutive_failures": 3,
    "first_failed_at": "2026-01-01T00:00:00.000Z",
    "last_failed_at": "2026-01-01T03:00:00.000Z",
}


def test_a_gateway_that_raises_in_a_post_hook_is_a_warning_not_an_error() -> None:
    gw = FakeGateway()
    outcome = run_hooks(gw, settings(post_retro=[{"cmd": "a"}]), post_payload(gw))
    assert outcome.entries[0].status == "spawn_failed" and outcome.warnings == ("hook_failed",)


def test_async_entries_run_beside_the_others_and_are_joined_before_return() -> None:
    gw = FakeGateway()
    for c in "abc":
        gw.on(sh(c), exited(0))
    cfg = settings(
        post_retro=[{"cmd": "a", "async": True}, {"cmd": "b"}, {"cmd": "c", "async": True}]
    )
    outcome = run_hooks(gw, cfg, post_payload(gw))
    assert sorted(c["argv"][2] for c in gw.calls) == ["a", "b", "c"]  # type: ignore[index]
    assert [(e.index, e.run_async) for e in outcome.entries] == [(0, True), (1, False), (2, True)]


def test_warning_text_is_fixed_and_carries_no_hook_output() -> None:
    gw = FakeGateway()
    gw.on(sh("a"), exited(1, "SECRET text"))
    outcome = run_hooks(gw, settings(post_retro=[{"cmd": "a"}]), post_payload(gw))
    lines = hooks.warning_text(outcome, "post-retro")
    assert lines == ["warning: post-retro hook entry 1 failed (exit)"]
    pre = hooks.HookOutcome(output_ignored=2)
    assert "hook_output_ignored" in hooks.warning_text(pre, "pre-retro")[0]


# -- the real gateway -------------------------------------------------------------------------


def real(cmd: str, timeout: float = 20) -> HookSettings:
    return settings(pre_retro=[{"cmd": cmd, "timeout": timeout}])


def test_real_hook_reads_the_payload_and_prints_a_decision(tmp_path: Path) -> None:
    gw = SystemGateway(kill_grace=1)
    out = tmp_path / "stdin.json"
    cmd = f'cat > \'{out}\'; printf \'{{"decision_id": "%s"}}\' "$SESSION_AUDIT_ID"'
    payload = hooks.pre_retro_payload(
        gw,
        retro_id=RETRO,
        scope=SCOPE,
        sessions=1,
        digest_bytes=1,
        items=0,
        skill="p:s",
        model=None,
        key=KEY,
        max_turns=1,
        timeout_seconds=1,
    )
    outcome = run_hooks(gw, real(cmd), payload)
    assert outcome.decision is Decision.ALLOW and outcome.decision_ids == (RETRO,)
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert seen == payload.document
    validate(seen, "session-audit/hooks/pre-retro.json")


def real_payload(gw: SystemGateway) -> hooks.HookPayload:
    return hooks.pre_retro_payload(
        gw,
        retro_id=RETRO,
        scope=SCOPE,
        sessions=1,
        digest_bytes=1,
        items=0,
        skill="p:s",
        model=None,
        key=KEY,
        max_turns=1,
        timeout_seconds=1,
    )


def test_real_veto_by_timeout_kills_the_group_with_a_grandchild(tmp_path: Path) -> None:
    gw = SystemGateway(kill_grace=1)
    pids = tmp_path / "pids"
    cmd = f"sleep 60 & echo $! > '{pids}'; wait"
    outcome = run_hooks(gw, real(cmd, timeout=0.5), real_payload(gw))
    grandchild = int(pids.read_text(encoding="utf-8"))
    try:
        assert outcome.decision is Decision.VETO and outcome.entries[0].status == "timeout"
        assert wait_until_gone(grandchild)
    finally:
        if not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_real_escalation_and_veto_by_exit_code() -> None:
    gw = SystemGateway(kill_grace=1)
    assert run_hooks(gw, real("exit 13"), real_payload(gw)).decision is Decision.ESCALATE
    assert run_hooks(gw, real("exit 1"), real_payload(gw)).decision is Decision.VETO
    assert run_hooks(gw, real("kill -9 $$"), real_payload(gw)).decision is Decision.VETO


def test_real_megabytes_of_stdout_are_ignored_not_a_veto(tmp_path: Path) -> None:
    gw = SystemGateway(kill_grace=1)
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys\nfor _ in range(160): sys.stdout.write('x' * 65536)\n", encoding="utf-8"
    )
    started = time.monotonic()
    outcome = run_hooks(gw, real(f"{PY} '{script}'"), real_payload(gw))
    assert outcome.decision is Decision.ALLOW and outcome.output_ignored == 1
    assert time.monotonic() - started < 15


def test_real_hook_that_closes_stdout_and_exits_zero_allows() -> None:
    gw = SystemGateway(kill_grace=1)
    outcome = run_hooks(gw, real("exec >&- 2>&-; exit 0"), real_payload(gw))
    assert outcome.decision is Decision.ALLOW and outcome.output_ignored == 0


def test_real_hook_that_ignores_term_is_killed_after_the_grace(tmp_path: Path) -> None:
    gw = SystemGateway(kill_grace=0.5)
    pids = tmp_path / "leader"
    cmd = f"echo $$ > '{pids}'; trap '' TERM; while :; do sleep 0.05; done"
    outcome = run_hooks(gw, real(cmd, timeout=0.5), real_payload(gw))
    assert outcome.decision is Decision.VETO
    assert wait_until_gone(int(pids.read_text(encoding="utf-8")))


def test_real_command_with_quotes_and_newlines_runs_as_one_script() -> None:
    gw = SystemGateway(kill_grace=1)
    cmd = 'x=\'a "b" \'"\'"\'c\'"\'"\ncase "$x" in *b*) exit 0;; esac\nexit 1'
    assert run_hooks(gw, real(cmd), real_payload(gw)).decision is Decision.ALLOW


def test_real_async_post_hook_is_joined_and_its_grandchild_is_gone(tmp_path: Path) -> None:
    gw = SystemGateway(kill_grace=1)
    pids = tmp_path / "pids"
    cfg = settings(post_retro=[{"cmd": f"sleep 60 & echo $! > '{pids}'", "async": True}])
    payload = hooks.post_retro_payload(
        gw, retro_id=RETRO, state="done", reason=None, findings=0, candidates=0, cost_usd=None
    )
    outcome = run_hooks(gw, cfg, payload)
    grandchild = int(pids.read_text(encoding="utf-8"))
    try:
        assert outcome.entries[0].status == "ok" and outcome.entries[0].run_async
        assert wait_until_gone(grandchild)
    finally:
        if not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


# -- an interrupt ends every started group (R1-1) ---------------------------------------------


def wait_for_pid_file(path: Path) -> int:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("the hook never wrote its pid")


def post_cfg(*entries: dict[str, Any]) -> HookSettings:
    return settings(post_retro=list(entries))


def test_an_interrupt_while_joining_async_entries_ends_their_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = SystemGateway(kill_grace=1)
    pids = tmp_path / "pids"
    cfg = post_cfg({"cmd": f"sleep 60 & echo $! > '{pids}'; wait", "async": True, "timeout": 60})
    real_join = threading.Thread.join
    calls: list[int] = []

    def join(self: threading.Thread, timeout: float | None = None) -> None:
        if not calls:
            calls.append(1)
            wait_for_pid_file(pids)
            raise KeyboardInterrupt
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", join)
    started = time.monotonic()
    grandchild = 0
    try:
        with pytest.raises(KeyboardInterrupt):
            run_hooks(gw, cfg, real_post_payload(gw))
        grandchild = wait_for_pid_file(pids)
        assert wait_until_gone(grandchild), "the hook outlived the interrupted call"
        assert time.monotonic() - started < 20
    finally:
        if grandchild and not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_an_interrupted_sync_entry_ends_the_async_groups_running_beside_it(
    tmp_path: Path,
) -> None:
    pids = tmp_path / "pids"

    class Interrupting(SystemGateway):
        def run_hook(self, command: str, **kw: Any) -> CallResult:
            if command == "interrupt-me":
                wait_for_pid_file(pids)
                raise KeyboardInterrupt
            return super().run_hook(command, **kw)

    gw = Interrupting(kill_grace=1)
    cfg = post_cfg(
        {"cmd": f"sleep 60 & echo $! > '{pids}'; wait", "async": True, "timeout": 60},
        {"cmd": "interrupt-me"},
    )
    started = time.monotonic()
    grandchild = 0
    try:
        with pytest.raises(KeyboardInterrupt):
            run_hooks(gw, cfg, real_post_payload(gw))
        grandchild = wait_for_pid_file(pids)
        assert wait_until_gone(grandchild), "the async hook outlived the interrupted call"
        assert time.monotonic() - started < 20
    finally:
        if grandchild and not gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def real_post_payload(gw: SystemGateway) -> hooks.HookPayload:
    return hooks.post_retro_payload(
        gw, retro_id=RETRO, state="done", reason=None, findings=1, candidates=0, cost_usd=None
    )
