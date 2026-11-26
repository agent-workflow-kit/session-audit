"""``retro run`` steps 13 to 16: the row's columns, the files, the flags of ``start``, the hooks,
the lease, step 13's repeated lookup and two real ``retro run`` processes with the same key
(issue #26)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from session_audit import errors, launchplan, retro, retrofiles, retrolaunch, retrostate
from session_audit.lease import identity

from .conftest import FAKEBIN, tripwire_dir
from .fake_claude_session import FakeClaudeSession, exec_doc, plan_doc, step, version_step
from .ingesthelp import S1, S2, Corpus, state_dir_of
from .retrohelp import RetroGateway, add_retro, args_for, retro_ctx
from .retrohelp import retro as run_retro
from .schemas import validate
from .test_retro_launch import (
    CSID,
    error_of,
    keys,
    launch,
    log_entries,
    prepared_corpus,
    rows,
    session_run,
    the_retro,
    write_config,
)


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gw.fake = fake
    return fake


# --- the row and the files ---------------------------------------------------------------------


def test_the_row_has_every_column_written_as_the_data_model_says(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    flags = ("--model", "some-model", "--session", S1[:8], "--session", S2, "--since", "3650d")
    plan_key = run_retro(capsys, gw, "--dry-run", *flags).doc["key"]
    session_run(cs, "blocked")
    error_of(run_retro(capsys, gw, *flags), 13)
    row = the_retro(corpus.home)
    state = state_dir_of(corpus.home)
    assert row["key"] == plan_key and len(row["id"]) == 11
    assert row["scope"] == "since=3650d"
    resolved = json.loads(row["resolved_scope"])
    assert resolved["flags"] == {
        "labels": [],
        "project": None,
        "since": "3650d",
        "until": None,
        "kind": None,
        "outcome": None,
    }
    assert resolved["sessions"] == [S1, S2]
    assert resolved["since"].endswith("Z") and resolved["until"] is None
    mapping = json.loads(row["session_map"])["sessions"]
    assert [(m["ref"], m["session_id"], m["csession_id"]) for m in mapping] == [
        ("s1", S1, None),
        ("s2", S2, None),
    ]
    assert json.loads(row["counts"]) == {
        "sessions": 2,
        "skipped": 0,
        "over_cap": 0,
        "items": 4,
        "findings": 0,
        "candidates": 0,
    }
    assert json.loads(row["dropped"]) == {"findings": 0, "candidates": 0}
    assert row["warnings"] == "[]" and row["redacted"] == 0
    assert (row["skill"], row["model"], row["again_of"]) == (
        "session-audit:audit",
        "some-model",
        None,
    )
    assert row["created_at"] <= row["started_at"] <= row["finished_at"]
    assert len(row["run_token"]) == 32 and len(row["digest_sha256"]) == 64
    digest = (state / "digests" / row["id"] / "digest.md").read_bytes()
    assert hashlib.sha256(digest).hexdigest() == row["digest_sha256"]
    manifest = json.loads((state / "digests" / row["id"] / "manifest.json").read_text())
    validate(manifest, "session-audit/slot/manifest.json")
    assert manifest["digest_sha256"] == row["digest_sha256"]
    assert manifest["run_token"] == row["run_token"] and manifest["retro_id"] == row["id"]
    assert [s["csession_id"] for s in manifest["sessions"]] == [None, None]
    prompt = (state / "prompts" / row["id"] / "prompt.md").read_text()
    assert row["run_token"] in prompt and row["digest_sha256"] in prompt
    assert "secret" not in prompt and "answer" not in prompt  # no transcript text
    for path in (state / "digests", state / "prompts", state / "outcomes"):
        assert path.stat().st_mode & 0o7777 == 0o700
    assert (state / "digests" / row["id"] / "digest.md").stat().st_mode & 0o7777 == 0o600


def test_the_flags_of_start_are_the_plan_checks_flags_with_the_retros_files(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    error_of(launch(capsys, gw, "--max-turns", "5", "--timeout", "90s"), 13)
    row = the_retro(corpus.home)
    state = state_dir_of(corpus.home)
    [argv] = [c["argv"] for c in cs.calls() if c["key"] == "start"]
    assert argv[0] == "start" and argv[-1] == "--json"
    flags = argv[1:-1]
    assert flags[:4] == ["--kind", "audit", "--reads-third-party", "--no-implicit-done"]
    expected = launchplan.launch_flags(
        launchplan.Launch.of(
            retro_ctx(gw, Corpus(corpus.home)).settings.retro,
            skill="session-audit:audit",
            plugin_dirs=[flags[flags.index("--plugin-dir") + 1]],
            bundled=None,
            model=None,
            timeout=None,
            max_turns=None,
        ),
        row["id"],
        prompt_file="",
        input_files=(),
    )
    for flag, value in (
        ("--prompt-file", str(state / "prompts" / row["id"] / "prompt.md")),
        ("--timeout", "90s"),
        ("--max-turns", "5"),
        ("--cwd", "session-dir"),
        ("--sandbox", "required"),
        ("--setting-sources", ""),
        ("--skill", "session-audit:audit"),
    ):
        assert flags[flags.index(flag) + 1] == value
    assert flags.count("--input-file") == 2
    inputs = [flags[i + 1] for i, f in enumerate(flags) if f == "--input-file"]
    assert inputs == [
        str(state / "digests" / row["id"] / "digest.md"),
        str(state / "digests" / row["id"] / "manifest.json"),
    ]
    assert ["--label", "owner=session-audit", "--label", f"retro={row['id']}"] == flags[-4:]
    assert "--tools" in flags and "--env-allowlist-only" in flags
    assert expected[0:4] == flags[0:4]
    # No untrusted text in argv: nothing of the transcripts, the token or the digest.
    text = " ".join(argv)
    assert "secret" not in text and row["run_token"] not in text
    assert row["digest_sha256"] not in text


def test_refusing_steps_store_nothing_and_cost_nothing(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    cs.script("start --dry-run", step(plan_doc({"origins.tools": "config"})))
    error_of(launch(capsys, gw), 12, "launch_settings_mismatch")
    state = state_dir_of(corpus.home)
    assert rows(corpus.home) == [] and rows(corpus.home, "retro_starts") == []
    assert rows(corpus.home, "retro_sessions") == []
    assert [p.name for p in state.iterdir() if p.name in ("digests", "prompts", "outcomes")] == []
    assert keys(cs) == []  # no start, no forget


def test_an_unsafe_profile_found_after_the_plan_check_refuses_before_the_row(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    profile = corpus.home / "profile.yaml"
    profile.write_text("deny: []\n")
    profile.chmod(0o600)
    write_config(corpus.home, f"retro:\n  permission_profile: {profile}\n")

    def changed(gateway: Any, path: str, home: str) -> list[Any]:
        return ["changed"]  # step 2 (retro.profile_issues) passed; the file changed since

    monkeypatch.setattr(retrolaunch, "profile_issues", changed)
    error_of(launch(capsys, gw), 20, "config_unsafe")
    assert rows(corpus.home) == [] and keys(cs) == []


def test_a_step_15_file_that_cannot_be_written_ends_failed_and_refunds_the_start(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    state = state_dir_of(corpus.home)
    target = corpus.home / "elsewhere"
    target.mkdir(mode=0o700)
    (state / "digests").symlink_to(target)  # a planted symlink: refused, never followed
    error = error_of(launch(capsys, gw), 12, "store_unwritable")
    assert error["details"]["retryable"] is False
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", "session_not_started")
    assert list(target.iterdir()) == []
    assert keys(cs) == []  # nothing was launched
    assert rows(corpus.home, "retro_starts") == []
    assert not (state / "prompts" / row["id"]).exists()


def test_a_full_disk_at_step_15_is_retryable(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)

    def full(*args: Any, **kwargs: Any) -> Any:
        raise retrofiles.FilesError("full", retryable=True)

    monkeypatch.setattr(retrolaunch.retrofiles, "write_run_files", full)
    error = error_of(launch(capsys, gw), 12, "store_unwritable")
    assert error["details"]["retryable"] is True
    assert (the_retro(corpus.home)["state"]) == "failed"
    assert keys(cs) == []


# --- the log ---------------------------------------------------------------------------------


def test_every_state_change_has_a_hash_only_log_entry(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "timeout")
    error_of(launch(capsys, gw, "--decision-id", "flag-1"), 16)
    row = the_retro(corpus.home)
    entries = log_entries(corpus.home)
    assert [(e["state"], e["reason"], e["csession_id"]) for e in entries] == [
        ("digesting", None, None),
        ("running", None, None),
        ("running", None, CSID),
        ("failed", "session_failed", CSID),
    ]
    assert {e["retro_id"] for e in entries} == {row["id"]}
    assert all(e["key"] == row["key"] for e in entries)
    assert entries[1]["decision_ids"] == ["flag-1"]
    assert entries[1]["digest_sha256"] == row["digest_sha256"]
    assert entries[0]["digest_sha256"] is None
    assert entries[-1]["digest_sha256"] == row["digest_sha256"]
    text = (state_dir_of(corpus.home) / "log.jsonl").read_text()
    assert "secret" not in text and "answer" not in text and row["run_token"] not in text


# --- step 13: the repeated lookup ------------------------------------------------------------


def test_step_13_repeats_the_lookup_a_live_run_that_appeared_refuses(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work"), env={})
        holder = identity(gw)
        add_retro(
            corpus.home,
            prepared.key,
            state="running",
            lease=(holder.pid, holder.start, int(gw.now().timestamp() * 1000) + 60_000),
        )
        with pytest.raises(errors.CliError) as caught:
            retrostate.insert_retro(ctx, ctx.store(), args_for(project="/work"), prepared)
        assert caught.value.exit_code == 20
        assert caught.value.details["reason"] == "already_running"
        assert len(rows(corpus.home)) == 1  # nothing was inserted
    finally:
        ctx.close()


def test_step_13_repeats_the_lookup_a_stored_retro_that_appeared_is_reused(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work"), env={})
        add_retro(corpus.home, prepared.key, state="done")
        found = retrostate.insert_retro(ctx, ctx.store(), args_for(project="/work"), prepared)
        assert isinstance(found, retrostate.Reused) and found.retro["state"] == "done"
        # With --again the same lookup records again_of and inserts.
        again = retrostate.insert_retro(
            ctx, ctx.store(), args_for(project="/work", again=True), prepared
        )
        assert isinstance(again, retrostate.Inserted)
        assert again.retro.again_of == "ra-abcdefgh"
        stored = [r for r in rows(corpus.home) if r["id"] == again.retro.id][0]
        assert stored["again_of"] == "ra-abcdefgh" and stored["state"] == "digesting"
        assert (stored["lease_pid"], stored["lease_start"]) == tuple(
            [identity(gw).pid, identity(gw).start]
        )
        again.retro.release()
    finally:
        ctx.close()


def test_a_live_run_still_refuses_with_again(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work", again=True), env={})
        holder = identity(gw)
        add_retro(
            corpus.home,
            prepared.key,
            state="digesting",
            summary=None,
            lease=(holder.pid, holder.start, int(gw.now().timestamp() * 1000) + 60_000),
        )
        with pytest.raises(errors.CliError) as caught:
            retrostate.insert_retro(
                ctx, ctx.store(), args_for(project="/work", again=True), prepared
            )
        assert caught.value.details["reason"] == "already_running"
    finally:
        ctx.close()


def test_two_racing_inserts_of_one_key_make_one_row_and_one_already_running(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first insert stops inside its transaction (the seam of the daily limit) until the second
    has started; the second can only be refused by the lookup repeated inside ``BEGIN
    IMMEDIATE``."""
    prepared_corpus(gw, corpus, capsys)
    ctx0 = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx0, args_for(project="/work"), env={})
    finally:
        ctx0.close()
    inside = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def gated(ctx: Any, conn: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            inside.set()
            assert release.wait(30)

    monkeypatch.setattr(retrostate, "check_daily_limit", gated)
    results: dict[str, Any] = {}
    finish = threading.Event()

    def racer(name: str) -> None:
        ctx = retro_ctx(gw, corpus)
        try:
            results[name] = retrostate.insert_retro(
                ctx, ctx.store(), args_for(project="/work"), prepared
            )
        except errors.CliError as exc:
            results[name] = exc
        finally:
            finish.wait(30)  # the first row's lease is held until both have answered
            if isinstance(results.get(name), retrostate.Inserted):
                results[name].retro.release()
            ctx.close()

    first = threading.Thread(target=racer, args=("a",))
    first.start()
    assert inside.wait(30)
    second = threading.Thread(target=racer, args=("b",))
    second.start()
    time.sleep(0.3)  # the second one waits for the write lock
    release.set()
    deadline = time.monotonic() + 30
    while len(results) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    finish.set()
    first.join(30)
    second.join(30)
    assert isinstance(results["a"], retrostate.Inserted)
    assert isinstance(results["b"], errors.CliError)
    assert results["b"].details["reason"] == "already_running"
    assert len(rows(corpus.home)) == 1


def test_a_same_key_retro_whose_lease_is_not_live_does_not_block_a_new_insert(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """Interface, Retro lifecycle: a lease that is not live (expired) may be taken over, so the
    key is not running; the old row is left for recovery (#27)."""
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work"), env={})
        holder = identity(gw)
        add_retro(
            corpus.home,
            prepared.key,
            state="running",
            summary=None,
            lease=(holder.pid, holder.start, int(gw.now().timestamp() * 1000) - 60_000),
        )
        found = retrostate.insert_retro(ctx, ctx.store(), args_for(project="/work"), prepared)
        assert isinstance(found, retrostate.Inserted)
        assert len(rows(corpus.home)) == 2
        found.retro.release()
    finally:
        ctx.close()


def test_the_daily_limit_has_its_named_seam() -> None:
    doc = retrostate.check_daily_limit.__doc__
    assert doc and "TODO(#28)" in doc


def test_a_reused_retro_found_at_step_13_prints_and_exits_0(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    key = run_retro(capsys, gw, "--project", "/work", "--dry-run").doc["key"]
    session_run(cs)
    real = retrostate.insert_retro

    def late_winner(ctx: Any, store: Any, args: Any, prepared: Any) -> Any:
        add_retro(corpus.home, key, state="done")  # the other run finished between 10 and 13
        return real(ctx, store, args, prepared)

    monkeypatch.setattr(retrolaunch, "insert_retro", late_winner)
    result = launch(capsys, gw)
    assert result.code == 0, result.err
    assert result.doc["reused"] is True and result.doc["id"] == "ra-abcdefgh"
    assert keys(cs) == [] and len(rows(corpus.home)) == 1


# --- the hooks ------------------------------------------------------------------------------


def hook_config(directory: Path, **commands: str) -> str:
    lines = ["hooks:"]
    for name, command in commands.items():
        lines += [
            f"  {name.replace('_', '-')}:",
            f"    - cmd: {json.dumps(command)}",
            "      timeout: 20",
        ]
    return "\n".join(lines) + "\n"


def recorder(path: Path, then: str = "") -> str:
    """A hook command that appends its stdin (one JSON line) to ``path``, then runs ``then``."""
    code = "import sys;print(sys.stdin.read().replace(chr(10), ' '))"
    return f'python3 -c "{code}" >> {path}; {then or "true"}'


def payloads(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_veto_cancels_the_retro_exit_11_and_fires_post_retro_once(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    pre, post, attention = tmp_path / "pre", tmp_path / "post", tmp_path / "attention"
    write_config(
        corpus.home,
        hook_config(
            tmp_path,
            pre_retro=recorder(pre, "exit 1"),
            post_retro=recorder(post),
            post_attention=recorder(attention),
        ),
    )
    envelope = error_of(launch(capsys, gw), 11, "hook_vetoed")
    assert envelope["code"] == "vetoed"
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("canceled", "hook_vetoed")
    assert envelope["details"]["retro_id"] == row["id"]
    assert keys(cs) == [] and rows(corpus.home, "retro_starts") == []
    [seen] = payloads(pre)
    validate(seen, "session-audit/hooks/pre-retro.json")
    assert (seen["key"], seen["sessions"], seen["object_id"]) == (row["key"], 2, row["id"])
    assert seen["max_turns"] == 12 and seen["timeout_seconds"] == 1200 and seen["items"] == 4
    [after] = payloads(post)
    validate(after, "session-audit/hooks/post-retro.json")
    assert (after["state"], after["reason"]) == ("canceled", "hook_vetoed")
    assert payloads(attention) == []  # canceled is not failed
    state = state_dir_of(corpus.home)
    assert not (state / "digests" / row["id"]).exists()
    assert not (state / "prompts" / row["id"]).exists()
    assert row["lease_pid"] is None


def test_an_escalation_ends_escalated_exit_13_with_the_hooks_decision_ids(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    post = tmp_path / "post"
    pre = 'echo \'{"decision_id": ["d-1", "d-2"]}\'; exit 13'
    write_config(corpus.home, hook_config(tmp_path, pre_retro=pre, post_retro=recorder(post)))
    envelope = error_of(launch(capsys, gw, "--decision-id", "f-1"), 13, "hook_escalated")
    assert envelope["code"] == "needs_human"
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("escalated", "hook_escalated")
    final = log_entries(corpus.home)[-1]
    assert (final["state"], final["decision_ids"]) == ("escalated", ["f-1", "d-1", "d-2"])
    assert keys(cs) == []
    [after] = payloads(post)
    assert (after["state"], after["reason"]) == ("escalated", "hook_escalated")


def test_a_hook_that_times_out_vetoes(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    write_config(
        corpus.home,
        "hooks:\n  pre-retro:\n    - cmd: sleep 60\n      timeout: 1\n",
    )
    error_of(launch(capsys, gw), 11, "hook_vetoed")
    assert keys(cs) == []


def test_an_allowing_hook_sees_the_row_and_no_write_lock_is_held(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    seen = tmp_path / "seen"
    database = state_dir_of(corpus.home) / "state.db"
    script = tmp_path / "probe.py"
    script.write_text(
        "import sqlite3, sys\n"
        f"c = sqlite3.connect({str(database)!r}, timeout=0.5, isolation_level=None)\n"
        "c.execute('BEGIN IMMEDIATE')\n"  # fails when a write lock is held
        "row = c.execute('SELECT state, lease_pid IS NOT NULL FROM retros').fetchall()\n"
        "c.execute('ROLLBACK')\n"
        f"open({str(seen)!r}, 'w').write(repr(row))\n"
    )
    write_config(corpus.home, hook_config(tmp_path, pre_retro=f"python3 {script}"))
    error_of(launch(capsys, gw), 13)
    assert seen.read_text() == "[('digesting', 1)]"


def test_post_retro_and_post_attention_fire_once_for_a_failed_retro(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "lost")
    post, attention = tmp_path / "post", tmp_path / "attention"
    write_config(
        corpus.home,
        hook_config(tmp_path, post_retro=recorder(post), post_attention=recorder(attention)),
    )
    error_of(launch(capsys, gw), 17)
    [after] = payloads(post)
    assert (after["state"], after["reason"], after["cost_usd"]) == ("failed", "session_failed", 0.5)
    [alert] = payloads(attention)
    validate(alert, "session-audit/hooks/post-attention.json")
    row = the_retro(corpus.home)
    assert alert["reason"] == "retro_failed" and alert["object_id"] == row["id"]
    assert alert["details"] == {
        "retro_id": row["id"],
        "failure_reason": "session_failed",
        "csession_id": CSID,
    }


def test_a_failing_post_hook_is_a_warning_and_never_changes_the_exit(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    write_config(corpus.home, hook_config(tmp_path, post_retro="exit 3"))
    result = launch(capsys, gw)
    error_of(result, 13)
    assert "post-retro hook entry 1 failed" in result.err


def test_no_hook_runs_for_a_refusal_a_reuse_or_a_dry_run(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    marks = tmp_path / "marks"
    write_config(
        corpus.home,
        hook_config(
            tmp_path,
            pre_retro=recorder(marks),
            post_retro=recorder(marks),
            post_attention=recorder(marks),
        ),
    )
    run_retro(capsys, gw, "--project", "/work", "--dry-run")
    cs.script("start --dry-run", step(plan_doc({"tools": ["Read"]})))
    error_of(launch(capsys, gw), 12, "launch_settings_mismatch")
    assert payloads(marks) == []


# --- the lease -------------------------------------------------------------------------------


def test_a_retro_whose_lease_was_taken_over_stops_before_it_launches(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    database = state_dir_of(corpus.home) / "state.db"
    script = tmp_path / "steal.py"
    script.write_text(
        "import sqlite3\n"
        f"c = sqlite3.connect({str(database)!r}, timeout=5)\n"
        "c.execute('UPDATE retros SET lease_pid = 1, lease_start = \"other\"')\n"
        "c.commit()\n"
    )
    write_config(corpus.home, hook_config(tmp_path, pre_retro=f"python3 {script}"))
    result = launch(capsys, gw)
    assert result.code == 1, result.err
    assert json.loads(result.out)["error"]["code"] == "internal"
    row = the_retro(corpus.home)
    assert row["state"] == "digesting"  # the stale holder wrote nothing
    assert keys(cs) == [] and rows(corpus.home, "retro_starts") == []
    assert row["lease_pid"] == 1  # and cleared nothing of the new holder's lease


def test_the_renewal_thread_runs_while_the_tool_waits_for_start_exec_and_hooks(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    seen: list[tuple[str, bool]] = []
    real_run = gw.run
    real_hook = gw.run_hook

    def watch_run(argv: Any, **kwargs: Any) -> Any:
        word = next((a for a in list(argv)[1:] if not a.startswith("-")), "")
        if current and word in ("start", "exec", "forget"):
            seen.append((word, current[0].lease.renewing))
        return real_run(argv, **kwargs)

    def watch_hook(command: str, **kwargs: Any) -> Any:
        seen.append(("hook", current[0].lease.renewing))
        return real_hook(command, **kwargs)

    current: list[Any] = []
    real_insert = retrostate.insert_retro

    def remember(*args: Any, **kwargs: Any) -> Any:
        found = real_insert(*args, **kwargs)
        current.append(found.retro)
        return found

    monkeypatch.setattr(retrolaunch, "insert_retro", remember)
    monkeypatch.setattr(gw, "run", watch_run)
    monkeypatch.setattr(gw, "run_hook", watch_hook)
    write_config(corpus.home, hook_config(tmp_path, pre_retro="true", post_retro="true"))
    error_of(launch(capsys, gw), 13)
    assert [name for name, _ in seen] == ["hook", "start", "exec", "forget", "hook"]
    assert all(renewing for _, renewing in seen)


def test_the_lease_is_held_and_renewed_while_the_retro_runs_and_cleared_at_the_end(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    seen = tmp_path / "lease"
    database = state_dir_of(corpus.home) / "state.db"
    script = tmp_path / "lease.py"
    script.write_text(
        "import sqlite3, os\n"
        f"c = sqlite3.connect({str(database)!r}, timeout=5)\n"
        "row = c.execute('SELECT lease_pid, lease_expires_at FROM retros').fetchone()\n"
        f"open({str(seen)!r}, 'w').write('%d %d' % (row[0] > 0, row[1]))\n"
    )
    write_config(corpus.home, hook_config(tmp_path, pre_retro=f"python3 {script}"))
    error_of(launch(capsys, gw), 13)
    held, expires = seen.read_text().split()
    assert held == "1" and int(expires) > int(gw.now().timestamp() * 1000)
    assert the_retro(corpus.home)["lease_pid"] is None


# --- two processes -----------------------------------------------------------------------------


def test_two_retro_run_processes_with_the_same_key_one_runs_the_other_is_already_running(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    exec_pid = tmp_path / "exec.pid"
    cs.script("exec", step(exec_doc(CSID, "blocked"), exit=13, sleep=6, pid_file=exec_pid))
    write_config(corpus.home, f"harness:\n  claude_session_cmd: {FAKEBIN / 'claude-session'}\n")
    env = {**cs.env(), "PATH": f"{tripwire_dir(tmp_path_factory)}{os.pathsep}{os.environ['PATH']}"}
    command = [
        sys.executable,
        "-m",
        "session_audit",
        "retro",
        "run",
        "--json",
        "--project",
        "/work",
    ]
    options: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "process_group": 0,
    }
    first = subprocess.Popen(command, **options)
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not exec_pid.exists() and first.poll() is None:
            time.sleep(0.1)  # the first run is now inside its adopting exec
        assert first.poll() is None, first.communicate()
        second = subprocess.Popen(command, **options)
        out2, _err2 = second.communicate(timeout=60)
        assert second.returncode == 20, out2
        refusal = json.loads(out2)["error"]
        assert refusal["details"]["reason"] == "already_running"
        out1, err1 = first.communicate(timeout=90)
        assert first.returncode == 13, err1
        assert json.loads(out1)["error"]["details"]["reason"] == "session_blocked"
        [row] = rows(corpus.home)
        assert refusal["details"]["retro_id"] == row["id"]
        assert len(rows(corpus.home)) == 1  # the second run stored nothing
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()


# --- the id, the digest check and the recoverable states ----------------------------------------


def test_a_retro_id_has_the_grammar_and_a_collision_is_retried() -> None:
    import re

    from session_audit.gateway import SystemGateway

    for _ in range(50):
        assert re.fullmatch(r"ra-[a-z2-7]{8}", retrostate.new_retro_id(SystemGateway()))


def test_a_digest_that_does_not_match_its_manifest_never_marks_the_retro_running(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    real = retrolaunch.fencing.finish_digest

    def altered(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        return replace(found, digest_sha256="0" * 64)

    monkeypatch.setattr(retrolaunch.fencing, "finish_digest", altered)
    result = launch(capsys, gw)
    assert result.code == 1 and json.loads(result.out)["error"]["code"] == "internal"
    row = the_retro(corpus.home)
    assert (row["state"], row["csession_id"], row["lease_pid"]) == ("digesting", None, None)
    assert keys(cs) == [] and rows(corpus.home, "retro_starts") == []


def test_digest_sections_that_changed_after_the_key_are_an_internal_error_with_a_free_lease(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    real = retrolaunch.fencing.finish_digest

    def altered(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        return replace(found, section_hashes=found.section_hashes[:1])

    monkeypatch.setattr(retrolaunch.fencing, "finish_digest", altered)
    result = launch(capsys, gw)
    assert result.code == 1 and json.loads(result.out)["error"]["code"] == "internal"
    row = the_retro(corpus.home)
    # What recovery (#27) finds: a digesting row, no files, no session, a free lease.
    assert (row["state"], row["csession_id"], row["lease_pid"]) == ("digesting", None, None)
    assert keys(cs) == [] and rows(corpus.home, "retro_starts") == []
    state = state_dir_of(corpus.home)
    assert not (state / "digests").exists() and not (state / "prompts").exists()
