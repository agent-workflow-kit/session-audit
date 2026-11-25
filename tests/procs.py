"""Helpers to check on processes a test started."""

from __future__ import annotations

import time
from pathlib import Path


def gone(pid: int) -> bool:
    """True when ``pid`` is no longer a running process (a zombie awaiting its parent is gone)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in {"Z", "X"}


def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if gone(pid):
            return True
        time.sleep(0.02)
    return gone(pid)
