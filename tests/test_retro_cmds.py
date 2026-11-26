"""``retro list``, ``retro show``, ``retro delete`` and ``export retros`` (issue #31): golden
outputs against the schemas, every stored state, sanitizing, the delete transaction and its
refusals, and concurrency with a writer, all through the real CLI on a private store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from session_audit import retrofiles, retroview
from session_audit.cli import main
from session_audit.retroview import encode_artifact, load_retro
from session_audit.store import Store

from .fake_claude_session import FakeClaudeSession, plan_doc, step, version_step
from .golden import assert_golden
from .ingesthelp import Corpus, connect, state_dir_of
from .recoveryhelp import (
    CSID,
    housekeep,
    plant_session,
    script_cleanup,
    script_status,
    session_rows,
)
from .retrohelp import KEY_A, RetroGateway, add_retro, live_lease, rid
from .schemas import validate

KEY_B = "b" * 64
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
ESC = "\x1b[31m"
BIDI = "‮"
DAY = 86_400_000


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


@pytest.fixture
def store(corpus: Corpus, gw: RetroGateway) -> Path:
    from .recoveryhelp import ensure_store

    ensure_store(corpus.home)
    return state_dir_of(corpus.home)


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> Any:
        return json.loads(self.out)

    @property
    def lines(self) -> list[Any]:
        return [json.loads(x) for x in self.out.splitlines()]


def run(
    capsys: pytest.CaptureFixture[str], gw: RetroGateway, *argv: str, tty: bool = False
) -> Result:
    capsys.readouterr()
    code = main([*argv], gateway=gw, stdin_is_tty=tty)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


def artifact(title: str = "a title", detail: str = "a detail", proposal: str = "do it") -> str:
    finding = {
        "id": "f1",
        "category": "friction",
        "severity": "low",
        "title": title,
        "detail": detail,
        "evidence": [
            {"kind": "item", "ref": "s1.i001"},
            {"kind": "metric", "ref": "s1.active_seconds"},  # a metric with no stored key
        ],
    }
    candidate = {
        "id": "c1",
        "target_class": "skill",
        "target_hint": title,
        "proposal": proposal,
        "findings": ["f1"],
        "risk": "medium",
        "sessions": ["s1"],
    }
    coverage = {"digest_bytes": 10, "items_total": 2, "items_kept": 2, "sessions": []}
    return encode_artifact(
        {"findings": [finding], "candidates": [candidate]},
        [],
        coverage,
        {"total.tool_calls": 1},
    )


def done(home: Path, n: int, key: str = KEY_A, created: int = 1000, **more: Any) -> str:
    counts = more.pop("counts", None)
    if counts is None:
        counts = {
            "sessions": 1,
            "skipped": 0,
            "over_cap": 0,
            "items": 2,
            "findings": 1,
            "candidates": 1,
        }
    add_retro(
        home,
        key,
        retro_id=rid(n),
        created=created,
        counts=json.dumps(counts),
        artifact=more.pop("artifact", artifact()),
        **more,
    )
    return rid(n)


def ids_in(home: Path) -> list[str]:
    conn = connect(home)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM retros ORDER BY id")]
    finally:
        conn.close()


# --- retro list ---------------------------------------------------------------------------------


def test_list_without_a_store_is_empty_and_creates_nothing(
    gw: RetroGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    result = run(capsys, gw, "retro", "list", "--json")
    assert (result.code, result.doc) == (0, {"retros": [], "truncated": False})
    validate(result.doc, "session-audit/cli/retro-list.json")
    assert not state_dir_of(_private_environment).exists()


def seeded(corpus: Corpus, store: Path) -> dict[str, str]:
    home = corpus.home
    return {
        "old": done(home, 1, created=1 * DAY),
        "new": done(home, 2, created=3 * DAY, summary="s2"),  # same key, later: supersedes old
        "other": done(home, 3, key=KEY_B, created=2 * DAY, session=S2),
        "failed": done(home, 4, key=KEY_B, created=4 * DAY, state="failed", reason="no_artifact"),
        "canceled": done(
            home, 5, key=KEY_B, created=5 * DAY, state="canceled", reason="interrupted"
        ),
        "running": done(home, 6, key=KEY_B, created=6 * DAY, state="running"),
    }


def test_list_rows_are_newest_first_and_golden(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    result = run(capsys, gw, "retro", "list", "--json")
    assert result.code == 0
    validate(result.doc, "session-audit/cli/retro-list.json")
    rows = result.doc["retros"]
    assert [r["id"] for r in rows] == [
        ids["running"],
        ids["canceled"],
        ids["failed"],
        ids["new"],
        ids["other"],
        ids["old"],
    ]
    by = {r["id"]: r for r in rows}
    assert by[ids["old"]]["superseded"] is True
    assert by[ids["new"]]["superseded"] is False
    assert by[ids["other"]]["superseded"] is False  # no later done retro with KEY_B
    assert by[ids["canceled"]]["reason"] == "interrupted"
    assert by[ids["running"]]["reason"] is None and by[ids["running"]]["finished_at"] is None
    assert (by[ids["new"]]["findings"], by[ids["new"]]["candidates"]) == (1, 1)
    assert_golden({"retros": [rows[3]], "truncated": False}, "retro-list-done")
    assert_golden({"retros": rows[:2], "truncated": False}, "retro-list-states")


def test_list_rows_carry_no_model_text(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done(corpus.home, 1, artifact=artifact(title="SECRETTITLE", detail="SECRETDETAIL"))
    result = run(capsys, gw, "retro", "list", "--json")
    assert "SECRET" not in result.out and "a summary" not in result.out
    assert "SECRET" not in run(capsys, gw, "retro", "list").out


def test_list_filters(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)

    def listed(*argv: str) -> list[str]:
        result = run(capsys, gw, "retro", "list", "--json", *argv)
        assert result.code == 0, result.out
        return [r["id"] for r in result.doc["retros"]]

    assert listed("--state", "failed", "--state", "canceled") == [ids["canceled"], ids["failed"]]
    assert listed("--key", KEY_A) == [ids["new"], ids["old"]]
    assert listed("--skill", "session-audit:audit") != []
    assert listed("--skill", "other-plugin:audit") == []
    assert listed("--session", S2) == [ids["other"]]


def test_list_session_prefix_of_an_unknown_session_is_exit_10(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded(corpus, store)
    result = run(capsys, gw, "retro", "list", "--json", "--session", "99999999")
    assert result.code == 10


def test_list_since_keeps_created_at_on_or_after(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    result = run(capsys, gw, "retro", "list", "--json", "--since", "1970-01-05")
    assert [r["id"] for r in result.doc["retros"]] == [
        ids["running"],
        ids["canceled"],
        ids["failed"],
    ]
    result = run(capsys, gw, "retro", "list", "--json", "--since", "1970-01-08")
    assert result.doc["retros"] == []


def test_list_limit_and_truncated(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded(corpus, store)
    result = run(capsys, gw, "retro", "list", "--json", "--limit", "2")
    assert len(result.doc["retros"]) == 2 and result.doc["truncated"] is True
    result = run(capsys, gw, "retro", "list", "--json", "--limit", "6")
    assert len(result.doc["retros"]) == 6 and result.doc["truncated"] is False


def test_list_skips_a_row_that_does_not_fit_and_counts_nothing_for_it(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    conn = connect(corpus.home)
    conn.execute("UPDATE retros SET counts = '{}' WHERE id = ?", (ids["new"],))
    conn.execute("UPDATE retros SET resolved_scope = '[]' WHERE id = ?", (ids["old"],))
    conn.commit()
    conn.close()
    result = run(capsys, gw, "retro", "list", "--json", "--limit", "4")
    assert [r["id"] for r in result.doc["retros"]] == [
        ids["running"],
        ids["canceled"],
        ids["failed"],
        ids["other"],
    ]
    assert result.doc["truncated"] is False


BAD_ROWS = {
    "model": ("model", "bad\x01model"),
    "warnings": ("warnings", '["nope"]'),
    "session_map": ("session_map", "{}"),
    "turns": ("turns", -1),
    "cost": ("cost_usd", -1.0),
    "sandbox": ("sandbox", 5),
    "redacted": ("redacted", -1),
    "dropped": ("dropped", "{}"),
    "skill": ("skill", "not a skill"),
    "outcome": ("outcome", "Not A Token"),
    "again_of": ("again_of", "nope"),
}


@pytest.mark.parametrize("bad", sorted(BAD_ROWS))
def test_list_and_show_agree_on_a_row_that_does_not_fit(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    bad: str,
) -> None:
    good, broken = done(corpus.home, 1), done(corpus.home, 2, key=KEY_B)
    column, value = BAD_ROWS[bad]
    conn = connect(corpus.home)
    conn.execute(f"UPDATE retros SET {column} = ? WHERE id = ?", (value, broken))
    conn.commit()
    conn.close()
    listed = run(capsys, gw, "retro", "list", "--json")
    assert [r["id"] for r in listed.doc["retros"]] == [good]
    assert run(capsys, gw, "retro", "show", "--json", broken).code == 10


def test_list_and_show_agree_on_a_done_retro_without_an_artifact_or_summary(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = done(corpus.home, 1)
    no_artifact = done(corpus.home, 2)  # same key, later: would supersede ``old``
    no_summary = done(corpus.home, 3, key=KEY_B)
    conn = connect(corpus.home)
    conn.execute("DELETE FROM retro_artifacts WHERE retro_id = ?", (no_artifact,))
    conn.execute("UPDATE retro_artifacts SET summary = '' WHERE retro_id = ?", (no_summary,))
    conn.commit()
    conn.close()
    listed = run(capsys, gw, "retro", "list", "--json")
    assert [(r["id"], r["superseded"]) for r in listed.doc["retros"]] == [(old, False)]
    for bad in (no_artifact, no_summary):
        assert run(capsys, gw, "retro", "show", "--json", bad).code == 10


def test_list_notes_the_rows_that_did_not_fit_unless_quiet(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done(corpus.home, 1)
    broken = done(corpus.home, 2, key=KEY_B)
    conn = connect(corpus.home)
    conn.execute("UPDATE retros SET warnings = '[1]' WHERE id = ?", (broken,))
    conn.commit()
    conn.close()
    noted = run(capsys, gw, "retro", "list", "--json")
    assert "1 stored retro(s) did not fit and were not listed" in noted.err
    assert len(noted.doc["retros"]) == 1
    quiet = run(capsys, gw, "retro", "list", "--json", "--quiet")
    assert quiet.err == ""


def test_list_human_output_sanitizes_the_scope_and_prints_rows(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    text = run(capsys, gw, "retro", "list").out
    assert ids["old"] in text and "superseded" in text and "failed/no_artifact" in text


def test_the_list_of_a_store_is_readable_while_a_writer_holds_the_lock(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    writer = connect(corpus.home)
    writer.isolation_level = None
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM retros WHERE id = ?", (ids["old"],))
    try:
        listed = run(capsys, gw, "retro", "list", "--json")
        shown = run(capsys, gw, "retro", "show", "--json", ids["old"])
        exported = run(capsys, gw, "export", "retros")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert listed.code == 0 and len(listed.doc["retros"]) == 6
    assert shown.code == 0 and shown.doc["id"] == ids["old"]  # the uncommitted delete is unseen
    assert exported.code == 0 and len(exported.lines) == 6


# --- retro show ---------------------------------------------------------------------------------


def test_show_json_of_a_done_retro_is_the_object_with_superseded(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    result = run(capsys, gw, "retro", "show", "--json", ids["old"])
    assert result.code == 0
    validate(result.doc, "session-audit/cli/retro-show.json")
    assert result.doc["superseded"] is True and "digest" not in result.doc
    assert result.doc["findings"][0]["id"] == "f1" and result.doc["summary"] == "a summary"
    assert result.doc["sessions"] == [{"ref": "s1", "session_id": S1, "csession_id": None}]
    assert_golden(result.doc, "retro-show-done")


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("digesting", None),
        ("running", None),
        ("validating", None),
        ("failed", "runner_lost"),
        ("escalated", "hook_escalated"),
        ("canceled", "interrupted"),
        ("canceled", "hook_vetoed"),
    ],
)
def test_show_json_of_a_retro_that_did_not_finish_has_empty_artifact_fields(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
    reason: str | None,
) -> None:
    retro_id = rid(9)
    add_retro(corpus.home, KEY_A, retro_id=retro_id, state=state, reason=reason, summary=None)
    result = run(capsys, gw, "retro", "show", "--json", retro_id)
    assert result.code == 0, result.out
    validate(result.doc, "session-audit/cli/retro-show.json")
    doc = result.doc
    assert (doc["state"], doc["reason"]) == (state, reason)
    assert doc["summary"] is None
    assert (doc["findings"], doc["candidates"], doc["skipped"], doc["metrics"]) == ([], [], [], {})
    assert doc["coverage"] == {"digest_bytes": 0, "items_total": 0, "items_kept": 0, "sessions": []}
    assert doc["superseded"] is False
    if state == "running":
        assert_golden(doc, "retro-show-running")


def test_show_resolves_a_prefix_and_reports_unknown_and_ambiguous(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for retro_id in ("ra-aaaaaaab", "ra-aaaaaaac", "ra-bbbbbbbb"):
        add_retro(corpus.home, KEY_A, retro_id=retro_id)
    assert run(capsys, gw, "retro", "show", "--json", "ra-bbbb").doc["id"] == "ra-bbbbbbbb"
    assert run(capsys, gw, "retro", "show", "--json", "ra-aaaaaaab").code == 0
    assert run(capsys, gw, "retro", "show", "--json", "ra-zzzz").code == 10
    ambiguous = run(capsys, gw, "retro", "show", "--json", "ra-aaaa")
    assert ambiguous.code == 21
    assert ambiguous.doc["error"]["details"]["matches"] == ["ra-aaaaaaab", "ra-aaaaaaac"]
    assert run(capsys, gw, "retro", "show", "--json", "ra-").code == 2


def test_show_of_a_row_that_does_not_fit_is_exit_10(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    retro_id = done(corpus.home, 1)
    conn = connect(corpus.home)
    conn.execute("UPDATE retro_artifacts SET artifact = 'not json'")
    conn.commit()
    conn.close()
    result = run(capsys, gw, "retro", "show", "--json", retro_id)
    assert result.code == 10
    assert load_retro(connect(corpus.home), retro_id) is None


def test_show_without_a_store_is_exit_10_and_creates_nothing(
    gw: RetroGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    assert run(capsys, gw, "retro", "show", "--json", "ra-abcdefgh").code == 10
    assert not state_dir_of(_private_environment).exists()


HOSTILE = f"{ESC}red{BIDI}\x07 evil\nretro ra-forged: done\r{ESC}"


def test_show_human_output_sanitizes_every_untrusted_field(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    retro_id = done(
        corpus.home,
        1,
        artifact=artifact(title=HOSTILE, detail=HOSTILE, proposal=HOSTILE),
        summary=HOSTILE[:1500],
        model=HOSTILE[:3].replace("\x1b", "m"),
    )
    result = run(capsys, gw, "retro", "show", retro_id)
    assert result.code == 0
    assert "\x1b" not in result.out and BIDI not in result.out and "\x07" not in result.out
    assert "\r" not in result.out
    assert not any(line.startswith("retro ra-forged") for line in result.out.splitlines())
    assert "<U+001B>" in result.out  # the marker is visible
    assert "s1 = " + S1 in result.out  # the mapping from refs to sessions


def test_show_json_keeps_hostile_text_but_ascii_escaped(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    retro_id = done(corpus.home, 1, artifact=artifact(title=HOSTILE))
    result = run(capsys, gw, "retro", "show", "--json", retro_id)
    assert result.out.isascii() and "\x1b" not in result.out
    assert result.doc["findings"][0]["title"] == HOSTILE


def put_digest(home: Path, retro_id: str, text: str) -> Path:
    path = state_dir_of(home) / "digests" / retro_id / "digest.md"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_show_digest_is_sanitized_and_null_once_pruned(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    retro_id = done(corpus.home, 1)
    digest = f"<untrusted-x>\nline {ESC}one{BIDI}\n</untrusted-x>\n"
    put_digest(corpus.home, retro_id, digest)
    text = run(capsys, gw, "retro", "show", retro_id, "--digest").out
    assert "\x1b" not in text and BIDI not in text
    assert "<untrusted-x>" in text and "</untrusted-x>" in text
    as_json = run(capsys, gw, "retro", "show", "--json", "--digest", retro_id)
    validate(as_json.doc, "session-audit/cli/retro-show.json")
    assert as_json.doc["digest"] == digest and as_json.out.isascii()
    path = state_dir_of(corpus.home) / "digests" / retro_id / "digest.md"
    path.unlink()
    pruned = run(capsys, gw, "retro", "show", "--json", "--digest", retro_id)
    assert pruned.code == 0 and pruned.doc["digest"] is None
    validate(pruned.doc, "session-audit/cli/retro-show.json")
    assert "not stored" in run(capsys, gw, "retro", "show", retro_id, "--digest").out


def test_show_digest_never_follows_a_symlink(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    retro_id = done(corpus.home, 1)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "digest.md").write_text("PLANTED")
    digests = state_dir_of(corpus.home) / "digests"
    digests.mkdir(mode=0o700)
    (digests / retro_id).symlink_to(target)
    result = run(capsys, gw, "retro", "show", "--json", "--digest", retro_id)
    assert result.code == 0 and result.doc["digest"] is None and "PLANTED" not in result.out


def test_read_digest_refuses_ids_that_are_not_retro_ids(
    gw: RetroGateway, corpus: Corpus, store: Path
) -> None:
    for bad in ("../x", "ra-ABCDEFGH", "ra-abcdefgh/..", "", "ra-abcdefgh\n"):
        assert retrofiles.read_digest(gw, str(store), bad) is None
        assert retrofiles.remove_digest(gw, str(store), bad) is False


# --- retro delete -------------------------------------------------------------------------------


def delete(
    capsys: pytest.CaptureFixture[str],
    gw: RetroGateway,
    *ids: str,
    tty: bool = False,
    yes: bool = True,
) -> Result:
    flags = ["--yes"] if yes else []
    return run(capsys, gw, "retro", "delete", "--json", *flags, *ids, tty=tty)


def test_delete_removes_rows_digest_and_logs(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keep, gone = done(corpus.home, 1, key=KEY_B), done(corpus.home, 2)
    path = put_digest(corpus.home, gone, "x")
    result = delete(capsys, gw, gone)
    assert result.code == 0 and result.doc == {"deleted": [gone]}
    validate(result.doc, "session-audit/cli/retro-delete.json")
    assert ids_in(corpus.home) == [keep]
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM retro_artifacts").fetchone()[0] == 1
    conn.close()
    assert not path.parent.exists()
    entries = [
        json.loads(x) for x in (store / "log.jsonl").read_text().splitlines() if "retro_delete" in x
    ]
    assert len(entries) == 1
    assert (entries[0]["retro_id"], entries[0]["key"]) == (gone, KEY_A)
    assert entries[0]["actor"] == "auto:unknown"
    assert set(entries[0]) == {"ts", "actor", "action", "retro_id", "key"}


def test_delete_with_prefixes_lists_full_ids_once(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one = done(corpus.home, 1)
    result = delete(capsys, gw, one[:8], one)
    assert result.code == 0 and result.doc == {"deleted": [one]}


def test_one_unknown_id_deletes_nothing_and_is_exit_10(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one, two = done(corpus.home, 1), done(corpus.home, 2)
    put_digest(corpus.home, one, "x")
    result = delete(capsys, gw, one, two, "ra-zzzzzzzz")
    assert result.code == 10
    assert json.loads(result.out)["error"]["code"] == "not_found"
    assert ids_in(corpus.home) == sorted([one, two])
    assert (store / "digests" / one / "digest.md").exists()
    assert (
        "retro_delete" not in (store / "log.jsonl").read_text()
        if (store / "log.jsonl").exists()
        else True
    )


def test_delete_without_a_store_is_exit_10_and_creates_nothing(
    gw: RetroGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    assert delete(capsys, gw, "ra-abcdefgh").code == 10
    assert not state_dir_of(_private_environment).exists()


def test_a_live_run_is_in_use_and_nothing_is_deleted(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    quiet = done(corpus.home, 1)
    live = rid(2)
    add_retro(
        corpus.home, KEY_B, retro_id=live, state="running", summary=None, lease=live_lease(gw)
    )
    result = delete(capsys, gw, quiet, live)
    assert result.code == 20
    error = json.loads(result.out)["error"]
    assert error["details"] == {"reason": "in_use", "retro_id": live}
    assert ids_in(corpus.home) == sorted([quiet, live])
    validate(json.loads(result.out), "session-audit/cli/error.json")


def test_a_retro_whose_lease_is_dead_or_expired_can_be_deleted(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pid, start, expires = live_lease(gw)
    expired = rid(1)
    add_retro(
        corpus.home,
        KEY_A,
        retro_id=expired,
        state="running",
        summary=None,
        lease=(pid, start, expires - 10 * 60 * 1000),
    )
    dead = rid(2)
    add_retro(
        corpus.home,
        KEY_B,
        retro_id=dead,
        state="digesting",
        summary=None,
        lease=(2**22 + 1, "never", expires),
    )
    result = delete(capsys, gw, expired, dead)
    assert result.code == 0 and ids_in(corpus.home) == []


def test_a_final_retro_with_a_lease_still_set_is_not_in_use(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one = done(corpus.home, 1, lease=live_lease(gw))
    assert delete(capsys, gw, one).code == 0


def test_confirmation_flow(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = done(corpus.home, 1)
    asked: list[str] = []
    answers = ["n\n", "", "maybe", "y\n", "YES\n"]

    def ask(prompt: str, *, limit: int = 64) -> str:
        asked.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(gw, "ask", ask, raising=False)
    # no terminal and no --yes: usage error, nobody is asked
    no_tty = delete(capsys, gw, one, yes=False)
    assert no_tty.code == 2 and asked == []
    assert json.loads(no_tty.out)["error"]["details"] == {"reason": "confirmation_required"}
    # --yes never asks, with or without a terminal, with or without --json
    spare, spare2 = done(corpus.home, 8, key=KEY_B), done(corpus.home, 9, key=KEY_B)
    assert delete(capsys, gw, spare, tty=True).code == 0 and asked == []
    human = run(capsys, gw, "retro", "delete", "--yes", spare2, tty=True)
    assert human.code == 0 and asked == [] and ids_in(corpus.home) == [one]
    # a terminal asks; anything but y / yes deletes nothing
    for _ in range(3):
        refused = delete(capsys, gw, one, tty=True, yes=False)
        assert refused.code == 2
    assert len(asked) == 3 and ids_in(corpus.home) == [one]
    assert one in asked[0]
    accepted = delete(capsys, gw, one, tty=True, yes=False)
    assert accepted.code == 0 and ids_in(corpus.home) == []
    two = done(corpus.home, 2)
    assert delete(capsys, gw, two, tty=True, yes=False).code == 0


def test_unknown_ids_are_found_before_anyone_is_asked(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ask(prompt: str, *, limit: int = 64) -> str:
        raise AssertionError("asked")

    monkeypatch.setattr(gw, "ask", ask, raising=False)
    assert delete(capsys, gw, "ra-zzzzzzzz", tty=True, yes=False).code == 10


@pytest.mark.parametrize(
    "bad",
    ["../../etc", "ra-../x", "ra-abcd/..", "ra-abc", "/etc", "ra-abcd\nra-efgh", ""],
)
def test_ids_outside_the_grammar_are_refused_before_any_path_is_built(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    done(corpus.home, 1)
    victim = store / "digests"
    victim.mkdir(mode=0o700)
    (victim / "keep").mkdir()
    result = delete(capsys, gw, bad)
    assert result.code == 2
    assert (victim / "keep").exists() and len(ids_in(corpus.home)) == 1


def test_a_digest_that_cannot_be_removed_keeps_every_retro(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .recoveryhelp import plant_local_files

    one, two = done(corpus.home, 1), done(corpus.home, 2)
    first_files = plant_local_files(corpus.home, one)
    stuck = put_digest(corpus.home, two, "x").parent / "sub"
    stuck.mkdir(mode=0o700)
    (stuck / "file").write_text("x")
    stuck.chmod(0o500)  # no unlink in it: the removal cannot finish
    try:
        result = delete(capsys, gw, one, two)
    finally:
        stuck.chmod(0o700)
    assert result.code == 12
    assert json.loads(result.out)["error"]["details"] == {
        "reason": "store_unwritable",
        "retryable": True,
    }
    assert "no retro was deleted" not in result.out
    assert ids_in(corpus.home) == sorted([one, two])
    # the planning pass refused before anything was removed: the first retro's files are intact
    assert all(p.exists() for p in first_files.values())
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM retro_artifacts").fetchone()[0] == 2
    conn.close()
    assert (
        "retro_delete" not in (store / "log.jsonl").read_text()
        if (store / "log.jsonl").exists()
        else True
    )


def test_a_prompt_dir_that_cannot_be_removed_of_a_later_id_keeps_every_file(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .recoveryhelp import plant_local_files

    one, two = done(corpus.home, 1), done(corpus.home, 2)
    first_files = plant_local_files(corpus.home, one)
    second_files = plant_local_files(corpus.home, two)
    stuck = second_files["prompt"].parent / "sub"
    stuck.mkdir(mode=0o700)
    (stuck / "file").write_text("x")
    stuck.chmod(0o500)
    try:
        result = delete(capsys, gw, one, two)
    finally:
        stuck.chmod(0o700)
    assert result.code == 12
    assert ids_in(corpus.home) == sorted([one, two])
    assert all(p.exists() for p in [*first_files.values(), *second_files.values()])


def test_a_failing_log_entry_after_the_commit_is_exit_12_naming_the_deleted_ids(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit import errors
    from session_audit.log import LogWriter

    one, two, three = done(corpus.home, 1), done(corpus.home, 2), done(corpus.home, 3)
    real = LogWriter.append
    calls = [0]

    def flaky(self: LogWriter, actor: str, action: str, **fields: Any) -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise errors.CliError(
                "failed", "log", details={"reason": "store_unwritable", "retryable": True}
            )
        return real(self, actor, action, **fields)

    monkeypatch.setattr(LogWriter, "append", flaky)
    result = delete(capsys, gw, one, two, three)
    assert result.code == 12
    details = result.doc["error"]["details"]
    assert details["deleted"] == [one, two, three]
    assert "log" in result.doc["error"]["message"]
    assert ids_in(corpus.home) == []
    assert calls[0] == 3  # every entry was attempted
    logged = [
        json.loads(x)["retro_id"]
        for x in (store / "log.jsonl").read_text().splitlines()
        if "retro_delete" in x
    ]
    assert logged == [two, three]


def test_the_run_files_of_a_retro_go_with_it(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .recoveryhelp import plant_local_files

    one = done(corpus.home, 1)
    files = plant_local_files(corpus.home, one)
    assert delete(capsys, gw, one).code == 0
    assert not any(p.exists() for p in files.values())
    assert not (store / "prompts" / one).exists()


def test_delete_never_touches_retro_starts_or_retro_sessions_and_the_cleanup_goes_on(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    one = done(corpus.home, 1, csession_id=CSID)
    plant_session(corpus.home, CSID, one)
    conn = connect(corpus.home)
    conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES ('2026-01-01', ?)", (one,))
    conn.commit()
    conn.close()
    assert delete(capsys, gw, one).code == 0
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM retro_starts").fetchone()[0] == 1
    conn.close()
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "pending"
    (tmp_path / "fake").mkdir()
    cs = FakeClaudeSession(tmp_path / "fake")
    cs.script("--version", version_step())
    cs.script("start --dry-run", step(plan_doc()))
    gw.fake = cs
    script_status(cs, one)
    script_cleanup(cs)
    housekeep(gw, corpus)  # the housekeeping of retro run and prune picks the row up
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "done"


def test_a_busy_checkpoint_is_a_retryable_exit_12_after_the_rows_are_gone(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit import errors

    one = done(corpus.home, 1)

    def busy(self: Store) -> None:
        raise errors.CliError(
            "failed", "busy", details={"reason": "store_unwritable", "retryable": True}
        )

    monkeypatch.setattr(Store, "checkpoint", busy)
    result = delete(capsys, gw, one)
    assert result.code == 12
    assert json.loads(result.out)["error"]["details"]["retryable"] is True
    assert ids_in(corpus.home) == []


def test_the_checkpoint_is_attempted_when_the_delete_fails(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = done(corpus.home, 1)
    put_digest(corpus.home, one, "x")
    calls: list[str] = []
    monkeypatch.setattr(Store, "checkpoint", lambda self: calls.append("checkpoint"))
    monkeypatch.setattr(retrofiles, "remove_digest", lambda *a, **k: False)
    assert delete(capsys, gw, one).code == 12
    assert calls == ["checkpoint"]


def test_a_real_checkpoint_blocked_by_a_reader_is_exit_12(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one = done(corpus.home, 1)
    reader = connect(corpus.home)
    reader.isolation_level = None
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM retros").fetchone()
    try:
        result = delete(capsys, gw, one)
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert result.code == 12
    assert ids_in(corpus.home) == []


def test_rows_go_in_pages_of_one_delete_in(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from session_audit.commands import retro_delete

    monkeypatch.setattr(retro_delete, "PAGE", 2)
    made = [done(corpus.home, n, created=n) for n in range(1, 6)]
    statements: list[str] = []
    real = Store.transaction

    def traced(self: Store) -> Any:
        self.conn.set_trace_callback(lambda s: statements.append(s))
        return real(self)

    monkeypatch.setattr(Store, "transaction", traced)
    assert delete(capsys, gw, *made).code == 0
    deletes = [s for s in statements if s.startswith("DELETE FROM retros ")]
    assert len(deletes) == 3 and all("IN (" in s for s in deletes)
    assert ids_in(corpus.home) == []


def test_deleting_the_newest_done_retro_clears_superseded_of_the_older_one(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = done(corpus.home, 1, created=DAY)
    new = done(corpus.home, 2, created=2 * DAY)
    assert run(capsys, gw, "retro", "show", "--json", old).doc["superseded"] is True
    assert delete(capsys, gw, new).code == 0
    assert run(capsys, gw, "retro", "show", "--json", old).doc["superseded"] is False


# --- export retros ------------------------------------------------------------------------------


def test_export_retros_prints_the_full_object_oldest_first(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    result = run(capsys, gw, "export", "retros", "--json", "--include-self-improve")
    assert result.code == 0 and result.out.isascii()
    for line in result.lines:
        validate(line, "session-audit/cli/export-retros.json")
    assert [x["id"] for x in result.lines] == [
        ids["old"],
        ids["other"],
        ids["new"],
        ids["failed"],
        ids["canceled"],
        ids["running"],
    ]
    shown = run(capsys, gw, "retro", "show", "--json", ids["new"]).doc
    assert result.lines[2] == shown
    assert result.lines[0]["superseded"] is True and result.lines[0]["findings"]
    assert_golden(result.lines[2], "export-retros-first")


def test_export_retros_since_and_empty_store(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, gw, "export", "retros").out == ""
    ids = seeded(corpus, store)
    result = run(capsys, gw, "export", "retros", "--since", "1970-01-06")
    assert [x["id"] for x in result.lines] == [ids["canceled"], ids["running"]]


def test_export_retros_without_a_store_prints_nothing_and_creates_nothing(
    gw: RetroGateway, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    result = run(capsys, gw, "export", "retros")
    assert (result.code, result.out) == (0, "")
    assert not state_dir_of(_private_environment).exists()


def test_export_retros_leaves_out_and_counts_a_row_that_does_not_fit(
    gw: RetroGateway, corpus: Corpus, store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = seeded(corpus, store)
    conn = connect(corpus.home)
    conn.execute("UPDATE retros SET session_map = '{}' WHERE id = ?", (ids["old"],))
    conn.commit()
    conn.close()
    result = run(capsys, gw, "export", "retros")
    assert len(result.lines) == 5 and "1 stored retro(s) did not fit" in result.err


def test_a_store_error_after_the_first_line_is_exit_12_on_stderr_only(
    gw: RetroGateway,
    corpus: Corpus,
    store: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded(corpus, store)
    real = retroview.load_retro
    calls = [0]

    def flaky(conn: sqlite3.Connection, retro_id: str) -> Any:
        calls[0] += 1
        if calls[0] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, retro_id)

    monkeypatch.setattr(retroview, "load_retro", flaky)
    result = run(capsys, gw, "export", "retros", "--json")
    assert result.code == 12
    assert len(result.lines) == 1  # stdout holds only data lines
    assert result.err.strip() != "" and "error" not in result.out


def test_the_unfit_count_of_export_retros_starts_again_when_the_stream_is_retried(
    gw: RetroGateway, corpus: Corpus, store: Path
) -> None:
    from session_audit.retroquery import export_lines

    done(corpus.home, 1)
    broken = done(corpus.home, 2, key=KEY_B)
    conn = connect(corpus.home)
    conn.execute("UPDATE retros SET warnings = '[1]' WHERE id = ?", (broken,))
    conn.commit()
    unfit = [0]
    for _ in range(2):  # what read_stream does on a retry: call the stream function again
        assert len(list(export_lines(conn, None, unfit))) == 1
    conn.close()
    assert unfit == [1]


def test_a_closed_pipe_ends_export_retros_with_exit_1(
    gw: RetroGateway, corpus: Corpus, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_usage_export import ClosedPipe

    seeded(corpus, store)
    pipe = ClosedPipe(1)
    monkeypatch.setattr("sys.stdout", pipe)
    assert main(["export", "retros"], gateway=gw, stdin_is_tty=False) == 1
    assert pipe.writes <= 3
