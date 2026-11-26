"""Transcript discovery and safe open (interface.md, Ingest rules 1 to 3 and 7).

``open_discovery`` opens the transcript roots and returns a ``Discovery``. Its two steps are
deliberately separate, so the caller (``ingest``) can check its budgets between files:

- ``Discovery.candidates()`` lists transcripts lazily, one project directory at a time (project
  directories by descending mtime, then the files of the project by descending mtime). It opens no
  transcript. A listed file is only a hint: it can change before it is opened.
- ``Discovery.open(candidate)`` opens one file from its root descriptor, directory by directory
  (``openat`` with ``O_NOFOLLOW``), checks the descriptor (regular file, one link, owned by the
  user) and only then clears ``O_NONBLOCK``. It returns ``Opened`` or a ``Skip`` reason that is
  already counted in ``Discovery.stats``. Self-exclusion and pruned sessions are decided here,
  from the store, at every open.

Everything that touches the file system goes through ``gateway.py``. File contents and raw path
text never enter a message: ``display_path`` is the one way to show a path.
"""

from __future__ import annotations

import enum
import errno
import json
import os
import re
import stat as statmod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol

from . import errors
from .gateway import DirHandle, Gateway
from .textsafe import fix_surrogates, sanitize

if TYPE_CHECKING:
    import sqlite3

    from .command import Context

SCAN_CAP = 100_000  # directory entries listed per root per run
META_CAP = 64 * 1024  # bytes read of agent-<id>.meta.json
NAME_CAP = 128  # agent_type and tool-use id are stored at most this long
DISPLAY_CAP = 4096

_UUID = rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_MAIN_FILE = re.compile(rb"(" + _UUID + rb")\.jsonl")
_UUID_NAME = re.compile(_UUID)
_AGENT_FILE = re.compile(rb"agent-([A-Za-z0-9_.-]{1,128})\.jsonl")
_WORKFLOW_NAME = re.compile(rb"[A-Za-z0-9_.-]{1,128}")
_DOTS = (b".", b"..")

Kind = Literal["main", "subagent", "workflow"]


class Skip(enum.Enum):
    """Why ``Discovery.open`` returned no file. Each reason has a counter in ``ScanStats``."""

    UNSAFE = "unsafe"  # not a regular file, more than one link, wrong owner, a symlink
    EXCLUDED = "excluded"  # a retro session's own project directory
    VANISHED = "vanished"  # gone between the listing and the open
    PRUNED = "pruned"  # a pruned session's file that has not changed since


@dataclass(frozen=True)
class Candidate:
    """A transcript as listed. ``path`` is raw bytes (the key of the ``files`` table);
    ``rel`` are its components below the root. ``session_id`` is the lowercase UUID the path
    names (an agent's parent session, taken from its directory), ``agent_id`` the ``<id>`` of
    ``agent-<id>.jsonl`` and ``workflow`` the ``<wf>`` directory, both None where they do not
    apply."""

    root_index: int
    rel: tuple[bytes, ...]
    path: bytes
    kind: Kind
    session_id: str
    agent_id: str | None
    workflow: str | None
    project: bytes
    size: int
    mtime_ns: int
    dev: int
    ino: int

    @property
    def project_dir(self) -> str:
        """The project directory name as text, for the store (``sessions.project_dir``) and the
        ``excluded`` rule. A name that is not UTF-8 keeps its undecodable bytes as lone
        surrogates here: use ``fix_surrogates`` before it enters the store or any output."""
        return os.fsdecode(self.project)

    @property
    def parent_session_id(self) -> str | None:
        """The session an agent belongs to (None for a main file)."""
        return None if self.kind == "main" else self.session_id


@dataclass
class Opened:
    """A transcript opened safely: ``file`` is blocking, ``stat`` is its ``fstat`` (size, mtime,
    device and inode as they are now, not as they were listed)."""

    candidate: Candidate
    file: BinaryIO
    stat: os.stat_result

    def close(self) -> None:
        self.file.close()


@dataclass(frozen=True)
class AgentMeta:
    """What ``agent-<id>.meta.json`` says; a field that is missing or not a string is None."""

    agent_type: str | None
    tool_use_id: str | None


@dataclass
class ScanStats:
    """Counters of one run. ``seen`` is every file whose name is a transcript name, whatever
    became of it. ``unsafe``, ``excluded`` and ``vanished`` feed the ingest counts of the same
    names; ``pruned`` and ``unreadable_dirs`` have no ingest count of their own."""

    seen: int = 0
    unsafe: int = 0
    excluded: int = 0
    vanished: int = 0
    pruned: int = 0
    unreadable_dirs: int = 0
    capped_roots: list[int] = field(default_factory=list)


@dataclass
class Root:
    """One configured root. ``reason`` is None for a usable root, else why it is not:
    ``missing``, ``not_directory``, ``unreadable``, ``not_owned``, ``writable`` or ``duplicate``
    (the same directory as an earlier root)."""

    configured: str
    handle: DirHandle | None
    reason: str | None = None
    identity: tuple[int, int] | None = None

    @property
    def usable(self) -> bool:
        return self.handle is not None and self.reason is None


@dataclass(frozen=True)
class PathFilter:
    """A ``--path`` resolved to components below one root."""

    root_index: int
    rel: tuple[bytes, ...]
    empty: bool = False  # the path does not exist or goes through a file: it selects nothing


def display_path(path: bytes) -> str:
    """A raw path shown to a person: lone surrogates fixed, then sanitized and bounded."""
    return sanitize(fix_surrogates(os.fsdecode(path)), DISPLAY_CAP)


def _no_readable_roots() -> errors.CliError:
    return errors.CliError(
        "failed",
        "no transcript root is readable",
        details={"reason": "no_readable_roots", "retryable": False},
        hint="Check transcript_roots in the config and the owner and mode of each root; a root "
        "must not be group or other writable (chmod g-w <root>).",
    )


def _outside(field_name: str = "--path") -> errors.CliError:
    return errors.usage(
        "the path is not below a transcript root",
        field=field_name,
        hint="Give an absolute path of a directory or transcript under a configured root, "
        "without .. or symlinked components.",
    )


def _open_root(gateway: Gateway, configured: str, seen: set[tuple[int, int]]) -> Root:
    try:
        handle = gateway.open_root(configured)
    except FileNotFoundError:
        return Root(configured, None, "missing")
    except NotADirectoryError:
        return Root(configured, None, "not_directory")
    except OSError:
        return Root(configured, None, "unreadable")
    try:
        info = handle.stat()
        identity = (info.st_dev, info.st_ino)
        reason: str | None = None
        if not statmod.S_ISDIR(info.st_mode):
            reason = "not_directory"
        elif info.st_uid != gateway.uid():
            reason = "not_owned"
        elif info.st_mode & 0o022:
            reason = "writable"
        elif identity in seen:
            reason = "duplicate"
    except BaseException:
        handle.close()
        raise
    if reason is not None:
        handle.close()
        return Root(configured, None, reason, identity)
    seen.add(identity)
    return Root(configured, handle, None, identity)


def _classify(
    rel: tuple[bytes, ...],
) -> tuple[Kind, bytes, bytes | None, bytes | None] | None:
    """``(kind, session, agent, workflow)`` of the components below a root when they name a
    transcript by Ingest rules 2 (the same patterns the listing uses), else None."""
    if not rel[0] or rel[0] in _DOTS:
        return None
    if len(rel) == 2:
        main = _MAIN_FILE.fullmatch(rel[1])
        return ("main", main.group(1), None, None) if main is not None else None
    if len(rel) not in (4, 6) or not _UUID_NAME.fullmatch(rel[1]) or rel[2] != b"subagents":
        return None
    if len(rel) == 4:
        agent = _AGENT_FILE.fullmatch(rel[3])
        return ("subagent", rel[1], agent.group(1), None) if agent is not None else None
    if rel[3] != b"workflows" or not _WORKFLOW_NAME.fullmatch(rel[4]) or rel[4] in _DOTS:
        return None
    agent = _AGENT_FILE.fullmatch(rel[5])
    return ("workflow", rel[1], agent.group(1), rel[4]) if agent is not None else None


def _components(path: bytes) -> list[bytes]:
    return [part for part in path.split(b"/") if part and part != b"."]


class Discovery:
    """The roots of one run, open. Use as a context manager (or call ``close``)."""

    def __init__(
        self,
        gateway: Gateway,
        roots: list[Root],
        filters: list[PathFilter],
        *,
        excluded: Callable[[str], bool],
        pruned_at: Callable[[str], int | None],
        scan_cap: int = SCAN_CAP,
    ) -> None:
        self.gateway = gateway
        self.roots = roots
        self.filters = filters
        self.stats = ScanStats()
        self._excluded = excluded
        self._pruned_at = pruned_at
        self._cap = scan_cap

    def __enter__(self) -> Discovery:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for root in self.roots:
            if root.handle is not None:
                root.handle.close()

    @property
    def unusable_roots(self) -> list[tuple[str, str]]:
        """``(configured root, reason)`` of every root that cannot be used, in config order. A
        duplicate of an earlier root is not a problem and is left out."""
        return [
            (r.configured, r.reason)
            for r in self.roots
            if r.reason is not None and r.reason != "duplicate"
        ]

    @property
    def warnings(self) -> list[str]:
        """Fixed tokens for the run's ``warnings`` (``scan_capped``, ``root_unusable``,
        ``dirs_unreadable``); the caller may add the root's display text."""
        found: list[str] = []
        if self.unusable_roots:
            found.append("root_unusable")
        if self.stats.capped_roots:
            found.append("scan_capped")
        if self.stats.unreadable_dirs:
            found.append("dirs_unreadable")
        return found

    # --- listing -------------------------------------------------------------------------------

    def candidates(self) -> Iterator[Candidate]:
        """Every transcript below the usable roots, lazily; see the module text for the order."""
        for index, root in enumerate(self.roots):
            if root.handle is not None and root.reason is None:
                yield from self._scan_root(index, root.handle)

    def _wanted(self, root_index: int, rel: tuple[bytes, ...]) -> bool:
        """Whether ``rel`` is on the way to, or inside, a ``--path`` (all of the root without
        one), compared by whole components."""
        if not self.filters:
            return True
        for each in self.filters:
            if each.root_index == root_index and not each.empty:
                n = min(len(rel), len(each.rel))
                if rel[:n] == each.rel[:n]:
                    return True
        return False

    def _list(self, index: int, handle: DirHandle, left: list[int]) -> list[bytes]:
        """The entries of ``handle`` within the root's remaining allowance (``left[0]``)."""
        try:
            names, more = handle.entries(max(left[0], 0))
        except OSError:
            self.stats.unreadable_dirs += 1
            return []
        left[0] -= len(names)
        if more and index not in self.stats.capped_roots:
            self.stats.capped_roots.append(index)
        return names

    def _lstat(self, handle: DirHandle, name: bytes) -> os.stat_result | None:
        try:
            return handle.lstat(name)
        except FileNotFoundError:
            return None
        except OSError:
            self.stats.unreadable_dirs += 1
            return None

    def _open_dir(self, handle: DirHandle, name: bytes) -> DirHandle | None:
        """A real directory (not a symlink, checked by ``lstat`` first) or None."""
        info = self._lstat(handle, name)
        if info is None:
            return None
        if statmod.S_ISLNK(info.st_mode):
            self.stats.unsafe += 1
            return None
        if not statmod.S_ISDIR(info.st_mode):
            return None
        try:
            return handle.open_dir(name)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                self.stats.unsafe += 1
            else:
                self.stats.unreadable_dirs += 1
            return None

    def _scan_root(self, index: int, root: DirHandle) -> Iterator[Candidate]:
        left = [self._cap]
        projects: list[tuple[int, bytes]] = []
        for name in self._list(index, root, left):
            if not self._wanted(index, (name,)):
                continue
            info = self._lstat(root, name)
            if info is None:
                continue
            if statmod.S_ISLNK(info.st_mode):
                self.stats.unsafe += 1  # a project directory that is a symlink is never entered
            elif statmod.S_ISDIR(info.st_mode):
                projects.append((info.st_mtime_ns, name))
        projects.sort(key=lambda item: (-item[0], item[1]))
        base = os.fsencode(self.roots[index].configured).rstrip(b"/")
        for _, project in projects:
            found = self._scan_project(index, root, base, project, left)
            found.sort(key=lambda c: (-c.mtime_ns, c.rel))
            yield from found

    def _scan_project(
        self, index: int, root: DirHandle, base: bytes, project: bytes, left: list[int]
    ) -> list[Candidate]:
        found: list[Candidate] = []
        handle = self._open_dir(root, project)
        if handle is None:
            return found
        try:
            for name in self._list(index, handle, left):
                main = _MAIN_FILE.fullmatch(name)
                if main is not None:
                    if self._wanted(index, (project, name)):
                        rel = (project, name)
                        self._add(found, index, base, rel, "main", main.group(1), handle)
                elif _UUID_NAME.fullmatch(name) and self._wanted(index, (project, name)):
                    self._scan_session_dir(index, base, project, name, handle, left, found)
        finally:
            handle.close()
        return found

    def _scan_session_dir(
        self,
        index: int,
        base: bytes,
        project: bytes,
        uuid: bytes,
        parent: DirHandle,
        left: list[int],
        found: list[Candidate],
    ) -> None:
        """Only ``<uuid>/subagents`` is looked at (one ``lstat``, no listing of the session
        directory), so tool-results, memory and any other name or depth are never read."""
        session = self._open_dir(parent, uuid)
        if session is None:
            return
        try:
            rel = (project, uuid, b"subagents")
            if not self._wanted(index, rel):
                return
            agents = self._open_dir(session, b"subagents")
            if agents is None:
                return
            try:
                for name in self._list(index, agents, left):
                    if name == b"workflows":
                        self._scan_workflows(index, base, rel, agents, left, found)
                        continue
                    agent = _AGENT_FILE.fullmatch(name)
                    if agent is not None and self._wanted(index, (*rel, name)):
                        self._add(
                            found, index, base, (*rel, name), "subagent", uuid, agents, agent, None
                        )
            finally:
                agents.close()
        finally:
            session.close()

    def _scan_workflows(
        self,
        index: int,
        base: bytes,
        rel: tuple[bytes, ...],
        agents: DirHandle,
        left: list[int],
        found: list[Candidate],
    ) -> None:
        rel = (*rel, b"workflows")
        if not self._wanted(index, rel):
            return
        workflows = self._open_dir(agents, b"workflows")
        if workflows is None:
            return
        try:
            for wf in self._list(index, workflows, left):
                if wf in _DOTS or not _WORKFLOW_NAME.fullmatch(wf):
                    continue
                if not self._wanted(index, (*rel, wf)):
                    continue
                inner = self._open_dir(workflows, wf)
                if inner is None:
                    continue
                try:
                    for name in self._list(index, inner, left):
                        agent = _AGENT_FILE.fullmatch(name)
                        if agent is not None and self._wanted(index, (*rel, wf, name)):
                            self._add(
                                found,
                                index,
                                base,
                                (*rel, wf, name),
                                "workflow",
                                rel[1],
                                inner,
                                agent,
                                wf,
                            )
                finally:
                    inner.close()
        finally:
            workflows.close()

    def _add(
        self,
        found: list[Candidate],
        index: int,
        base: bytes,
        rel: tuple[bytes, ...],
        kind: Kind,
        session: bytes,
        handle: DirHandle,
        agent: re.Match[bytes] | None = None,
        workflow: bytes | None = None,
    ) -> None:
        """Add the file ``rel[-1]`` of ``handle`` unless its ``lstat`` already says it is unsafe
        (a symlink, a FIFO, a socket, a device, a hard link, another owner)."""
        self.stats.seen += 1
        info = self._lstat(handle, rel[-1])
        if info is None:
            self.stats.vanished += 1
            return
        if not self._safe(info):
            self.stats.unsafe += 1
            return
        agent_id = agent.group(1).decode("ascii") if agent is not None else None
        found.append(
            Candidate(
                root_index=index,
                rel=rel,
                path=base + b"/" + b"/".join(rel),
                kind=kind,
                session_id=session.decode("ascii"),
                agent_id=agent_id,
                workflow=workflow.decode("ascii") if workflow is not None else None,
                project=rel[0],
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                dev=info.st_dev,
                ino=info.st_ino,
            )
        )

    def _safe(self, info: os.stat_result) -> bool:
        return (
            statmod.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == self.gateway.uid()
        )

    def candidate_for(self, path: bytes) -> Candidate | None:
        """The ``Candidate`` a stored path (``files.path``) names, or None when it is not below a
        usable root or its name is not a transcript name of rule 2. No file is touched: the result
        is only as good as the names, so open it with ``open`` (which walks it safely). ``size``,
        ``mtime_ns``, ``dev`` and ``ino`` are 0 (a listing's facts are only hints)."""
        for index, root in enumerate(self.roots):
            if root.handle is None or root.reason is not None:
                continue
            base = os.fsencode(root.configured).rstrip(b"/") + b"/"
            if not path.startswith(base):
                continue
            rel = tuple(path[len(base) :].split(b"/"))
            found = _classify(rel)
            if found is None:
                continue
            kind, session, agent, workflow = found
            return Candidate(
                root_index=index,
                rel=rel,
                path=path,
                kind=kind,
                session_id=session.decode("ascii"),
                agent_id=agent.decode("ascii") if agent is not None else None,
                workflow=workflow.decode("ascii") if workflow is not None else None,
                project=rel[0],
                size=0,
                mtime_ns=0,
                dev=0,
                ino=0,
            )
        return None

    # --- opening -------------------------------------------------------------------------------

    def open(self, candidate: Candidate) -> Opened | Skip:
        """Open one listed transcript. The self-exclusion table is read now, so a retro session
        recorded after the listing is still excluded; a file that is excluded is never opened."""
        if self._excluded(candidate.project_dir):
            self.stats.excluded += 1
            return Skip.EXCLUDED
        handle = self.roots[candidate.root_index].handle
        if handle is None:
            self.stats.unsafe += 1
            return Skip.UNSAFE
        found = self._open_regular(handle, candidate.rel)
        if isinstance(found, Skip):
            self._count(found)
            return found
        file, info = found
        try:
            pruned = self._pruned_at(candidate.session_id)
        except BaseException:
            file.close()
            raise
        if pruned is not None and info.st_mtime_ns // 1_000_000 <= pruned:
            file.close()
            self.stats.pruned += 1
            return Skip.PRUNED
        try:
            self.gateway.set_blocking(file)
        except BaseException:
            file.close()
            raise
        return Opened(candidate, file, info)

    def settled(self, candidate: Candidate) -> bool:
        """True for a listed file ``open`` would skip as excluded or pruned (judged by the listing
        alone, nothing is counted): it is never part of what a budget stop leaves remaining."""
        if self._excluded(candidate.project_dir):
            return True
        pruned = self._pruned_at(candidate.session_id)
        return pruned is not None and candidate.mtime_ns // 1_000_000 <= pruned

    def _count(self, reason: Skip) -> None:
        if reason is Skip.VANISHED:
            self.stats.vanished += 1
        else:
            self.stats.unsafe += 1

    def _open_regular(
        self, root: DirHandle, rel: tuple[bytes, ...]
    ) -> tuple[BinaryIO, os.stat_result] | Skip:
        """Walk ``rel`` from ``root`` with ``openat`` and check what was opened. A directory
        or file that is a symlink, or any open that fails, is a skip, never an error. ENOENT is
        ``VANISHED``; every other failure (ELOOP, ENOTDIR, ENXIO, EACCES, EMFILE, ...) is
        ``UNSAFE``, the conservative reading (interface.md, Ingest rules 3)."""
        held: list[DirHandle] = []
        try:
            current = root
            for name in rel[:-1]:
                current = current.open_dir(name)
                held.append(current)
            file = current.open_file(rel[-1])
        except FileNotFoundError:
            return Skip.VANISHED
        except OSError:
            return Skip.UNSAFE  # ELOOP, ENOTDIR, ENXIO, EISDIR, EACCES, ...
        finally:
            for each in held:
                each.close()
        try:
            info = self.gateway.fstat(file)
            if not self._safe(info):
                file.close()
                return Skip.UNSAFE
        except BaseException:
            file.close()
            raise
        return file, info

    def read_meta(self, candidate: Candidate) -> AgentMeta | None:
        """The ``agent-<id>.meta.json`` beside an agent file, read with a 64 KiB cap through the
        same safe open. A missing, unsafe, oversized, empty or malformed file gives None."""
        if candidate.agent_id is None:
            return None
        name = f"agent-{candidate.agent_id}.meta.json".encode("ascii")
        handle = self.roots[candidate.root_index].handle
        if handle is None:
            return None
        found = self._open_regular(handle, (*candidate.rel[:-1], name))
        if isinstance(found, Skip):
            if found is Skip.UNSAFE:  # a missing meta file is normal and not counted
                self._count(found)
            return None
        file, _ = found
        try:
            self.gateway.set_blocking(file)
            data = file.read(META_CAP + 1)
        except OSError:
            return None
        finally:
            file.close()
        return parse_meta(data)


def parse_meta(data: bytes) -> AgentMeta | None:
    """``agentType`` and ``toolUseId`` of a meta file's bytes; None when it is oversized (more
    than 64 KiB), empty or not a JSON object. Strings are cleaned of lone surrogates and cut at
    128 characters."""
    if not data or len(data) > META_CAP:
        return None
    try:
        document = json.loads(data)
    except (ValueError, RecursionError, UnicodeError):
        return None
    if not isinstance(document, dict):
        return None

    def text(key: str) -> str | None:
        value = document.get(key)
        if not isinstance(value, str) or not value:
            return None
        return fix_surrogates(value)[:NAME_CAP]

    return AgentMeta(text("agentType"), text("toolUseId"))


# --- the store's side of the rules ---------------------------------------------------------------


class _HasConn(Protocol):
    """A ``Store``, or any read-only view of one (a dry run never opens the store for writing)."""

    @property
    def conn(self) -> sqlite3.Connection: ...


def store_excluded(store: _HasConn) -> Callable[[str], bool]:
    """Ingest rules 7: the ``excluded(project_dir_name)`` rule, reading ``retro_sessions`` on
    every call (never a set loaded earlier)."""

    def excluded(project_dir_name: str) -> bool:
        try:
            project_dir_name.encode("utf-8")
        except UnicodeEncodeError:
            return False  # a retro session's directory name is recorded as text: this is not it
        row = store.conn.execute(
            "SELECT 1 FROM retro_sessions WHERE project_dir = ? LIMIT 1", (project_dir_name,)
        ).fetchone()
        return row is not None

    return excluded


def store_pruned_at(store: _HasConn) -> Callable[[str], int | None]:
    """The prune time (epoch milliseconds) of a pruned session, else None."""

    def pruned_at(session_id: str) -> int | None:
        row = store.conn.execute(
            "SELECT pruned_at FROM pruned_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    return pruned_at


# --- opening a run's roots -------------------------------------------------------------------


def resolve_path_filters(
    gateway: Gateway, roots: list[Root], paths: Sequence[str]
) -> list[PathFilter]:
    """Resolve each ``--path`` by components below a configured root, after the root was opened.

    The path must be absolute and start with the components of a configured root (or of its real
    path), compared component by component and never as a string prefix. Below the root every
    component is checked on the descriptor with ``O_NOFOLLOW``; a ``..`` anywhere, a symlinked
    component or a path above or beside every root exits 2 naming ``--path``. A component that
    does not exist is not an error: the filter simply matches nothing."""
    filters: list[PathFilter] = []
    for text in paths:
        raw = os.fsencode(text)
        if not raw.startswith(b"/"):
            raise _outside()
        parts = [p for p in raw.split(b"/") if p and p != b"."]
        if b".." in parts:
            raise _outside()
        match = _match_root(roots, parts)
        if match is None:
            raise _outside()
        index, rel = match
        handle = roots[index].handle
        assert handle is not None
        exists = _check_below(handle, rel)
        filters.append(PathFilter(index, tuple(rel), empty=not exists))
    return filters


def _match_root(roots: list[Root], parts: list[bytes]) -> tuple[int, list[bytes]] | None:
    """The root a path is below, by whole components: the configured spelling or the real path.
    A duplicate root's spelling maps to the root that was kept for the same directory."""
    best: tuple[int, list[bytes]] | None = None
    best_len = -1
    for root in roots:
        target = root
        if root.reason == "duplicate":
            target = next((r for r in roots if r.usable and r.identity == root.identity), root)
        if target.handle is None or not target.usable:
            continue
        index = roots.index(target)
        for candidate in (os.fsencode(root.configured), target.handle.path):
            base = _components(candidate)
            if parts[: len(base)] == base and len(base) > best_len:
                best, best_len = (index, parts[len(base) :]), len(base)
    return best


def _check_below(root: DirHandle, rel: list[bytes]) -> bool:
    """Check every component of ``rel`` below ``root`` without following a symlink. True when the
    whole path exists; False when a component is missing, too long, not a name the file system
    takes, or has something that is not a directory before the last component (the path then
    selects nothing). A symlinked component exits 2."""
    held: list[DirHandle] = []
    try:
        current = root
        for position, name in enumerate(rel):
            try:
                info = current.lstat(name)
            except (FileNotFoundError, ValueError):
                return False
            except OSError as exc:
                if exc.errno == errno.ENAMETOOLONG:
                    return False
                raise _outside() from None
            if statmod.S_ISLNK(info.st_mode):
                raise _outside()
            if position < len(rel) - 1:
                if not statmod.S_ISDIR(info.st_mode):
                    return False
                try:
                    current = current.open_dir(name)
                except FileNotFoundError:
                    return False
                except OSError:
                    raise _outside() from None
                held.append(current)
        return True
    finally:
        for each in held:
            each.close()


def open_discovery(
    gateway: Gateway,
    roots: Sequence[str],
    *,
    excluded: Callable[[str], bool],
    pruned_at: Callable[[str], int | None],
    paths: Sequence[str] = (),
    scan_cap: int = SCAN_CAP,
) -> Discovery:
    """Open the roots (each checked once: a real directory, owned by the user, not group or other
    writable; a root that fails is reported in ``unusable_roots`` and the others continue) and
    resolve ``paths``. Raises ``CliError`` exit 12 ``no_readable_roots`` when no root is usable
    and exit 2 for a ``--path`` outside every root."""
    seen: set[tuple[int, int]] = set()
    opened: list[Root] = []
    try:
        for root in roots:
            opened.append(_open_root(gateway, root, seen))
        if not any(root.usable for root in opened):
            raise _no_readable_roots()
        filters = resolve_path_filters(gateway, opened, paths)
    except BaseException:
        for root in opened:
            if root.handle is not None:
                root.handle.close()
        raise
    return Discovery(
        gateway, opened, filters, excluded=excluded, pruned_at=pruned_at, scan_cap=scan_cap
    )


def discover(ctx: Context, paths: Sequence[str] = ()) -> Discovery:
    """``open_discovery`` for a command: the configured roots, the store's exclusion and prune
    rules. The caller closes the result."""
    store = ctx.store()
    return open_discovery(
        ctx.gateway,
        ctx.settings.transcript_roots,
        excluded=store_excluded(store),
        pruned_at=store_pruned_at(store),
        paths=paths,
    )
