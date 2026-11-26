"""``retro run`` end to end through the artifact check (issue #29): the stored retro, the single
transaction of the final write, the printed retro, hooks and log, and every validation reason as
exit 22. The frozen file is written by a wrapper of ``Launcher._collect`` once the run's identity
(retro id, run token, digest hash, manifest) exists."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from session_audit import cli, retroartifact, retrolaunch
from session_audit import retro as retro_mod
from session_audit.retroview import load_retro

from .fake_claude_session import FakeClaudeSession
from .ingesthelp import Corpus, connect, state_dir_of
from .retrohelp import RetroGateway
from .schemas import validate
from .test_retro_launch import (
    CSID,
    error_of,
    launch,
    log_entries,
    prepared_corpus,
    rows,
    session_run,
    the_retro,
    write_config,
)
from .test_retro_launch_flow import hook_config, payloads, recorder

MARKER = "flow-marker-" + "b" * 8

Builder = Callable[[Any], dict[str, Any]]


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
    from .fake_claude_session import plan_doc, step, version_step

    (tmp_path / "fake").mkdir()
    fake = FakeClaudeSession(tmp_path / "fake")
    fake.script("--version", version_step())
    fake.script("start --dry-run", step(plan_doc()))
    gw.fake = fake
    return fake


def good(launcher: Any, **over: Any) -> dict[str, Any]:
    item = launcher.fenced.manifest["sessions"][0]["items"][0]
    base: dict[str, Any] = {
        "retro_id": launcher.row.id,
        "run_token": launcher.run_token,
        "digest_sha256": launcher.fenced.digest_sha256,
        "marker": MARKER,
        "summary": "It went fine",
        "findings": [
            {
                "id": "f1",
                "category": "friction",
                "severity": "low",
                "title": "Slow start",
                "detail": "It took a while",
                "evidence": [{"kind": "item", "ref": item}],
            }
        ],
        "candidates": [
            {
                "id": "c1",
                "target_class": "config",
                "target_hint": "settings",
                "proposal": "Change a setting",
                "findings": ["f1"],
            }
        ],
    }
    return {**base, **over}


def freeze(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    build: Builder | bytes | None = good,
    *,
    real_marker: bool = False,
) -> None:
    """Make ``collect`` return a report whose frozen file holds what ``build`` makes."""
    real = retrolaunch.Launcher._collect

    def collect(self: Any, csid: str) -> Any:
        collected = real(self, csid)
        assert collected is not None
        if build is None:
            return dataclasses.replace(collected, report=None)
        data = build if isinstance(build, bytes) else json.dumps(build(self)).encode()
        path = tmp_path / "frozen-audit.json"
        path.write_bytes(data)
        info = {
            "name": "audit.json",
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        report = {"status": "done", "implicit": False, "artifact": info}
        return dataclasses.replace(collected, report=report)

    monkeypatch.setattr(retrolaunch.Launcher, "_collect", collect)
    if not real_marker:
        monkeypatch.setattr(retroartifact, "bundled_marker", lambda _gw, _dir: MARKER)


def stored_row_count(home: Path) -> int:
    return len(rows(home, "retro_artifacts"))


def test_a_valid_artifact_ends_done_and_is_stored_and_printed(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)
    post = tmp_path / "post"
    write_config(corpus.home, hook_config(tmp_path, post_retro=recorder(post)))
    result = launch(capsys, gw)
    assert result.code == 0, result.err
    document = json.loads(result.out)
    validate(document, "session-audit/cli/retro-run.json")
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("done", None) and row["finished_at"] is not None
    assert (document["id"], document["state"], document["reused"]) == (row["id"], "done", False)
    assert document["counts"]["findings"] == 1 and document["counts"]["candidates"] == 1
    assert document["counts"]["sessions"] == 2 and document["dropped"] == {
        "findings": 0,
        "candidates": 0,
    }
    assert document["summary"] == "It went fine" and document["candidates"][0]["risk"] == "high"
    assert document["metrics"]["total.tool_calls"] >= 0 and document["cost_usd"] == 0.5
    assert stored_row_count(corpus.home) == 1
    conn = connect(corpus.home)
    try:
        assert load_retro(conn, row["id"]) == {k: v for k, v in document.items() if k != "reused"}
    finally:
        conn.close()
    [seen] = payloads(post)
    assert (seen["state"], seen["findings"], seen["candidates"]) == ("done", 1, 1)
    done = [e for e in log_entries(corpus.home) if e["state"] == "done"]
    assert [(e["findings"], e["candidates"]) for e in done] == [(1, 1)]
    assert row["lease_pid"] is None


def test_the_marker_of_the_bundled_skill_file_validates_without_a_stub(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The real ``marker.txt`` of the bundled plugin (issue #30), read by ``bundled_marker``."""
    real = (
        Path(__file__).resolve().parent.parent / "plugin/skills/audit/references/marker.txt"
    ).read_text(encoding="ascii")
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, marker=real.strip()), real_marker=True)
    result = launch(capsys, gw)
    assert result.code == 0, result.err
    assert the_retro(corpus.home)["state"] == "done"


def test_the_second_run_reuses_the_stored_retro(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)
    first = json.loads(launch(capsys, gw).out)
    again = json.loads(launch(capsys, gw).out)
    assert again["reused"] is True and again["id"] == first["id"]


def test_human_output_of_a_done_retro_is_not_empty(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, summary="a\x1b[31mred"))
    capsys.readouterr()
    code = cli.main(["retro", "run", "--project", "/work"], gateway=gw, stdin_is_tty=False)
    out = capsys.readouterr().out
    assert code == 0 and "is done" in out and "summary:" in out and "\x1b" not in out


def test_the_artifact_is_never_written_to_disk_by_the_tool(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, summary="UNIQUE-MODEL-TEXT-" + "z" * 20))
    assert launch(capsys, gw).code == 0
    hits = [
        p
        for p in state_dir_of(corpus.home).rglob("*")
        if p.is_file()
        and p.name != "state.db"
        and not p.name.startswith("state.db-")
        and b"UNIQUE-MODEL-TEXT" in p.read_bytes()
    ]
    assert hits == []


# --- every reason, end to end, exit 22 ---------------------------------------------------------

CASES: dict[str, tuple[str, Builder | bytes | None]] = {
    "no_artifact": ("no_artifact", None),
    "invalid_json": ("artifact_invalid", b"{not json"),
    "identity_mismatch": ("identity_mismatch", lambda run: good(run, run_token="q" * 24)),
    "marker_mismatch": ("marker_mismatch", lambda run: good(run, marker="wrong-marker-" + "c" * 8)),
    "no_valid_findings": (
        "no_valid_findings",
        lambda run: good(
            run,
            findings=[
                {
                    **good(run)["findings"][0],
                    "evidence": [{"kind": "item", "ref": "s1.i999"}],
                }
            ],
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_validation_reason_exits_22_and_stores_nothing(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> None:
    reason, build = CASES[name]
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=build is not None)
    freeze(monkeypatch, tmp_path, build)
    post, attention = tmp_path / "post", tmp_path / "attention"
    write_config(
        corpus.home,
        hook_config(tmp_path, post_retro=recorder(post), post_attention=recorder(attention)),
    )
    envelope = error_of(launch(capsys, gw), 22, reason)
    assert envelope["code"] == "artifact_invalid"
    row = the_retro(corpus.home)
    assert (row["state"], row["reason"]) == ("failed", reason)
    assert envelope["details"]["retro_id"] == row["id"]
    assert stored_row_count(corpus.home) == 0
    [seen] = payloads(post)
    assert (seen["state"], seen["findings"], seen["candidates"]) == ("failed", 0, 0)
    assert len(payloads(attention)) == 1


def test_a_run_whose_session_failed_never_reads_the_artifact(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "failed", artifact=True)
    freeze(monkeypatch, tmp_path)
    error_of(launch(capsys, gw), 18)
    assert stored_row_count(corpus.home) == 0


# --- one transaction ---------------------------------------------------------------------------


def test_the_artifact_and_the_done_state_are_one_transaction(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failure while the artifact row is written leaves the retro ``validating`` with no stored
    artifact (recovery turns it into ``failed/runner_lost``), never ``done`` without a row."""
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)

    def boom(conn: Any, retro_id: str, stored: Any) -> None:
        conn.execute(
            "INSERT INTO retro_artifacts (retro_id, artifact, summary) VALUES (?, '{}', 'x')",
            (retro_id,),
        )
        raise RuntimeError("crash between the two writes")

    monkeypatch.setattr(retroartifact, "write_artifact", boom)
    result = launch(capsys, gw)
    assert result.code == 1
    row = the_retro(corpus.home)
    assert row["state"] == "validating" and row["finished_at"] is None and row["redacted"] == 0
    assert stored_row_count(corpus.home) == 0  # the insert rolled back with the state write
    assert row["lease_pid"] is None


def test_a_done_run_reads_the_frozen_file_before_the_session_is_forgotten(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)
    seen: list[str] = []
    real = retroartifact.validate_and_store_artifact

    def spy(collected: Any, context: Any) -> Any:
        seen.append("forget" if any(c["key"] == "forget" for c in cs.calls()) else "before")
        return real(collected, context)

    monkeypatch.setattr(retrolaunch, "validate_and_store_artifact", spy)
    assert launch(capsys, gw).code == 0
    assert seen == ["before"] and CSID in json.dumps(cs.calls())


# --- review fixes: expected marker, masks of the check, read-back ------------------------------


def with_custom_skill(corpus: Corpus, tmp_path: Path, retro_extra: str = "") -> Path:
    from .test_retro_plan import make_plugin

    plugin = make_plugin(tmp_path / "custom", "custom", "review")
    write_config(
        corpus.home,
        f"retro:\n  plugin_dirs:\n    - {plugin}\n{retro_extra}",
    )
    return plugin


def run_custom(capsys: pytest.CaptureFixture[str], gw: RetroGateway) -> Any:
    return launch(capsys, gw, "--skill", "custom:review")


def test_the_bundled_skill_expects_the_bundled_marker_not_the_configured_one(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)  # the bundled marker is MARKER; good() writes MARKER
    write_config(corpus.home, "retro:\n  skill_marker: configured-marker-" + "e" * 8 + "\n")
    assert launch(capsys, gw).code == 0


def test_the_bundled_skill_rejects_the_configured_marker(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = "configured-marker-" + "e" * 8
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, marker=configured))
    write_config(corpus.home, f"retro:\n  skill_marker: {configured}\n")
    error_of(launch(capsys, gw), 22, "marker_mismatch")


def test_a_custom_skill_expects_its_configured_marker(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = "custom-marker-" + "d" * 8
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, marker=configured))
    with_custom_skill(corpus, tmp_path, f"  skill_marker: {configured}\n")
    result = run_custom(capsys, gw)
    assert result.code == 0, result.err


def test_a_custom_skill_does_not_use_the_bundled_marker(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)  # writes the bundled MARKER
    with_custom_skill(corpus, tmp_path, "  skill_marker: custom-marker-" + "d" * 8 + "\n")
    error_of(run_custom(capsys, gw), 22, "marker_mismatch")


def test_a_custom_skill_without_a_marker_rejects_any_marker(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)  # a marker is written
    with_custom_skill(corpus, tmp_path)
    error_of(run_custom(capsys, gw), 22, "marker_mismatch")


def test_a_custom_skill_without_a_marker_accepts_an_artifact_without_one(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(
        monkeypatch,
        tmp_path,
        lambda run: {k: v for k, v in good(run).items() if k != "marker"},
    )
    with_custom_skill(corpus, tmp_path)
    result = run_custom(capsys, gw)
    assert result.code == 0, result.err


SECRET = "s3cr3t-" + "value-9876"


def test_a_secret_in_a_mask_env_variable_is_redacted_and_counted_end_to_end(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUDIT_PLAIN_NAME", SECRET)
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, summary=f"it leaked {SECRET} here"))
    write_config(corpus.home, "redaction:\n  mask_env: [AUDIT_PLAIN_NAME]\n")
    result = launch(capsys, gw)
    assert result.code == 0, result.err
    document = json.loads(result.out)
    assert SECRET not in result.out and "[REDACTED:" in document["summary"]
    assert the_retro(corpus.home)["redacted"] >= 1
    stored = rows(corpus.home, "retro_artifacts")[0]["artifact"]
    assert SECRET not in stored


def test_the_check_redacts_with_the_masks_of_the_digest_not_os_environ(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path, lambda run: good(run, summary=f"it leaked {SECRET} here"))
    real = retro_mod.prepare

    def prepare(*a: Any, **kw: Any) -> Any:
        return real(*a, **{**kw, "env": {"AUDIT_TOKEN": SECRET}})

    monkeypatch.setattr(retro_mod, "prepare", prepare)
    result = launch(capsys, gw)
    assert result.code == 0, result.err
    assert SECRET not in result.out and "[REDACTED:" in json.loads(result.out)["summary"]


def test_a_failing_read_back_of_a_done_retro_only_notes_it(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)

    def boom(conn: Any, retro_id: str) -> Any:
        raise RuntimeError("the read-back failed")

    monkeypatch.setattr(retrolaunch, "load_retro", boom)
    result = launch(capsys, gw)
    assert result.code == 0 and result.out == ""
    assert "could not be read back" in result.err
    assert the_retro(corpus.home)["state"] == "done"


def test_the_read_back_does_not_take_the_write_lock(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "done", artifact=True)
    freeze(monkeypatch, tmp_path)
    real = retrolaunch.load_retro
    writable: list[bool] = []

    def spy(conn: Any, retro_id: str) -> Any:
        try:
            conn.execute("CREATE TABLE probe_write (x)")
            writable.append(True)
        except Exception:
            writable.append(False)
        return real(conn, retro_id)

    monkeypatch.setattr(retrolaunch, "load_retro", spy)
    assert launch(capsys, gw).code == 0
    assert writable == [False]
