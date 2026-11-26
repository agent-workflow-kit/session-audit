"""The launch flags and the launch plan check (issue #24; interface, Retro launch items 1 and 4)."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from session_audit import errors, launchplan
from session_audit.config import RetroSettings
from session_audit.gateway import CallResult, SystemGateway

from .fake_claude_session import (
    FakeClaudeSession,
    error_doc,
    failing_plan,
    plan_doc,
    refusing_plan,
    step,
)

SKILL = "session-audit:audit"


def launch(**changes: Any) -> launchplan.Launch:
    settings = RetroSettings().model_copy(update=changes.pop("settings", {}))
    return launchplan.Launch.of(
        settings,
        skill=SKILL,
        plugin_dirs=("/bundled",),
        bundled="/bundled",
        model=changes.pop("model", None),
        timeout=changes.pop("timeout", None),
        max_turns=changes.pop("max_turns", None),
    )


# --- project_dir_name ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cwd", "name"),
    [
        ("/", "-"),
        ("/home/u/work", "-home-u-work"),
        ("/home/u/my project", "-home-u-my-project"),
        ("/a/b_c.d-e", "-a-b-c-d-e"),  # underscore, dot and dash are all one dash
        ("/a//b/./c/../d", "-a-b-d"),  # normalized first
        ("/Ab9Zz", "-Ab9Zz"),
        ("/état", "--tat"),  # one dash per character, not per byte
        ("/日本", "---"),
        ("/a²b", "-a-b"),  # superscript two is isalnum() but not ASCII: replaced
        ("/a٣b", "-a-b"),  # an Arabic-Indic digit: replaced
        ("/a\nb", "-a-b"),
        ("/a\x00b", "-a-b"),
        ("/😀", "--"),  # one dash per code point
    ],
)
def test_project_dir_name_replaces_every_non_ascii_alphanumeric_character(
    cwd: str, name: str
) -> None:
    assert launchplan.project_dir_name(cwd) == name


def test_project_dir_name_is_ascii_for_any_input() -> None:
    text = "".join(chr(code) for code in range(0x20, 0x2FF))
    assert re.fullmatch(r"[A-Za-z0-9-]+", launchplan.project_dir_name("/" + text))


def test_project_dir_name_follows_symlinks_like_realpath(tmp_path: Path) -> None:
    real = tmp_path / "real dir"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert launchplan.project_dir_name(str(link)) == launchplan.project_dir_name(str(real))
    assert launchplan.project_dir_name(str(link)).endswith("real-dir")


@pytest.mark.live
def test_live_every_project_directory_name_of_this_machine_follows_the_encoding() -> None:
    """The names (never their contents) under the real ``~/.claude/projects`` hold only ASCII
    letters, digits and dashes and start with the dash of the root ``/``. (91 of 91 names of the
    survey of the design; run with ``pytest -m live``.)"""
    home = pwd.getpwuid(os.getuid()).pw_dir
    root = Path(home) / ".claude" / "projects"
    names = [entry.name for entry in os.scandir(root)] if root.is_dir() else []
    assert all(re.fullmatch(r"-[A-Za-z0-9-]*", name) for name in names)


# --- the flags ----------------------------------------------------------------------------------


def test_the_flags_of_a_default_launch() -> None:
    flags = launchplan.launch_flags(
        launch(), "ra-abcdefgh", prompt_file="/t/prompt.md", input_files=["/t/d.md", "/t/m.json"]
    )
    assert flags == [
        "--kind", "audit", "--reads-third-party", "--no-implicit-done",
        "--skill", SKILL,
        "--plugin-dir", "/bundled",
        "--prompt-file", "/t/prompt.md",
        "--input-file", "/t/d.md", "--input-file", "/t/m.json",
        "--cwd", "session-dir",
        "--permission-profile", "/bundled/profiles/audit-retro.yaml",
        "--sandbox", "required", "--setting-sources", "",
        "--env-allowlist-only", "PATH", "--env-allowlist-only", "HOME",
        "--env-allowlist-only", "USER", "--env-allowlist-only", "LANG",
        "--env-allowlist-only", "TERM",
        "--tools", "Read", "Write", "Bash", "Skill",
        "--timeout", "1200s", "--max-turns", "12",
        "--label", "owner=session-audit", "--label", "retro=ra-abcdefgh",
    ]  # fmt: skip


def test_the_flags_follow_the_settings_and_the_command_line() -> None:
    custom = launch(
        settings={
            "sandbox": "preferred",
            "env_allowlist": (),
            "tools": ("Read", "Skill"),
            "permission_profile": "/p/profile.yaml",
            "setting_sources": (),
        },
        model="opus",
        timeout=timedelta(seconds=1.5),
        max_turns=3,
    )
    flags = launchplan.launch_flags(custom, "ra-abcdefgh", prompt_file="p", input_files=[])
    assert flags[flags.index("--sandbox") + 1] == "preferred"
    assert flags[flags.index("--permission-profile") + 1] == "/p/profile.yaml"
    assert flags[flags.index("--env-allowlist-only") + 1] == ""  # an empty list stays empty
    assert flags[flags.index("--tools") : flags.index("--tools") + 3] == [
        "--tools",
        "Read",
        "Skill",
    ]
    assert flags[flags.index("--timeout") + 1] == "1500ms"
    assert flags[flags.index("--max-turns") + 1] == "3"
    assert flags[flags.index("--model") + 1] == "opus"
    assert "--input-file" not in flags


def test_a_model_flag_is_only_passed_when_set() -> None:
    flags = launchplan.launch_flags(launch(), "ra-abcdefgh", prompt_file="p", input_files=[])
    assert "--model" not in flags


def test_the_plan_flags_hold_neither_the_prompt_nor_the_inputs_nor_a_retro_id() -> None:
    flags = launchplan.plan_flags(launch(model="opus"))
    assert "--prompt-file" not in flags and "--input-file" not in flags
    assert "retro=<retro_id>" in flags
    assert flags[:4] == ["--kind", "audit", "--reads-third-party", "--no-implicit-done"]
    assert all(1 <= len(flag) <= 512 for flag in flags) and len(flags) <= 200
    assert "--setting-sources=" in flags and "--setting-sources" not in flags


# --- the plan check -----------------------------------------------------------------------------


class Watching(SystemGateway):
    """The real gateway; looks at the placeholder directory while the call runs."""

    def __init__(self) -> None:
        super().__init__(kill_grace=1)
        self.seen: dict[str, Any] = {}
        self.raises: BaseException | None = None

    def run(self, argv: Any, **kwargs: Any) -> CallResult:
        flags = list(argv)
        paths = {
            "prompt": flags[flags.index("--prompt-file") + 1],
            "inputs": [flags[i + 1] for i, f in enumerate(flags) if f == "--input-file"],
        }
        directory = Path(paths["prompt"]).parent
        self.seen = {
            "paths": paths,
            "dir_mode": stat.S_IMODE(directory.stat().st_mode),
            "files": {
                p.name: (p.stat().st_size, stat.S_IMODE(p.stat().st_mode))
                for p in directory.iterdir()
            },
            "timeout": kwargs.get("timeout"),
            "parent": str(directory.parent),
        }
        if self.raises is not None:
            raise self.raises
        return super().run(argv, **kwargs)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaudeSession:
    session = FakeClaudeSession(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_SCENARIO", str(session.scenario_path))
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_LOG", str(session.log_path))
    return session


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


def check(
    fake: FakeClaudeSession,
    state: Path,
    *,
    gateway: SystemGateway | None = None,
    spec: launchplan.Launch | None = None,
) -> launchplan.LaunchPlan:
    return launchplan.plan_check(
        gateway or SystemGateway(kill_grace=1),
        str(fake.program),
        spec or launch(),
        state_directory=str(state),
    )


def script(fake: FakeClaudeSession, document: Any, exit: int = 0, **more: Any) -> None:  # noqa: A002
    fake.script("start --dry-run", step(document, exit=exit, **more))


def refusal(fake: FakeClaudeSession, state: Path, **kwargs: Any) -> errors.CliError:
    with pytest.raises(errors.CliError) as raised:
        check(fake, state, **kwargs)
    return raised.value


def only_the_plan_call(fake: FakeClaudeSession) -> None:
    assert [c["key"] for c in fake.calls()] == ["start --dry-run"]  # no real start, ever
    fake.assert_outputs_valid()


def test_a_clean_plan_passes_and_runs_exactly_the_real_flags_with_placeholders(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    gateway = Watching()
    result = check(fake, state, gateway=gateway)
    assert result == launchplan.LaunchPlan(
        "/state/sessions/cs-aaaaaaaa/work", "-state-sessions-cs-aaaaaaaa-work"
    )
    (call,) = fake.calls()
    argv = call["argv"]
    assert argv[0] == "start" and argv[-2:] == ["--dry-run", "--json"]
    flags = launchplan.launch_flags(
        launch(),
        launchplan.PLACEHOLDER_RETRO_ID,
        prompt_file=gateway.seen["paths"]["prompt"],
        input_files=gateway.seen["paths"]["inputs"],
    )
    assert argv[1:-2] == flags
    only_the_plan_call(fake)


def test_the_placeholders_are_empty_private_files_in_a_private_directory_and_are_removed(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    gateway = Watching()
    check(fake, state, gateway=gateway)
    seen = gateway.seen
    assert seen["parent"] == str(state / "tmp") and seen["timeout"] == launchplan.PLAN_TIMEOUT
    assert seen["dir_mode"] == 0o700
    assert seen["files"] == {name: (0, 0o600) for name in launchplan.PLACEHOLDER_FILES}
    assert seen["paths"]["inputs"][0].endswith("/digest.md")
    assert seen["paths"]["inputs"][1].endswith("/manifest.json")
    assert list(state.iterdir()) == []  # the directory and the tmp directory made for it are gone


def test_the_placeholder_modes_hold_whatever_the_umask(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    gateway = Watching()
    old = os.umask(0o000)
    try:
        check(fake, state, gateway=gateway)
    finally:
        os.umask(old)
    assert gateway.seen["dir_mode"] == 0o700
    assert set(gateway.seen["files"].values()) == {(0, 0o600)}


def test_a_state_directory_that_does_not_exist_is_not_left_behind(
    fake: FakeClaudeSession, tmp_path: Path
) -> None:
    script(fake, plan_doc())
    fresh = tmp_path / "a" / "b" / "state"
    gateway = Watching()
    check(fake, fresh, gateway=gateway)
    assert gateway.seen["dir_mode"] == 0o700
    assert not (tmp_path / "a" / "b" / "state").exists()  # created for the call, then removed


def test_a_removal_leaves_other_entries_of_the_state_directory_alone(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    (state / "state.db").write_bytes(b"x")
    (state / "tmp").mkdir(mode=0o700)
    (state / "tmp" / "other-entry").write_bytes(b"x")
    check(fake, state)
    assert sorted(p.name for p in state.iterdir()) == ["state.db", "tmp"]
    assert [p.name for p in (state / "tmp").iterdir()] == ["other-entry"]


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o500])
def test_an_unsafe_tmp_directory_is_refused_before_anything_is_created(
    mode: int, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    (state / "tmp").mkdir(mode=0o700)
    (state / "tmp").chmod(mode)
    error = refusal(fake, state)
    assert error.details["reason"] == "store_unwritable"
    assert fake.calls() == [] and list((state / "tmp").iterdir()) == []


def test_a_tmp_symlink_is_refused(fake: FakeClaudeSession, state: Path, tmp_path: Path) -> None:
    script(fake, plan_doc())
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    (state / "tmp").symlink_to(tmp_path / "elsewhere")
    assert refusal(fake, state).details["reason"] == "store_unwritable"
    assert list((tmp_path / "elsewhere").iterdir()) == [] and fake.calls() == []


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit(143), RuntimeError("boom")])
def test_the_placeholders_are_removed_when_the_call_raises_or_is_interrupted(
    raised: BaseException, fake: FakeClaudeSession, tmp_path: Path
) -> None:
    fresh = tmp_path / "fresh-state"
    gateway = Watching()
    gateway.raises = raised
    with pytest.raises(type(raised)):
        check(fake, fresh, gateway=gateway)
    assert gateway.seen["files"]  # they existed while the call ran
    assert not fresh.exists()
    existing = tmp_path / "state"
    existing.mkdir(mode=0o700)
    with pytest.raises(type(raised)):
        check(fake, existing, gateway=gateway)
    assert list(existing.iterdir()) == []


class Racing(SystemGateway):
    """A concurrent plan check: the directory named by ``lose`` is created by the other process
    just before this one's ``mkdir`` (so it raises FileExistsError once); ``vanish`` removes the
    tmp directory just before the first ``plan-`` directory is made (FileNotFoundError once)."""

    def __init__(self, lose: str | None = None, vanish: bool = False) -> None:
        super().__init__(kill_grace=1)
        self.lose = lose
        self.vanish = vanish

    def mkdir(self, path: Any, mode: int) -> None:
        if self.lose is not None and os.fspath(path) == self.lose:
            self.lose = None
            super().mkdir(path, mode)  # the other process made it first
        elif self.vanish and os.path.basename(os.fspath(path)).startswith("plan-"):
            self.vanish = False
            os.rmdir(os.path.dirname(os.fspath(path)))  # the other process's cleanup took tmp
        super().mkdir(path, mode)


@pytest.mark.parametrize("which", ["state", "tmp"])
def test_a_directory_made_by_a_concurrent_check_counts_as_existing(
    which: str, fake: FakeClaudeSession, tmp_path: Path
) -> None:
    script(fake, plan_doc())
    fresh = tmp_path / "fresh"
    lose = fresh if which == "state" else fresh / "tmp"
    check(fake, fresh, gateway=Racing(lose=str(lose)))
    assert fresh.is_dir()  # not ours to remove: the other check owns it
    assert [c["key"] for c in fake.calls()] == ["start --dry-run"]


def test_the_plan_directory_is_made_again_once_when_tmp_vanishes(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    (state / "tmp").mkdir(mode=0o700)
    check(fake, state, gateway=Racing(vanish=True))
    assert [c["key"] for c in fake.calls()] == ["start --dry-run"]


def test_a_second_vanish_is_not_retried_again(fake: FakeClaudeSession, state: Path) -> None:
    class Always(Racing):
        def mkdir(self, path: Any, mode: int) -> None:
            self.vanish = True
            super().mkdir(path, mode)

    (state / "tmp").mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        check(fake, state, gateway=Always())


# --- exact values for every safety setting -----------------------------------------------------

MISMATCHES: list[tuple[str, dict[str, Any]]] = [
    ("tools", {"tools": ["Read", "Write", "Bash", "Skill", "Edit"]}),
    ("tools", {"tools": ["Read", "Write", "Bash"]}),
    ("tools", {"tools": []}),
    ("tools", {"tools": None}),
    ("implicit_done", {"implicit_done": True}),
    ("reads_third_party", {"profile.reads_third_party": False}),
    ("setting_sources", {"profile.setting_sources": ["user"]}),
    ("setting_sources", {"profile.setting_sources": ["project", "local"]}),
    ("mcp", {"profile.mcp": "inherit"}),
    ("env.inherit", {"profile.env.inherit": True}),
    ("env.set_names", {"profile.env.set_names": ["ANTHROPIC_API_KEY"]}),
    ("env.path_prepend", {"profile.env.path_prepend": ["/opt/bin"]}),
    ("write_dirs", {"profile.write_dirs": ["/tmp/x"]}),
    ("notes_context", {"profile.notes_context": True}),
    ("claude_args", {"profile.claude_args": ["--dangerously-skip-permissions"]}),
    ("sandbox", {"sandbox.mode": "preferred", "sandbox_lowered": True}),
    ("sandbox", {"sandbox.mode": "off", "sandbox_lowered": True}),
    ("sandbox_lowered", {"sandbox_lowered": True}),
    ("repo", {"repo": "host/group/project"}),
    ("repo", {"repo": ""}),
    ("origins", {"origins.sandbox": "config"}),
    ("origins", {"origins.mcp": "repo"}),
    ("origins", {"origins.tools": "previous_run"}),
    ("origins", {"origins.setting_sources": "derived"}),
    ("origins", {"origins.auto_exit": "config"}),  # a key the tool does not know: closed
    ("origins", {"origins.a_future_safety_field": "repo"}),
    ("origins", {"origins": {}}),  # nothing shown: not vacuously fine
    ("cwd", {"cwd": "/state/sessions/cs-aaaaaaaa/wörk"}),
    ("cwd", {"cwd": "/état/sessions/cs-aaaaaaaa/work"}),
    ("cwd", {"cwd": "/state/sessions/😀/work"}),
    ("cwd", {"cwd": "state/sessions/cs-aaaaaaaa/work"}),
    ("cwd", {"cwd": ""}),
    ("project_dir", {"cwd": "/" + "a" * 200}),  # 201 characters with the root dash
]


@pytest.mark.parametrize(
    ("name", "changes"), MISMATCHES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_each_safety_setting_changed_is_exit_12_launch_settings_mismatch(
    name: str, changes: dict[str, Any], fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc(changes))
    error = refusal(fake, state)
    assert error.exit_code == 12 and error.code == "failed"
    assert error.details == {"reason": "launch_settings_mismatch", "retryable": False}
    plan = launchplan.parse_plan(json.dumps(plan_doc(changes)))
    expected = [name, "sandbox_lowered"] if name == "sandbox" else [name]  # lowered goes with it
    assert launchplan.settings_mismatches(plan, RetroSettings()) == expected
    only_the_plan_call(fake)
    assert list(state.iterdir()) == []  # nothing stored, nothing left


def test_each_check_is_named_exactly_once_and_the_clean_plan_has_none() -> None:
    clean = launchplan.parse_plan(json.dumps(plan_doc()))
    assert launchplan.settings_mismatches(clean, RetroSettings()) == []
    names = {name for name, _ in MISMATCHES}
    assert names == {
        "tools", "implicit_done", "reads_third_party", "setting_sources", "mcp", "env.inherit",
        "env.set_names", "env.path_prepend", "write_dirs", "notes_context", "claude_args",
        "sandbox", "sandbox_lowered", "repo", "origins", "cwd", "project_dir",
    }  # fmt: skip


@pytest.mark.parametrize("key", launchplan.REQUIRED_ORIGIN_KEYS)
def test_a_safety_key_missing_from_origins_fails_closed(key: str) -> None:
    document = plan_doc()
    del document["origins"][key]
    plan = launchplan.parse_plan(json.dumps(document))
    assert launchplan.settings_mismatches(plan, RetroSettings()) == ["origins"]


def test_the_required_origin_keys_are_those_of_a_clean_plan_and_exclude_the_open_ones() -> None:
    assert set(launchplan.REQUIRED_ORIGIN_KEYS) <= set(plan_doc()["origins"])
    assert not set(launchplan.REQUIRED_ORIGIN_KEYS) & set(launchplan.OPEN_ORIGIN_KEYS)


@pytest.mark.parametrize("key", ["model", "timeout", "max_turns"])
def test_the_open_origin_keys_may_be_absent(key: str) -> None:
    document = plan_doc()
    del document["origins"][key]
    plan = launchplan.parse_plan(json.dumps(document))
    assert launchplan.settings_mismatches(plan, RetroSettings()) == []


@pytest.mark.parametrize("key", ["model", "timeout", "max_turns"])
@pytest.mark.parametrize("origin", ["builtin", "config", "repo", "previous_run", "flag", "derived"])
def test_the_open_origin_keys_may_come_from_any_config(
    key: str, origin: str, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc({f"origins.{key}": origin}))
    assert check(fake, state).cwd == "/state/sessions/cs-aaaaaaaa/work"


@pytest.mark.parametrize("origin", ["builtin", "flag", "flag-implied"])
def test_an_unknown_origins_key_is_fine_when_builtin_or_flag(
    origin: str, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc({"origins.a_future_field": origin}))
    assert check(fake, state)


def test_the_tool_list_is_compared_as_a_set_of_the_configured_tools(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc({"tools": ["Skill", "Bash", "Write", "Read"]}))
    assert check(fake, state)  # the order of the effective list does not matter
    script(fake, plan_doc({"tools": ["Read", "Skill"]}))
    spec = launch(settings={"tools": ("Skill", "Read")})
    assert check(fake, state, spec=spec)
    script(fake, plan_doc({"tools": ["Read", "Read", "Skill"]}))
    assert refusal(fake, state, spec=spec).details["reason"] == "launch_settings_mismatch"


def test_a_lowered_sandbox_in_the_config_is_exactly_what_the_plan_must_show(
    fake: FakeClaudeSession, state: Path
) -> None:
    spec = launch(settings={"sandbox": "preferred"})
    script(fake, plan_doc({"sandbox.mode": "preferred", "sandbox_lowered": True}))
    assert check(fake, state, spec=spec)
    script(fake, plan_doc({"sandbox.mode": "preferred", "sandbox_lowered": False}))
    assert refusal(fake, state, spec=spec).details["reason"] == "launch_settings_mismatch"
    script(fake, plan_doc())  # required where preferred was asked for
    assert refusal(fake, state, spec=spec).details["reason"] == "launch_settings_mismatch"


def test_a_session_directory_path_of_exactly_200_name_characters_passes(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc({"cwd": "/" + "a" * 199}))
    assert check(fake, state)


# --- would_refuse, would_fail and failed calls --------------------------------------------------


def test_would_refuse_is_claude_session_error_and_never_a_launch(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, refusing_plan("live"), exit=20)
    error = refusal(fake, state)
    assert error.details == {"reason": "claude_session_error", "retryable": False}
    only_the_plan_call(fake)


@pytest.mark.parametrize(
    ("end_reason", "retry", "pinned"),
    [
        ("sandbox_unavailable", False, True),
        ("unsafe_plugin_dir", False, True),
        ("plugin_dir_missing", False, True),
        ("claude_not_found", False, True),
        ("unsafe_setting_sources", False, True),
        ("launch_interrupted", True, False),  # not in the pinned validation enum: a hand check
        ("never_adopted", True, False),
        ("some_future_reason", False, False),
    ],
)
def test_would_fail_is_session_not_started_with_its_end_reason(
    end_reason: str, retry: bool, pinned: bool, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, failing_plan(end_reason), exit=12, unvalidated=not pinned)
    error = refusal(fake, state)
    assert error.exit_code == 12
    assert error.details == {
        "reason": "session_not_started",
        "retryable": retry,
        "end_reason": end_reason,
    }


def test_a_refusal_wins_over_a_failure(fake: FakeClaudeSession, state: Path) -> None:
    script(fake, refusing_plan("tainted", would_fail="sandbox_unavailable"), exit=20)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


@pytest.mark.parametrize("code", [1, 2, 10, 21, 99, 3, 127])
def test_any_other_exit_is_a_failed_call_even_with_a_perfect_plan(
    code: int, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc(), exit=code, unvalidated=True)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (plan_doc({"would_fail": "sandbox_unavailable"}), 0),  # exit 0 and a failure
        (plan_doc({"would_refuse": "live"}), 0),
        (plan_doc({"would_refuse": "live"}), 12),
        (plan_doc(), 12),  # exit 12 and no would_fail
        (plan_doc(), 20),  # exit 20 and no would_refuse
        (error_doc("failed", "the state directory cannot be created"), 12),
        (error_doc("schema_newer", "newer"), 12),
        (error_doc("refused", "no plan", reason="expired"), 20),
    ],
)
def test_a_document_that_disagrees_with_the_exit_is_not_trusted(
    document: Any, code: int, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, document, exit=code, unvalidated=True)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


def mutate(path: str, value: Any) -> dict[str, Any]:
    document = plan_doc()
    *parents, last = path.split(".")
    target = document
    for part in parents:
        target = target[part]
    if value is KeyError:
        del target[last]
    else:
        target[last] = value
    return document


BAD_DOCUMENTS = [
    mutate("dry_run", False),
    mutate("dry_run", KeyError),
    mutate("profile", KeyError),
    mutate("profile.env", None),
    mutate("profile.env.inherit", "false"),
    mutate("profile.env.inherit", 0),
    mutate("profile.setting_sources", "[]"),
    mutate("profile.mcp", None),
    mutate("profile.reads_third_party", None),
    mutate("profile.write_dirs", [1]),
    mutate("profile.claude_args", KeyError),
    mutate("tools", "Read"),
    mutate("tools", [1]),
    mutate("tools", KeyError),
    mutate("implicit_done", None),
    mutate("sandbox", []),
    mutate("sandbox.mode", None),
    mutate("sandbox_lowered", "no"),
    mutate("repo", 7),
    mutate("repo", KeyError),
    mutate("cwd", None),
    mutate("origins", []),
    mutate("origins", {"sandbox": 1}),
    mutate("would_fail", "Not A Token"),
    mutate("would_refuse", 5),
    mutate("would_fail", KeyError),
]


@pytest.mark.parametrize("document", BAD_DOCUMENTS)
def test_a_plan_that_does_not_fit_the_fields_read_is_a_failed_call(
    document: dict[str, Any], fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, document, unvalidated=True)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        '"x"',
        "{}",
        '{"dry_run": true',
        '{"dry_run": true, "dry_run": true}',
        '{"dry_run": NaN}',
        "[" * 100000,
    ],
)
def test_output_that_is_not_a_document_is_a_failed_call(
    text: str, fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, text, unvalidated=True)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


def test_a_duplicate_key_is_refused_even_when_the_last_value_would_pass(
    fake: FakeClaudeSession, state: Path
) -> None:
    text = json.dumps(plan_doc())
    text = text.replace(
        '"implicit_done": false', '"implicit_done": true, "implicit_done": false', 1
    )
    script(fake, text, unvalidated=True)
    assert refusal(fake, state).details["reason"] == "claude_session_error"


def test_a_call_past_its_timeout_is_a_failed_call_and_the_placeholders_go(
    fake: FakeClaudeSession, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launchplan, "PLAN_TIMEOUT", 0.5)
    script(fake, plan_doc(), sleep=30.0)
    error = refusal(fake, state)
    assert error.details["reason"] == "claude_session_error"
    assert list(state.iterdir()) == []


def test_output_past_the_cap_is_a_failed_call(
    fake: FakeClaudeSession, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launchplan, "PLAN_OUTPUT_LIMIT", 100)
    script(fake, plan_doc())
    assert refusal(fake, state).details["reason"] == "claude_session_error"


def test_a_claude_session_that_cannot_be_started_is_a_failed_call(
    state: Path,
) -> None:
    with pytest.raises(errors.CliError) as raised:
        launchplan.plan_check(
            SystemGateway(kill_grace=1),
            str(state / "no-such-program"),
            launch(),
            state_directory=str(state),
        )
    assert raised.value.details["reason"] == "claude_session_error"
    assert list(state.iterdir()) == []


def test_the_command_is_the_configured_name_and_no_text_but_flags_reaches_argv(
    fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc())
    check(fake, state)
    (call,) = fake.calls()
    # Every argv element is a flag, a fixed word, a validated value or a path of the tool's own.
    assert all(isinstance(a, str) and "\n" not in a and "\x00" not in a for a in call["argv"])
    assert call["stdin"] is None


@pytest.mark.parametrize(
    "changes", [{"profile.mcp": ""}, {"origins.implicit_done": "unheard-of"}, {"origins.tools": ""}]
)
def test_a_value_outside_the_pinned_enums_is_a_mismatch_too(
    changes: dict[str, Any], fake: FakeClaudeSession, state: Path
) -> None:
    script(fake, plan_doc(changes), unvalidated=True)  # a newer claude-session may print one
    assert refusal(fake, state).details["reason"] == "launch_settings_mismatch"
