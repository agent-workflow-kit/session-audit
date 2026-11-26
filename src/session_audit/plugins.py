"""The skill preflight (interface, Retro launch item 0; Order of checks step 4).

``skill_preflight`` finds the plugin directory that holds the retro's skill, before anything is
stored or spent::

    skill = plugin ":" name        # a bare name cannot load with no setting sources: exit 2
    name  = [a-z0-9][a-z0-9-]{0,63}  # checked before any path is built from it: exit 2

For the bundled plugin (a package resource, no owner or mode check: it is the tool's own code and
installed tool directories are group writable by default) and then for each ``retro.plugin_dirs``
entry, in this order:

1. a configured directory that is, contains or is inside a credential or state location (the
   list claude-session refuses, plus the state directory), or is ``$HOME`` or an ancestor of it:
   exit 12 ``unsafe_plugin_dir``;
2. a configured directory is walked WITHOUT following symlinks (descriptor walk: every name is
   looked up relative to its parent's descriptor, a directory is opened ``O_NOFOLLOW`` and its
   ``fstat`` is compared with the ``lstat`` that classified it). Every entry must be a regular
   file or a directory, owned by the user and neither group nor other writable. A symlink (also
   one that stays inside the tree), a FIFO, a socket, a device, more than 10,000 entries, more
   than 64 MiB of file bytes or more than 32 levels is exit 12 ``unsafe_plugin_dir``. Toolchain
   directories are not special: the same rule holds below ``node_modules`` or ``.venv``;
3. the plugin name is the ``name`` of ``.claude-plugin/plugin.json`` (at most 64 KiB, strict JSON)
   or, when the manifest is absent, the directory's base name. A manifest that is present but
   unreadable, too large, not strict JSON or without a string ``name`` makes the directory not a
   match (never a fall back to the base name);
4. a match needs ``skills/<name>/SKILL.md`` as a regular file (no symlink on the way).

No match anywhere is exit 12 ``skill_not_found``. The directories for ``--plugin-dir`` are the
bundled one (always, when it exists) and the one that matched.

Every file system access goes through ``DirHandle`` descriptors of the gateway. Credential
locations are compared by path components, never by string prefix.
"""

from __future__ import annotations

import importlib.resources
import os
import re
import stat as statmod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import args as a
from . import errors
from .config import home_dir
from .gateway import DirHandle, Gateway
from .harness import Invalid, strict_loads

__all__ = [
    "MANIFEST_CAP",
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_ENTRIES",
    "SKILL_NAME",
    "PluginChoice",
    "bundled_plugin_dir",
    "credential_locations",
    "skill_preflight",
]

SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}", re.ASCII)
MAX_ENTRIES = 10_000  # claude-session's limits for a plugin copy
MAX_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 32
MANIFEST_CAP = 64 * 1024
MANIFEST_DIR = b".claude-plugin"
MANIFEST_FILE = b"plugin.json"
BUNDLED_NAME = "session-audit"

# Relative to $HOME: claude-session's credential paths (its Path rules), which a plugin directory
# may not be, contain or be inside, plus ``~/.local/state`` where the state directory lives.
HOME_CREDENTIALS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config",
    ".claude",
    ".netrc",
    ".git-credentials",
    ".local/state",
    ".local/share/keyrings",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".gradle/gradle.properties",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
)


@dataclass(frozen=True)
class PluginChoice:
    """``found`` is the directory that holds the skill; ``dirs`` are the ``--plugin-dir`` values
    (the bundled plugin first when it exists, then ``found`` when it is another directory)."""

    found: str
    dirs: tuple[str, ...]


class _Unsafe(Exception):
    """A configured directory failed its check (never shown: the error is a fixed one)."""


def unsafe_plugin_dir() -> errors.CliError:
    return errors.CliError(
        "failed",
        "a configured plugin directory is not safe to load",
        details={"reason": "unsafe_plugin_dir", "retryable": False, "field": "retro.plugin_dirs"},
        hint=(
            "Each entry of retro.plugin_dirs must be a tree of regular files and directories "
            "that you own and that are not group or other writable, outside credential and "
            "configuration locations, within 10,000 entries, 64 MiB and 32 levels."
        ),
    )


def skill_not_found() -> errors.CliError:
    return errors.CliError(
        "failed",
        "no plugin directory holds the skill",
        details={"reason": "skill_not_found", "retryable": False},
        hint="Name a skill of the bundled plugin or of a directory in retro.plugin_dirs.",
    )


# --- the bundled plugin ----------------------------------------------------------------------


def bundled_plugin_dir(gateway: Gateway, package_dir: str | None = None) -> str | None:
    """``realpath(<package>/plugin)``, or None when it does not exist. The wheel carries it as
    ``session_audit/plugin`` (``pyproject.toml``, force-include). In a source checkout (the
    package sits in ``<root>/src`` and ``<root>/pyproject.toml`` exists) the plugin is the
    ``plugin`` directory of the repository root. ``package_dir`` is for tests."""
    if package_dir is None:
        located = importlib.resources.files("session_audit")
        if not isinstance(located, os.PathLike):
            return None  # a package that is not on a file system (a zip import): not supported
        package_dir = os.fspath(located)
    package = os.path.normpath(package_dir)
    candidates = [os.path.join(package, "plugin")]
    root = os.path.dirname(os.path.dirname(package))
    if os.path.basename(os.path.dirname(package)) == "src" and _is_file(
        gateway, os.path.join(root, "pyproject.toml")
    ):
        candidates.append(os.path.join(root, "plugin"))
    for candidate in candidates:
        try:
            if statmod.S_ISDIR(gateway.stat(candidate).st_mode):
                return os.path.realpath(candidate)
        except OSError:
            continue
    return None


def _is_file(gateway: Gateway, path: str) -> bool:
    try:
        return statmod.S_ISREG(gateway.lstat(path).st_mode)
    except OSError:
        return False


# --- credential and state locations ----------------------------------------------------------


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in os.path.normpath(path).split("/") if p)


def _related(one: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """One path is, contains or is inside the other (a prefix of components either way)."""
    short = min(len(one), len(other))
    return one[:short] == other[:short]


def credential_locations(
    env: Mapping[str, str], home: str, state_directory: str | None = None
) -> list[str]:
    """The locations a configured plugin directory may not overlap (``$HOME`` and its ancestors
    are a separate rule). Each is listed as written and as ``realpath`` sees it."""
    found = [os.path.join(home, name) for name in HOME_CREDENTIALS]
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        value = env.get(variable, "")
        if value.startswith("/") and "\x00" not in value:
            found.append(value)
    data = env.get("XDG_DATA_HOME", "")
    if data.startswith("/") and "\x00" not in data:
        found.append(os.path.join(data, "keyrings"))
    if state_directory:
        found.append(state_directory)
    return list(dict.fromkeys([*found, *(os.path.realpath(p) for p in found)]))


def _refused_location(path: str, locations: Sequence[str], home: str) -> bool:
    """True when ``path`` (checked as written and by its realpath) is, contains or is inside a
    credential location, or is ``$HOME`` or an ancestor of it (``/`` included)."""
    homes = {_parts(home), _parts(os.path.realpath(home))}
    for form in (path, os.path.realpath(path)):
        mine = _parts(form)
        if any(mine == h[: len(mine)] for h in homes):  # the root, an ancestor of home, or home
            return True
        if any(_related(mine, _parts(location)) for location in locations):
            return True
    return False


# --- the tree walk ---------------------------------------------------------------------------


@dataclass
class _Budget:
    entries: int = 0
    size: int = 0


def _check_owner_and_mode(info: os.stat_result, uid: int) -> None:
    if info.st_uid != uid or info.st_mode & 0o022:
        raise _Unsafe


def _walk(gateway: Gateway, handle: DirHandle, depth: int, budget: _Budget, uid: int) -> None:
    names, more = handle.entries(MAX_ENTRIES - budget.entries)
    if more:
        raise _Unsafe
    budget.entries += len(names)
    for name in names:
        try:
            info = handle.lstat(name)
        except OSError:
            raise _Unsafe from None  # gone or unreadable: the tree is changing
        if statmod.S_ISDIR(info.st_mode):
            if depth + 1 > MAX_DEPTH:
                raise _Unsafe
            try:
                child = handle.open_dir(name)  # O_NOFOLLOW: a swapped symlink is refused
            except OSError:
                raise _Unsafe from None
            try:
                opened = child.stat()
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise _Unsafe  # swapped between the lstat and the open
                _check_owner_and_mode(opened, uid)
                _walk(gateway, child, depth + 1, budget, uid)
            finally:
                child.close()
        elif statmod.S_ISREG(info.st_mode):
            _check_owner_and_mode(info, uid)
            budget.size += info.st_size
            if budget.size > MAX_BYTES:
                raise _Unsafe
        else:  # a symlink, FIFO, socket or device
            raise _Unsafe


def _check_tree(gateway: Gateway, root: DirHandle, uid: int) -> None:
    info = root.stat()
    if not statmod.S_ISDIR(info.st_mode):
        raise _Unsafe
    _check_owner_and_mode(info, uid)
    _walk(gateway, root, 0, _Budget(), uid)


# --- the manifest and the skill file ---------------------------------------------------------

_ABSENT = object()
_INVALID = object()


def _manifest_name(gateway: Gateway, root: DirHandle) -> object:
    """The manifest's ``name`` (a non-empty string), ``_ABSENT`` for no manifest file (or an empty
    name: the base name then counts) and ``_INVALID`` for a manifest that cannot be trusted."""
    try:
        directory = root.open_dir(MANIFEST_DIR)
    except FileNotFoundError:
        return _ABSENT
    except OSError:
        return _INVALID
    try:
        try:
            handle = directory.open_file(MANIFEST_FILE)
        except FileNotFoundError:
            return _ABSENT
        except OSError:
            return _INVALID
        try:
            if not statmod.S_ISREG(gateway.fstat(handle).st_mode):
                return _INVALID
            gateway.set_blocking(handle)
            data = handle.read(MANIFEST_CAP + 1)
        except OSError:
            return _INVALID
        finally:
            handle.close()
    finally:
        directory.close()
    if len(data) > MANIFEST_CAP:
        return _INVALID
    try:
        document = strict_loads(data.decode("utf-8"))
    except (Invalid, UnicodeDecodeError):
        return _INVALID
    if not isinstance(document, dict):
        return _INVALID
    name = document.get("name")
    if name is None or name == "":
        return _ABSENT
    return name if isinstance(name, str) else _INVALID


def _has_skill(root: DirHandle, name: str) -> bool:
    """``skills/<name>/SKILL.md`` is a regular file, no symlink on the way."""
    try:
        skills = root.open_dir(b"skills")
    except OSError:
        return False
    try:
        try:
            directory = skills.open_dir(name.encode("ascii"))
        except OSError:
            return False
        try:
            return statmod.S_ISREG(directory.lstat(b"SKILL.md").st_mode)
        except OSError:
            return False
        finally:
            directory.close()
    finally:
        skills.close()


def _matches(gateway: Gateway, root: DirHandle, real: str, plugin: str, name: str) -> bool:
    found = _manifest_name(gateway, root)
    if found is _INVALID:
        return False
    plugin_name = os.path.basename(real) if found is _ABSENT else found
    return plugin_name == plugin and _has_skill(root, name)


# --- the preflight ---------------------------------------------------------------------------


def skill_preflight(
    gateway: Gateway,
    skill: str,
    *,
    plugin_dirs: Sequence[str],
    env: Mapping[str, str],
    state_directory: str | None = None,
    bundled: str | None = None,
) -> PluginChoice:
    """The plugin directory that holds ``skill`` (``plugin:name``). Exit 2 for a malformed skill
    (before any path is built), 12 ``unsafe_plugin_dir`` or 12 ``skill_not_found``.
    ``bundled`` is the bundled plugin directory (``bundled_plugin_dir`` when None)."""
    a.check_skill(skill)  # exit 2 naming --skill: a bare name, a bad plugin part or name part
    plugin, _, name = skill.partition(":")
    if not SKILL_NAME.fullmatch(name):
        raise errors.usage("invalid skill", field="--skill", hint="Use plugin:skill.")
    if bundled is None:
        bundled = bundled_plugin_dir(gateway)
    bundled_dirs = (os.path.realpath(bundled),) if bundled is not None else ()
    home = home_dir(env)
    locations = credential_locations(env, home, state_directory)
    uid = gateway.uid()
    for directory, configured in [(b, False) for b in bundled_dirs] + [
        (d, True) for d in plugin_dirs
    ]:
        if configured:
            if _refused_location(directory, locations, home):
                raise unsafe_plugin_dir()
            try:
                if statmod.S_ISLNK(gateway.lstat(directory).st_mode):
                    raise unsafe_plugin_dir()  # the entry itself is a symlink: not walked
            except OSError:
                continue  # missing: claude-session reports it; it holds no skill
        try:
            root = gateway.open_root(directory)
        except OSError:
            continue
        try:
            real = os.fsdecode(root.path)
            if configured:
                # The entry is checked where it really is, as well as where it was written.
                if _refused_location(real, locations, home):
                    raise unsafe_plugin_dir()
                try:
                    _check_tree(gateway, root, uid)
                    again = gateway.lstat(real)
                    opened = root.stat()
                    if (again.st_dev, again.st_ino) != (opened.st_dev, opened.st_ino):
                        raise _Unsafe  # the directory was swapped while it was walked
                except (_Unsafe, OSError):
                    raise unsafe_plugin_dir() from None
            if _matches(gateway, root, real, plugin, name):
                dirs = (*bundled_dirs, *(() if real in bundled_dirs else (real,)))
                return PluginChoice(real, dirs)
        finally:
            root.close()
    raise skill_not_found()
