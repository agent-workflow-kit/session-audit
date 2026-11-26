"""The skill preflight (issue #24; interface, Retro launch item 0)."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from session_audit import errors, plugins
from session_audit.gateway import SystemGateway

from .fake_gateway import FakeGateway


class Counting(SystemGateway):
    """The real file system; counts every path lookup, to show that a refusal looked at none."""

    def __init__(self, uid: int | None = None) -> None:
        super().__init__()
        self.touched = 0
        self._uid = uid

    def lstat(self, path: Any) -> os.stat_result:
        self.touched += 1
        return super().lstat(path)

    def stat(self, path: Any) -> os.stat_result:
        self.touched += 1
        return super().stat(path)

    def open_root(self, path: Any) -> Any:
        self.touched += 1
        return super().open_root(path)

    def uid(self) -> int:
        return super().uid() if self._uid is None else self._uid


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "h"
    path.mkdir(mode=0o755)
    return path


def env_of(home: Path, **more: str) -> dict[str, str]:
    return {"HOME": str(home), **more}


def make_plugin(
    path: Path,
    *,
    name: str | None = "demo",
    skill: str | None = "audit",
    manifest: str | bytes | None = None,
) -> Path:
    """A plugin directory the preflight accepts (mode 755 and 644, owned by the test user)."""
    path.mkdir(parents=True, mode=0o755, exist_ok=True)
    path.chmod(0o755)
    if manifest is not None or name is not None:
        meta = path / ".claude-plugin"
        meta.mkdir(mode=0o755, exist_ok=True)
        meta.chmod(0o755)
        file = meta / "plugin.json"
        if manifest is None:
            file.write_text(json.dumps({"name": name}), encoding="utf-8")
        elif isinstance(manifest, bytes):
            file.write_bytes(manifest)
        else:
            file.write_text(manifest, encoding="utf-8")
        file.chmod(0o644)
    if skill is not None:
        directory = path / "skills" / skill
        directory.mkdir(parents=True, mode=0o755, exist_ok=True)
        for part in (path / "skills", directory):
            part.chmod(0o755)
        (directory / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (directory / "SKILL.md").chmod(0o644)
    return path


def run(
    gateway: Any,
    skill: str,
    home: Path,
    *,
    dirs: tuple[str, ...] = (),
    bundled: str | None = None,
    state: str | None = None,
    **env: str,
) -> plugins.PluginChoice:
    return plugins.skill_preflight(
        gateway,
        skill,
        plugin_dirs=dirs,
        env=env_of(home, **env),
        state_directory=state,
        bundled=bundled,
    )


def refusal(call: Any) -> errors.CliError:
    with pytest.raises(errors.CliError) as raised:
        call()
    return raised.value


def reason(error: errors.CliError) -> str | None:
    return error.details.get("reason")


# --- the skill name is checked before any path is built -----------------------------------------


@pytest.mark.parametrize(
    "skill",
    [
        "audit",  # a bare name
        "demo:",
        ":audit",
        "demo:a/b",
        "demo:..",
        "demo:../x",
        "demo:.hidden",
        "demo:Audit",
        "demo:AUDIT",
        "demo:" + "a" * 65,
        "demo:a:b",
        "de/mo:audit",
        "demo:audit\n",
        "demo:aud\x00it",
        "demo:äudit",
        "",
        "d" * 200,
    ],
)
def test_a_malformed_skill_is_exit_2_before_any_path_is_built(skill: str, home: Path) -> None:
    gateway = Counting()
    error = refusal(lambda: run(gateway, skill, home, dirs=(str(home / "x"),), bundled=str(home)))
    assert error.exit_code == 2 and error.details["field"] == "--skill"
    assert gateway.touched == 0


def test_a_name_at_the_limit_is_looked_up(home: Path) -> None:
    name = "a" * 64
    plugin = make_plugin(home / "p", skill=name)
    choice = run(Counting(), f"demo:{name}", home, dirs=(str(plugin),))
    assert choice.found == str(plugin.resolve())


# --- the bundled plugin: no owner or mode check -------------------------------------------------


def test_the_bundled_plugin_passes_with_group_writable_modes_and_another_owner(
    home: Path,
) -> None:
    bundled = make_plugin(home / "wheel" / "plugin", name="session-audit")
    for path in [bundled, *bundled.rglob("*")]:  # umask 002: drwxrwxr-x and -rw-rw-r--
        path.chmod(0o775 if path.is_dir() else 0o664)
    gateway = Counting(uid=os.geteuid() + 1)  # nothing is owned by "me": the bundle is not checked
    choice = run(gateway, "session-audit:audit", home, bundled=str(bundled))
    assert choice == plugins.PluginChoice(str(bundled.resolve()), (str(bundled.resolve()),))


def test_a_configured_directory_with_the_same_modes_is_refused(home: Path) -> None:
    plugin = make_plugin(home / "p")
    (plugin / "skills" / "audit" / "SKILL.md").chmod(0o664)
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(plugin),)))
    assert error.exit_code == 12 and reason(error) == "unsafe_plugin_dir"


def test_the_found_directory_is_added_to_the_bundled_one(home: Path) -> None:
    bundled = make_plugin(home / "b", name="session-audit")
    custom = make_plugin(home / "c", name="demo", skill="review")
    choice = run(Counting(), "demo:review", home, dirs=(str(custom),), bundled=str(bundled))
    assert choice.found == str(custom.resolve())
    assert choice.dirs == (str(bundled.resolve()), str(custom.resolve()))


def test_the_bundled_directory_is_not_passed_twice(home: Path) -> None:
    bundled = make_plugin(home / "b", name="session-audit")
    choice = run(
        Counting(), "session-audit:audit", home, dirs=(str(bundled),), bundled=str(bundled)
    )
    assert choice.dirs == (str(bundled.resolve()),)


def test_a_missing_bundled_plugin_is_skill_not_found_not_a_crash(home: Path) -> None:
    error = refusal(
        lambda: run(Counting(), "session-audit:audit", home, bundled=str(home / "nope"))
    )
    assert reason(error) == "skill_not_found" and error.details["retryable"] is False


# --- matching ------------------------------------------------------------------------------------


def test_the_plugin_name_comes_from_the_manifest_not_the_directory_name(home: Path) -> None:
    plugin = make_plugin(home / "renamed", name="demo")
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found
    error = refusal(lambda: run(Counting(), "renamed:audit", home, dirs=(str(plugin),)))
    assert reason(error) == "skill_not_found"


def test_without_a_manifest_the_base_name_is_the_plugin_name(home: Path) -> None:
    plugin = make_plugin(home / "tool", name=None)
    assert run(Counting(), "tool:audit", home, dirs=(str(plugin),)).found == str(plugin.resolve())


def test_an_empty_manifest_name_falls_back_to_the_base_name(home: Path) -> None:
    plugin = make_plugin(home / "tool", manifest='{"name": ""}')
    assert run(Counting(), "tool:audit", home, dirs=(str(plugin),)).found


@pytest.mark.parametrize(
    "manifest",
    [
        "not json",
        "[]",
        '{"name": 7}',
        '{"name": ["tool"]}',
        '{"name": "tool", "name": "tool"}',  # a duplicate key
        '{"name": "tool", "x": NaN}',
        '{"name": "tool", "x": ' + "[" * 30000 + "]" * 30000 + "}",
        b'{"name": "tool"}\xff',
        b'{"name": "tool", "pad": "' + b"x" * (plugins.MANIFEST_CAP) + b'"}',  # over 64 KiB
        "",
    ],
)
def test_a_hostile_manifest_is_never_a_match_even_when_the_base_name_would_be(
    manifest: str | bytes, home: Path
) -> None:
    plugin = make_plugin(home / "tool", manifest=manifest)
    error = refusal(lambda: run(Counting(), "tool:audit", home, dirs=(str(plugin),)))
    assert reason(error) == "skill_not_found"


def test_a_manifest_exactly_at_the_cap_is_read(home: Path) -> None:
    body = b'{"name": "tool", "pad": "'
    pad = b"x" * (plugins.MANIFEST_CAP - len(body) - 2) + b'"}'
    assert len(body + pad) == plugins.MANIFEST_CAP
    plugin = make_plugin(home / "other", manifest=body + pad)
    assert run(Counting(), "tool:audit", home, dirs=(str(plugin),)).found


def test_a_manifest_that_is_a_symlink_or_a_fifo_is_not_followed_or_opened_blocking(
    home: Path,
) -> None:
    plugin = make_plugin(home / "tool", name=None, skill="audit")
    meta = plugin / ".claude-plugin"
    meta.mkdir(mode=0o755)
    meta.chmod(0o755)
    os.mkfifo(meta / "plugin.json", 0o644)
    # The walk refuses the FIFO before anything is opened.
    error = refusal(lambda: run(Counting(), "tool:audit", home, dirs=(str(plugin),)))
    assert reason(error) == "unsafe_plugin_dir"
    # Bundled (not walked): the FIFO is detected by fstat, never read, and is no match.
    error = refusal(lambda: run(Counting(), "tool:audit", home, bundled=str(plugin)))
    assert reason(error) == "skill_not_found"


@pytest.mark.parametrize("variant", ["missing", "directory", "symlink", "wrong_name"])
def test_the_skill_file_must_be_a_regular_file_at_the_exact_place(variant: str, home: Path) -> None:
    plugin = make_plugin(home / "p", skill=None if variant == "missing" else "audit")
    skill = plugin / "skills" / "audit" / "SKILL.md"
    if variant == "directory":
        skill.unlink()
        skill.mkdir(mode=0o755)
    elif variant == "symlink":
        skill.unlink()
        skill.symlink_to(home / "elsewhere.md")
    elif variant == "wrong_name":
        (plugin / "skills" / "audit").rename(plugin / "skills" / "other")
    bundled = str(plugin)  # not walked: the matching itself must refuse
    assert reason(refusal(lambda: run(Counting(), "demo:audit", home, bundled=bundled))) == (
        "skill_not_found"
    )


def test_a_skills_directory_that_is_a_symlink_is_not_followed(home: Path) -> None:
    plugin = make_plugin(home / "p", skill=None)
    real = make_plugin(home / "real", name=None)
    (plugin / "skills").symlink_to(real / "skills")
    error = refusal(lambda: run(Counting(), "demo:audit", home, bundled=str(plugin)))
    assert reason(error) == "skill_not_found"


def test_the_first_match_wins_and_later_directories_are_not_checked(home: Path) -> None:
    first = make_plugin(home / "a")
    unsafe = make_plugin(home / "b")
    (unsafe / "skills").chmod(0o777)
    choice = run(Counting(), "demo:audit", home, dirs=(str(first), str(unsafe)))
    assert choice.found == str(first.resolve())


def test_an_unsafe_directory_before_the_match_refuses_even_if_a_later_one_matches(
    home: Path,
) -> None:
    unsafe = make_plugin(home / "a", name="other")
    (unsafe / "skills").chmod(0o777)
    good = make_plugin(home / "b")
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(unsafe), str(good))))
    assert reason(error) == "unsafe_plugin_dir"


def test_a_missing_configured_directory_is_skipped(home: Path) -> None:
    good = make_plugin(home / "b")
    choice = run(Counting(), "demo:audit", home, dirs=(str(home / "nope"), str(good)))
    assert choice.found == str(good.resolve())
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(home / "nope"),)))
    assert reason(error) == "skill_not_found"


# --- the walk ------------------------------------------------------------------------------------


def unsafe(home: Path, plugin: Path, gateway: Any = None) -> errors.CliError:
    error = refusal(lambda: run(gateway or Counting(), "demo:audit", home, dirs=(str(plugin),)))
    assert error.exit_code == 12 and error.code == "failed", error
    assert reason(error) == "unsafe_plugin_dir" and error.details["retryable"] is False
    return error


@pytest.mark.parametrize("mode", [0o020, 0o002, 0o022, 0o777])
@pytest.mark.parametrize("where", ["root", "directory", "file", "nested_file", "manifest"])
def test_a_group_or_other_writable_entry_is_refused(where: str, mode: int, home: Path) -> None:
    plugin = make_plugin(home / "p")
    target = {
        "root": plugin,
        "directory": plugin / "skills",
        "file": plugin / "skills" / "audit" / "SKILL.md",
        "nested_file": plugin / "skills" / "audit" / "SKILL.md",
        "manifest": plugin / ".claude-plugin" / "plugin.json",
    }[where]
    target.chmod(target.stat().st_mode | mode)
    unsafe(home, plugin)


def test_an_entry_owned_by_someone_else_is_refused(home: Path) -> None:
    plugin = make_plugin(home / "p")
    unsafe(home, plugin, Counting(uid=os.geteuid() + 1))


def test_a_readable_executable_unwritable_tree_is_accepted(home: Path) -> None:
    plugin = make_plugin(home / "p")
    script = plugin / "bin.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    (plugin / "ro").mkdir(mode=0o555)
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found


@pytest.mark.parametrize("target", ["outside", "inside", "dangling", "directory", "self"])
def test_a_symlink_is_refused_whether_it_escapes_the_tree_or_not(target: str, home: Path) -> None:
    plugin = make_plugin(home / "p")
    secret = home / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    where = {
        "outside": secret,
        "inside": plugin / "skills" / "audit" / "SKILL.md",
        "dangling": home / "gone",
        "directory": home,
        "self": plugin / "loop",
    }[target]
    (plugin / "loop").symlink_to(where)
    unsafe(home, plugin)


def test_a_symlink_loop_in_a_subdirectory_does_not_hang_or_recurse(home: Path) -> None:
    plugin = make_plugin(home / "p")
    (plugin / "skills" / "up").symlink_to(plugin)
    unsafe(home, plugin)


def test_the_configured_entry_itself_may_not_be_a_symlink(home: Path) -> None:
    plugin = make_plugin(home / "p")
    link = home / "link"
    link.symlink_to(plugin)
    unsafe(home, link)


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_a_special_file_is_refused_and_never_opened(kind: str, home: Path) -> None:
    plugin = make_plugin(home / "p")
    path = plugin / "special"
    if kind == "fifo":
        os.mkfifo(path, 0o644)
    else:
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(path))
            path.chmod(0o644)
            unsafe(home, plugin)
        finally:
            server.close()
        return
    unsafe(home, plugin)


def test_a_device_is_refused(home: Path) -> None:
    gateway = FakeGateway()
    gateway.add_dir("/plugins/p", mode=0o755)
    gateway.add_dir("/plugins/p/skills", mode=0o755)
    gateway.add_dir("/plugins/p/skills/audit", mode=0o755)
    gateway.add_file("/plugins/p/skills/audit/SKILL.md", mode=0o644)
    gateway.add_device("/plugins/p/dev", mode=0o644)
    error = refusal(
        lambda: plugins.skill_preflight(
            gateway, "p:audit", plugin_dirs=["/plugins/p"], env={"HOME": "/home/u"}, bundled=None
        )
    )
    assert reason(error) == "unsafe_plugin_dir"


def fill(plugin: Path, count: int) -> None:
    directory = plugin / "bulk"
    directory.mkdir(mode=0o755)
    for index in range(count):
        (directory / f"f{index}").touch(mode=0o644)


def test_ten_thousand_entries_pass_and_one_more_is_refused(home: Path) -> None:
    plugin = make_plugin(home / "p")
    present = sum(1 for _ in plugin.rglob("*"))
    fill(plugin, plugins.MAX_ENTRIES - present - 1)  # the bulk directory is one entry itself
    assert sum(1 for _ in plugin.rglob("*")) == plugins.MAX_ENTRIES
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found
    (plugin / "one-more").touch(mode=0o644)
    unsafe(home, plugin)


def test_thirty_two_levels_pass_and_thirty_three_are_refused(home: Path) -> None:
    plugin = make_plugin(home / "p")
    deep = plugin
    for level in range(plugins.MAX_DEPTH):
        deep = deep / f"d{level}"
        deep.mkdir(mode=0o755)
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found
    (deep / "d32").mkdir(mode=0o755)
    unsafe(home, plugin)


def test_sixty_four_mebibytes_pass_and_one_byte_more_is_refused(home: Path) -> None:
    plugin = make_plugin(home / "p")
    big = plugin / "big.bin"
    big.touch(mode=0o644)
    skill = plugin / "skills" / "audit" / "SKILL.md"
    big.write_bytes(b"")
    with open(big, "r+b") as handle:  # a sparse file: the size counts, no disk is used
        handle.truncate(
            plugins.MAX_BYTES
            - skill.stat().st_size
            - (plugin / ".claude-plugin" / "plugin.json").stat().st_size
        )
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found
    with open(big, "r+b") as handle:
        handle.truncate(big.stat().st_size + 1)
    unsafe(home, plugin)


def test_toolchain_directories_get_no_exception(home: Path) -> None:
    plugin = make_plugin(home / "p")
    modules = plugin / "node_modules" / "dep"
    modules.mkdir(parents=True, mode=0o755)
    (modules / "bin").symlink_to("../x")  # the usual .bin link: still refused (docs: no exception)
    unsafe(home, plugin)


def test_a_directory_swapped_for_a_symlink_during_the_walk_is_refused() -> None:
    gateway = FakeGateway()
    for path in ("/plugins/p", "/plugins/p/skills", "/plugins/p/skills/audit", "/plugins/p/x"):
        gateway.add_dir(path, mode=0o755)
    gateway.add_file("/plugins/p/skills/audit/SKILL.md", mode=0o644)

    def swap(path: str) -> None:
        if path == "/plugins/p/x":
            gateway.add_symlink("/plugins/p/x", "/etc")

    gateway.on_open = swap
    error = refusal(
        lambda: plugins.skill_preflight(
            gateway, "p:audit", plugin_dirs=["/plugins/p"], env={"HOME": "/home/u"}, bundled=None
        )
    )
    assert reason(error) == "unsafe_plugin_dir"


def test_a_directory_replaced_by_another_between_lstat_and_open_is_refused() -> None:
    gateway = FakeGateway()
    for path in ("/plugins/p", "/plugins/p/skills", "/plugins/p/skills/audit", "/plugins/p/x"):
        gateway.add_dir(path, mode=0o755)
    gateway.add_file("/plugins/p/skills/audit/SKILL.md", mode=0o644)

    def swap(path: str) -> None:
        if path == "/plugins/p/x":
            gateway.add_dir("/plugins/p/x", mode=0o755)  # another directory: another inode

    gateway.on_open = swap
    error = refusal(
        lambda: plugins.skill_preflight(
            gateway, "p:audit", plugin_dirs=["/plugins/p"], env={"HOME": "/home/u"}, bundled=None
        )
    )
    assert reason(error) == "unsafe_plugin_dir"


def test_a_clean_tree_in_the_in_memory_gateway_is_accepted() -> None:
    gateway = FakeGateway()
    for path in ("/plugins/p", "/plugins/p/skills", "/plugins/p/skills/audit"):
        gateway.add_dir(path, mode=0o755)
    gateway.add_file("/plugins/p/skills/audit/SKILL.md", mode=0o644)
    choice = plugins.skill_preflight(
        gateway, "p:audit", plugin_dirs=["/plugins/p"], env={"HOME": "/home/u"}, bundled=None
    )
    assert choice.found == "/plugins/p"


# --- credential and state locations -------------------------------------------------------------


def locations(home: Path) -> Mapping[str, Path]:
    xdg = home.parent / "xdg"
    return {
        "ssh": home / ".ssh",
        "inside_ssh": home / ".ssh" / "sub",
        "aws": home / ".aws",
        "gnupg": home / ".gnupg",
        "claude": home / ".claude",
        "inside_claude": home / ".claude" / "plugins" / "p",
        "config": home / ".config",
        "inside_config": home / ".config" / "gcloud" / "p",
        "state": home / ".local" / "state",
        "inside_state": home / ".local" / "state" / "session-audit" / "p",
        "xdg_config": xdg / "c",
        "inside_xdg_config": xdg / "c" / "p",
        "xdg_state": xdg / "s",
        "inside_xdg_state": xdg / "s" / "p",
        "git_credentials": home / ".git-credentials",
    }


@pytest.mark.parametrize("name", list(locations(Path("/h"))))
def test_a_directory_in_or_at_a_credential_location_is_refused(name: str, home: Path) -> None:
    where = locations(home)[name]
    plugin = make_plugin(where, name="demo")
    environ = {
        "XDG_CONFIG_HOME": str(home.parent / "xdg" / "c"),
        "XDG_STATE_HOME": str(home.parent / "xdg" / "s"),
    }
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(plugin),), **environ))
    assert reason(error) == "unsafe_plugin_dir"


def test_a_directory_that_contains_a_credential_location_is_refused(home: Path) -> None:
    (home / ".ssh").mkdir(mode=0o700)
    (home / "stuff").mkdir()
    for ancestor in (home / ".config" / "x" / "..", home):  # contains .ssh, is $HOME
        error = refusal(lambda a=ancestor: run(Counting(), "demo:audit", home, dirs=(str(a),)))
        assert reason(error) == "unsafe_plugin_dir"


@pytest.mark.parametrize("which", ["home", "parent", "root"])
def test_home_and_its_ancestors_are_refused(which: str, home: Path) -> None:
    plugin = {"home": home, "parent": home.parent, "root": Path("/")}[which]
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(plugin),)))
    assert reason(error) == "unsafe_plugin_dir"


def test_the_state_directory_is_refused(home: Path, tmp_path: Path) -> None:
    state = tmp_path / "elsewhere" / "session-audit"
    plugin = make_plugin(state / "plugin")
    error = refusal(
        lambda: run(Counting(), "demo:audit", home, dirs=(str(plugin),), state=str(state))
    )
    assert reason(error) == "unsafe_plugin_dir"


def test_a_location_reached_through_a_symlink_is_refused(home: Path) -> None:
    (home / ".aws").mkdir(mode=0o700)
    alias = home / "alias"
    alias.symlink_to(home / ".aws")
    # The configured entry is a symlink, and its realpath is a credential location: refused
    # either way, and by the location rule first.
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(alias),)))
    assert reason(error) == "unsafe_plugin_dir"
    # A real directory below a symlinked ancestor is judged by where it really is.
    plugin = make_plugin(home / ".aws" / "p")
    via = home / "alias" / "p"
    error = refusal(lambda: run(Counting(), "demo:audit", home, dirs=(str(via),)))
    assert reason(error) == "unsafe_plugin_dir"
    assert plugin.exists()


def test_a_sibling_whose_name_only_starts_like_a_location_is_accepted(home: Path) -> None:
    for sibling in (".sshx", ".claude-plugins", ".configs", ".local/state2"):
        plugin = make_plugin(home / sibling / "p")
        assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found


def test_a_plugin_under_the_home_directory_but_outside_the_locations_is_accepted(
    home: Path,
) -> None:
    plugin = make_plugin(home / "code" / "plugins" / "p")
    assert run(Counting(), "demo:audit", home, dirs=(str(plugin),)).found


# --- the bundled directory lookup ---------------------------------------------------------------


def test_the_bundled_plugin_is_the_package_resource_next_to_the_modules(tmp_path: Path) -> None:
    package = tmp_path / "site" / "session_audit"
    (package / "plugin").mkdir(parents=True)
    assert plugins.bundled_plugin_dir(SystemGateway(), str(package)) == str(
        (package / "plugin").resolve()
    )


def test_the_bundled_plugin_of_a_symlinked_install_is_resolved(tmp_path: Path) -> None:
    package = tmp_path / "site" / "session_audit"
    package.mkdir(parents=True)
    real = tmp_path / "real-plugin"
    real.mkdir()
    (package / "plugin").symlink_to(real)
    assert plugins.bundled_plugin_dir(SystemGateway(), str(package)) == str(real.resolve())


def test_a_source_checkout_uses_the_plugin_directory_of_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    package = root / "src" / "session_audit"
    package.mkdir(parents=True)
    (root / "plugin").mkdir()
    gateway = SystemGateway()
    assert plugins.bundled_plugin_dir(gateway, str(package)) is None  # no pyproject yet
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    assert plugins.bundled_plugin_dir(gateway, str(package)) == str((root / "plugin").resolve())


def test_a_package_without_a_plugin_has_no_bundled_directory(tmp_path: Path) -> None:
    package = tmp_path / "session_audit"
    package.mkdir()
    (package / "plugin").write_text("a file", encoding="utf-8")
    assert plugins.bundled_plugin_dir(SystemGateway(), str(package)) is None


def test_the_default_lookup_finds_this_repository_plugin_and_its_skill(home: Path) -> None:
    bundled = plugins.bundled_plugin_dir(SystemGateway())
    assert bundled is not None and (Path(bundled) / ".claude-plugin" / "plugin.json").is_file()
    choice = run(Counting(), "session-audit:audit", home)
    assert choice.found == bundled and choice.dirs == (bundled,)
