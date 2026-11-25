"""Text safety: the output sanitizer, ``fix_surrogates`` and the display helpers.

The sanitizer carries the algorithm text of awkit's "Output sanitizing" section as this tool's
own code (a tool repo never imports awkit). It allows by Unicode general category and replaces
everything else by a visible marker ``<U+XXXX>``:

* categories L, N, P and S are kept;
* Zs becomes a space;
* a combining mark (M) is kept only when at most three marks already follow the last kept
  character; after a marker, or at the start, none is kept;
* the INVISIBLE set and every other code point (Cc, Cf, Cs, Co, Cn, Zl, Zp, LF, TAB) become a
  marker; U+FFFC (category So) is replaced too, as the digest text names it;
* a literal ``<U+`` becomes ``<U+003C>U+`` so a marker cannot be forged.

Cost is linear: one pass over the code points, and a marker is at most ten characters.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from typing import Any

__all__ = [
    "decode_utf8",
    "dumps_json",
    "fix_surrogates",
    "neutralize_untrusted",
    "sanitize",
    "show",
    "write_stderr",
]

_INVISIBLE = frozenset(
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
    # U+FFFC is a symbol (So) but the digest text lists it among the replaced code points.
    | {0xFFFC}
)
_MAX_MARKS = 3
_SURROGATES = re.compile("[\ud800-\udfff]")
_UNTRUSTED_CLOSE = re.compile(r"(?i)</untrusted")


def _marker(ch: str) -> str:
    return f"<U+{ord(ch):04X}>"


def sanitize(s: str, limit: int | None = None) -> str:
    """Return ``s`` with everything outside the allowlist replaced by a marker.

    ``limit`` bounds the work (the input is cut first) and the output: when the input or the
    output is longer, the result is cut between units (a marker is one unit) and ends in ``…``,
    at most ``limit`` characters in all.
    """
    if type(s) is not str:
        raise TypeError(f"sanitize needs a str, got {type(s).__name__}")
    cut = limit is not None and len(s) > limit
    if limit is not None:
        s = s[:limit]
    out: list[str] = []
    marks = _MAX_MARKS  # no combining mark may attach here
    for i, ch in enumerate(s):
        cat = unicodedata.category(ch)
        if ch == "<" and s.startswith("U+", i + 1):
            out.append("<U+003C>")
            marks = _MAX_MARKS
        elif ord(ch) in _INVISIBLE:
            out.append(_marker(ch))
            marks = _MAX_MARKS
        elif cat[0] in "LNPS":
            out.append(ch)
            marks = 0
        elif cat[0] == "M" and marks < _MAX_MARKS:
            out.append(ch)
            marks += 1
        elif cat == "Zs":
            out.append(" ")
            marks = 0
        else:
            out.append(_marker(ch))
            marks = _MAX_MARKS
    text = "".join(out)
    if limit is not None and (cut or len(text) > limit):
        keep: list[str] = []
        used = 0
        for unit in out:
            if used + len(unit) > limit - 1:
                break
            keep.append(unit)
            used += len(unit)
        text = "".join(keep) + "…"
    return text


def fix_surrogates(s: str) -> str:
    """Replace each lone surrogate with U+FFFD, so the text always encodes as UTF-8."""
    if type(s) is not str:
        raise TypeError(f"fix_surrogates needs a str, got {type(s).__name__}")
    return _SURROGATES.sub("�", s)


def decode_utf8(data: bytes) -> str:
    """Decode bytes as UTF-8, each invalid byte becoming U+FFFD (never a surrogate)."""
    return data.decode("utf-8", errors="replace")


def neutralize_untrusted(s: str) -> str:
    """Write every ``</untrusted`` (any case) as ``&lt;/untrusted``; the digest applies it last."""
    return _UNTRUSTED_CLOSE.sub("&lt;/untrusted", s)


def show(value: object) -> str:
    """The text to print for a value that is not one of the tool's own constants."""
    if isinstance(value, bytes | bytearray):
        text = decode_utf8(bytes(value))
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    return sanitize(fix_surrogates(text))


def _fixed(value: Any) -> Any:
    if isinstance(value, str):
        return fix_surrogates(value)
    if isinstance(value, dict):
        return {_fixed(k): _fixed(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_fixed(v) for v in value]
    return value


def dumps_json(doc: Any) -> str:
    """``--json`` output: ASCII only (control and bidi characters are escaped), lone surrogates
    fixed first, and no NaN or infinity."""
    return json.dumps(_fixed(doc), ensure_ascii=True, allow_nan=False)


def write_stderr(message: str) -> None:
    """Print one line to stderr through the sanitizer (a hostile message cannot drive the
    terminal)."""
    sys.stderr.write(show(message) + "\n")
    sys.stderr.flush()
