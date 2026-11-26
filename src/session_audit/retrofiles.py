"""The retro's files under the state directory (Order of checks step 15; issue #26).

``digests/<retro_id>/digest.md`` and ``manifest.json``, ``prompts/<retro_id>/prompt.md``, and the
path of ``outcomes/<retro_id>.json`` (written by claude-session, never by the tool). Every
directory is created 0700 and every file 0600 through the gateway, never through a symlink:

* an existing parent (``digests``, ``prompts``, ``outcomes``) must be a real directory of this
  user with mode exactly 0700; the retro's own directory must not exist yet (a random id never
  collides, so a pre-existing path is hostile or stale: refused, never followed, never changed);
* a file is written to a temporary name in the retro's directory (``O_EXCL`` by name; a file that
  already exists is refused), flushed and ``fsync``-ed, then atomically replaced onto its final
  name (``Gateway.replace`` also syncs the directory), so a reader never sees a partial file;
* a failure (a full disk, a refused path) removes what THIS call created and raises
  ``FilesError``; ``delete_local`` is the idempotent removal by retro id that the cleanup and
  the recovery of #27 use (it never follows a symlink and leaves what it cannot prove its own).

The prompt holds the retro id, the run token, the digest hash and the input paths relative to the
session directory: no transcript text.
"""

from __future__ import annotations

import hashlib
import os
import stat as statmod
from dataclasses import dataclass
from typing import BinaryIO

from .errors import CliError
from .fencing import FencedDigest
from .gateway import DirHandle, Gateway
from .removal import remove_tree
from .store import DIR_MODE, FILE_MODE

__all__ = [
    "DIGESTS",
    "OUTCOMES",
    "PROMPTS",
    "FilesError",
    "RunFiles",
    "delete_local",
    "outcome_path",
    "prompt_text",
    "write_run_files",
]

DIGESTS = "digests"
PROMPTS = "prompts"
OUTCOMES = "outcomes"


class FilesError(Exception):
    """A step 15 file could not be written. ``retryable`` is true for a full disk or a read-only
    file system (the user can fix that), false for a refused path."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class RunFiles:
    prompt: str
    digest: str
    manifest: str
    outcome: str


def outcome_path(state: str, retro_id: str) -> str:
    return os.path.join(state, OUTCOMES, f"{retro_id}.json")


def prompt_text(retro_id: str, run_token: str, digest_sha256: str) -> str:
    """The prompt file: ids, a token, a hash and relative paths only."""
    return (
        f"retro_id: {retro_id}\n"
        f"run_token: {run_token}\n"
        f"digest_sha256: {digest_sha256}\n"
        "inputs:\n"
        "- inputs/1/digest.md\n"
        "- inputs/1/manifest.json\n"
    )


def _check_private(gateway: Gateway, path: str) -> None:
    try:
        info = gateway.lstat(path)
    except OSError as exc:
        raise FilesError("a directory of the state directory cannot be read") from exc
    if (
        not statmod.S_ISDIR(info.st_mode)
        or info.st_uid != gateway.uid()
        or statmod.S_IMODE(info.st_mode) != DIR_MODE
    ):
        raise FilesError("a directory of the state directory is not a private directory")


def _ensure_parent(gateway: Gateway, path: str) -> None:
    """``path`` exists as a private directory: created 0700 when absent, checked when present."""
    try:
        gateway.lstat(path)
    except FileNotFoundError:
        try:
            gateway.mkdir(path, DIR_MODE)
        except FileExistsError:
            pass  # a concurrent run made it: checked below like any existing one
        except OSError as exc:
            raise _disk(exc) from exc
    except OSError as exc:
        raise FilesError("a directory of the state directory cannot be read") from exc
    _check_private(gateway, path)


def _disk(exc: OSError) -> FilesError:
    full = exc.errno in (28, 122, 30)  # ENOSPC, EDQUOT, EROFS
    return FilesError("a file of the retro cannot be written", retryable=full)


def _write_file(gateway: Gateway, directory: str, name: str, data: bytes) -> None:
    """Write ``data`` to ``directory/name`` (0600): a temporary file, then an atomic replace."""
    final = os.path.join(directory, name)
    temporary = os.path.join(directory, f".{name}.tmp-{gateway.token_hex(8)}")
    handle: BinaryIO | None = None
    created = False
    try:
        try:
            gateway.lstat(temporary)
        except FileNotFoundError:
            pass
        else:
            raise FilesError("a temporary file of the retro already exists")
        handle = gateway.open_private(temporary, create=True, mode=FILE_MODE)
        created = True
        info = gateway.fstat(handle)
        if not statmod.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise FilesError("a temporary file of the retro is not a fresh regular file")
        view = memoryview(data)
        while view:
            view = view[handle.write(view) :]
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        gateway.replace(temporary, final)
        created = False
    except FilesError:
        raise
    except OSError as exc:
        raise _disk(exc) from exc
    finally:
        if handle is not None:
            handle.close()
        if created:
            _unlink(gateway, directory, os.path.basename(temporary))


def _unlink(gateway: Gateway, directory: str, name: str) -> None:
    try:
        handle = gateway.open_root(directory)
    except OSError:
        return
    try:
        handle.unlink(name.encode())
    except OSError:
        pass
    finally:
        handle.close()


def _make_directory(gateway: Gateway, parent: str, name: str, made: list[str]) -> str:
    path = os.path.join(parent, name)
    try:
        gateway.mkdir(path, DIR_MODE)
    except FileExistsError:
        raise FilesError("a directory of the retro already exists") from None
    except OSError as exc:
        raise _disk(exc) from exc
    made.append(path)
    return path


def _remove_made(gateway: Gateway, made: list[str]) -> None:
    for path in reversed(made):
        parent, name = os.path.dirname(path), os.path.basename(path)
        try:
            handle = gateway.open_root(parent)
        except OSError:
            continue
        try:
            remove_tree(gateway, handle, name.encode())
        except OSError:
            pass
        finally:
            handle.close()


def write_run_files(
    gateway: Gateway, state: str, retro_id: str, run_token: str, fenced: FencedDigest
) -> RunFiles:
    """Step 15's files. ``digest_sha256`` of the manifest equals the SHA-256 of the written digest
    bytes (checked here, an internal error otherwise). Raises ``FilesError`` after removing what
    this call created."""
    sha = hashlib.sha256(fenced.digest_bytes).hexdigest()
    if sha != fenced.digest_sha256 or fenced.manifest.get("digest_sha256") != sha:
        raise CliError("internal", "the digest hash does not match its manifest")
    made: list[str] = []
    try:
        for parent in (DIGESTS, PROMPTS, OUTCOMES):
            _ensure_parent(gateway, os.path.join(state, parent))
        digest_dir = _make_directory(gateway, os.path.join(state, DIGESTS), retro_id, made)
        prompt_dir = _make_directory(gateway, os.path.join(state, PROMPTS), retro_id, made)
        _write_file(gateway, digest_dir, "digest.md", fenced.digest_bytes)
        _write_file(gateway, digest_dir, "manifest.json", fenced.manifest_bytes)
        text = prompt_text(retro_id, run_token, fenced.digest_sha256)
        _write_file(gateway, prompt_dir, "prompt.md", text.encode("ascii"))
    except BaseException:
        _remove_made(gateway, made)
        raise
    return RunFiles(
        os.path.join(prompt_dir, "prompt.md"),
        os.path.join(digest_dir, "digest.md"),
        os.path.join(digest_dir, "manifest.json"),
        outcome_path(state, retro_id),
    )


def _open_parent(gateway: Gateway, state: str, parent: str) -> DirHandle | None:
    """``<state>/<parent>`` opened through the state directory with ``O_NOFOLLOW`` (a symlinked
    parent is never entered). None when it does not exist; ``OSError`` for a symlink or a
    non-directory."""
    try:
        root = gateway.open_root(state)
    except FileNotFoundError:
        return None
    try:
        return root.open_dir(parent.encode())
    except FileNotFoundError:
        return None
    finally:
        root.close()


def delete_local(gateway: Gateway, state: str, retro_id: str) -> bool:
    """Remove ``prompts/<retro_id>/`` and ``outcomes/<retro_id>.json``; True when both are gone
    (Collect and cleanup, ``delete_local``). Idempotent; never follows a symlink, a symlinked
    ``prompts`` or ``outcomes`` included (that is "not clean", nothing is deleted through it). The
    digest directory stays until its retention expires."""
    clean = True
    for parent, name in ((PROMPTS, retro_id), (OUTCOMES, f"{retro_id}.json")):
        try:
            handle = _open_parent(gateway, state, parent)
        except OSError:
            clean = False
            continue
        if handle is None:
            continue
        try:
            clean &= remove_tree(gateway, handle, name.encode())
        except OSError:
            clean = False
        finally:
            handle.close()
    return clean


def remove_digest(gateway: Gateway, state: str, retro_id: str) -> bool:
    """Remove ``digests/<retro_id>/`` (a retro that never ran has no use for its digest); a
    symlinked ``digests`` is not entered (False)."""
    try:
        handle = _open_parent(gateway, state, DIGESTS)
    except OSError:
        return False
    if handle is None:
        return True
    try:
        return remove_tree(gateway, handle, retro_id.encode())
    except OSError:
        return False
    finally:
        handle.close()
