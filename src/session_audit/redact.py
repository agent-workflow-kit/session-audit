"""Redaction and the fixed digest text pipeline (issue #20).

``digest_text`` runs, in this order: ``fix_surrogates``, the 65,536 code point work bound, NFC,
redaction, the cut (at a code point, never inside a ``[REDACTED:<kind>]`` marker), the sanitizer
and the ``</untrusted`` neutralizer. The output is deterministic. Redaction reads a window of
twice the work bound, so a secret that starts inside the bound and ends after it is redacted whole;
the output ends at the bound, or after the marker of a match that starts inside it.

Redaction finds spans (built-in patterns from ``redact_patterns.toml``, private-key blocks,
literal masks, configured token prefixes), merges overlapping spans and writes one marker per
merged span. Every search is linear: patterns are bounded and anchored (checked by the tests),
private keys and literal masks use ``str.find``.

No mask value is ever stored, logged or shown: ``Redactor`` keeps them private, its ``repr`` shows
counts only, it cannot be pickled or copied, and no error message carries a value.
"""

from __future__ import annotations

import base64
import bisect
import json
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
_VALUE_CAP = 16_384  # the longest value a pattern matches whole; a longer one is continued in code
_MAX_PER_LINE = 4  # per-line masks of one multi-line value once the 128 would be exceeded
_MAX_EXTRA_MASK = 2 * WORK_BOUND  # a JSON-escaped form can be longer than its value
_KEY_WINDOW = 4096  # an orphan END line redacts at most this far back

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
_PK_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
_MARK_KINDS = frozenset({"token", "jwt", "env_value", "prefix", "private_key"})
# How a value that reached _VALUE_CAP is continued (the class stops at the value's own end).
_EXTEND = {
    "dq": re.compile(r'[^"\r\n]*'),
    "sq": re.compile(r"[^'\r\n]*"),
    "bare": re.compile(r"[^\s,}\]#]*"),
    "line": re.compile(r"[^\r\n]*"),
    "flow": re.compile(r"[^\s\"'#,}\]]*"),
    "userinfo": re.compile(r"[^\s/?#]*"),
    "value": re.compile(r"[^\s\"']*"),
    "hdr": re.compile(r"[^\r\n\"']*"),
    "conn": re.compile(r"[^;\r\n\"']*"),
    "xml": re.compile(r"[^<\r\n]*"),
    "json": re.compile(r'(?:[^"\\\r\n]|\\.)*'),
    "escaped": re.compile(r'[^"\\\r\n]*'),
}
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
    extend: str | None = None


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
        extend = entry.get("extend")
        if extend is not None and extend not in _EXTEND:
            raise RuntimeError(f"redact_patterns.toml: {name}: unknown extend mode")
        out.append(_Pattern(name, entry["kind"], regex, entry.get("group", 0), extend))
    return tuple(out)


def builtin_patterns() -> tuple[_Pattern, ...]:
    """The loaded built-in patterns (for the tests)."""
    return _patterns()


def _private_key_spans(text: str, spans: list[Span]) -> None:
    blocks: list[tuple[int, int]] = []
    pos = 0
    while True:
        i = text.find("-----BEGIN ", pos)
        if i < 0:
            break
        m = _PK_HEADER.match(text, i)
        if m is None:
            pos = i + 1
            continue
        end_line = f"-----END {m.group('label')}-----"
        j = text.find(end_line, m.end())
        end = len(text) if j < 0 else j + len(end_line)
        spans.append((i, end, "private_key"))
        blocks.append((i, end))
        pos = end
    _orphan_end_spans(text, blocks, spans)


def _orphan_end_spans(text: str, blocks: list[tuple[int, int]], spans: list[Span]) -> None:
    """An END line without its BEGIN (a tail-truncated output): the line and the key body before
    it, back to the previous blank line or at most ``_KEY_WINDOW`` characters."""
    starts = [b[0] for b in blocks]
    pos = 0
    while True:
        i = text.find("-----END ", pos)
        if i < 0:
            return
        m = _PK_END.match(text, i)
        if m is None:
            pos = i + 1
            continue
        pos = m.end()
        k = bisect.bisect_right(starts, i) - 1
        if k >= 0 and blocks[k][0] <= i < blocks[k][1]:
            continue  # inside a block that has its BEGIN
        low = max(0, i - _KEY_WINDOW)
        if k >= 0:
            low = max(low, blocks[k][1])  # never back into the previous block
        start = low
        for blank in ("\n\n", "\n\r\n"):
            j = text.rfind(blank, low, i)
            if j >= 0:
                start = max(start, j + len(blank))
        spans.append((start, m.end(), "private_key"))


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


def _prepare_masks(values: Iterable[object]) -> tuple[str, ...]:
    """The mask values every entry point shares: surrogates fixed, NFC, 8 to 65,536 characters
    after that, longest first, at most ``MAX_MASKS``."""
    kept = {_normal(v) for v in values if isinstance(v, str)}
    ok = sorted((v for v in kept if MIN_MASK_LENGTH <= len(v) <= WORK_BOUND), key=_longest_first)
    return tuple(ok[:MAX_MASKS])


def _longest_first(v: str) -> tuple[int, str]:
    return (-len(v), v)


def _json_forms(values: Iterable[str]) -> set[str]:
    """The JSON string-escaped forms of the values (tool output is often JSON), when they differ;
    at most two per value."""
    out: set[str] = set()
    for v in values:
        for ascii_only in (True, False):
            form = json.dumps(v, ensure_ascii=ascii_only)[1:-1]
            if form != v and MIN_MASK_LENGTH <= len(form) <= _MAX_EXTRA_MASK:
                out.add(form)
    return out


def _fold(text: str) -> str:
    """The same text with each character that has an ASCII letter or digit as the base of its
    decomposition replaced by that base (same length, so offsets carry over). A combining mark
    after the last character of a token composes under NFC and would otherwise defeat the match;
    the folded text only ever matches more."""
    if text.isascii():
        return text
    table: dict[int, str] = {}
    for ch in set(text):
        if ch.isascii():
            continue
        d = unicodedata.normalize("NFD", ch)
        if len(d) > 1 and d[0].isascii() and d[0].isalnum():
            table[ord(ch)] = d[0]
    return text.translate(table) if table else text


def _extend(text: str, start: int, end: int, mode: str) -> int:
    """The end of a value that reached the length bound: over the rest of the same run."""
    first = text[start]
    if mode in ("value", "line", "flow") and first in "\"'":
        if end - start > 1 and text[end - 1] == first:
            return end  # closed
        mode = "dq" if first == '"' else "sq"
    stop = _EXTEND[mode].match(text, end).end()  # type: ignore[union-attr]
    if mode == "userinfo":
        at = text.rfind("@", end, stop)  # the host starts at the last "@" of the run
        if at != -1:
            return at
    return stop


def _past_marks(text: str, end: int) -> int:
    stop = min(len(text), end + 32)
    while end < stop and unicodedata.combining(text[end]):
        end += 1
    return end


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
    32) and a stripped form. The result is longest first and at most ``MAX_MASKS`` long; when
    more are found, the whole values win over the stripped forms, and those over the per-line
    forms (at most 4 per value, the longest lines), and the warning counts what was dropped.
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
    whole: list[str] = []
    stripped: list[str] = []
    per_line: list[list[str]] = []
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
        whole.append(value)
        if value.strip() != value:
            stripped.append(value.strip())
        if "\n" in value or "\r" in value:
            lines = [ln.strip() for ln in value.splitlines()[:_MAX_LINES_PER_VALUE]]
            per_line.append([ln for ln in lines if len(ln) >= MIN_MASK_LENGTH])
    if short:
        warn(
            f"redaction: {short} secret-named environment value(s) shorter than "
            f"{MIN_MASK_LENGTH} characters are not masked"
        )
    if huge:
        warn(f"redaction: {huge} environment value(s) over {WORK_BOUND} characters are not masked")
    # Priority when the 128 would be exceeded: the whole values, then their stripped forms, then
    # the per-line forms (at most 4 per source value, the longest lines).
    whole = [v for v in whole if len(v) >= MIN_MASK_LENGTH]
    stripped = [v for v in stripped if len(v) >= MIN_MASK_LENGTH]
    everything = set(whole) | set(stripped) | {ln for group in per_line for ln in group}
    dropped = 0
    if len(everything) > MAX_MASKS:
        limited = [sorted(g, key=lambda v: (-len(v), v))[:_MAX_PER_LINE] for g in per_line]
        dropped += sum(len(g) for g in per_line) - sum(len(g) for g in limited)
        per_line = limited
    candidates: list[str] = []
    seen: set[str] = set()
    for tier in (whole, stripped, [ln for g in per_line for ln in g]):
        for v in sorted(set(tier), key=lambda v: (-len(v), v)):
            if v not in seen:
                seen.add(v)
                candidates.append(v)
    if len(candidates) > MAX_MASKS:
        dropped += len(candidates) - MAX_MASKS
        candidates = candidates[:MAX_MASKS]
    if dropped:
        warn(f"redaction: {dropped} mask value(s) over the limit are not masked")
    return sorted(candidates, key=lambda v: (-len(v), v))


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
    # base64 may be wrapped (MIME, 64 or 76 columns): search it with the white space left out and
    # map the hit back to the original offsets
    index = [i for i, c in enumerate(text) if c not in " \t\r\n"]
    compact = text if len(index) == len(text) else "".join(text[i] for i in index)
    spans: list[Span] = []
    for needle, mode in needles:
        if len(needle) > len(text):
            continue
        if mode == _B64:
            for s, e in _find_all(compact, needle):
                a, z = (s, e) if compact is text else (index[s], index[e - 1] + 1)
                tail = _B64_TAIL.match(text, z)  # the character and padding the needle left out
                spans.append((a, tail.end() if tail else z, "encoded"))
        else:
            spans.extend((s, e, "encoded") for s, e in _find_all(lowered, needle))
    return spans


def _apply(text: str, spans: list[Span], limit: int | None = None) -> tuple[str, Counter[str]]:
    """``text`` with each merged span replaced by its marker. With ``limit``, the result ends at
    that offset, or after the marker of a span that starts before it and ends after it."""
    counts: Counter[str] = Counter()
    if not spans and limit is None:
        return text, counts
    end_of_output = len(text) if limit is None else limit
    out: list[str] = []
    last = 0
    for start, end, kind in _merge(spans):
        if start >= end_of_output:
            break
        if end - start <= 40 and _MARKER.fullmatch(text, start, end):
            continue  # already a marker: redaction is idempotent
        out.append(text[last:start])
        out.append(marker(kind))
        counts[kind] += 1
        last = end
    out.append(text[last:end_of_output])
    return "".join(out), counts


def find_encoded(mask_values: Iterable[str], text: str) -> bool:
    """True when ``text`` holds base64 (three alignments, standard or url-safe), hex (any case) or
    percent-encoding (any hex case) of any mask value (values under 8 characters are ignored)."""
    return bool(_encoded_spans(_encoded_needles(_prepare_masks(mask_values)), text))


def redact_encoded(mask_values: Iterable[str], text: str) -> tuple[str, int]:
    """``text`` with each encoded mask value replaced by ``[REDACTED:encoded]`` and the count."""
    needles = _encoded_needles(_prepare_masks(mask_values))
    out, counts = _apply(text, _encoded_spans(needles, text))
    return out, sum(counts.values())


class Redactor:
    """Built-in patterns plus literal masks plus configured token prefixes.

    ``masks`` come from ``build_masks``. Values under 8 characters are dropped; at most
    ``MAX_MASKS`` are kept (longest first), plus their JSON-escaped forms. Nothing here reveals a
    mask value.
    """

    __slots__ = ("_folded", "_literals", "_masks", "_needles", "_prefixes")

    def __init__(self, masks: Iterable[str] = (), extra_token_prefixes: Iterable[str] = ()):
        self._masks: tuple[str, ...] = _prepare_masks(masks)
        # the values and their JSON-escaped forms (at most two more per value, not counted in
        # MAX_MASKS), and the same with each character folded to its ASCII base
        literals = sorted({*self._masks, *_json_forms(self._masks)}, key=_longest_first)
        self._literals: tuple[str, ...] = tuple(literals)
        self._folded: tuple[str, ...] = tuple({_fold(v) for v in literals})
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
        folded = _fold(text)  # patterns and prefixes read it; same offsets as ``text``
        for pat in _patterns():
            for m in pat.regex.finditer(folded):
                s, e = m.span(pat.group)
                if e <= s:
                    continue
                if pat.extend is not None and e - s >= _VALUE_CAP:
                    e = _extend(folded, s, e, pat.extend)
                if pat.kind in _MARK_KINDS:
                    e = _past_marks(text, e)
                spans.append((s, e, pat.kind))
        for mask in self._literals:
            spans.extend((s, _past_marks(text, e), "env_value") for s, e in _find_all(text, mask))
        if folded is not text:
            for mask in self._folded:
                spans.extend(
                    (s, _past_marks(text, e), "env_value") for s, e in _find_all(folded, mask)
                )
        for prefix in self._prefixes:
            pos = 0
            while (s := folded.find(prefix, pos)) >= 0:
                e = s + len(prefix)
                tail = _PREFIX_TAIL.match(folded, e)
                if tail is None:
                    pos = s + 1
                    continue
                spans.append((s, _past_marks(text, tail.end()), "prefix"))
                pos = tail.end()  # the run is consumed: no rescanning, so the scan stays linear
        return spans

    def redact_counted(self, text: str, limit: int | None = None) -> tuple[str, Counter[str]]:
        """``text`` redacted, and how many markers of each kind were written. With ``limit`` the
        output ends at that offset of ``text`` (or after the marker of a match that starts before
        it and ends after it), so the caller can pass a window longer than the part it keeps."""
        if not isinstance(text, str):
            raise TypeError(f"redact needs a str, got {type(text).__name__}")
        return _apply(text, self._spans(str.__str__(text)), limit)

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
    rd = redactor or _DEFAULT
    if len(t) <= WORK_BOUND:
        t = rd.redact(unicodedata.normalize("NFC", t))
    else:
        # A secret that starts inside the bound may end after it: redact a window of twice the
        # bound, keep the part up to the bound (or through the marker of a match that straddles it).
        limit = len(unicodedata.normalize("NFC", t[:WORK_BOUND]))
        window = unicodedata.normalize("NFC", t[: 2 * WORK_BOUND])
        t = rd.redact_counted(window, min(limit, len(window)))[0]
    t = cut(t, cap)
    t = textsafe.sanitize(t)
    return textsafe.neutralize_untrusted(t)
