"""Incremental reading of one transcript (issue #11): lines with their offsets, time budgets,
change detection and the progress record of the ``files`` table.

The reader takes a file that ``reader.Discovery.open`` returned (``Opened``: blocking, already
checked, ``stat`` taken from the descriptor) and the stored progress of its path (or None for a
file never seen). It yields the complete lines (bytes, LF included) with their start offsets; it
parses nothing and stores nothing (#12 parses, #13 stores events, #15 drives this module).

The caller commits progress only after the events of the same lines are stored, in one
transaction: ``checkpoint()`` returns the progress as of the lines yielded so far (call it
between two iterations, for example after every chunk of lines) and ``save_progress`` writes it.
Only hashes of the head and the tail are ever kept; no transcript content is stored or logged,
and every error text is fixed.

Rules (interface.md, Ingest rules 3 and 6):

- A line is read with ``readline(8 MiB + 1)``: up to 8 MiB of content plus its LF is a line. A
  longer record is skipped to its LF in 1 MiB steps and counted once in ``counts.oversized``, in
  the run that found it. Only LF-terminated lines are returned; a partial tail stays unconsumed
  for the next run, except a tail longer than 8 MiB, which sets ``skip_to_lf`` and moves the
  offset to the end of file (the next run discards through the next LF first).
- Budgets are checked between lines and between skip steps with the gateway's monotonic clock.
- The file is reread from the start when (dev, inode) changed, its size is below the offset, the
  head hash differs or the tail hash differs (and when forced, or when the stored rules version
  is lower than the current one). The head is the first ``min(4096, offset)`` bytes, rewritten as
  the offset grows while it is shorter than 4096; the tail is the 4096 bytes ending at the offset.
  A same-size in-place rewrite that touches neither window is not detectable (design limit).
"""

from __future__ import annotations

import enum
import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple

from .gateway import Gateway
from .reader import Candidate, Opened

LINE_CAP = 8 * 1024 * 1024  # content of a line, LF excluded
SKIP_STEP = 1024 * 1024
WINDOW = 4096  # head and tail hash windows
_LF = b"\n"


class Reason(enum.Enum):
    """Why the read starts where it does."""

    NEW = "new"  # no stored progress
    CONTINUE = "continue"  # unchanged: read from the stored offset
    IDENTITY = "identity"  # (dev, inode) changed
    SHRUNK = "shrunk"  # size below the stored offset
    HEAD = "head"  # head hash differs (or the stored head is inconsistent)
    TAIL = "tail"  # tail hash differs (or the stored tail is inconsistent)
    RULES = "rules"  # stored rules version is lower than the current one
    FORCED = "forced"  # the caller asked (--rebuild)

    @property
    def reread(self) -> bool:
        """True when the file is read again from the start and its old events must go."""
        return self not in (Reason.NEW, Reason.CONTINUE)


class Stop(enum.Enum):
    EOF = "eof"
    FILE_BUDGET = "file_budget"
    RUN_BUDGET = "run_budget"


class Line(NamedTuple):
    offset: int  # of the first byte of the line
    data: bytes  # the line, LF included


@dataclass(frozen=True)
class Progress:
    """The progress columns of a ``files`` row."""

    dev: int
    inode: int
    size: int
    offset: int
    head_len: int
    head_sha: str | None
    tail_sha: str | None
    rules_version: int
    skip_to_lf: bool


@dataclass
class Counts:
    lines: int = 0  # yielded
    oversized: int = 0  # records over 8 MiB found in this run (lines.oversized)
    bytes_read: int = 0  # consumed from the file: yielded, skipped or discarded


def run_deadline(gateway: Gateway, budget: timedelta) -> float:
    """The monotonic time at which a run with ``budget`` ends."""
    return gateway.monotonic() + budget.total_seconds()


def decode_line(line: bytes) -> str:
    """A line as text: explicit UTF-8 (never the locale); an invalid or cut sequence becomes U+FFFD
    and never stops ingest."""
    return line.decode("utf-8", errors="replace")


def signed_id(n: int) -> int:
    """An unsigned 64-bit id as SQLite's signed INTEGER holds it (st_dev and st_ino can be
    2**63 or more); consumers comparing listed ids to stored ids must apply it."""
    return n - 2**64 if n >= 2**63 else n


def _is_int(x: object) -> bool:
    return type(x) is int


def _well_typed(p: Progress) -> bool:
    """False for a stored row whose columns are not of the stored types (a tampered store)."""
    ints = (p.dev, p.inode, p.size, p.offset, p.head_len, p.rules_version)
    hashes = (p.head_sha, p.tail_sha)
    return (
        all(_is_int(n) for n in ints)
        and all(h is None or type(h) is str for h in hashes)
        and type(p.skip_to_lf) is bool
    )


def _sha(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


class IncrementalRead:
    """One run over one file; iterate it once. See the module docstring."""

    def __init__(
        self,
        opened: Opened,
        previous: Progress | None,
        *,
        gateway: Gateway,
        rules_version: int,
        file_budget: timedelta,
        run_deadline: float | None,
        force: bool = False,
    ) -> None:
        self._file = opened.file
        self._gateway = gateway
        self._info = opened.stat
        self._rules = rules_version
        self._file_budget = file_budget.total_seconds()
        self._run_deadline = run_deadline
        self._file_deadline = 0.0
        self.counts = Counts()
        self.stop: Stop | None = None
        self._started = False
        self._head = bytearray()
        self._tail = bytearray()
        self._skip = False
        self._pos = 0
        self.reason = self._detect(previous, force)

    @property
    def complete(self) -> bool:
        """True when the run ended at the end of the file (not at a budget)."""
        return self.stop is Stop.EOF

    # --- change detection ----------------------------------------------------------------------

    def _window(self, start: int, length: int) -> bytes:
        self._file.seek(start)
        return self._file.read(length)

    def _detect(self, prev: Progress | None, force: bool) -> Reason:
        if prev is None:
            return Reason.NEW
        if force:
            return Reason.FORCED
        if not _well_typed(prev):
            return Reason.HEAD  # an inconsistent stored row is reread, never trusted
        if prev.rules_version < self._rules:
            return Reason.RULES
        if (prev.dev, prev.inode) != (signed_id(self._info.st_dev), signed_id(self._info.st_ino)):
            return Reason.IDENTITY
        offset = prev.offset
        if offset < 0 or self._info.st_size < offset:
            return Reason.SHRUNK if offset >= 0 else Reason.HEAD
        if offset == 0:
            self._skip = prev.skip_to_lf
            return Reason.CONTINUE
        if prev.head_len != min(WINDOW, offset) or prev.head_sha is None:
            return Reason.HEAD
        head = self._window(0, prev.head_len)
        if len(head) != prev.head_len or _sha(head) != prev.head_sha:
            return Reason.HEAD
        tail_start = max(0, offset - WINDOW)
        tail = self._window(tail_start, offset - tail_start)
        if prev.tail_sha is None or len(tail) != offset - tail_start or _sha(tail) != prev.tail_sha:
            return Reason.TAIL
        self._head = bytearray(head)
        self._tail = bytearray(tail)
        self._pos = offset
        self._skip = prev.skip_to_lf
        return Reason.CONTINUE

    # --- progress ------------------------------------------------------------------------------

    def checkpoint(self) -> Progress:
        """The progress after everything consumed so far (the lines yielded, any record skipped
        and any pending skip). Call between two iterations."""
        return Progress(
            dev=signed_id(self._info.st_dev),
            inode=signed_id(self._info.st_ino),
            size=max(self._info.st_size, self._pos),
            offset=self._pos,
            head_len=len(self._head),
            head_sha=_sha(self._head) if self._head else None,
            tail_sha=_sha(self._tail) if self._tail else None,
            rules_version=self._rules,
            skip_to_lf=self._skip,
        )

    def _consume(self, data: bytes) -> None:
        self._pos += len(data)
        self.counts.bytes_read += len(data)
        if len(self._head) < WINDOW:
            self._head += data[: WINDOW - len(self._head)]
        if len(data) >= WINDOW:
            self._tail = bytearray(data[-WINDOW:])
        else:
            self._tail = bytearray((self._tail + data)[-WINDOW:])

    # --- budgets -------------------------------------------------------------------------------

    def _budget_hit(self) -> Stop | None:
        now = self._gateway.monotonic()
        if self._run_deadline is not None and now >= self._run_deadline:
            return Stop.RUN_BUDGET
        if now >= self._file_deadline:
            return Stop.FILE_BUDGET
        return None

    # --- reading -------------------------------------------------------------------------------

    def __iter__(self) -> Iterator[Line]:
        if self._started:
            return
        self._started = True
        self._file_deadline = self._gateway.monotonic() + self._file_budget
        self._file.seek(self._pos)
        if self._skip and not self._skip_record():
            return
        while True:
            hit = self._budget_hit()
            if hit is not None:
                self.stop = hit
                return
            data = self._file.readline(LINE_CAP + 1)
            if not data:
                self.stop = Stop.EOF
                return
            if data.endswith(_LF):
                start = self._pos
                self._consume(data)
                self.counts.lines += 1
                yield Line(start, data)
            elif len(data) > LINE_CAP:
                self.counts.oversized += 1
                self._consume(data)
                self._skip = True
                if not self._skip_record():
                    return
            else:  # a partial tail: left for the next run
                self.stop = Stop.EOF
                return

    def _skip_record(self) -> bool:
        """Discard through the next LF in steps, checking the budgets at each. True when the
        record ended (the read goes on); False when the run is over (``stop`` is set and
        ``skip_to_lf`` stays set)."""
        while True:
            hit = self._budget_hit()
            if hit is not None:
                self.stop = hit
                return False
            chunk = self._file.readline(SKIP_STEP)
            if not chunk:
                self.stop = Stop.EOF
                return False
            self._consume(chunk)
            if chunk.endswith(_LF):
                self._skip = False
                return True


# --- the files table ---------------------------------------------------------------------------

_COLUMNS = "dev, inode, size, offset, head_len, head_sha, tail_sha, rules_version, skip_to_lf"


def load_progress(conn: sqlite3.Connection, path: bytes) -> Progress | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM files WHERE path = ?", (path,)).fetchone()
    if row is None:
        return None
    return Progress(
        dev=row[0],
        inode=row[1],
        size=row[2],
        offset=row[3],
        head_len=row[4],
        head_sha=row[5],
        tail_sha=row[6],
        rules_version=row[7],
        # Exactly 0/1 is a flag; anything else stays raw so _well_typed rejects it.
        skip_to_lf=bool(row[8]) if type(row[8]) is int and row[8] in (0, 1) else row[8],
    )


def save_progress(conn: sqlite3.Connection, path: bytes, progress: Progress) -> None:
    """Write the progress columns of ``path`` (insert the row when it is new). Touches no other
    column; call inside the transaction that stores the events of the same lines."""
    conn.execute(
        f"INSERT INTO files (path, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET dev = excluded.dev, inode = excluded.inode, "
        "size = excluded.size, offset = excluded.offset, head_len = excluded.head_len, "
        "head_sha = excluded.head_sha, tail_sha = excluded.tail_sha, "
        "rules_version = excluded.rules_version, skip_to_lf = excluded.skip_to_lf",
        (
            path,
            progress.dev,
            progress.inode,
            progress.size,
            progress.offset,
            progress.head_len,
            progress.head_sha,
            progress.tail_sha,
            progress.rules_version,
            int(progress.skip_to_lf),
        ),
    )


def current_rules_version(conn: sqlite3.Connection) -> int:
    """``meta.rules_version``: the version the current code reads with."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'rules_version'").fetchone()
    return int(row[0]) if row is not None else 0


def stale_rules_paths(conn: sqlite3.Connection, rules_version: int) -> set[bytes]:
    """Paths of the files whose stored rules version is lower than ``rules_version``."""
    rows = conn.execute("SELECT path FROM files WHERE rules_version < ?", (rules_version,))
    return {bytes(r[0]) for r in rows}


def stale_first(candidates: Iterable[Candidate], stale: set[bytes]) -> list[Candidate]:
    """The candidates with the lower-rules files first (each part keeps its order), so a budget
    that runs out spends itself on the rereads before new bytes."""
    items = list(candidates)
    return [c for c in items if c.path in stale] + [c for c in items if c.path not in stale]


__all__ = [
    "LINE_CAP",
    "SKIP_STEP",
    "WINDOW",
    "Counts",
    "IncrementalRead",
    "Line",
    "Progress",
    "Reason",
    "Stop",
    "current_rules_version",
    "decode_line",
    "load_progress",
    "run_deadline",
    "save_progress",
    "stale_first",
    "stale_rules_paths",
]
