"""Removing a directory tree under the state directory, safely (interface.md, Retention and
Retro lifecycle, cleanup).

The walk is depth first through directory descriptors (``DirHandle``): every entry is looked up
relative to its parent, a symlink is unlinked and never followed, and nothing is entered that is
not a real directory of this user on the same device as its parent. A tree that holds anything
else (another owner, another device, a special file it cannot unlink, more than ``MAX_DEPTH``
levels or ``MAX_ENTRIES`` names in one directory) is left in place and reported as not removed.
The caller (``retention``, and later the retro cleanup) decides what a refusal means.
"""

from __future__ import annotations

import errno
import os
import stat as statmod

from .gateway import DirHandle, Gateway

MAX_DEPTH = 32
MAX_ENTRIES = 10_000


def is_component(name: bytes) -> bool:
    """One path component: not empty, not ``.`` or ``..``, no ``/`` and no NUL."""
    return bool(name) and name not in (b".", b"..") and b"/" not in name and b"\x00" not in name


def remove_tree(gateway: Gateway, parent: DirHandle, name: bytes, *, apply: bool = True) -> bool:
    """Remove the entry ``name`` of ``parent`` and everything below it. True when it is gone (a
    missing entry counts), False when something was left in place or ``name`` is not a single
    path component (nothing is touched then). With ``apply=False`` nothing is removed and the
    answer is the one a real removal would give (the planning form of ``prune`` and ``retro
    delete``); a directory the owner cannot write to and search is reported as not removable."""
    if not is_component(name):
        return False
    try:
        info = parent.lstat(name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not statmod.S_ISDIR(info.st_mode):
        return _unlink(gateway, parent, name, info.st_uid, apply)
    return _remove_dir(gateway, parent, name, info, 1, apply)


def _unlink(gateway: Gateway, parent: DirHandle, name: bytes, uid: int, apply: bool) -> bool:
    if uid != gateway.uid():
        return False
    if not apply:
        return True
    try:
        parent.unlink(name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _remove_dir(
    gateway: Gateway, parent: DirHandle, name: bytes, info: os.stat_result, depth: int, apply: bool
) -> bool:
    if depth > MAX_DEPTH or info.st_uid != gateway.uid():
        return False
    try:
        handle = parent.open_dir(name)  # O_NOFOLLOW: a swapped symlink is refused, not entered
    except FileNotFoundError:
        return True
    except OSError:
        return False
    clean = True
    try:
        opened = handle.stat()
        if opened.st_dev != parent.stat().st_dev or opened.st_uid != gateway.uid():
            return False
        if not apply and opened.st_mode & 0o300 != 0o300:
            return False  # planning: an owner without write and search on it cannot empty it
        names, more = handle.entries(MAX_ENTRIES)
        if more:
            return False
        for child in names:
            try:
                child_info = handle.lstat(child)
            except FileNotFoundError:
                continue
            except OSError:
                clean = False
                continue
            if statmod.S_ISDIR(child_info.st_mode):
                if child_info.st_dev != opened.st_dev:
                    clean = False
                elif not _remove_dir(gateway, handle, child, child_info, depth + 1, apply):
                    clean = False
            elif not _unlink(gateway, handle, child, child_info.st_uid, apply):
                clean = False
    finally:
        handle.close()
    if not clean:
        return False
    if not apply:
        return True
    try:
        parent.rmdir(name)
    except FileNotFoundError:
        return True
    except OSError as exc:
        return exc.errno == errno.ENOENT
    return True
