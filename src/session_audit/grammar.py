"""Every grammar a user types or a config holds, parsed in one place (interface.md, Time
arguments, Config, Exit codes and Prefixes; ``schemas/defs.json`` for the session, claude-session,
retro and timestamp grammars, ``schemas/hooks/*.json`` for the event id, ``schemas/config.json``
for durations and sizes).

* Parsers take the field at fault (a flag or a config key, always a constant of the caller) and
  raise a typed ``GrammarError`` that carries what the error envelope needs: ``code``,
  ``exit_code``, a fixed ``message``, ``details`` and an optional fixed ``hint``. The input text
  is never part of an error, so it cannot reach a terminal or a log; ``str(error)`` goes through
  the sanitizer for the field name.
* Parsers are strict and ASCII-only: no surrounding whitespace, no sign, no fraction where none
  is documented, no non-ASCII digit, no lowercase of text that is not ASCII. Anything long is
  refused before a regular expression sees it.
* The clock is never read here: ``parse_when`` takes ``now`` (the gateway's ``now()``) and the
  configured time zone from the caller.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Context, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .textsafe import sanitize

__all__ = [
    "AmbiguousPrefixError",
    "GrammarError",
    "GrammarUsageError",
    "IngestEvery",
    "PrefixNotFoundError",
    "format_timestamp",
    "format_timestamp_ms",
    "parse_actor",
    "parse_ago",
    "parse_csession_id",
    "parse_decision_id",
    "parse_duration",
    "parse_event_id",
    "parse_ingest_every",
    "parse_int",
    "parse_label",
    "parse_label_key",
    "parse_label_value",
    "parse_retro_id",
    "parse_retro_prefix",
    "parse_session_id",
    "parse_session_prefix",
    "parse_size",
    "parse_time_zone",
    "parse_when",
    "resolve_prefix",
]

# Documented limits (interface.md, Config).
RETENTION_MAX = timedelta(days=3650)
RETRO_TIMEOUT_MIN = timedelta(seconds=1)
RETRO_TIMEOUT_MAX = timedelta(seconds=7200)
QUIET_AFTER_MAX = timedelta(days=7)
HOOK_TIMEOUT_MAX = timedelta(seconds=600)
INGEST_BUDGET_MAX = timedelta(seconds=3600)
INGEST_FILE_BUDGET_MAX = timedelta(seconds=600)
CHARS_RANGE = (20, 65536)  # digest.*_chars
LIMIT_RANGE = (1, 1000)  # --limit
MAX_TURNS_RANGE = (1, 1000)  # --max-turns, retro.max_turns
MAX_SESSIONS_RANGE = (1, 500)  # retro.max_sessions
RETROS_PER_DAY_RANGE = (1, 1000)  # limits.retros_per_day
# The documented size grammar has no maximum; this bounds what a byte count can mean.
DEFAULT_SIZE_MAX = 1 << 30

MAX_PREFIX_MATCHES = 20
_MAX_INPUT = 4096  # nothing in any grammar is longer; longer text is refused unread
_MAX_DIGITS = 15

_ASCII = re.ASCII
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _ASCII)
_CSESSION_ID = re.compile(r"cs-[a-z2-7]{8}", _ASCII)
_RETRO_ID = re.compile(r"ra-[a-z2-7]{8}", _ASCII)
_RETRO_PREFIX = re.compile(r"ra-[a-z2-7]{4,8}", _ASCII)
_EVENT_ID = re.compile(r"ev-[a-z2-7]{26}", _ASCII)
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}", _ASCII)
_LABEL_VALUE = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}", _ASCII)
_ACTOR = re.compile(r"(?:human|auto):[A-Za-z0-9._@/-]{1,64}", _ASCII)
_DECISION_ID = re.compile(r"[A-Za-z0-9._:/@-]{1,128}", _ASCII)
_ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+){0,2}", _ASCII)
_AGO = re.compile(r"([1-9][0-9]{0,8})([smhd])", _ASCII)
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", _ASCII)
_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(?:(Z)|([+-])([0-9]{2}):([0-9]{2}))",
    _ASCII,
)
_DURATION = re.compile(r"([0-9]{1,9})(?:\.([0-9]+))?(ms|s|m|d)?", _ASCII)  # config.json duration
_SIZE = re.compile(r"([1-9][0-9]{0,9})(B|KB|MB)?", _ASCII)  # config.json size
_DURATION_MAX_LEN = 32
_SIZE_MAX_LEN = 16
_DIGITS = re.compile(rf"[0-9]{{1,{_MAX_DIGITS}}}", _ASCII)
_INGEST_EVERY = re.compile(r"([1-9][0-9]{0,3})([mh]?)", _ASCII)

_INGEST_MINUTES = frozenset({5, 6, 10, 12, 15, 20, 30})
_INGEST_HOURS = frozenset({1, 2, 3, 4, 6, 8, 12, 24})
_DURATION_UNITS = {
    None: Decimal(1),
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "d": Decimal(86400),
}
_EXACT = Context(prec=2500)  # exact arithmetic for any input the grammar can hold
_MICRO = Decimal(1_000_000)
_SIZE_UNITS = {None: 1, "B": 1, "KB": 1024, "MB": 1024 * 1024}


class GrammarError(Exception):
    """Base of every grammar failure. ``code`` and ``exit_code`` are the error envelope's
    (`schemas/cli/error.json`); ``details`` holds only keys of its closed set, never the input."""

    code = "internal"
    exit_code = 1

    def __init__(
        self,
        message: str,
        field: str,
        *,
        hint: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.hint = hint
        self.details: dict[str, object] = {"field": field} if details is None else details

    def __str__(self) -> str:
        return f"{sanitize(str(self.field), 80)}: {self.message}"


class GrammarUsageError(GrammarError):
    """Exit 2: the value does not match its grammar or is out of range."""

    code = "usage"
    exit_code = 2


class AmbiguousPrefixError(GrammarError):
    """Exit 21: a prefix matches more than one id."""

    code = "ambiguous"
    exit_code = 21


class PrefixNotFoundError(GrammarError):
    """Exit 10: no id starts with the prefix."""

    code = "not_found"
    exit_code = 10


def _bad(field: str, message: str, hint: str | None = None) -> GrammarUsageError:
    return GrammarUsageError(message, field, hint=hint)


def _text(value: object, field: str, what: str, hint: str | None = None) -> str:
    """The value as a plain str that is short enough to match, else a usage error."""
    if not isinstance(value, str):
        raise _bad(field, f"invalid {what}", hint)
    value = str.__str__(value)
    if len(value) > _MAX_INPUT:
        raise _bad(field, f"invalid {what}", hint)
    return value


def _match(pattern: re.Pattern[str], value: object, field: str, what: str, hint: str) -> str:
    text = _text(value, field, what, hint)
    if not pattern.fullmatch(text):
        raise _bad(field, f"invalid {what}", hint)
    return text


# ---------------------------------------------------------------------------------------------
# ids (exact: no case folding; prefixes below fold case)


def _is_session_shape(text: str, minimum: int = 8) -> bool:
    """Whether ``text`` is a prefix of a lowercase UUID, ``minimum`` characters or more."""
    if not text.isascii() or not minimum <= len(text) <= 36:
        return False
    for i, ch in enumerate(text):
        if i in (8, 13, 18, 23):
            if ch != "-":
                return False
        elif ch not in "0123456789abcdef":
            return False
    return True


def parse_session_id(value: object, *, field: str) -> str:
    return _match(
        _SESSION_ID, value, field, "session id", "A Claude Code session id is a lowercase UUID."
    )


def parse_csession_id(value: object, *, field: str) -> str:
    text = _text(value, field, "claude-session id")
    if _CSESSION_ID.fullmatch(text):
        return text
    hint = "A claude-session id looks like cs-abcdefgh."
    if _is_session_shape(text.lower()):
        hint = f"{field} takes a claude-session id (cs-abcdefgh), not a Claude Code session id."
    raise _bad(field, "invalid claude-session id", hint)


def parse_retro_id(value: object, *, field: str) -> str:
    return _match(_RETRO_ID, value, field, "retro id", "A retro id looks like ra-abcdefg2.")


def parse_event_id(value: object, *, field: str) -> str:
    return _match(
        _EVENT_ID, value, field, "event id", "An event id is ev- and 26 base32 characters."
    )


def parse_label_key(value: object, *, field: str) -> str:
    return _match(
        _LABEL_KEY, value, field, "label key", "A label key matches [a-z0-9][a-z0-9_.-]{0,63}."
    )


def parse_label_value(value: object, *, field: str) -> str:
    return _match(
        _LABEL_VALUE,
        value,
        field,
        "label value",
        "A label value has 0 to 256 characters from A-Za-z0-9._:/#@+=,-.",
    )


def parse_label(value: object, *, field: str) -> tuple[str, str]:
    """``KEY=VALUE``, split at the first ``=`` (a key never contains one)."""
    text = _text(value, field, "label", "A label is KEY=VALUE.")
    key, sep, val = text.partition("=")
    if not sep:
        raise _bad(field, "invalid label", "A label is KEY=VALUE.")
    return (parse_label_key(key, field=field), parse_label_value(val, field=field))


def parse_actor(value: object, *, field: str) -> str:
    return _match(_ACTOR, value, field, "actor", "An actor is human:<name> or auto:<name>.")


def parse_decision_id(value: object, *, field: str) -> str:
    return _match(
        _DECISION_ID,
        value,
        field,
        "decision id",
        "A decision id has 1 to 128 characters from A-Za-z0-9._:/@-.",
    )


# ---------------------------------------------------------------------------------------------
# prefixes


def parse_session_prefix(value: object, *, field: str) -> str:
    """A Claude Code session id or a prefix of one, 8 to 36 characters, lowercased."""
    hint = "A session prefix is at least 8 characters of a session id (hex digits and dashes)."
    text = _text(value, field, "session prefix", hint)
    if _CSESSION_ID.fullmatch(text.lower()) and text.isascii():
        hint = f"{field} takes a Claude Code session id, not a claude-session id."
        raise _bad(field, "invalid session prefix", hint)
    if not text.isascii():
        raise _bad(field, "invalid session prefix", hint)
    lowered = text.lower()
    if not _is_session_shape(lowered):
        raise _bad(field, "invalid session prefix", hint)
    return lowered


def parse_retro_prefix(value: object, *, field: str) -> str:
    """``ra-`` and 4 to 8 base32 characters, lowercased."""
    hint = "A retro prefix is ra- and at least 4 more characters."
    text = _text(value, field, "retro prefix", hint)
    if not text.isascii() or not _RETRO_PREFIX.fullmatch(text.lower()):
        raise _bad(field, "invalid retro prefix", hint)
    return text.lower()


def resolve_prefix(prefix: str, known: Iterable[str], *, field: str) -> str:
    """The one id of ``known`` that starts with ``prefix`` (the output of a ``parse_*_prefix``;
    an id equal to the prefix wins). Raises ``PrefixNotFoundError`` (exit 10) or
    ``AmbiguousPrefixError`` (exit 21, at most 20 sorted ``matches`` and the ``matched`` total)."""
    matches: set[str] = set()
    for known_id in known:
        if known_id == prefix:
            return known_id
        if known_id.startswith(prefix):
            matches.add(known_id)
    if not matches:
        raise PrefixNotFoundError("not found", field, details={"id": prefix})
    if len(matches) == 1:
        return next(iter(matches))
    ordered = sorted(matches)
    raise AmbiguousPrefixError(
        "ambiguous prefix",
        field,
        details={"matches": ordered[:MAX_PREFIX_MATCHES], "matched": len(ordered)},
    )


# ---------------------------------------------------------------------------------------------
# time


# Names in the host's tz database that are not zones (a link to the local file, a rule file, a
# test zone) and the duplicate trees ``posix/`` and ``right/``.
_ZONE_REFUSED = frozenset({"localtime", "posixrules", "Factory"})
_ZONE_REFUSED_ROOTS = frozenset({"posix", "right"})


def parse_time_zone(value: object, *, field: str) -> ZoneInfo:
    hint = "A time zone is an IANA name such as UTC or America/New_York."
    text = _text(value, field, "time zone", hint)
    if len(text) > 64 or not _ZONE_NAME.fullmatch(text):
        raise _bad(field, "invalid time zone", hint)
    if text in _ZONE_REFUSED or text.partition("/")[0] in _ZONE_REFUSED_ROOTS:
        raise _bad(field, "invalid time zone", hint)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _bad(field, "invalid time zone", hint) from None


_WHEN_HINT = (
    "Use YYYY-MM-DDTHH:MM:SS (optional .fraction) followed by Z or +HH:MM, a date "
    "YYYY-MM-DD, or a duration ago such as 30m, 2h or 7d."
)
_AGO_HINT = "Use a duration ago: <n>s, <n>m, <n>h or <n>d."
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ago(value: object, *, field: str) -> timedelta:
    """``<n>(s|m|h|d)`` with n of 1 to 9 digits and no leading zero (the ``WHEN`` duration grammar
    and the stored ``since``/``until`` of the schemas); zero is refused."""
    text = _text(value, field, "duration ago", _AGO_HINT)
    found = _AGO.fullmatch(text)
    if not found:
        raise _bad(field, "invalid duration ago", _AGO_HINT)
    try:
        return timedelta(seconds=int(found.group(1)) * _UNIT_SECONDS[found.group(2)])
    except OverflowError:
        raise _bad(field, "duration ago out of range", _AGO_HINT) from None


def parse_when(value: object, *, field: str, now: datetime, time_zone: tzinfo) -> datetime:
    """``WHEN`` as an aware UTC datetime. ``now`` is the gateway's clock (aware). A date is the
    first instant of that day in ``time_zone`` (a midnight that does not exist gives the
    transition instant, an ambiguous one its first occurrence); ISO 8601 text is
    ``YYYY-MM-DDTHH:MM:SS[.f]`` (1 to 9 fraction digits, truncated to microseconds) then ``Z`` or
    ``+HH:MM``/``-HH:MM`` (the stored pattern of ``schemas/defs.json``); bare digits are refused."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("parse_when needs an aware now")
    text = _text(value, field, "time", _WHEN_HINT)
    try:
        if _AGO.fullmatch(text):
            return now.astimezone(UTC) - parse_ago(text, field=field)
        date = _DATE.fullmatch(text)
        if date:
            year, month, day = (int(g) for g in date.groups())
            local = datetime(year, month, day, tzinfo=time_zone)  # fold=0
            return local.astimezone(UTC)
        iso = _ISO.fullmatch(text)
        if iso:
            return _iso(iso)
    except (ValueError, OverflowError):
        raise _bad(field, "invalid time", _WHEN_HINT) from None
    raise _bad(field, "invalid time", _WHEN_HINT)


def _iso(found: re.Match[str]) -> datetime:
    year, month, day, hour, minute = (int(found.group(i)) for i in range(1, 6))
    second = int(found.group(6) or 0)
    micro = int((found.group(7) or "0").ljust(6, "0")[:6])
    if found.group(8):
        zone = UTC
    else:
        off_h, off_m = int(found.group(10)), int(found.group(11))
        if off_h > 23 or off_m > 59:
            raise ValueError("offset")
        delta = timedelta(hours=off_h, minutes=off_m)
        zone = timezone(-delta if found.group(9) == "-" else delta)
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=zone).astimezone(UTC)


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a timestamp needs an aware datetime")
    try:
        u = moment.astimezone(UTC)
    except OverflowError:
        raise ValueError("timestamp out of range") from None
    return f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"


def format_timestamp(moment: datetime) -> str:
    """UTC with a ``Z`` suffix (`schemas/defs.json` ``timestamp``): whole seconds, or the
    fraction without trailing zeros."""
    base = _stamp(moment)
    micro = moment.astimezone(UTC).microsecond
    fraction = f".{micro:06d}".rstrip("0") if micro else ""
    return f"{base}{fraction}Z"


def format_timestamp_ms(moment: datetime) -> str:
    """UTC, always three fraction digits, truncated (`schemas/defs.json` ``timestamp_ms``)."""
    base = _stamp(moment)
    return f"{base}.{moment.astimezone(UTC).microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------------------------
# durations, sizes, counts


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_duration(
    value: object,
    *,
    field: str,
    maximum: timedelta,
    minimum: timedelta = timedelta(milliseconds=1),
    allow_days: bool = False,
) -> timedelta:
    """A duration as a ``timedelta`` (whole microseconds, any finer part truncated).

    Text is ``[0-9]{1,9}(\\.[0-9]+)?(ms|s|m)?`` (``d`` too when ``allow_days``; no unit means
    seconds; no ``h``, no sign, 32 characters at most), the grammar of ``schemas/config.json``.
    A number is a finite positive int or float of seconds (a bool is refused). The value is held
    exactly (``Decimal``, a float by its shortest decimal form, as typed) and compared with the
    bounds before anything is rounded, so no input slips past a maximum. Zero in any form, also
    after truncation, is refused; the
    bounds are inclusive."""
    units = "ms, s, m or d" if allow_days else "ms, s or m"
    hint = f"Use <n>{{{units}}} (n may have a fraction) or seconds; zero is not allowed."
    seconds: Decimal
    if isinstance(value, bool):
        raise _bad(field, "invalid duration", hint)
    if isinstance(value, int):
        seconds = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _bad(field, "invalid duration", hint)
        seconds = Decimal(repr(value))
    else:
        text = _text(value, field, "duration", hint)
        found = _DURATION.fullmatch(text)
        if not found or len(text) > _DURATION_MAX_LEN:
            raise _bad(field, "invalid duration", hint)
        whole, fraction, unit = found.groups()
        if unit == "d" and not allow_days:
            raise _bad(field, "invalid duration", hint)
        number = Decimal(f"{whole}.{fraction}" if fraction is not None else whole)
        seconds = _EXACT.multiply(number, _DURATION_UNITS[unit])
    if seconds <= 0 or seconds < _seconds_of(minimum) or seconds > _seconds_of(maximum):
        raise _bad(field, "duration out of range", hint)
    micro = _EXACT.multiply(seconds, _MICRO).to_integral_value(rounding="ROUND_DOWN")
    if micro == 0:
        raise _bad(field, "duration out of range", hint)
    return timedelta(microseconds=int(micro))


def _seconds_of(delta: timedelta) -> Decimal:
    return Decimal(delta // timedelta(microseconds=1)) / _MICRO


def parse_size(value: object, *, field: str, maximum: int = DEFAULT_SIZE_MAX) -> int:
    """A whole byte count, or a whole number with ``B``, ``KB`` or ``MB`` (1 KB = 1024 bytes):
    the ``schemas/config.json`` pattern ``[1-9][0-9]{0,9}(B|KB|MB)?``, so no fraction, no zero,
    no leading zero and at most 10 digits. ``maximum`` defaults to 1 GiB (the docs give none);
    a key with its own maximum passes it."""
    hint = "Use a whole number of bytes, or a whole number followed by B, KB or MB."
    if _is_int(value):
        assert isinstance(value, int)
        total = value
    else:
        text = _text(value, field, "size", hint)
        found = _SIZE.fullmatch(text)
        if not found or len(text) > _SIZE_MAX_LEN:
            raise _bad(field, "invalid size", hint)
        total = int(found.group(1)) * _SIZE_UNITS[found.group(2)]
    if total < 1 or total > maximum:
        raise _bad(field, "size out of range", hint)
    return total


def parse_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    """A whole number (an int or a str of digits) from ``minimum`` to ``maximum``."""
    hint = f"Use a whole number from {minimum} to {maximum}."
    if _is_int(value):
        assert isinstance(value, int)
        number = value
    else:
        text = _text(value, field, "number", hint)
        if not _DIGITS.fullmatch(text):
            raise _bad(field, "invalid number", hint)
        number = int(text)
    if not minimum <= number <= maximum:
        raise _bad(field, "number out of range", hint)
    return number


@dataclass(frozen=True)
class IngestEvery:
    """A validated ``cron install --ingest-every`` interval."""

    minutes: int

    @property
    def canonical(self) -> str:
        if self.minutes >= 60:
            return f"{self.minutes // 60}h"
        return f"{self.minutes}m"


def parse_ingest_every(value: object, *, field: str = "--ingest-every") -> IngestEvery:
    """``<n>m`` for n in {5, 6, 10, 12, 15, 20, 30}, ``<n>h`` for n in {1, 2, 3, 4, 6, 8, 12,
    24}, ``60m`` as ``1h``, or a bare integer (an int or digits) meaning minutes from the minute
    set only: a bare 60 is refused (write ``60m`` or ``1h``)."""
    hint = (
        "Use <n>m with n in 5, 6, 10, 12, 15, 20, 30, or <n>h with n in 1, 2, 3, 4, 6, 8, 12, "
        "24; write 60m or 1h for an hour."
    )
    if _is_int(value):
        text = str(value)
    else:
        text = _text(value, field, "interval", hint)
    found = _INGEST_EVERY.fullmatch(text)
    if not found:
        raise _bad(field, "invalid interval", hint)
    number, unit = int(found.group(1)), found.group(2)
    if unit == "h":
        if number not in _INGEST_HOURS:
            raise _bad(field, "invalid interval", hint)
        return IngestEvery(number * 60)
    if number not in _INGEST_MINUTES and not (unit == "m" and number == 60):
        raise _bad(field, "invalid interval", hint)
    return IngestEvery(number)
