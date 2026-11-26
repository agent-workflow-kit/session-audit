"""The digest read (issue #21): the stored offset, identity and hashes, the safe open, the skip
reasons, the scope and the single read snapshot."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from session_audit import digest
from session_audit.digest import DigestScope, _Bounded
from session_audit.errors import CliError
from session_audit.sessionquery import Filters

from .digesthelp import (
    OLD,
    assistant,
    build,
    call,
    make_ctx,
    prompt,
    result,
    say,
    turn_line,
    write,
)
from .eventhelp import raw
from .ingesthelp import S1, S2, S3, Corpus, TickGateway, connect, state_dir_of

NOW = 1_767_225_600  # 2026-01-01T00:00:00Z: the fake clock


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> TickGateway:
    gateway = TickGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


def reasons(out: digest.DigestResult) -> dict[str, str]:
    return {s.session_id: s.reason for s in out.skipped}


def texts(out: digest.DigestResult, index: int = 0) -> list[str]:
    return [i.text for i in out.sections[index].items]


# --- the stored offset ---------------------------------------------------------------------------


def test_nothing_past_the_stored_offset_is_read_or_shown(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("before", 1)])
    build(capsys, gw, corpus)  # ingest
    with open(path, "ab") as handle:
        handle.write(prompt("AFTER-THE-OFFSET", 2))  # complete line, not ingested yet
        handle.write(b'{"type":"user","message":{"content":"PARTIAL-TAIL')  # no LF
    os.utime(path, (OLD, OLD))
    out = build(capsys, gw, corpus, ingest_first=False)
    assert texts(out) == ["before"]
    assert out.skipped == ()


def test_a_partial_last_line_is_not_a_line(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("whole", 1)])
    with open(path, "ab") as handle:
        handle.write(prompt("unterminated", 2)[:-1])
    os.utime(path, (OLD, OLD))
    assert texts(build(capsys, gw, corpus)) == ["whole"]


def test_a_stored_offset_inside_a_line_is_a_changed_transcript(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("one one one", 1), prompt("two two two", 2)])
    build(capsys, gw, corpus)
    conn = connect(corpus.home)
    conn.execute("UPDATE files SET offset = offset - 5")
    conn.commit()
    conn.close()
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_changed"} and out.sections == ()


def test_the_bounded_view_never_reads_past_its_limit(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc\ndefgh\nijk\n")
    with open(path, "rb") as handle:
        view = _Bounded(handle, 7)
        assert view.readline(100) == b"abc\n"
        assert view.readline(100) == b"def"  # cut at the limit, no LF
        assert view.readline(100) == b""
        assert view.seek(0) == 0 and view.read(100) == b"abc\ndef"
        assert view.read(100) == b""
        view.seek(5)
        assert view.read(-1) == b"ef"
        view.seek(50)
        assert view.read(10) == b"" and view.readline(10) == b""
        with pytest.raises(ValueError):
            view.seek(1, 1)


# --- identity, hashes and the safe open ----------------------------------------------------------


def test_a_rewritten_file_is_transcript_changed_and_never_read(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("original", 1)])
    write(corpus, S2, [prompt("stays", 1)], project="p2")
    build(capsys, gw, corpus)
    path.write_bytes(prompt("FORGED-REPLACEMENT", 1) + b"x" * 200)  # same inode, new bytes
    os.utime(path, (OLD, OLD))
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_changed"}
    assert [s.session_id for s in out.sections] == [S2]


def test_a_replaced_file_has_another_inode(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("original", 1)])
    build(capsys, gw, corpus)
    same = path.read_bytes()
    other = path.with_name("replacement.tmp")
    other.write_bytes(same)  # identical bytes, created while the old inode is still in use
    os.utime(other, (OLD, OLD))
    os.replace(other, path)
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_changed"}


def test_a_truncated_file_is_transcript_changed(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("a" * 50, 1), prompt("b" * 50, 2)])
    build(capsys, gw, corpus)
    os.truncate(path, 20)
    assert reasons(build(capsys, gw, corpus, ingest_first=False)) == {S1: "transcript_changed"}


def test_a_vanished_file_is_transcript_missing(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(corpus, S1, [prompt("gone", 1)])
    build(capsys, gw, corpus)
    path.unlink()
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_missing"} and out.sections == ()


def test_a_symlink_or_fifo_in_place_of_the_file_is_never_read_nor_blocks(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write(corpus, S1, [prompt("original", 1)])
    path2 = write(corpus, S2, [prompt("second", 1)], project="p2")
    build(capsys, gw, corpus)
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(decoy)
    path2.unlink()
    os.mkfifo(path2)
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_missing", S2: "transcript_missing"}


def test_a_hard_linked_file_is_never_read(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write(corpus, S1, [prompt("original", 1)])
    build(capsys, gw, corpus)
    os.link(path, tmp_path / "second-name")
    assert reasons(build(capsys, gw, corpus, ingest_first=False)) == {S1: "transcript_missing"}


def test_a_session_of_a_retro_run_is_excluded_by_its_project_directory(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("mine", 1)], project="p-retro")
    build(capsys, gw, corpus)
    conn = connect(corpus.home)
    conn.execute(
        "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, project_dir, created_at)"
        " VALUES ('c1', 'r1', '/x', 'p-retro', 1)"
    )
    conn.commit()
    conn.close()
    assert reasons(build(capsys, gw, corpus, ingest_first=False)) == {S1: "transcript_missing"}


def test_a_stored_path_with_a_nul_is_transcript_missing_and_never_raises(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("x", 1)])
    build(capsys, gw, corpus)
    conn = connect(corpus.home)
    bad = os.fsencode(str(corpus.root)) + b"/pr\0oj/" + S1.encode() + b".jsonl"
    conn.execute("UPDATE files SET path = ?", (bad,))
    conn.commit()
    conn.close()
    assert reasons(build(capsys, gw, corpus, ingest_first=False)) == {S1: "transcript_missing"}


def test_a_reread_in_progress_is_transcript_changed(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("x", 1)])
    build(capsys, gw, corpus)
    conn = connect(corpus.home)
    conn.execute("UPDATE files SET rereading = 1")
    conn.commit()
    conn.close()
    assert reasons(build(capsys, gw, corpus, ingest_first=False)) == {S1: "transcript_changed"}


def test_a_file_that_changes_between_the_two_passes_starts_the_build_again(
    corpus: Corpus,
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write(corpus, S1, [prompt("one", 1), prompt("two", 2)])
    write(corpus, S2, [prompt("stable", 1)], project="p2")
    build(capsys, gw, corpus)
    real = digest._render_kept
    calls: list[str] = []

    def tampering(run: digest._Run, scan: digest._Scan, wanted: set[int]) -> object:
        calls.append(scan.session_id)
        if scan.session_id == S1 and calls.count(S1) == 1:
            path.write_bytes(b"x" * 400)  # between pass 1 and pass 2
        return real(run, scan, wanted)

    monkeypatch.setattr(digest, "_render_kept", tampering)
    out = build(capsys, gw, corpus, ingest_first=False)
    assert reasons(out) == {S1: "transcript_changed"}
    assert [(s.ref, s.session_id) for s in out.sections] == [("s1", S2)]  # renumbered, exact
    assert out.sections[0].items[0].ref == "s1.i001"


# --- live, empty, no transcript ------------------------------------------------------------------


def test_a_session_changed_within_quiet_after_is_live(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("busy", 1)], mtime=NOW - 600)  # ten minutes ago
    write(corpus, S2, [prompt("calm", 1)], project="p2", mtime=NOW - 3600)
    out = build(capsys, gw, corpus)
    assert reasons(out) == {S1: "live"} and [s.session_id for s in out.sections] == [S2]
    out = build(
        capsys,
        gw,
        corpus,
        DigestScope(quiet_after=timedelta(minutes=5), max_sessions=500),
        ingest_first=False,
    )
    assert out.skipped == () and len(out.sections) == 2


def test_a_live_subagent_file_makes_the_session_live(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("old main", 1)], mtime=NOW - 7200)
    corpus.write_agent(
        S1,
        "ag",
        [assistant("a", [{"type": "text", "text": "x"}], 2, agentId="ag")],
        project="p1",
        mtime=NOW - 60,
    )
    assert reasons(build(capsys, gw, corpus)) == {S1: "live"}


def test_a_session_with_no_item_is_empty(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [turn_line(1000, 1), result("t", "fine output", 2)])  # events, no item
    write(corpus, S2, [b"not json\n", b"{}\n"], project="p2")
    write(corpus, S3, [prompt("has one", 1)], project="p3")
    out = build(capsys, gw, corpus)
    assert reasons(out) == {S1: "empty", S2: "empty"}
    assert [s.session_id for s in out.sections] == [S3]


# --- the scope -----------------------------------------------------------------------------------


def test_candidates_by_ended_at_descending_and_the_cap_counts_what_was_never_reached(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [say("a", "old", 10)], project="p1", mtime=NOW - 3600)
    write(corpus, S2, [say("b", "newest but busy", 90)], project="p2", mtime=NOW - 60)
    write(corpus, S3, [say("c", "middle", 50)], project="p3", mtime=NOW - 3600)
    four = "44444444-4444-4444-8444-444444444444"
    write(corpus, four, [say("d", "newer", 70)], project="p4", mtime=NOW - 3600)
    out = build(capsys, gw, corpus, DigestScope(max_sessions=2))
    # S2 is first (newest) and live: skipped; then S4 and S3 are kept; S1 is never reached
    assert reasons(out) == {S2: "live"}
    assert [s.session_id for s in out.sections] == [S3, four] and out.over_cap == 1
    assert out.matched_total == 4
    assert [s.ref for s in out.sections] == ["s1", "s2"]


def test_explicit_session_ids_and_the_filters_and_together(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("a", 1, cwd="/work/a")], project="p1")
    write(corpus, S2, [say("b", "b", 2, cwd="/work/b")], project="p2")
    write(corpus, S3, [say("c", "c", 3, cwd="/work/b/sub")], project="p3")
    build(capsys, gw, corpus)
    only = build(capsys, gw, corpus, DigestScope(session_ids=(S2, S2)), ingest_first=False)
    assert [s.session_id for s in only.sections] == [S2]
    both = build(
        capsys,
        gw,
        corpus,
        DigestScope(session_ids=(S1, S2, S3), filters=Filters(project="/work/b")),
        ingest_first=False,
    )
    assert [s.session_id for s in both.sections] == [S2, S3]
    unknown = build(capsys, gw, corpus, DigestScope(session_ids=("f" * 8,)), ingest_first=False)
    assert unknown.sections == () and unknown.matched_total == 0


def label_session(corpus: Corpus, session: str, label: str) -> None:
    conn = connect(corpus.home)
    conn.execute(
        "INSERT INTO harness_runs (csession_id, updated_at, claude_session_ids, labels, final) "
        "VALUES (?, '2026-01-01T00:00:00Z', ?, ?, 1)",
        (f"cs-{session[:4]}", json.dumps([session]), json.dumps([label])),
    )
    conn.commit()
    conn.close()


def test_self_improve_sessions_are_left_out_unless_asked_and_never_when_named(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("normal", 1)], project="p1")
    write(corpus, S2, [prompt("improver", 2)], project="p2")
    build(capsys, gw, corpus)
    label_session(corpus, S2, "owner=self-improve")
    default = build(capsys, gw, corpus, DigestScope(), ingest_first=False)
    assert [s.session_id for s in default.sections] == [S1] and default.matched_total == 1
    included = build(
        capsys,
        gw,
        corpus,
        DigestScope(filters=Filters(include_self_improve=True)),
        ingest_first=False,
    )
    assert [s.session_id for s in included.sections] == [S1, S2]
    named = build(capsys, gw, corpus, DigestScope(session_ids=(S2,)), ingest_first=False)
    assert [s.session_id for s in named.sections] == [S2]


def test_the_scope_validates_max_sessions() -> None:
    for bad in (0, -1, 501, True):
        with pytest.raises(ValueError):
            DigestScope(max_sessions=bad)
    assert DigestScope(max_sessions=500).max_sessions == 500


def test_more_than_500_sessions_stop_at_s500_and_count_the_rest(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(504):
        session = f"{n:08x}-0000-4000-8000-000000000000"
        write(corpus, session, [prompt(f"p{n}", n % 3000)], project=f"proj{n}")
    out = build(capsys, gw, corpus, DigestScope(max_sessions=500), max_bytes=10_000_000)
    assert len(out.sections) == 500 and out.sections[-1].ref == "s500"
    assert out.over_cap == 4 and out.matched_total == 504
    assert len({s.session_id for s in out.sections}) == 500


def test_item_numbers_grow_past_three_digits(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt(f"p{n}", n % 3000) for n in range(1005)])
    out = build(capsys, gw, corpus, max_bytes=10_000_000, session_max_bytes=10_000_000)
    refs = [i.ref for i in out.sections[0].items]
    assert refs[0] == "s1.i001" and refs[998] == "s1.i999" and refs[999] == "s1.i1000"
    assert refs[-1] == "s1.i1005" and len(set(refs)) == 1005


# --- the store -----------------------------------------------------------------------------------


def test_no_store_is_an_empty_result_and_creates_nothing(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    out = build(capsys, gw, corpus, ingest_first=False)
    assert out.sections == () and out.matched_total == 0 and out.skipped == ()
    assert not state_dir_of(corpus.home).exists()


def test_a_newer_store_is_refused_exit_20(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("x", 1)])
    build(capsys, gw, corpus)
    conn = connect(corpus.home)
    conn.execute("INSERT INTO schema_migrations (number, name, applied_at) VALUES (999999, 'x', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(CliError) as caught:
        build(capsys, gw, corpus, ingest_first=False)
    assert (caught.value.exit_code, caught.value.details["reason"]) == (20, "store_newer")


def test_an_unreadable_store_is_exit_12(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("x", 1)])
    build(capsys, gw, corpus)
    (state_dir_of(corpus.home) / "state.db").write_bytes(b"this is not a database" * 100)
    with pytest.raises(CliError) as caught:
        build(capsys, gw, corpus, ingest_first=False)
    assert caught.value.exit_code == 12


def test_the_whole_build_runs_in_one_read_transaction(
    corpus: Corpus,
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(corpus, S1, [prompt("x", 1), say("m", "y", 2)])
    write(corpus, S2, [prompt("z", 1)], project="p2")
    build(capsys, gw, corpus)
    seen: list[tuple[int, bool, bool]] = []
    real = digest._build

    def watching(ctx: object, conn: sqlite3.Connection, *rest: object) -> digest.DigestResult:
        before = conn.in_transaction
        out = real(ctx, conn, *rest)  # type: ignore[arg-type]
        seen.append((id(conn), before, conn.in_transaction))
        return out

    monkeypatch.setattr(digest, "_build", watching)
    build(capsys, gw, corpus, ingest_first=False)
    assert len(seen) == 1 and seen[0][1:] == (True, True)  # one connection, one open transaction


def test_the_run_reads_no_environment_and_the_caller_passes_it(
    corpus: Corpus,
    gw: TickGateway,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEAKY_SECRET_TOKEN", "from-os-environ-value-123")
    write(
        corpus,
        S1,
        [prompt("a from-os-environ-value-123 b and passed-in-secret-value-456 c", 1)],
    )
    out = build(capsys, gw, corpus, env={"MY_API_TOKEN": "passed-in-secret-value-456"})
    (text,) = texts(out)
    assert "passed-in-secret-value-456" not in text and "[REDACTED:env_value]" in text
    assert "from-os-environ-value-123" in text  # os.environ is never read by the library


def test_mask_env_names_and_extra_prefixes_come_from_the_settings(
    corpus: Corpus, gw: TickGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("v=plain-value-12345 and zzk_abcdefgh12345 end", 1)])
    out = build(
        capsys,
        gw,
        corpus,
        env={"HARMLESS": "plain-value-12345"},
        mask_env=("HARMLESS",),
        extra_token_prefixes=("zzk_",),
    )
    (text,) = texts(out)
    assert text == "v=[REDACTED:env_value] and [REDACTED:prefix] end"


def test_the_context_helper_builds_what_the_cli_would(corpus: Corpus, gw: TickGateway) -> None:
    ctx = make_ctx(gw, corpus.home, corpus, prompt_chars=77)
    assert ctx.settings.digest.prompt_chars == 77 and ctx.settings.transcript_roots
    assert raw({"a": 1}) == b'{"a": 1}\n' and call and result  # helpers import
