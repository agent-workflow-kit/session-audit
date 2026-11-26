"""The retro key, reuse, the refresh and the digest scope of ``retro run`` (issue #23)."""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import timedelta
from pathlib import Path

import pytest

from session_audit import retro, retrokey
from session_audit.fencing import SectionHash, finish_digest
from session_audit.lease import IngestTarget, acquire
from session_audit.retro import Prepared
from session_audit.retroscope import WAIT_SECONDS
from session_audit.store import open_store

from .digesthelp import prompt, write
from .ingesthelp import S1, S2, S3, Corpus, connect, state_dir_of
from .retrohelp import (
    KEY_A,
    RetroGateway,
    add_retro,
    args_for,
    ingested,
    live_lease,
    mtime_of,
    retro_ctx,
    rid,
    seed,
    touch,
)
from .retrohelp import (
    retro as run_retro,
)
from .schemas import validate


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    gateway = RetroGateway()
    monkeypatch.setattr(gateway, "machine_time_zone", lambda env: "UTC")
    return gateway


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return Corpus(_private_environment)


def prepare(gw: RetroGateway, corpus: Corpus, **given: object) -> Prepared:
    """Step 1 to 10 for ``given`` (the namespace) over a store that ingest filled."""
    extra = {
        k: given.pop(k)
        for k in ("dry_run", "retro_settings", "mask_env", "extra_token_prefixes", "prompt_chars")
        if k in given
    }
    ctx = retro_ctx(gw, corpus, **extra)  # type: ignore[arg-type]
    try:
        return retro.prepare(ctx, args_for(**given), env={})
    finally:
        ctx.close()


def two(gw: RetroGateway, corpus: Corpus, capsys: pytest.CaptureFixture[str]) -> None:
    seed(corpus, S1, "first")
    seed(corpus, S2, "second", project="p2")
    ingested(capsys, gw)


# --- the key --------------------------------------------------------------------------------------


def test_the_key_follows_the_documented_formula() -> None:
    parts = [("b" * 36, "2" * 64), ("a" * 36, "1" * 64)]
    sections = [SectionHash(f"s{i}", sid, digest) for i, (sid, digest) in enumerate(parts, 1)]
    doc = {
        "v": 2,
        "sessions": [["a" * 36, "1" * 64], ["b" * 36, "2" * 64]],
        "skill": "p:s",
        "model": "",
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected = hashlib.sha256(text.encode()).hexdigest()
    assert retrokey.retro_key(sections, "p:s", None) == expected
    assert retrokey.retro_key(reversed(sections), "p:s", "") == expected  # order and "" for None
    assert retrokey.retro_key(sections, "p:s", "m") != expected
    assert retrokey.retro_key(sections, "p:t", None) != expected


def test_the_digest_format_enters_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sections = [SectionHash("s1", "a" * 36, "1" * 64)]
    before = retrokey.retro_key(sections, "p:s", None)
    monkeypatch.setattr(retrokey, "DIGEST_FORMAT", retrokey.DIGEST_FORMAT + 1)
    assert retrokey.retro_key(sections, "p:s", None) != before


def test_the_key_is_stable_and_the_scope_does_not_enter_it(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    by_ids = prepare(gw, corpus, session=[S1, S2])
    by_prefix = prepare(gw, corpus, session=[S2[:8], S1[:12]])
    by_project = prepare(gw, corpus, project="/work")
    by_filter_and_ids = prepare(gw, corpus, session=[S1, S2], project="/work")
    assert by_ids.key == by_prefix.key == by_project.key == by_filter_and_ids.key
    assert by_ids.key == prepare(gw, corpus, session=[S1, S2]).key
    assert len(by_ids.key) == 64
    one = prepare(gw, corpus, session=[S1])
    assert one.key != by_ids.key  # the digested sessions are


def test_the_nonce_never_enters_the_key(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    prepared = prepare(gw, corpus, project="/work")
    first = finish_digest(prepared.digest, "ra-abcdefgh", "t" * 32, gw)
    second = finish_digest(prepared.digest, "ra-abcdefgh", "t" * 32, gw)
    assert first.nonce != second.nonce and first.digest_sha256 != second.digest_sha256
    assert first.section_hashes == second.section_hashes == prepared.hashes
    assert retrokey.retro_key(first.section_hashes, prepared.skill, prepared.model) == prepared.key


def test_transcript_bytes_change_the_key(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    before = prepare(gw, corpus, project="/work")
    corpus.append_main(S1, [prompt("one more thing", 5)], project="p1")
    touch(corpus.root / "p1" / f"{S1}.jsonl")
    after = prepare(gw, corpus, project="/work")  # the refresh ingests the new bytes
    assert after.key != before.key


def test_digest_settings_the_redaction_list_skill_and_model_change_the_key(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("use zq9tok_ABCDEFGHIJKL for " + "w" * 400, 1, cwd="/work/p1")])
    ingested(capsys, gw)
    base = prepare(gw, corpus, project="/work").key
    assert prepare(gw, corpus, project="/work", prompt_chars=50).key != base
    assert prepare(gw, corpus, project="/work", extra_token_prefixes=("zq9tok_",)).key != base
    assert prepare(gw, corpus, project="/work", skill="p:other").key != base
    assert prepare(gw, corpus, project="/work", model="some-model").key != base
    assert prepare(gw, corpus, project="/work", retro_settings={"model": "some-model"}).key == (
        prepare(gw, corpus, project="/work", model="some-model").key
    )
    # What changes nothing in the text changes nothing in the key.
    assert prepare(gw, corpus, project="/work", extra_token_prefixes=("nomatch_",)).key == base


def test_a_masked_environment_value_changes_the_key_only_through_the_text(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    write(corpus, S1, [prompt("the value is hunter2hunter2 ok", 1, cwd="/work/p1")])
    ingested(capsys, gw)
    ctx = retro_ctx(gw, corpus, mask_env=("MY_NAME",))
    args = args_for(project="/work")
    plain = retro.prepare(ctx, args, env={}).key
    masked = retro.prepare(ctx, args, env={"MY_NAME": "hunter2hunter2"}).key
    ctx.close()
    assert plain != masked


def test_the_key_of_a_random_corpus_changes_exactly_with_the_digested_text(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    rng = random.Random(7)
    seen: dict[tuple[str, ...], str] = {}
    for round_ in range(5):
        texts = tuple(rng.choice(["alpha", "beta", "gamma"]) for _ in range(2))
        write(corpus, S1, [prompt(texts[0], 1, cwd="/work/p1")])
        write(corpus, S2, [prompt(texts[1], 1, cwd="/work/p2")])
        ingested(capsys, gw)
        key = prepare(gw, corpus, project="/work").key
        assert seen.setdefault(texts, key) == key, f"round {round_}"
    assert len(set(seen.values())) == len(seen)  # different texts, different keys


# --- reuse and already_running --------------------------------------------------------------------


def key_of(gw: RetroGateway, corpus: Corpus, **given: object) -> str:
    return prepare(gw, corpus, dry_run=True, **given).key  # type: ignore[arg-type]


def test_a_done_retro_with_the_key_is_reused_exit_0(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key)
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 0, result.err
    document = result.doc
    validate(document, "session-audit/cli/retro-run.json")
    assert document["reused"] is True and document["id"] == "ra-abcdefgh" and document["key"] == key
    # A scope that digests the same sessions finds it too: the scope is not in the key.
    again = run_retro(capsys, gw, "--project", "/work/p1")
    assert again.code == 0 and again.doc["id"] == "ra-abcdefgh"
    # Human output sanitizes the stored summary.
    plain = run_retro(capsys, gw, "--session", S1)
    assert plain.code == 0


def test_human_output_of_a_reused_retro_prints_no_raw_escapes(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    from session_audit import cli

    two(gw, corpus, capsys)
    add_retro(corpus.home, key_of(gw, corpus, session=[S1]), summary="ok \x1b[31mred\x1b[0m")
    capsys.readouterr()
    code = cli.main(["retro", "run", "--session", S1], gateway=gw, stdin_is_tty=False)
    out = capsys.readouterr().out
    assert code == 0 and "\x1b" not in out and "ra-abcdefgh" in out


def test_again_runs_despite_a_done_retro_and_records_again_of(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key)
    result = run_retro(capsys, gw, "--session", S1, "--again")
    assert result.code == 1  # past step 10: the seam of #24 and #26, not a reuse
    prepared = prepare(gw, corpus, session=[S1], again=True)
    assert prepared.reuse is None and prepared.again_of == "ra-abcdefgh"
    assert prepare(gw, corpus, session=[S1]).again_of is None  # only --again records it
    assert prepare(gw, corpus, session=[S2], again=True).again_of is None  # another key


def test_again_of_names_the_newest_final_retro_with_the_key(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, retro_id="ra-aaaaaaaa", created=10)
    add_retro(
        corpus.home, key, retro_id="ra-bbbbbbbb", created=20, state="failed", reason="no_artifact"
    )
    assert prepare(gw, corpus, session=[S1], again=True).again_of == "ra-bbbbbbbb"


def test_a_changed_key_is_not_reused(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    add_retro(corpus.home, KEY_A)
    assert run_retro(capsys, gw, "--session", S1).code == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": None},  # no artifact row
        {"summary": ""},
        {"resolved_scope": "[]"},
        {"resolved_scope": "{}"},
        {"resolved_scope": '{"flags": {}, "sessions": [], "since": null, "until": null}'},
        {"session_map": "{}"},
        {"session_map": '{"sessions": []}'},
        {"counts": "{}"},
        {"warnings": '["bogus"]'},
        {"warnings": '"x"'},
        {"created_at": -5},
        {"created_at": 2**62},
        {"skill": "bare"},
        {"skill": None},
        {"id": "ra-ABCDEFGH"},
        {"csession_id": "cs-bad"},
        {"cost_usd": -1},
        {"sandbox": 7},
        {"redacted": -1},
        {"again_of": "x"},
        {"reason": "interrupted"},
    ],
)
def test_a_damaged_done_row_is_never_reused(
    overrides: dict[str, object],
    corpus: Corpus,
    gw: RetroGateway,
    capsys: pytest.CaptureFixture[str],
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, **overrides)  # type: ignore[arg-type]
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 1, result.out  # not reused; on to the seam
    assert "reused" not in result.out


def test_the_newest_valid_done_retro_wins_over_a_damaged_newer_one(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, retro_id="ra-aaaaaaaa", created=10)
    add_retro(corpus.home, key, retro_id="ra-bbbbbbbb", created=20, summary=None)
    assert run_retro(capsys, gw, "--session", S1).doc["id"] == "ra-aaaaaaaa"


def test_a_non_final_retro_with_a_live_lease_is_already_running(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, state="digesting", lease=live_lease(gw), summary=None)
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 20 and result.doc["error"]["details"] == {
        "reason": "already_running",
        "retro_id": "ra-abcdefgh",
    }
    validate(result.doc, "session-audit/cli/error.json")
    # --again does not get past a live run of the same key.
    assert run_retro(capsys, gw, "--session", S1, "--again").code == 20
    # A dry run reports it and refuses nothing.
    dry = prepare(gw, corpus, session=[S1], dry_run=True)
    assert dry.running_retro == "ra-abcdefgh" and dry.reused_retro is None


@pytest.mark.parametrize(
    "lease",
    [
        (None, None, None),  # no lease
        (2**30, "never-a-start", 4_102_444_800_000),  # a pid that is not this start time
        (os.getpid(), "wrong-start-token", 4_102_444_800_000),
        ("12", "x", 4_102_444_800_000),  # a hostile type in an INTEGER column
        (os.getpid(), 5, 4_102_444_800_000),
        (os.getpid(), "x", "soon"),
    ],
)
def test_a_dead_expired_or_malformed_lease_is_not_running(
    lease: tuple[object, object, object],
    corpus: Corpus,
    gw: RetroGateway,
    capsys: pytest.CaptureFixture[str],
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, state="running", lease=lease, summary=None)  # type: ignore[arg-type]
    assert run_retro(capsys, gw, "--session", S1).code == 1


def test_an_expired_lease_of_a_live_process_is_not_running(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    pid, start, _ = live_lease(gw)
    add_retro(corpus.home, key, state="validating", lease=(pid, start, 1), summary=None)
    assert run_retro(capsys, gw, "--session", S1).code == 1


def test_a_retro_in_a_final_state_is_never_running_and_a_store_full_of_one_key_is_bounded(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    pid, start, expires = live_lease(gw)
    add_retro(
        corpus.home,
        key,
        state="failed",
        reason="no_artifact",
        lease=(pid, start, expires),
        summary=None,
    )
    conn = connect(corpus.home)
    for n in range(300):  # 300 damaged done rows of the same key
        conn.execute(
            "INSERT INTO retros (id, key, state, created_at) VALUES (?, ?, 'done', ?)",
            (f"ra-{n:08d}".replace("0", "a").replace("1", "b").replace("8", "c")[:11], key, n),
        )
    conn.commit()
    conn.close()
    assert run_retro(capsys, gw, "--session", S1).code == 1


# --- the digest scope -----------------------------------------------------------------------------


def test_a_filter_scope_over_the_cap_is_trimmed_with_over_cap_never_refused(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [f"0000000{n}-0000-4000-8000-000000000000" for n in range(1, 5)]
    for sid in ids:
        seed(corpus, sid)
    ingested(capsys, gw)
    prepared = prepare(gw, corpus, project="/work", retro_settings={"max_sessions": 2})
    assert prepared.kept == 2 and prepared.over_cap == 2 and prepared.matched_total == 4
    assert [s.session_id for s in prepared.digest.sections] == ids[:2]


def test_quiet_after_skips_a_recent_session_as_live_and_the_next_candidate_takes_its_place(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = {sid: seed(corpus, sid) for sid in (S1, S2, S3)}
    touch(paths[S1], mtime_of(gw, 5 * 60))
    ingested(capsys, gw)
    default = prepare(gw, corpus, project="/work", retro_settings={"max_sessions": 1})
    assert [(s.session_id, s.reason) for s in default.skipped] == [(S1, "live")]
    assert [s.session_id for s in default.digest.sections] == [S2]
    assert default.over_cap == 1
    quiet = prepare(
        gw,
        corpus,
        project="/work",
        retro_settings={"max_sessions": 1, "quiet_after": timedelta(minutes=1)},
    )
    assert [s.session_id for s in quiet.digest.sections] == [S1] and quiet.skipped == ()


def test_a_file_changed_after_its_refresh_is_skipped_transcript_changed(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    path = corpus.root / "p1" / f"{S1}.jsonl"
    path.unlink()  # replaced by another file: new inode, other head bytes
    write(
        corpus, S1, [prompt("a completely different history", 1, cwd="/work/p1")] * 3, project="p1"
    )
    held = hold_ingest(gw, corpus)  # no refresh can happen: the stored offsets are read
    prepared = prepare(gw, corpus, project="/work")
    assert [(s.session_id, s.reason) for s in prepared.skipped] == [(S1, "transcript_changed")]
    assert [s.session_id for s in prepared.digest.sections] == [S2]
    assert "ingest_busy" in prepared.warnings
    held.release()


def hold_ingest(gw: RetroGateway, corpus: Corpus):  # noqa: ANN201
    store = open_store(gw, str(state_dir_of(corpus.home)))
    lease = acquire(store, IngestTarget())
    assert lease is not None
    return lease


def test_the_ingest_lease_is_waited_for_at_most_60_seconds_once_for_the_whole_retro(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = {sid: seed(corpus, sid) for sid in (S1, S2, S3)}
    touch(paths[S1], mtime_of(gw, 60))  # live: the cap of 1 forces more refreshes
    touch(paths[S2], mtime_of(gw, 60))
    ingested(capsys, gw)
    held = hold_ingest(gw, corpus)
    prepared = prepare(gw, corpus, project="/work", retro_settings={"max_sessions": 1})
    assert sum(gw.sleeps) == WAIT_SECONDS and max(gw.sleeps) <= 1.0
    assert [s.session_id for s in prepared.digest.sections] == [S3]  # three refreshes tried
    assert prepared.warnings.count("ingest_busy") == 1
    held.release()


def test_a_free_lease_means_no_wait_and_no_warning(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    prepared = prepare(gw, corpus, project="/work")
    assert gw.sleeps == [] and "ingest_busy" not in prepared.warnings


def test_a_session_that_grows_is_refreshed_before_it_is_digested(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    corpus.append_main(S1, [prompt("appended after the last ingest", 9)], project="p1")
    touch(corpus.root / "p1" / f"{S1}.jsonl")
    text = b"".join(
        s.text.encode() for s in prepare(gw, corpus, session=[S1]).digest.sections
    ).decode()
    assert "appended after the last ingest" in text


def test_the_default_exclusion_of_self_improve_and_the_flag(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    conn = connect(corpus.home)
    conn.execute(
        "INSERT INTO harness_runs (csession_id, updated_at, claude_session_ids, labels, final) "
        "VALUES ('cs-aaaaaaab', '2026-01-01T00:00:00Z', ?, ?, 1)",
        (json.dumps([S1]), json.dumps(["owner=self-improve"])),
    )
    conn.commit()
    conn.close()
    default = prepare(gw, corpus, project="/work")
    assert [s.session_id for s in default.digest.sections] == [S2]
    included = prepare(gw, corpus, project="/work", include_self_improve=True)
    assert [s.session_id for s in included.digest.sections] == [S1, S2] or [
        s.session_id for s in included.digest.sections
    ] == [S2, S1]
    # Naming a session shows it, and its claude-session id is in the manifest mapping.
    named = prepare(gw, corpus, session=[S1])
    assert named.csession_ids == {S1: "cs-aaaaaaab"}
    both = prepare(gw, corpus, project="/work", include_self_improve=True)
    assert both.csession_ids == {S1: "cs-aaaaaaab", S2: None}


def test_the_retro_model_flag_is_not_a_session_filter(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    assert prepare(gw, corpus, project="/work", model="no-session-uses-this").kept == 2


# --- stderr counts --------------------------------------------------------------------------------


def test_the_counts_line_on_stderr_names_no_project_and_quiet_removes_it(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    write(corpus, S3, [b'{"type": "summary", "cwd": "/work/p3"}\n'], project="p3")
    ingested(capsys, gw)
    result = run_retro(capsys, gw, "--project", "/work")
    assert result.code == 1
    assert "2 sessions kept, 1 skipped, 2 projects" in result.err
    assert "/work" not in result.err and "p1" not in result.err
    assert "sessions kept" not in run_retro(capsys, gw, "--project", "/work", "--quiet").err


def test_errors_and_warnings_are_single_sanitized_lines(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_retro(capsys, gw, "--project", "/nowhere/\x1b[31m\nred")
    assert result.code == 20
    assert "\x1b" not in result.err and "red" not in result.err


# --- hostile scopes -------------------------------------------------------------------------------


def test_one_thousand_unknown_full_ids_are_exit_10_and_one_thousand_and_one_exit_2(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(corpus, S1)
    flags: list[str] = []
    for n in range(1000):
        flags += ["--session", f"{n:08x}-0000-4000-8000-000000000000"]
    assert run_retro(capsys, gw, *flags).code == 10
    assert run_retro(capsys, gw, *flags, "--session", S1).code == 2


def test_a_label_scope_with_no_harness_data_matches_nothing(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    result = run_retro(capsys, gw, "--label", "team=a", "--kind", "audit")
    assert result.code == 20 and result.doc["error"]["details"]["reason"] == "empty_scope"


def test_prefixes_of_different_lengths_and_case_resolve_to_one_session(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    keys = {
        prepare(gw, corpus, session=[p]).key
        for p in (S1[:8], S1[:13], S1[:23], S1, S1[:8].upper(), S1.upper())
    }
    assert len(keys) == 1


def test_the_seam_after_step_10_is_an_internal_error_and_writes_no_retro_row(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 1
    validate(result.doc, "session-audit/cli/error.json")
    assert result.doc["error"]["code"] == "internal"
    conn = connect(corpus.home)
    assert conn.execute("SELECT COUNT(*) FROM retros").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM retro_starts").fetchone()[0] == 0
    conn.close()


def test_property_reusing_a_done_retro_requires_an_identical_key(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    add_retro(corpus.home, key_of(gw, corpus, session=[S1]))
    rng = random.Random(11)
    for _ in range(4):
        text = rng.choice(["a", "b", "c", "d"]) * rng.randint(1, 30)
        write(corpus, S1, [prompt(text, 1, cwd="/work/p1")])
        ingested(capsys, gw)
        current = key_of(gw, corpus, session=[S1])
        reused = prepare(gw, corpus, session=[S1]).reuse
        assert (reused is not None) == (current == key_of_stored(corpus))
        assert reused is None  # the text differs from the stored one, so the key does
    seed(corpus, S1, "first")  # the original bytes again: the original key, so the retro is reused
    ingested(capsys, gw)
    restored = prepare(gw, corpus, session=[S1]).reuse
    assert restored is not None and restored["key"] == key_of_stored(corpus)


def key_of_stored(corpus: Corpus) -> str:
    conn = connect(corpus.home)
    try:
        return str(conn.execute("SELECT key FROM retros").fetchone()[0])
    finally:
        conn.close()


# --- review round 1 -------------------------------------------------------------------------------


def test_the_summary_of_a_reused_retro_is_one_sanitized_line(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    from session_audit import cli

    two(gw, corpus, capsys)
    summary = "first\nforged: line\rcarriage\x1b[31mred\x1b[0m\ttab"
    add_retro(corpus.home, key_of(gw, corpus, session=[S1]), summary=summary)
    capsys.readouterr()
    code = cli.main(["retro", "run", "--session", S1], gateway=gw, stdin_is_tty=False)
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert code == 0 and len(lines) == 3 and lines[2] == ""  # two lines and the final newline
    assert lines[1].startswith("summary: first")
    assert "\r" not in out and "\x1b" not in out and "\t" not in out
    assert not any(line.startswith("forged") for line in lines)


def test_a_forged_running_id_is_never_running_and_a_forged_again_of_never_recorded(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(
        corpus.home, key, retro_id="ra-ABCDEFGH", state="digesting", lease=live_lease(gw),
        summary=None,
    )  # fmt: skip
    add_retro(
        corpus.home, key, retro_id="ra-bad!bad!", state="failed", reason="no_artifact",
        created=50_000, summary=None,
    )  # fmt: skip
    dry = prepare(gw, corpus, session=[S1], dry_run=True)
    assert dry.running_retro is None
    prepared = prepare(gw, corpus, session=[S1], again=True)  # not refused as already_running
    assert prepared.again_of is None


def test_sixty_newer_dead_leases_do_not_hide_an_older_live_one(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(
        corpus.home, key, retro_id=rid(1), state="running", lease=live_lease(gw), created=1,
        summary=None,
    )  # fmt: skip
    for n in range(60):
        add_retro(
            corpus.home, key, retro_id=rid(100 + n), state="digesting", created=10_000 + n,
            lease=(2**30 + n, "never-a-start", 4_102_444_800_000), summary=None,
        )  # fmt: skip
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 20 and result.doc["error"]["details"] == {
        "reason": "already_running",
        "retro_id": rid(1),
    }


def test_sixty_newer_damaged_done_rows_do_not_hide_an_older_valid_one(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, retro_id=rid(1), created=1)
    conn = connect(corpus.home)
    for n in range(30):  # no artifact row at all
        conn.execute(
            "INSERT INTO retros (id, key, state, created_at) VALUES (?, ?, 'done', ?)",
            (rid(100 + n), key, 10_000 + n),
        )
    for n in range(30):  # an id that is not a retro id
        conn.execute(
            "INSERT INTO retros (id, key, state, created_at) VALUES (?, ?, 'done', ?)",
            (f"ra-{n:08d}", key, 20_000 + n),
        )
    conn.commit()
    conn.close()
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 0 and result.doc["id"] == rid(1)


def test_a_done_row_that_did_not_fit_is_counted_in_one_stderr_note(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, retro_id=rid(1), resolved_scope="{}")
    add_retro(corpus.home, key, retro_id=rid(2), resolved_scope="{}", created=2_000)
    prepared = prepare(gw, corpus, session=[S1])
    assert prepared.unfit_retros == 2 and prepared.reuse is None
    result = run_retro(capsys, gw, "--session", S1)
    assert result.code == 1
    assert "2 stored retro(s) with this key did not fit and were not reused" in result.err
    assert "warning: 2 stored" not in result.err and "ra-" not in result.err
    again = run_retro(capsys, gw, "--session", S1, "--again")
    assert "did not fit" not in again.err  # the user asked for a new run anyway
    # A usable retro of the key means no note.
    add_retro(corpus.home, key, retro_id=rid(3), created=3_000)
    assert prepare(gw, corpus, session=[S1]).unfit_retros == 0
    assert "did not fit" not in run_retro(capsys, gw, "--session", S1).err


def bulk_bad_rows(corpus: Corpus, key: str, count: int) -> None:
    """``count`` newer ``done`` rows that pass the SQL pre-filters but fail ``load_retro``."""
    for n in range(count):
        add_retro(corpus.home, key, retro_id=rid(100 + n), created=10_000 + n, session_map="{}")


def test_the_reuse_window_is_the_newest_fifty_rows_that_pass_the_sql_filters(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    key = key_of(gw, corpus, session=[S1])
    add_retro(corpus.home, key, retro_id=rid(1), created=1_000)  # valid, oldest
    bulk_bad_rows(corpus, key, 51)
    behind = prepare(gw, corpus, session=[S1])
    assert behind.reuse is None  # behind 51 newer rows that pass the filters: a re-run
    assert behind.unfit_retros == 52  # every done row of the key is counted, not only 50
    conn = connect(corpus.home)
    conn.execute("DELETE FROM retros WHERE id = ?", (rid(100),))
    conn.commit()
    conn.close()
    inside = prepare(gw, corpus, session=[S1])  # 50 newer bad rows: the valid one is the 51st
    assert inside.reuse is None
    conn = connect(corpus.home)
    conn.execute("DELETE FROM retros WHERE id = ?", (rid(101),))
    conn.commit()
    conn.close()
    reused = prepare(gw, corpus, session=[S1])  # 49 newer bad rows: it is the 50th
    assert reused.reuse is not None and reused.reuse["id"] == rid(1)
    assert reused.unfit_retros == 0


def test_warnings_hold_only_closed_tokens_and_notes_the_free_text(
    corpus: Corpus,
    gw: RetroGateway,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    two(gw, corpus, capsys)
    real = retro.build_digest

    def build(*args: object, **kwargs: object):  # noqa: ANN202
        result = real(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, warnings=("discovery said \x1b[31mred",))

    monkeypatch.setattr(retro, "build_digest", build)
    held = hold_ingest(gw, corpus)
    prepared = prepare(gw, corpus, project="/work")
    held.release()
    assert prepared.warnings == ("ingest_busy",)
    assert prepared.notes == ("discovery said \x1b[31mred",)
    # Free text goes to stderr through the sanitizer.
    result = run_retro(capsys, gw, "--project", "/work")
    assert "\x1b" not in result.err and "discovery said" in result.err


def test_the_last_batch_may_reach_past_the_kept_sessions_and_over_cap_adds_the_rest(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = [f"0000000{n}-0000-4000-8000-000000000000" for n in range(1, 9)]
    paths = {sid: seed(corpus, sid) for sid in ids}
    touch(paths[ids[0]], mtime_of(gw, 60))  # the first candidate is live
    ingested(capsys, gw)
    prepared = prepare(gw, corpus, project="/work", retro_settings={"max_sessions": 4})
    # Batches: 4 candidates (3 kept), then 2 more (5 kept: one over the cap, counted by the
    # build), then the 2 never offered.
    assert prepared.kept == 4 and prepared.matched_total == 8
    assert prepared.digest.over_cap == 1 and prepared.over_cap == 1 + 2
    assert [(s.session_id, s.reason) for s in prepared.skipped] == [(ids[0], "live")]


def test_a_stored_retro_is_reported_by_a_dry_run_and_the_explicit_ids_are_carried(
    corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    two(gw, corpus, capsys)
    add_retro(corpus.home, key_of(gw, corpus, session=[S1]))
    dry = prepare(gw, corpus, session=[S1[:8]], dry_run=True)
    assert dry.reused_retro == "ra-abcdefgh" and dry.reuse is None and dry.running_retro is None
    assert dry.explicit_sessions == (S1,)
    assert prepare(gw, corpus, project="/work").explicit_sessions == ()
