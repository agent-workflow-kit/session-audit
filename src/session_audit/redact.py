"""Redaction and the fixed digest text pipeline (issue #20).

``digest_text`` runs, in this order: ``fix_surrogates``, the 65,536 code point work bound, NFC,
redaction, the cut (at a code point, never inside a ``[REDACTED:<kind>]`` marker), the sanitizer
and the ``</untrusted`` neutralizer. The output is deterministic.

Redaction finds spans (built-in patterns from ``redact_patterns.toml``, private-key blocks,
literal masks, configured token prefixes), merges overlapping spans and writes one marker per
merged span. Every search is linear: patterns are bounded and anchored (checked by the tests),
private keys and literal masks use ``str.find``.

No mask value is ever stored, logged or shown: ``Redactor`` keeps them private, its ``repr`` shows
counts only, it cannot be pickled or copied, and no error message carries a value.
"""

from __future__ import annotations

import base64
import pkgutil
import re
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import NoReturn

from session_audit import textsafe

__all__ = [
    "DIGEST_FORMAT",
    "KINDS",
    "MAX_MASKS",
    "MIN_MASK_LENGTH",
    "UNICODE_VERSION",
    "WORK_BOUND",
    "Redactor",
    "build_masks",
    "cut",
    "digest_text",
    "find_encoded",
    "marker",
    "redact_encoded",
]

# Bumped in every release that changes the digest rendering or the bundled skill (see the
# retro key in docs/interface.md). The sanitizer reads the running interpreter's Unicode
# database, so the sanitized text of a code point assigned in a newer Unicode version differs
# across Python versions (U+2FFC: a marker on 3.12, kept on 3.13). UNICODE_VERSION names the
# database in use so a consumer can record or compare it; it is NOT part of the retro key.
DIGEST_FORMAT = 1
UNICODE_VERSION = unicodedata.unidata_version

WORK_BOUND = 65_536
MIN_MASK_LENGTH = 8
MAX_MASKS = 128
_MAX_LINES_PER_VALUE = 32
_MAX_PREFIXES = 64
_ENCODED_WINDOW = 512  # bytes: a longer value is checked by its head and its tail

KINDS = (
    "token",
    "private_key",
    "url_userinfo",
    "cookie",
    "auth_header",
    "cli_secret",
    "json_secret",
    "jwt",
    "assignment",
    "env_value",
    "prefix",
    "encoded",
)

_MARKER = re.compile(r"\[REDACTED:[a-z_]{1,16}\]")
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|KEY|CREDENTIAL|AUTH", re.IGNORECASE
)
_PREFIX_OK = re.compile(r"[A-Za-z0-9_-]{3,64}")
_PREFIX_TAIL = re.compile(r"[A-Za-z0-9_-]{8,}")
_PK_HEADER = re.compile(r"-----BEGIN (?P<label>[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?)-----")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

Span = tuple[int, int, str]


def marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True)
class _Pattern:
    name: str
    kind: str
    regex: re.Pattern[str]
    group: str | int


@cache
def _patterns() -> tuple[_Pattern, ...]:
    data = pkgutil.get_data("session_audit", "redact_patterns.toml")
    if data is None:
        raise RuntimeError("redact_patterns.toml is missing from the package")
    entries = tomllib.loads(data.decode("utf-8"))["pattern"]
    out: list[_Pattern] = []
    for entry in entries:
        name = entry["name"]
        if entry["kind"] not in KINDS:
            raise RuntimeError(f"redact_patterns.toml: {name}: unknown kind")
        try:
            regex = re.compile(entry["regex"])
        except re.error as exc:
            raise RuntimeError(f"redact_patterns.toml: {name}: {exc}") from None
        out.append(_Pattern(name, entry["kind"], regex, entry.get("group", 0)))
    return tuple(out)


def builtin_patterns() -> tuple[_Pattern, ...]:
    """The loaded built-in patterns (for the tests)."""
    return _patterns()


def _private_key_spans(text: str, spans: list[Span]) -> None:
    pos = 0
    while True:
        i = text.find("-----BEGIN ", pos)
        if i < 0:
            return
        m = _PK_HEADER.match(text, i)
        if m is None:
            pos = i + 1
            continue
        end_line = f"-----END {m.group('label')}-----"
        j = text.find(end_line, m.end())
        end = len(text) if j < 0 else j + len(end_line)
        spans.append((i, end, "private_key"))
        pos = end


def _find_all(text: str, needle: str) -> Iterable[tuple[int, int]]:
    pos = 0
    n = len(needle)
    while True:
        i = text.find(needle, pos)
        if i < 0:
            return
        yield i, i + n
        pos = i + n


def _merge(spans: list[Span]) -> list[Span]:
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[Span] = []
    for start, end, kind in spans:
        if merged and start <= merged[-1][1]:
            prev = merged[-1]
            if end > prev[1]:
                merged[-1] = (prev[0], end, prev[2])
        else:
            merged.append((start, end, kind))
    return merged


def _fail(what: str) -> NoReturn:
    raise ValueError(what)


def _normal(value: str) -> str:
    return unicodedata.normalize("NFC", textsafe.fix_surrogates(value))


def build_masks(
    env: Mapping[str, str],
    mask_env: Sequence[str] = (),
    warn: Callable[[str], None] = textsafe.write_stderr,
) -> list[str]:
    """The literal mask values for an environment the caller passes (never ``os.environ``).

    The names are every ``mask_env`` entry and every variable whose name contains TOKEN, SECRET,
    PASSWORD, PASSWD, PASSPHRASE, KEY, CREDENTIAL or AUTH (any case). A value shorter than 8
    characters is ignored; the warning names only a ``mask_env`` variable that is set, and a
    count for the rest, and never a value. A value over 65,536 characters can never occur in a
    part and is ignored with a count. A multi-line value also adds each of its lines (at most
    32) and a stripped form. The result is longest first and at most ``MAX_MASKS`` long.
    """
    explicit: list[str] = []
    for name in mask_env:
        if not isinstance(name, str):
            _fail("mask_env entries must be strings")
        if name not in explicit:
            explicit.append(name)
    automatic = sorted(
        n for n in env if isinstance(n, str) and _SECRET_NAME.search(n) and n not in explicit
    )
    found: list[str] = []
    short = 0
    huge = 0
    for name in [*explicit, *automatic]:
        raw = env.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            _fail("environment values must be strings")
        value = _normal(raw)
        if len(value) < MIN_MASK_LENGTH:
            if name in explicit:
                warn(
                    f"redaction: mask_env variable {textsafe.show(name)} has a value shorter "
                    f"than {MIN_MASK_LENGTH} characters; it is not masked"
                )
            else:
                short += 1
            continue
        if len(value) > WORK_BOUND:
            huge += 1
            continue
        found.append(value)
        stripped = value.strip()
        if stripped != value:
            found.append(stripped)
        if "\n" in value or "\r" in value:
            for line in value.splitlines()[:_MAX_LINES_PER_VALUE]:
                found.append(line.strip())
    if short:
        warn(
            f"redaction: {short} secret-named environment value(s) shorter than "
            f"{MIN_MASK_LENGTH} characters are not masked"
        )
    if huge:
        warn(f"redaction: {huge} environment value(s) over {WORK_BOUND} characters are not masked")
    unique = {v for v in found if len(v) >= MIN_MASK_LENGTH}
    ordered = sorted(unique, key=lambda v: (-len(v), v))
    if len(ordered) > MAX_MASKS:
        warn(f"redaction: {len(ordered) - MAX_MASKS} mask value(s) over the limit are not masked")
        ordered = ordered[:MAX_MASKS]
    return ordered


# ---- encoded forms of the mask values (artifact validation) -------------------------------------


def _b64_needles(data: bytes) -> list[str]:
    """base64 of ``data`` at the three alignments, without the characters that depend on its
    neighbours; both the standard and the url-safe alphabet, padding dropped."""
    out: list[str] = []
    for lead in (0, 1, 2):
        enc = base64.b64encode(b"\0" * lead + data).decode("ascii")
        drop_front = (0, 2, 3)[lead]
        body = enc.rstrip("=")
        if (lead + len(data)) % 3:
            body = body[:-1]  # the last character carries bits of the next byte
        body = body[drop_front:]
        if len(body) >= 8:
            out.append(body)
            out.append(body.replace("+", "-").replace("/", "_"))
    return out


def _percent_variants(data: bytes) -> list[str]:
    from urllib.parse import quote, quote_plus

    text = data.decode("utf-8", errors="replace")
    forms = {quote(text, safe=""), quote_plus(text, safe="")}
    forms.add("".join(f"%{b:02X}" for b in data))
    return [f for f in forms if f != text and len(f) >= MIN_MASK_LENGTH]


_PLAIN, _FOLD, _B64 = 0, 1, 2  # how a needle is matched; base64 hits also take their tail

_B64_TAIL = re.compile(r"[A-Za-z0-9+/_-]?={0,2}")


def _encoded_needles(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """(needle, mode) for each encoded form of each value."""
    seen: dict[str, int] = {}
    for value in values:
        data = value.encode("utf-8", errors="replace")
        windows = [data]
        if len(data) > 2 * _ENCODED_WINDOW:
            windows = [data[:_ENCODED_WINDOW], data[-_ENCODED_WINDOW:]]
        for w in windows:
            for needle in _b64_needles(w):
                seen.setdefault(needle, _B64)
            seen.setdefault(w.hex().lower(), _FOLD)
            for needle in _percent_variants(w):
                seen[needle.translate(_ASCII_LOWER)] = _FOLD
    return tuple(seen.items())


def _encoded_spans(needles: tuple[tuple[str, int], ...], text: str) -> list[Span]:
    if not needles:
        return []
    lowered = text.translate(_ASCII_LOWER)  # same length: ASCII only
    spans: list[Span] = []
    for needle, mode in needles:
        if len(needle) > len(text):
            continue
        haystack = lowered if mode == _FOLD else text
        for s, e in _find_all(haystack, needle):
            if mode == _B64:  # the character and padding the needle left out
                tail = _B64_TAIL.match(text, e)
                e = tail.end() if tail else e
            spans.append((s, e, "encoded"))
    return spans


def _apply(text: str, spans: list[Span]) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    if not spans:
        return text, counts
    out: list[str] = []
    last = 0
    for start, end, kind in _merge(spans):
        if end - start <= 40 and _MARKER.fullmatch(text, start, end):
            continue  # already a marker: redaction is idempotent
        out.append(text[last:start])
        out.append(marker(kind))
        counts[kind] += 1
        last = end
    out.append(text[last:])
    return "".join(out), counts


def find_encoded(mask_values: Iterable[str], text: str) -> bool:
    """True when ``text`` holds base64 (three alignments, standard or url-safe), hex (any case) or
    percent-encoding (any hex case) of any mask value (values under 8 characters are ignored)."""
    values = [_normal(v) for v in mask_values if len(v) >= MIN_MASK_LENGTH]
    return bool(_encoded_spans(_encoded_needles(values), text))


def redact_encoded(mask_values: Iterable[str], text: str) -> tuple[str, int]:
    """``text`` with each encoded mask value replaced by ``[REDACTED:encoded]`` and the count."""
    values = [_normal(v) for v in mask_values if len(v) >= MIN_MASK_LENGTH]
    out, counts = _apply(text, _encoded_spans(_encoded_needles(values), text))
    return out, sum(counts.values())


class Redactor:
    """Built-in patterns plus literal masks plus configured token prefixes.

    ``masks`` come from ``build_masks``. Values under 8 characters are dropped; at most
    ``MAX_MASKS`` are kept (longest first). Nothing here reveals a mask value.
    """

    __slots__ = ("_masks", "_needles", "_prefixes")

    def __init__(self, masks: Iterable[str] = (), extra_token_prefixes: Iterable[str] = ()):
        values = {_normal(v) for v in masks if isinstance(v, str)}
        kept = sorted(
            (v for v in values if MIN_MASK_LENGTH <= len(v) <= WORK_BOUND),
            key=lambda v: (-len(v), v),
        )
        self._masks: tuple[str, ...] = tuple(kept[:MAX_MASKS])
        prefixes = list(extra_token_prefixes)
        if len(prefixes) > _MAX_PREFIXES:
            _fail("too many extra token prefixes")
        for p in prefixes:
            if not isinstance(p, str) or not _PREFIX_OK.fullmatch(p):
                _fail("an extra token prefix is not 3 to 64 characters of A-Za-z0-9_-")
        self._prefixes: tuple[str, ...] = tuple(sorted(set(prefixes), key=lambda p: (-len(p), p)))
        self._needles: tuple[tuple[str, int], ...] | None = None

    @property
    def mask_count(self) -> int:
        return len(self._masks)

    def __repr__(self) -> str:
        return f"Redactor(masks={len(self._masks)}, prefixes={len(self._prefixes)})"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("Redactor holds secret values and cannot be pickled or copied")

    def _spans(self, text: str) -> list[Span]:
        spans: list[Span] = []
        _private_key_spans(text, spans)
        for pat in _patterns():
            for m in pat.regex.finditer(text):
                s, e = m.span(pat.group)
                if e > s:
                    spans.append((s, e, pat.kind))
        for mask in self._masks:
            spans.extend((s, e, "env_value") for s, e in _find_all(text, mask))
        for prefix in self._prefixes:
            pos = 0
            while (s := text.find(prefix, pos)) >= 0:
                e = s + len(prefix)
                tail = _PREFIX_TAIL.match(text, e)
                if tail is None:
                    pos = s + 1
                    continue
                spans.append((s, tail.end(), "prefix"))
                pos = tail.end()  # the run is consumed: no rescanning, so the scan stays linear
        return spans

    def redact_counted(self, text: str) -> tuple[str, Counter[str]]:
        """``text`` redacted, and how many markers of each kind were written."""
        if not isinstance(text, str):
            raise TypeError(f"redact needs a str, got {type(text).__name__}")
        return _apply(text, self._spans(str.__str__(text)))

    def redact(self, text: str) -> str:
        return self.redact_counted(text)[0]

    def find_encoded(self, text: str) -> bool:
        return bool(self._encoded(text))

    def redact_encoded(self, text: str) -> tuple[str, int]:
        out, counts = _apply(text, self._encoded(text))
        return out, sum(counts.values())

    def _encoded(self, text: str) -> list[Span]:
        if self._needles is None:
            self._needles = _encoded_needles(self._masks)
        return _encoded_spans(self._needles, text)


_DEFAULT = Redactor()


def cut(text: str, cap: int) -> str:
    """At most ``cap`` code points; a cut that would fall inside a ``[REDACTED:<kind>]`` marker
    moves back to the marker's start (so the result is never longer than ``cap``)."""
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("cut cap must be a non-negative int")
    if len(text) <= cap:
        return text
    start = text.rfind("[REDACTED:", 0, cap + 9)  # the last marker start before the cut
    if start >= 0:
        m = _MARKER.match(text, start)
        if m is not None and m.end() > cap:
            return text[:start]
    return text[:cap]


def digest_text(raw: str, cap: int, redactor: Redactor | None = None) -> str:
    """The fixed text pipeline for every digest text part."""
    if not isinstance(raw, str):
        raise TypeError(f"digest_text needs a str, got {type(raw).__name__}")
    t = textsafe.fix_surrogates(raw)
    t = t[:WORK_BOUND]
    t = unicodedata.normalize("NFC", t)
    t = (redactor or _DEFAULT).redact(t)
    t = cut(t, cap)
    t = textsafe.sanitize(t)
    return textsafe.neutralize_untrusted(t)
