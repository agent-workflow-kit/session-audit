"""A scriptable gateway for tests of the modules above it: no process, file or clock is real.

    gw = FakeGateway()
    gw.on(["claude-session", "list"], CallResult(CallStatus.EXITED, 0, stdout="{}"))
    gw.clock.advance(60)

``on`` matches a call by the argv prefix; the first matching rule that still has results left
answers (the last result of a rule repeats). An unmatched call fails the test, so no code under
test runs something nobody scripted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from session_audit.gateway import DEFAULT_STDOUT_LIMIT, CallResult, CallStatus

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = EPOCH) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._now = start.astimezone(UTC)
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class FakeGateway:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.calls: list[dict[str, object]] = []
        self.crontab = ""
        self._rules: list[tuple[tuple[str, ...], list[CallResult]]] = []

    def on(self, prefix: Sequence[str], *results: CallResult) -> None:
        if not results:
            raise ValueError("a rule needs at least one result")
        self._rules.append((tuple(prefix), list(results)))

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    ) -> CallResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout, "cwd": cwd})
        for prefix, results in self._rules:
            if tuple(argv[: len(prefix)]) == prefix:
                return results.pop(0) if len(results) > 1 else results[0]
        raise AssertionError(f"unscripted call: {list(argv)}")

    def now(self) -> datetime:
        return self.clock.now()

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def open_read(self, path: str | bytes | os.PathLike[str]) -> BinaryIO:
        raise NotImplementedError("file access is scripted by the issue that first needs it")

    def lstat(self, path: str | bytes | os.PathLike[str]) -> os.stat_result:
        raise NotImplementedError

    def list_dir(self, path: str | bytes | os.PathLike[str]) -> list[str]:
        raise NotImplementedError

    def crontab_read(self) -> CallResult:
        return CallResult(CallStatus.EXITED, 0, stdout=self.crontab)

    def crontab_write(self, text: str) -> CallResult:
        self.crontab = text
        return CallResult(CallStatus.EXITED, 0)
