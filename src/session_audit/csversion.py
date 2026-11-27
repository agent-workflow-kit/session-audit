"""The claude-session version check (interface, Retro launch: ``MIN_CS``).

``claude-session --version`` runs through the gateway (10 s timeout). The first line must be
``claude-session `` followed by a ``\\d+.\\d+.\\d+`` version (anything may follow it); versions
compare as integer tuples (``0.100.0`` is higher than ``0.45.2``). The outcomes are distinct and
none is read from an exit code of a failed call:

- ``OK``: the program ran, printed a version and it is at least ``MIN_CS``;
- ``MISSING``: the program could not be started;
- ``TOO_OLD``: a version below ``MIN_CS``;
- ``UNPARSABLE``: the program exited 0 but printed no ``claude-session <version>`` first line;
- ``FAILED``: the call timed out, was killed, or exited non-zero (it may be a program that is
  not claude-session at all).

Shared by ``ingest`` (``harness``), ``retro run`` and ``validate``; the
mapping of an outcome to an exit code belongs to each caller.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from .gateway import CallResult, CallStatus, Gateway

MIN_CS = (0, 45, 2)
VERSION_TIMEOUT = 10.0
VERSION_OUTPUT_LIMIT = 64 * 1024
_FIRST_LINE_MAX = 512
_PREFIX = "claude-session "
# ASCII digits only, and bounded, so a hostile first line cannot make a huge integer.
_VERSION = re.compile(r"(?<![0-9])([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})(?![0-9])", re.ASCII)


class Presence(enum.Enum):
    OK = "ok"
    MISSING = "missing"
    TOO_OLD = "too_old"
    UNPARSABLE = "unparsable"
    FAILED = "failed"


@dataclass(frozen=True)
class VersionCheck:
    presence: Presence
    version: tuple[int, int, int] | None = None
    result: CallResult | None = None  # the failed call, for ``FAILED``

    @property
    def ok(self) -> bool:
        return self.presence is Presence.OK


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The version of the first line of ``text``, which must start with ``claude-session <version>``
    (what argparse prints for the real program), or None."""
    first = text.split("\n", 1)[0][:_FIRST_LINE_MAX]
    if not first.startswith(_PREFIX):
        return None
    found = _VERSION.match(first, len(_PREFIX))
    if found is None:
        return None
    return (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def check_version(gateway: Gateway, command: str) -> VersionCheck:
    """Run ``<command> --version`` and classify what came back."""
    result = gateway.run(
        [command, "--version"], timeout=VERSION_TIMEOUT, stdout_limit=VERSION_OUTPUT_LIMIT
    )
    if result.status is CallStatus.SPAWN_FAILED:
        return VersionCheck(Presence.MISSING, result=result)
    if not result.ok:
        return VersionCheck(Presence.FAILED, result=result)
    version = parse_version(result.stdout)
    if version is None:
        return VersionCheck(Presence.UNPARSABLE)
    if version < MIN_CS:
        return VersionCheck(Presence.TOO_OLD, version)
    return VersionCheck(Presence.OK, version)
