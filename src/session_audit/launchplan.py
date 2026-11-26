"""The launch flags and the launch plan check (interface, Retro launch items 1 and 4; Order of
checks step 11).

``launch_flags`` is the one place that builds the claude-session flags of a retro launch, so the
plan check and the real ``start`` (#26) run the same list. Every safety setting is passed on
every call and never depends on another tool's configuration (CLAUDE.md, rule 3). No flag value
is untrusted text: the skill, the plugin directories, the model, the labels and the numbers were
validated by the grammar or are built here; the prompt and the inputs are file paths.

``plan_check`` runs ``claude-session start --dry-run --json`` with those flags and EMPTY
placeholder files of the same names in a 0700 directory under ``<state>/tmp`` (created with
exact modes through the gateway, removed afterwards by a descriptor walk, also when the call
fails, raises or is interrupted; directories the check had to create are removed too), then
compares the returned plan with the required settings, exactly:

* a call that did not exit 0, 12 or 20, timed out, was killed or printed a document that does not
  parse and fit the fields read here is exit 12 ``claude_session_error``; the checks below never
  read a failed call as a success;
* ``would_refuse`` is exit 12 ``claude_session_error`` (a new session cannot be refused),
  ``would_fail`` is exit 12 ``session_not_started`` with ``details.end_reason``;
* every safety setting must have the required value (``settings_mismatches`` names each one that
  does not) and every ``origins`` key outside ``OPEN_ORIGIN_KEYS`` must be ``builtin``, ``flag``
  or ``flag-implied``, so a key claude-session adds later fails closed; every key of
  ``REQUIRED_ORIGIN_KEYS`` must be present (a missing key fails closed too);
* the session directory path must be ASCII and its project directory name at most 200 characters.

The plan document is validated here by hand (the closed field list this check reads): the pinned
JSON schema is a test-time dependency only (``tests/schemas.py``), the fake claude-session
validates everything it prints against it.
"""

from __future__ import annotations

import os
import re
import stat as statmod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import errors
from .config import RetroSettings
from .gateway import CallResult, DirHandle, Gateway
from .harness import Invalid, strict_loads
from .removal import remove_tree
from .store import DIR_MODE, FILE_MODE, unwritable

__all__ = [
    "OPEN_ORIGIN_KEYS",
    "PLAN_TIMEOUT",
    "REQUIRED_ORIGIN_KEYS",
    "PLACEHOLDER_FILES",
    "PLACEHOLDER_RETRO_ID",
    "Launch",
    "LaunchPlan",
    "Plan",
    "launch_flags",
    "parse_plan",
    "plan_check",
    "plan_flags",
    "project_dir_name",
    "retryable",
    "settings_mismatches",
]

OPEN_ORIGIN_KEYS = ("model", "timeout", "max_turns")  # the only origins config may set
# The origins of every setting the check reads: a plan that does not show one of them cannot
# show that no config changed it, so a missing key is a mismatch like a changed one.
REQUIRED_ORIGIN_KEYS = (
    "tools",
    "implicit_done",
    "reads_third_party",
    "sandbox",
    "setting_sources",
    "mcp",
    "env.inherit",
    "env.set_names",
    "env.path_prepend",
    "write_dirs",
    "notes_context",
    "claude_args",
    "permission_profile",
    "cwd",
)
ORIGIN_OK = frozenset({"builtin", "flag", "flag-implied"})
PLAN_TIMEOUT = 120.0
PLAN_OUTPUT_LIMIT = 4 * 1024 * 1024
PLACEHOLDER_RETRO_ID = "ra-aaaaaaaa"  # the plan check runs before any row (and id) exists
PLACEHOLDER_FILES = ("prompt.md", "digest.md", "manifest.json")
PROJECT_DIR_MAX = 200
PROFILE_RELATIVE = "profiles/audit-retro.yaml"  # in the bundled plugin; TODO(#30): the file
_END_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_RETRYABLE_END_REASONS = frozenset({"launch_interrupted", "never_adopted"})


def project_dir_name(cwd: str) -> str:
    """Claude Code's encoding of a session directory into its transcript project directory:
    every character that is not ASCII alphanumeric becomes ``-``, one dash per character, after
    ``realpath``. (Characters outside ASCII are replaced too, so the result is always ASCII; how
    Claude Code treats one is unverified, which is why the plan check refuses such a path.)"""
    real = os.path.normpath(cwd) if "\x00" in cwd else os.path.realpath(cwd)
    return re.sub(r"[^A-Za-z0-9]", "-", real)


def retryable(end_reason: str) -> bool:
    """``retryable(end_reason)`` of the interface: only ``launch_interrupted`` and
    ``never_adopted``."""
    return end_reason in _RETRYABLE_END_REASONS


# --- the flags -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    """What a retro launch is made of (everything but the retro id and the file paths)."""

    skill: str
    plugin_dirs: tuple[str, ...]
    permission_profile: str
    model: str | None
    timeout: timedelta
    max_turns: int
    settings: RetroSettings

    @staticmethod
    def of(
        settings: RetroSettings,
        *,
        skill: str,
        plugin_dirs: Sequence[str],
        bundled: str | None,
        model: str | None,
        timeout: timedelta | None,
        max_turns: int | None,
    ) -> Launch:
        """The launch of ``settings`` with the flags of the command given over them. The profile
        is ``retro.permission_profile`` or the bundled one (``<bundled>/profiles/audit-retro.yaml``,
        the file #30 adds)."""
        profile = settings.permission_profile
        if profile is None:
            base = bundled if bundled is not None else "plugin"
            profile = os.path.join(base, PROFILE_RELATIVE)
        return Launch(
            skill,
            tuple(plugin_dirs),
            profile,
            model,
            timeout if timeout is not None else settings.timeout,
            max_turns if max_turns is not None else settings.max_turns,
            settings,
        )


def _duration(value: timedelta) -> str:
    """claude-session's duration text: whole seconds as ``<n>s``, else milliseconds."""
    millis = round(value / timedelta(milliseconds=1))
    return f"{millis // 1000}s" if millis % 1000 == 0 else f"{millis}ms"


def launch_flags(
    launch: Launch, retro_id: str, *, prompt_file: str, input_files: Sequence[str]
) -> list[str]:
    """The flags of ``claude-session start`` (without ``--json``, ``--dry-run``), Retro launch
    item 4, in a fixed order."""
    s = launch.settings
    flags = ["--kind", "audit", "--reads-third-party", "--no-implicit-done"]
    flags += ["--skill", launch.skill]
    for directory in launch.plugin_dirs:
        flags += ["--plugin-dir", directory]
    flags += ["--prompt-file", prompt_file]
    for path in input_files:
        flags += ["--input-file", path]
    flags += ["--cwd", "session-dir"]
    flags += ["--permission-profile", launch.permission_profile]
    flags += ["--sandbox", s.sandbox, "--setting-sources", ",".join(s.setting_sources)]
    for name in s.env_allowlist or ("",):  # an empty list is `--env-allowlist-only ''`
        flags += ["--env-allowlist-only", name]
    flags += ["--tools", *s.tools]
    flags += ["--timeout", _duration(launch.timeout), "--max-turns", str(launch.max_turns)]
    if launch.model is not None:
        flags += ["--model", launch.model]
    flags += ["--label", "owner=session-audit", "--label", f"retro={retro_id}"]
    return flags


def plan_flags(launch: Launch) -> list[str]:
    """``launch_flags`` for the dry-run plan: the flags without the prompt and the input files
    (their paths are temporary) and with ``<retro_id>`` where the retro id will be. A flag with
    an empty value is written ``--flag=`` (the schema has no empty item)."""
    flags = launch_flags(launch, "<retro_id>", prompt_file="", input_files=())
    index = flags.index("--prompt-file")
    flags = [*flags[:index], *flags[index + 2 :]]
    shown: list[str] = []
    for flag in flags:
        if flag == "" and shown:
            shown[-1] += "="
        else:
            shown.append(flag)
    return shown


# --- the plan document ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """The fields of the plan the check reads."""

    tools: list[str] | None
    implicit_done: bool
    reads_third_party: bool
    setting_sources: list[str]
    mcp: str
    env_inherit: bool
    env_set_names: list[str]
    env_path_prepend: list[str]
    write_dirs: list[str]
    notes_context: bool
    claude_args: list[str]
    sandbox_mode: str
    sandbox_lowered: bool
    repo: str | None
    cwd: str
    origins: dict[str, str]
    would_refuse: str | None
    would_fail: str | None


def _need(document: dict[str, Any], key: str) -> Any:
    if key not in document:
        raise Invalid("missing field")
    return document[key]


def _obj(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Invalid("not an object")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise Invalid("not a boolean")
    return value


def _text(value: Any, *, null: bool = False) -> Any:
    if value is None and null:
        return None
    if not isinstance(value, str):
        raise Invalid("not a string")
    return value


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Invalid("not a list of strings")
    return list(value)


def parse_plan(text: str) -> Plan:
    """The plan of ``start --dry-run --json`` output, or ``harness.Invalid``. Strict JSON
    (duplicate keys, NaN and Infinity refused); a field the check reads that is missing or has
    the wrong type is invalid; so is a document that is not a dry-run plan."""
    document = _obj(strict_loads(text))
    if _need(document, "dry_run") is not True:
        raise Invalid("not a dry-run plan")
    profile = _obj(_need(document, "profile"))
    env = _obj(_need(profile, "env"))
    sandbox = _obj(_need(document, "sandbox"))
    tools = _need(document, "tools")
    origins = _obj(_need(document, "origins"))
    if not all(isinstance(v, str) for v in origins.values()):
        raise Invalid("an origin is not a string")
    fail = _text(_need(document, "would_fail"), null=True)
    if fail is not None and not _END_REASON.fullmatch(fail):
        raise Invalid("would_fail is not a token")
    refuse = _text(_need(document, "would_refuse"), null=True)
    if refuse is not None and not _END_REASON.fullmatch(refuse):
        raise Invalid("would_refuse is not a token")
    return Plan(
        tools=None if tools is None else _texts(tools),
        implicit_done=_bool(_need(document, "implicit_done")),
        reads_third_party=_bool(_need(profile, "reads_third_party")),
        setting_sources=_texts(_need(profile, "setting_sources")),
        mcp=_text(_need(profile, "mcp")),
        env_inherit=_bool(_need(env, "inherit")),
        env_set_names=_texts(_need(env, "set_names")),
        env_path_prepend=_texts(_need(env, "path_prepend")),
        write_dirs=_texts(_need(profile, "write_dirs")),
        notes_context=_bool(_need(profile, "notes_context")),
        claude_args=_texts(_need(profile, "claude_args")),
        sandbox_mode=_text(_need(sandbox, "mode")),
        sandbox_lowered=_bool(_need(document, "sandbox_lowered")),
        repo=_text(_need(document, "repo"), null=True),
        cwd=_text(_need(document, "cwd")),
        origins=dict(origins),
        would_refuse=refuse,
        would_fail=fail,
    )


def settings_mismatches(plan: Plan, settings: RetroSettings) -> list[str]:
    """The names of the safety settings of ``plan`` that are not the required ones (empty when the
    plan is fine). The names are fixed words of this tool."""
    bad: list[str] = []
    if sorted(plan.tools or []) != sorted(settings.tools):
        bad.append("tools")
    checks = {
        "implicit_done": plan.implicit_done is False,
        "reads_third_party": plan.reads_third_party is True,
        "setting_sources": plan.setting_sources == [],
        "mcp": plan.mcp == "none",
        "env.inherit": plan.env_inherit is False,
        "env.set_names": plan.env_set_names == [],
        "env.path_prepend": plan.env_path_prepend == [],
        "write_dirs": plan.write_dirs == [],
        "notes_context": plan.notes_context is False,
        "claude_args": plan.claude_args == [],
        "sandbox": plan.sandbox_mode == settings.sandbox,
        "sandbox_lowered": plan.sandbox_lowered == (settings.sandbox != "required"),
        "repo": plan.repo is None,
        "origins": all(key in plan.origins for key in REQUIRED_ORIGIN_KEYS)
        and all(
            origin in ORIGIN_OK
            for key, origin in plan.origins.items()
            if key not in OPEN_ORIGIN_KEYS
        ),
        "cwd": _ascii_session_dir(plan.cwd),
        "project_dir": _project_dir_fits(plan.cwd),
    }
    bad.extend(name for name, ok in checks.items() if not ok)
    return bad


def _ascii_session_dir(cwd: str) -> bool:
    return cwd.startswith("/") and "\x00" not in cwd and os.path.realpath(cwd).isascii()


def _project_dir_fits(cwd: str) -> bool:
    return "\x00" not in cwd and len(project_dir_name(cwd)) <= PROJECT_DIR_MAX


# --- the placeholder directory -------------------------------------------------------------------


def _check_private(gateway: Gateway, path: str) -> None:
    info = gateway.lstat(path)
    if (
        not statmod.S_ISDIR(info.st_mode)
        or info.st_uid != gateway.uid()
        or info.st_mode & 0o7777 != DIR_MODE
    ):
        raise unwritable("a directory of the state directory is not a private directory")


class _Placeholders:
    """``<state>/tmp/plan-<token>`` with the empty files, and the way to remove it again. The
    directories that did not exist are created 0700 (exact) and are removed with it when they
    are empty again, so a refused dry run on a fresh home leaves nothing behind."""

    def __init__(self, gateway: Gateway, state: str) -> None:
        self.gateway = gateway
        self.created: list[str] = []  # directories this object made, outermost first
        self.directory = ""
        self.files: list[str] = []
        tmp = os.path.join(state, "tmp")
        try:
            self._ensure(tmp)
            name = "plan-" + gateway.token_hex(8)
            self.directory = os.path.join(tmp, name)
            try:
                gateway.mkdir(self.directory, DIR_MODE)
            except FileNotFoundError:
                self._ensure(tmp)  # a concurrent check removed the tmp directory: once more
                gateway.mkdir(self.directory, DIR_MODE)
            self.created.append(self.directory)
            for file in PLACEHOLDER_FILES:
                path = os.path.join(self.directory, file)
                gateway.open_private(path, create=True, mode=FILE_MODE).close()
                self.files.append(path)
        except BaseException:
            self.remove()
            raise

    def _ensure(self, tmp: str) -> None:
        """Create the missing directories down to ``tmp`` (0700) and check that the state and tmp
        directories are private. One a concurrent check made first (FileExistsError) exists: it
        is not ours to remove and is checked like any existing one."""
        for path in self._missing(tmp):
            try:
                self.gateway.mkdir(path, DIR_MODE)
            except FileExistsError:
                continue
            self.created.append(path)
        _check_private(self.gateway, os.path.dirname(tmp))
        _check_private(self.gateway, tmp)

    def _missing(self, directory: str) -> list[str]:
        missing: list[str] = []
        probe = directory
        while True:
            try:
                self.gateway.lstat(probe)
                break
            except FileNotFoundError:
                missing.append(probe)
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
        return list(reversed(missing))

    def remove(self) -> None:
        """Remove what ``__init__`` created; never raises (a leftover is an hour old at most for
        the housekeeping of #27)."""
        for path in reversed(self.created):
            parent, name = os.path.dirname(path), os.path.basename(path)
            try:
                handle: DirHandle = self.gateway.open_root(parent)
            except OSError:
                continue
            try:
                if path == self.directory:
                    remove_tree(self.gateway, handle, name.encode())
                else:
                    try:
                        handle.rmdir(name.encode())  # only when empty: others' files stay
                    except OSError:
                        pass
            except OSError:
                pass
            finally:
                handle.close()
        self.created.clear()


# --- the check -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchPlan:
    """What a passed plan check hands on: the session directory path of the plan (``cwd``; the
    sessions root of the launch is ``dirname(dirname(cwd))``) and its project directory name."""

    cwd: str
    project_dir: str


def _error(reason: str, message: str, **details: Any) -> errors.CliError:
    return errors.CliError(
        "failed", message, details={"reason": reason, "retryable": False, **details}
    )


def claude_session_error() -> errors.CliError:
    return _error("claude_session_error", "a claude-session call failed")


def _interpret(result: CallResult, settings: RetroSettings) -> LaunchPlan:
    if not result.exit_is(0, 12, 20):
        raise claude_session_error()
    try:
        plan = parse_plan(result.stdout)
    except Invalid:
        raise claude_session_error() from None
    # The exit code and the document must agree (the schema's own rule): 20 is a refusal, 12 a
    # failed validation, 0 neither. Anything else is a call that cannot be trusted.
    if (
        (result.returncode == 20 and plan.would_refuse is None)
        or (result.returncode == 12 and (plan.would_fail is None or plan.would_refuse))
        or (result.returncode == 0 and (plan.would_fail or plan.would_refuse))
    ):
        raise claude_session_error()
    if plan.would_refuse is not None:
        raise claude_session_error()  # a new session cannot be refused
    if plan.would_fail is not None:
        raise errors.CliError(
            "failed",
            "the retro session would not start",
            details={
                "reason": "session_not_started",
                "retryable": retryable(plan.would_fail),
                "end_reason": plan.would_fail,
            },
        )
    bad = settings_mismatches(plan, settings)
    if bad:
        raise errors.CliError(
            "failed",
            "the launch plan differs from the required settings",
            details={"reason": "launch_settings_mismatch", "retryable": False},
            hint=(
                "claude-session's plan differs in: "
                + ", ".join(bad)
                + ". A user or per-repo claude-session config that changes a safety setting of "
                "the audit kind makes every retro refuse until it is removed."
            ),
        )
    return LaunchPlan(plan.cwd, project_dir_name(plan.cwd))


def plan_check(
    gateway: Gateway, command: str, launch: Launch, *, state_directory: str
) -> LaunchPlan:
    """Order of checks step 11. Nothing is allocated by claude-session and nothing stays in the
    state directory; the retro id of the labels is a placeholder (no row exists yet)."""
    placeholders = _Placeholders(gateway, state_directory)
    try:
        prompt, *inputs = placeholders.files
        flags = launch_flags(launch, PLACEHOLDER_RETRO_ID, prompt_file=prompt, input_files=inputs)
        result = gateway.run(
            [command, "start", *flags, "--dry-run", "--json"],
            timeout=PLAN_TIMEOUT,
            stdout_limit=PLAN_OUTPUT_LIMIT,
        )
    finally:
        placeholders.remove()
    return _interpret(result, launch.settings)
