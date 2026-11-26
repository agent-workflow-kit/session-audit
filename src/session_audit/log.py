"""The ``log.jsonl`` writer (interface.md, Log; ``schemas/log.json``).

Hash-only and closed: an entry is validated against models that mirror ``schemas/log.json``
(``tests/test_log.py`` checks that the writer accepts a strict subset of what the schema accepts:
it refuses everything the schema refuses, and also the documents the schema allows but the
writer never produces, such as a count of ``1.0``), so a field that could hold transcript or
model text cannot be written. An entry that does not validate is a bug
in the caller and raises ``ValueError``; the log is never half-written.

An append takes ``log.lock`` (an exclusive ``flock``, waited for at most ``LOCK_TIMEOUT`` seconds)
and writes the whole line with one write to a file opened for append, so concurrent writers never
interleave. A last line that lacks its newline (a crash mid-write) is ended first, so the damage
stays on that one line. ``ts`` is the gateway clock in milliseconds UTC.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat as statmod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from . import errors
from .gateway import Gateway
from .textsafe import dumps_json

LOG_NAME = "log.jsonl"
LOCK_NAME = "log.lock"
LOCK_TIMEOUT = 5.0
TMP_NAME = "log.jsonl.tmp"
MAX_LINE = 1 << 20  # a longer line is never an entry of ours: kept as it is, undated
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")

_RESERVED = frozenset({"ts", "actor", "action"})
_Count = Annotated[int, Field(strict=True, ge=0)]
_Actor = Annotated[str, Field(pattern=r"^(human|auto):[A-Za-z0-9._@/-]{1,64}$", max_length=70)]
_Timestamp = Annotated[
    str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_RetroId = Annotated[str, Field(pattern=r"^ra-[a-z2-7]{8}$")]
_CsessionId = Annotated[str, Field(pattern=r"^cs-[a-z2-7]{8}$")]
_DecisionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/@-]{1,128}$")]
_RetroState = Literal[
    "digesting", "running", "validating", "done", "failed", "escalated", "canceled"
]
_RetroReason = Literal[
    "runner_lost",
    "session_not_started",
    "claude_session_error",
    "session_failed",
    "no_artifact",
    "artifact_invalid",
    "identity_mismatch",
    "marker_mismatch",
    "no_valid_findings",
    "hook_escalated",
    "session_blocked",
    "session_needs_input",
    "interrupted",
    "hook_vetoed",
]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IngestFiles(_Closed):
    seen: _Count
    new: _Count
    changed: _Count
    unchanged: _Count
    unsafe: _Count
    excluded: _Count
    vanished: _Count
    rewritten: _Count
    remaining: _Count


class IngestLines(_Closed):
    parsed: _Count
    errors: _Count
    oversized: _Count
    unknown: _Count


class IngestSessions(_Closed):
    new: _Count
    updated: _Count


class PruneTables(_Closed):
    sessions: _Count
    agents: _Count
    events: _Count
    files: _Count
    harness_runs: _Count
    retros: _Count
    digests: _Count
    log_entries: _Count
    retro_starts: _Count
    retro_sessions: _Count
    pruned_sessions: _Count


class IngestEntry(_Closed):
    ts: _Timestamp
    actor: _Actor
    action: Literal["ingest"]
    complete: bool
    files: IngestFiles
    lines: IngestLines
    sessions: IngestSessions


class RetroStateEntry(_Closed):
    ts: _Timestamp
    actor: _Actor
    action: Literal["retro_state"]
    retro_id: _RetroId
    state: _RetroState
    reason: _RetroReason | None
    key: _Sha256 | None
    digest_sha256: _Sha256 | None
    again_of: _RetroId | None
    csession_id: _CsessionId | None
    decision_ids: Annotated[list[_DecisionId], Field(max_length=32)]
    hook_output_ignored: _Count
    findings: Annotated[int, Field(strict=True, ge=0, le=50)] | None
    candidates: Annotated[int, Field(strict=True, ge=0, le=20)] | None


class RetroDeleteEntry(_Closed):
    ts: _Timestamp
    actor: _Actor
    action: Literal["retro_delete"]
    retro_id: _RetroId
    key: _Sha256 | None


class PruneEntry(_Closed):
    ts: _Timestamp
    actor: _Actor
    action: Literal["prune"]
    tables: PruneTables
    recovered: _Count


_Entry = Annotated[
    IngestEntry | RetroStateEntry | RetroDeleteEntry | PruneEntry, Field(discriminator="action")
]
_ENTRY: TypeAdapter[Any] = TypeAdapter(_Entry)


def timestamp_ms(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC, truncated (not rounded) to the millisecond."""
    if moment.tzinfo is None:
        raise ValueError("the log clock must be timezone-aware")
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def render(entry: Mapping[str, Any]) -> str:
    """The validated entry as one JSON line (no newline). Raises ``ValueError`` when the entry is
    not one of the four closed shapes."""
    try:
        model = _ENTRY.validate_python(dict(entry))
    except ValidationError as exc:  # keys and error types only, never the values
        kinds = sorted({".".join(str(p) for p in e["loc"]) + ":" + e["type"] for e in exc.errors()})
        raise ValueError(f"log entry rejected ({'; '.join(kinds)})") from None
    return dumps_json(model.model_dump(mode="json"))


def _unwritable(hint: str, *, retryable: bool = False) -> errors.CliError:
    return errors.CliError(
        "failed",
        "the log cannot be written",
        details={"reason": "store_unwritable", "retryable": retryable},
        hint=hint,
    )


class LogWriter:
    """Appends entries to ``<directory>/log.jsonl``; the directory has been checked by the store."""

    def __init__(self, gateway: Gateway, directory: str) -> None:
        self._gateway = gateway
        self._directory = directory

    def append(self, actor: str, action: str, **fields: Any) -> str:
        """Validate and append one entry, and return the line written. ``ts``, ``actor`` and
        ``action`` are set here and cannot be given in ``fields`` (a ``ValueError``)."""
        reserved = sorted(_RESERVED & fields.keys())
        if reserved:
            raise ValueError(f"log entry fields cannot set {', '.join(reserved)}")
        entry = {"ts": timestamp_ms(self._gateway.now()), "actor": actor, "action": action}
        entry.update(fields)
        line = render(entry) + "\n"
        data = line.encode("ascii")  # dumps_json is ASCII-only
        try:
            self._write(data)
        except OSError:
            raise _unwritable("log.jsonl or log.lock cannot be opened or written") from None
        return line

    def _open(self, name: str, *, append: bool) -> Any:
        path = f"{self._directory}/{name}"
        handle = self._gateway.open_private(path, create=True, append=append)
        try:
            info = os.fstat(handle.fileno())
            if (
                not statmod.S_ISREG(info.st_mode)
                or info.st_uid != self._gateway.uid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise _unwritable(f"{name} is not a private regular file of this user")
        except BaseException:
            handle.close()
            raise
        return handle

    def _write(self, data: bytes) -> None:
        with self._gateway.umask(0o077):
            lock = self._open(LOCK_NAME, append=False)
            try:
                if not self._gateway.lock_exclusive(lock, LOCK_TIMEOUT):
                    raise _unwritable("log.lock is held by another process", retryable=True)
                log = self._open(LOG_NAME, append=True)
                try:
                    self._write_all(log, self._torn_prefix(log) + data)
                finally:
                    log.close()
            finally:
                lock.close()

    @staticmethod
    def _torn_prefix(log: Any) -> bytes:
        """A newline when the file does not end with one, so the new entry starts its own line."""
        size = os.fstat(log.fileno()).st_size
        if size and os.pread(log.fileno(), 1, size - 1) != b"\n":
            return b"\n"
        return b""

    @staticmethod
    def _write_all(handle: Any, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = handle.write(view)
            if not written:
                raise OSError("short write")
            view = view[written:]

    # --- the prune rewrite -----------------------------------------------------------------------

    def count_before(self, cutoff: datetime) -> int:
        """How many entries a ``prune_before(cutoff)`` would remove (reads only: no lock, nothing
        written, no file created)."""
        limit = timestamp_ms(cutoff)
        try:
            log = self._gateway.open_private(
                f"{self._directory}/{LOG_NAME}", create=False, read_only=True
            )
        except FileNotFoundError:
            return 0
        except OSError:
            raise _unwritable("log.jsonl cannot be opened") from None
        try:
            return sum(1 for _, old in self._lines(log, limit) if old)
        except OSError:
            raise _unwritable("log.jsonl cannot be read") from None
        finally:
            log.close()

    def prune_before(self, cutoff: datetime) -> int:
        """Remove the entries older than ``cutoff`` (a ``ts`` below it) and return how many. Under
        ``log.lock``, so an append waits for the rewrite and never lands in the old file: the kept
        lines go to ``log.jsonl.tmp`` (mode 0600), which is synced and renamed over the log. A line
        that is not an entry with a ``ts`` (a torn line, anything undated) is kept byte for byte,
        so is a last line without its newline, and every entry at or after the cutoff stays. A
        crash leaves the old log or the new one; a stale temporary file is overwritten."""
        limit = timestamp_ms(cutoff)
        try:
            with self._gateway.umask(0o077):
                lock = self._open(LOCK_NAME, append=False)
                try:
                    if not self._gateway.lock_exclusive(lock, LOCK_TIMEOUT):
                        raise _unwritable("log.lock is held by another process", retryable=True)
                    return self._rewrite(limit)
                finally:
                    lock.close()
        except OSError:
            raise _unwritable("log.jsonl or log.lock cannot be rewritten") from None

    def _rewrite(self, limit: str) -> int:
        path = f"{self._directory}/{LOG_NAME}"
        try:
            log = self._open_existing(LOG_NAME)
        except FileNotFoundError:
            return 0
        try:
            removed = sum(1 for _, old in self._lines(log, limit) if old)
            if not removed:
                return 0  # nothing to remove: no temporary file, the log is untouched
            log.seek(0)
            tmp = self._open(TMP_NAME, append=False)
            try:
                tmp.truncate(0)
                for data, old in self._lines(log, limit):
                    if not old:
                        self._write_all(tmp, data)
                os.fsync(tmp.fileno())
            finally:
                tmp.close()
        finally:
            log.close()
        self._gateway.replace(f"{self._directory}/{TMP_NAME}", path)
        return removed

    def _open_existing(self, name: str) -> Any:
        handle = self._gateway.open_private(f"{self._directory}/{name}", create=False)
        try:
            info = os.fstat(handle.fileno())
            if not statmod.S_ISREG(info.st_mode) or info.st_uid != self._gateway.uid():
                raise _unwritable(f"{name} is not a regular file of this user")
        except BaseException:
            handle.close()
            raise
        return handle

    @staticmethod
    def _lines(handle: Any, limit: str) -> Any:
        """``(bytes, older)`` for each line of the log, newline included when present. A line is
        older when it is a JSON object whose ``ts`` is a timestamp below ``limit``; an overlong
        line is passed on in pieces and is never older."""
        reader = io.BufferedReader(handle, 1 << 16)
        try:
            while True:
                line = reader.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE and not line.endswith(b"\n"):
                    yield line, False
                    while True:  # the rest of an overlong line
                        rest = reader.readline(MAX_LINE)
                        if not rest:
                            return
                        yield rest, False
                        if rest.endswith(b"\n"):
                            break
                    continue
                yield line, LogWriter._older(line, limit)
        finally:
            reader.detach()  # the caller owns the handle

    @staticmethod
    def _older(line: bytes, limit: str) -> bool:
        try:
            doc = json.loads(line)
        except ValueError:
            return False
        if not isinstance(doc, dict):
            return False
        stamp = doc.get("ts")
        return isinstance(stamp, str) and _TS.fullmatch(stamp) is not None and stamp < limit
