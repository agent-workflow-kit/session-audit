"""``cleanup(rs)``, the idempotent step list over one ``retro_sessions`` row (issue #27): the
forgotten time, the purge check of both claude-session directories, the removal of the retro's
transcript project directory and ``session-env`` entries by exact name, ``delete_local``, the
states ``pending``, ``done``, ``incomplete`` and ``skipped``, and the hostile rows a planted
table could hold."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from session_audit import retrocleanup
from session_audit.command import Context
from session_audit.errors import CliError
from session_audit.retrocleanup import cleanup, load_session_row

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    forget_doc,
    plan_doc,
    step,
    version_step,
)
from .ingesthelp import Corpus, state_dir_of
from .recoveryhelp import (
    CSID,
    OTHER,
    PROJECT,
    UUIDS,
    keys,
    not_found,
    orphan,
    plant_local_files,
    plant_session,
    plant_transcripts,
    script_cleanup,
    session_rows,
)
from .retrohelp import RETRO, RetroGateway, retro_ctx


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


def run_cleanup(
    gw: RetroGateway,
    corpus: Corpus,
    csid: str = CSID,
    *,
    verify: bool = False,
    **settings: Any,
) -> tuple[str, Context]:
    ctx = retro_ctx(gw, corpus, retro_settings=settings or None)
    store = ctx.store()
    row = load_session_row(store, csid)
    assert row is not None
    try:
        return cleanup(ctx, store, row, None, verify=verify), ctx
    finally:
        ctx.close()


# --- forget and the forgotten time -------------------------------------------------------------


def test_forget_purge_then_everything_gone_is_done(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    sessions, private = corpus.home / "s" / CSID, corpus.home / "p" / CSID
    for directory in (sessions, private):
        directory.mkdir(parents=True)
        (directory / "x").write_text("x")
    plant_session(corpus.home, csession_dir=str(sessions), private_dir=str(private))
    plant_transcripts(corpus)
    files = plant_local_files(corpus.home)
    script_cleanup(cs, purge=[sessions, private])
    state, _ = run_cleanup(gw, corpus)
    assert state == "done"
    row = session_rows(corpus.home)[CSID]
    assert row["cleanup_state"] == "done" and row["forgotten_at"] is not None
    assert not (corpus.root / PROJECT).exists()
    assert not any((corpus.home / ".claude" / "session-env" / u).exists() for u in UUIDS)
    assert not files["prompt"].parent.exists() and not files["outcome"].exists()
    assert files["digest"].exists()  # the digest expires with its retention, not here
    assert keys(cs) == ["forget"]
    assert [c["argv"] for c in cs.calls() if c["key"] == "forget"] == [
        ["forget", CSID, "--purge", "--json"]
    ]
    cs.assert_outputs_valid()


@pytest.mark.parametrize("purged", [True, False])
def test_a_session_directory_that_remains_makes_the_cleanup_incomplete(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, purged: bool
) -> None:
    remaining = corpus.home / "s" / CSID
    remaining.mkdir(parents=True)
    plant_session(corpus.home, csession_dir=str(remaining))
    script_cleanup(cs, purged=purged)  # forget answers, whatever it says the directory stays
    state, _ = run_cleanup(gw, corpus)
    assert state == "incomplete"
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is not None


def test_a_remaining_private_directory_alone_makes_it_incomplete(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    private = corpus.home / "p" / CSID
    private.mkdir(parents=True)
    plant_session(corpus.home, private_dir=str(private))
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "incomplete"


def test_a_repeat_forget_answering_10_never_turns_an_unfinished_purge_into_done(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    remaining = corpus.home / "s" / CSID
    remaining.mkdir(parents=True)
    plant_session(corpus.home, csession_dir=str(remaining), state="incomplete", forgotten_at=5)
    cs.script("forget", not_found())
    for _ in range(3):  # housekeeping repeats it: still incomplete, never done
        assert run_cleanup(gw, corpus)[0] == "incomplete"
    assert keys(cs) == []  # forgotten already: claude-session is not asked again
    remaining.rmdir()  # claude-session's reconcile removed its copy
    assert run_cleanup(gw, corpus)[0] == "done"


def test_exit_10_is_already_forgotten_and_sets_the_time(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    cs.script("forget", not_found())
    assert run_cleanup(gw, corpus)[0] == "done"
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is not None


def test_exit_20_stops_the_session_and_forgets_again(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    script_cleanup(cs)
    cs.script(
        "forget", step(error_doc("refused", "live", reason="live"), exit=20), step(forget_doc(CSID))
    )
    assert run_cleanup(gw, corpus)[0] == "done"
    assert keys(cs) == ["forget", "stop", "forget"]
    [stop] = [c["argv"] for c in cs.calls() if c["key"] == "stop"]
    assert stop == ["stop", CSID, "--grace", "5s", "--json"]


def test_a_forget_answering_20_again_after_the_stop_stays_pending(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    plant_transcripts(corpus)
    script_cleanup(cs)
    live = step(error_doc("refused", "live", reason="live"), exit=20)
    cs.script("forget", live, live)
    assert run_cleanup(gw, corpus)[0] == "pending"
    assert keys(cs) == ["forget", "stop", "forget"]
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is None
    assert (corpus.root / PROJECT).exists()


def test_the_forgotten_time_is_not_written_when_the_lease_was_lost_after_the_forget(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    from session_audit.lease import lease_lost

    class Lost:
        def check_still_holder(self, conn: Any) -> None:
            raise lease_lost()

    plant_session(corpus.home)
    script_cleanup(cs)
    ctx = retro_ctx(gw, corpus)
    try:
        with pytest.raises(CliError):
            retrocleanup.forget_session(ctx, ctx.store(), Lost(), CSID)
    finally:
        ctx.close()
    assert keys(cs) == ["forget"]  # the call went out; the write behind it did not
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is None


@pytest.mark.parametrize(
    "forget",
    [
        step(forget_doc(OTHER)),  # a forget object naming another id
        step(error_doc("failed", "x"), exit=12),
        step(error_doc("internal", "x"), exit=1),
        step("junk", unvalidated=True),
        step(forget_doc(CSID), exit=1, unvalidated=True),
        step("x" * 2_000_000, unvalidated=True),  # an output flood past the cap
    ],
    ids=["other-id", "error-12", "internal", "junk", "exit-1", "flood"],
)
def test_the_forgotten_time_is_set_only_for_exit_10_or_a_forget_object_naming_the_id(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, forget: dict[str, Any]
) -> None:
    plant_session(corpus.home)
    plant_transcripts(corpus)
    cs.script("forget", forget)
    state, _ = run_cleanup(gw, corpus)
    assert state == "pending"
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is None
    assert (corpus.root / PROJECT).exists()  # nothing is removed before the forget is settled


def test_a_forget_that_hangs_is_cut_at_its_timeout_and_stays_pending(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_session(corpus.home)
    cs.script("forget", step(forget_doc(CSID), sleep=30))
    monkeypatch.setattr(retrocleanup, "CALL_TIMEOUT", 1.0)
    assert run_cleanup(gw, corpus)[0] == "pending"
    assert session_rows(corpus.home)[CSID]["forgotten_at"] is None


# --- the transcript project directory ----------------------------------------------------------


def test_the_project_directory_goes_from_every_root_by_its_exact_name(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    second = tmp_path / "second-root"
    second.mkdir(mode=0o700)
    (second / PROJECT).mkdir(mode=0o700)
    (second / PROJECT / f"{UUIDS[0]}.jsonl").write_text("{}")
    bystander = corpus.root / (PROJECT + "-2")  # another project: a longer name, never matched
    bystander.mkdir(mode=0o700)
    (bystander / "keep.jsonl").write_text("{}")
    plant_transcripts(corpus)
    plant_session(corpus.home)
    script_cleanup(cs)
    ctx_settings = {"transcript_roots": (str(corpus.root), str(second))}
    ctx = retro_ctx(gw, corpus)
    ctx.settings = ctx.settings.model_copy(update=ctx_settings)
    store = ctx.store()
    row = load_session_row(store, CSID)
    assert row is not None
    assert cleanup(ctx, store, row, None) == "done"
    ctx.close()
    assert not (corpus.root / PROJECT).exists() and not (second / PROJECT).exists()
    assert (bystander / "keep.jsonl").exists()


def test_session_env_entries_are_removed_for_the_ids_the_project_holds_only(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_transcripts(corpus)
    other = corpus.home / ".claude" / "session-env" / "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    other.mkdir(parents=True, mode=0o700)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "done"
    assert other.exists()  # not the retro's session


def test_a_symlink_where_the_project_directory_should_be_is_unlinked_not_followed(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "file").write_text("keep")
    (corpus.root / PROJECT).symlink_to(target)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "done"
    assert not os.path.lexists(corpus.root / PROJECT)
    assert (target / "file").read_text() == "keep"


def test_symlinks_inside_the_project_directory_are_unlinked_not_followed(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "file").write_text("keep")
    made = plant_transcripts(corpus)
    (made["project"] / "memory-link").symlink_to(target)
    (made["project"] / UUIDS[0] / "sub-link").symlink_to(target)
    env_entry = corpus.home / ".claude" / "session-env" / UUIDS[1]
    (env_entry / "link").symlink_to(target)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "done"
    assert (target / "file").read_text() == "keep"
    assert not made["project"].exists() and not env_entry.exists()


def test_a_symlinked_session_env_directory_is_never_entered(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    (target / UUIDS[0]).mkdir(parents=True)
    (target / UUIDS[0] / "file").write_text("keep")
    plant_transcripts(corpus, env=False)
    (corpus.home / ".claude" / "session-env").symlink_to(target)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "pending"
    assert (target / UUIDS[0] / "file").read_text() == "keep"
    assert (corpus.root / PROJECT).exists()  # kept: its ids are still needed


def test_a_root_that_is_not_the_users_leaves_everything_in_place(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = plant_transcripts(corpus)
    plant_session(corpus.home)
    script_cleanup(cs)
    ctx = retro_ctx(gw, corpus)
    store = ctx.store()
    row = load_session_row(store, CSID)
    assert row is not None
    monkeypatch.setattr(gw, "uid", lambda: os.geteuid() + 1)  # every owner is "another owner"
    assert cleanup(ctx, store, row, None) == "pending"
    ctx.close()
    assert made["project"].exists() and (made["project"] / f"{UUIDS[0]}.jsonl").exists()
    assert made["env0"].exists()


def test_a_group_writable_root_is_unusable_and_the_cleanup_stays_pending(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    made = plant_transcripts(corpus)
    corpus.root.chmod(0o770)
    plant_session(corpus.home)
    script_cleanup(cs)
    try:
        assert run_cleanup(gw, corpus)[0] == "pending"
    finally:
        corpus.root.chmod(0o700)
    assert made["project"].exists()


def test_a_missing_root_has_nothing_to_remove(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    plant_session(corpus.home)
    script_cleanup(cs)
    ctx = retro_ctx(gw, corpus)
    ctx.settings = ctx.settings.model_copy(update={"transcript_roots": (str(tmp_path / "none"),)})
    store = ctx.store()
    row = load_session_row(store, CSID)
    assert row is not None
    assert cleanup(ctx, store, row, None) == "done"
    ctx.close()


def test_remove_run_transcripts_false_keeps_the_project_directory(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    made = plant_transcripts(corpus)
    plant_session(corpus.home)
    files = plant_local_files(corpus.home)
    script_cleanup(cs)
    state, _ = run_cleanup(gw, corpus, remove_run_transcripts=False)
    assert state == "done"  # claude-session's copy is gone; the retro's transcripts stay
    assert made["project"].exists() and made["env0"].exists()
    assert not files["prompt"].parent.exists()  # local files still go


# --- hostile rows ------------------------------------------------------------------------------

HOSTILE_PROJECTS = [
    "../escape",
    "/abs/olute",
    "a/b",
    "..",
    ".",
    "",
    "has space",
    "nul\x00byte",
    "x" * 201,
    "café",
    "-ok-but-" + "ل",
    "a\nb",
]


@pytest.mark.parametrize("project", HOSTILE_PROJECTS, ids=range(len(HOSTILE_PROJECTS)))
def test_a_project_directory_name_that_fails_the_grammar_removes_nothing(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path, project: str
) -> None:
    bystander = tmp_path / "precious"
    bystander.mkdir()
    (bystander / "file").write_text("keep")
    made = plant_transcripts(corpus)
    plant_session(corpus.home, project=project)
    script_cleanup(cs)
    state, _ = run_cleanup(gw, corpus)
    assert state == "skipped"
    assert session_rows(corpus.home)[CSID]["cleanup_state"] == "skipped"
    assert keys(cs) == []  # not even a forget: the row is not trusted
    assert made["project"].exists() and (bystander / "file").exists()


@pytest.mark.parametrize(
    ("project", "directory"),
    [
        ("-state-sessions-cs-aaaaaaac-work", None),  # another session's project directory
        ("-state-sessions-cs-aaaaaaab-work-x", None),  # not ending in the id's own name
        ("-nonexistent", None),
        (PROJECT, "/nonexistent/sessions/cs-aaaaaaac"),  # a session directory of another id
        (PROJECT, "/nonexistent/sessions"),
    ],
)
def test_a_row_whose_paths_do_not_name_its_own_session_is_skipped_without_a_call(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    tmp_path: Path,
    project: str,
    directory: str | None,
) -> None:
    made = plant_transcripts(corpus, project=project) if project != PROJECT else {}
    plant_session(corpus.home, project=project, csession_dir=directory)
    script_cleanup(cs)
    state, _ = run_cleanup(gw, corpus, verify=True)
    assert state == "skipped"
    assert keys(cs) == []
    assert all(path.exists() for path in made.values())


def test_a_row_whose_retro_records_another_session_is_skipped_without_a_call(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    orphan(corpus.home, state="done", csid=OTHER)
    made = plant_transcripts(corpus)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, verify=True)[0] == "skipped"
    assert keys(cs) == [] and made["project"].exists()


def test_a_row_whose_retro_has_no_session_recorded_is_skipped_without_a_call(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    orphan(corpus.home, state="done", csid=None)
    plant_session(corpus.home)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, verify=True)[0] == "skipped"
    assert keys(cs) == []


@pytest.mark.parametrize(
    "csid", ["cs-../../x", "cs-AAAAAAAA", "../cs-aaaaaaab", "cs-aaaaaaa", "cs-aaaaaaabc", "-rf"]
)
def test_a_planted_csession_id_that_fails_the_grammar_is_skipped_without_a_call(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, csid: str
) -> None:
    plant_session(corpus.home, csid)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, csid)[0] == "skipped"
    assert keys(cs) == []


def test_a_planted_retro_id_that_is_not_an_id_is_skipped_and_no_path_is_built_from_it(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home, retro_id="../prompts/x")
    keep = state_dir_of(corpus.home) / "prompts" / "x"
    keep.mkdir(parents=True, mode=0o700)
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "skipped"
    assert keep.exists() and keys(cs) == []


@pytest.mark.parametrize(
    "directory",
    ["../../etc", "/etc", "a\x00b", "x" * 4000, "relative/path"],
)
def test_hostile_session_and_private_directories_are_only_looked_at(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, directory: str, tmp_path: Path
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    (guarded / "file").write_text("keep")
    plant_session(corpus.home, csession_dir=f"{directory}/{CSID}", private_dir=directory)
    script_cleanup(cs)
    state, _ = run_cleanup(gw, corpus)
    assert state in ("incomplete", "done")  # claude-session's, observed by lstat, never removed
    assert (guarded / "file").exists() and Path("/etc").exists()


def test_a_symlinked_session_directory_is_present_not_followed(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "file").write_text("keep")
    link = corpus.home / CSID
    link.symlink_to(target)
    plant_session(corpus.home, csession_dir=str(link))
    script_cleanup(cs)
    assert run_cleanup(gw, corpus)[0] == "incomplete"
    assert (target / "file").exists() and link.is_symlink()


# --- housekeeping's label check before a forget ------------------------------------------------


def test_verify_skips_a_session_without_the_tools_labels_and_never_forgets_it(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    from .recoveryhelp import script_status

    plant_session(corpus.home)
    script_status(cs, labels=["owner=someone-else", f"retro={RETRO}"])
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, verify=True)[0] == "skipped"
    assert keys(cs) == ["status"]


def test_verify_wrong_retro_label_is_not_ours_either(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    from .recoveryhelp import script_status

    plant_session(corpus.home)
    script_status(cs, labels=["owner=session-audit", "retro=ra-zzzzzzzz"])
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, verify=True)[0] == "skipped"
    assert keys(cs) == ["status"]


def test_verify_defers_when_status_cannot_answer(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    cs.script("status", step(error_doc("failed", "x"), exit=12))
    script_cleanup(cs)
    assert run_cleanup(gw, corpus, verify=True)[0] == "pending"
    assert keys(cs) == ["status"]


def test_verify_of_a_session_claude_session_no_longer_knows_goes_on_to_forget(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    cs.script("status", not_found())
    cs.script("forget", not_found())
    assert run_cleanup(gw, corpus, verify=True)[0] == "done"
    assert keys(cs) == ["status", "forget"]


def test_nothing_is_called_for_a_row_that_is_not_active(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    for state in ("done", "skipped"):
        plant_session(corpus.home, f"cs-aaaaaaa{'d' if state == 'done' else 'e'}", state=state)
    for csid in ("cs-aaaaaaad", "cs-aaaaaaae"):
        assert run_cleanup(gw, corpus, csid)[0] in ("done", "skipped")
    assert keys(cs) == []


# --- more hostile cases ------------------------------------------------------------------------


class _OtherDevice:
    """A directory handle that reports another device for every directory below the root: what a
    project directory (or a subdirectory) on another file system looks like to the walk."""

    def __init__(self, inner: Any, shift: bool = False) -> None:
        self._inner, self._shift = inner, shift

    def stat(self) -> os.stat_result:
        info = self._inner.stat()
        if not self._shift:
            return info
        fields = list(info)
        fields[2] = info.st_dev + 1  # st_dev
        return os.stat_result(fields)

    def open_dir(self, name: bytes) -> _OtherDevice:
        return _OtherDevice(self._inner.open_dir(name), True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_project_directory_on_another_device_is_left_in_place(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = plant_transcripts(corpus, env=False)
    plant_session(corpus.home)
    script_cleanup(cs)
    real = gw.open_root
    state_root = str(state_dir_of(corpus.home))

    def open_root(path: Any) -> Any:
        handle = real(path)
        return handle if str(path) == state_root else _OtherDevice(handle)

    monkeypatch.setattr(gw, "open_root", open_root)
    assert run_cleanup(gw, corpus)[0] == "pending"
    assert (made["project"] / f"{UUIDS[0]}.jsonl").exists()


def test_a_forget_that_floods_stderr_is_still_read(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    cs.script("forget", step(forget_doc(CSID), stderr="e" * 3_000_000))
    assert run_cleanup(gw, corpus)[0] == "done"


def test_the_transcript_command_is_never_called(
    gw: RetroGateway, corpus: Corpus, cs: FakeClaudeSession
) -> None:
    plant_session(corpus.home)
    plant_transcripts(corpus)
    script_cleanup(cs)
    run_cleanup(gw, corpus)
    assert "transcript" not in [call["argv"][0] for call in cs.calls() if call["argv"]]


def test_a_retro_run_with_remove_run_transcripts_false_stores_transcripts_kept(
    gw: RetroGateway,
    corpus: Corpus,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from .test_retro_launch import launch, prepared_corpus, session_run, the_retro, write_config

    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    write_config(corpus.home, "retro:\n  remove_run_transcripts: false\n")
    launch(capsys, gw)
    row = the_retro(corpus.home)
    assert json.loads(row["warnings"]) == ["transcripts_kept"]
    session = session_rows(corpus.home)[CSID]
    assert session["cleanup_state"] == "done"  # claude-session's copy is gone; the transcripts stay
