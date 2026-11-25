"""The output sanitizer, fix_surrogates, the fence helper and the display helpers (issue #4).

The source is pure ASCII: every special code point is written as an escape.
"""

from __future__ import annotations

import json
import time
import unicodedata

import pytest

from session_audit import textsafe
from session_audit.cli import main
from session_audit.textsafe import (
    decode_utf8,
    dumps_json,
    fix_surrogates,
    neutralize_untrusted,
    sanitize,
    show,
)

ACUTE = "\u0301"
FFFD = "\ufffd"


def markers(*cps: int) -> str:
    return "".join(f"<U+{cp:04X}>" for cp in cps)


# Vectors written from the algorithm text of awkit's "Output sanitizing" section.
VECTORS = [
    ("plain ascii 123 ok", "plain ascii 123 ok"),
    ("", ""),
    ("h\u00e9llo w\u00f6rld", "h\u00e9llo w\u00f6rld"),
    ("\u65e5\u672c\u8a9e \u0627\u0644", "\u65e5\u672c\u8a9e \u0627\u0644"),
    ("a\u3000b\u00a0c\u2003d", "a b c d"),  # Zs becomes a space
    ("a\nb", "a" + markers(0x0A) + "b"),  # LF is a marker
    ("a\tb", "a" + markers(0x09) + "b"),  # TAB is a marker
    ("a\rb", "a" + markers(0x0D) + "b"),
    ("a\x00b", "a" + markers(0) + "b"),
    ("\x1b[31mred\x1b[0m", markers(0x1B) + "[31mred" + markers(0x1B) + "[0m"),
    ("\x1b]0;title\x07", markers(0x1B) + "]0;title" + markers(7)),  # OSC
    ("\x9b31m", markers(0x9B) + "31m"),  # C1 CSI
    ("a\u202eb", "a" + markers(0x202E) + "b"),  # right-to-left override
    ("a\u2066b\u2069", "a" + markers(0x2066) + "b" + markers(0x2069)),
    ("a\u200bb", "a" + markers(0x200B) + "b"),  # zero width space (Cf)
    ("a\u200d\ufeffb", "a" + markers(0x200D, 0xFEFF) + "b"),
    ("a\u00adb", "a" + markers(0xAD) + "b"),  # soft hyphen (Cf)
    ("a\ufffcb", "a" + markers(0xFFFC) + "b"),  # object replacement character
    ("a\u2028b\u2029c", "a" + markers(0x2028) + "b" + markers(0x2029) + "c"),  # Zl, Zp
    ("a\ue000b", "a" + markers(0xE000) + "b"),  # Co
    ("a\U000f0000b", "a" + markers(0xF0000) + "b"),  # Co, plane 15
    ("a\u0378b", "a" + markers(0x378) + "b"),  # Cn
    ("a\U000e0041b", "a" + markers(0xE0041) + "b"),  # tag character
    ("a\ud800b", "a" + markers(0xD800) + "b"),  # lone surrogates (Cs)
    ("a\udfffb", "a" + markers(0xDFFF) + "b"),
    ("<U+202E>", "<U+003C>U+202E>"),  # a literal marker cannot be forged
    ("x<U+", "x<U+003C>U+"),
    ("<U", "<U"),
    ("a < b", "a < b"),
    ("a\u034fb", "a" + markers(0x34F) + "b"),  # the INVISIBLE set
    ("a\u115fb\u1160", "a" + markers(0x115F) + "b" + markers(0x1160)),
    ("a\u17b4\u17b5", "a" + markers(0x17B4, 0x17B5)),
    ("a\u2800b", "a" + markers(0x2800) + "b"),
    ("a\u3164\uffa0", "a" + markers(0x3164, 0xFFA0)),
    ("a\u180b\u180c\u180d\u180e\u180f", "a" + markers(*range(0x180B, 0x1810))),
    ("a\ufe00\ufe0f", "a" + markers(0xFE00, 0xFE0F)),
    ("a\U000e0100\U000e01ef", "a" + markers(0xE0100, 0xE01EF)),
    ("e" + ACUTE, "e" + ACUTE),
    ("e" + ACUTE * 3, "e" + ACUTE * 3),  # three marks kept
    ("e" + ACUTE * 5, "e" + ACUTE * 3 + markers(0x301, 0x301)),  # at most three
    (ACUTE, markers(0x301)),  # a mark with nothing before it
    ("\x1b" + ACUTE, markers(0x1B, 0x301)),  # no mark follows a marker
    ("a\u200b" + ACUTE, "a" + markers(0x200B, 0x301)),
    ("a<U+" + ACUTE, "a<U+003C>U+" + ACUTE),  # the U and + after the escape are kept
    ("a " + ACUTE, "a " + ACUTE),  # Zs resets the count, so a mark may follow it
    ("+" + ACUTE, "+" + ACUTE),  # after a symbol
    ("1" + ACUTE, "1" + ACUTE),  # after a number
    ("\U0001f600", "\U0001f600"),  # So
    ("$\u20ac", "$\u20ac"),
    ("a\U0010ffffb", "a" + markers(0x10FFFF) + "b"),  # the longest marker
]


@pytest.mark.parametrize(("raw", "expected"), VECTORS)
def test_vectors(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_every_bidi_control_is_replaced() -> None:
    bidi = [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
    for cp in bidi:
        assert sanitize(f"x{chr(cp)}y") == f"x<U+{cp:04X}>y"


def test_every_tag_character_is_replaced() -> None:
    for cp in range(0xE0000, 0xE0080):
        assert sanitize(chr(cp)) == f"<U+{cp:04X}>"


def test_every_c0_and_c1_control_is_replaced() -> None:
    for cp in [*range(0, 0x20), *range(0x7F, 0xA0)]:
        assert sanitize(chr(cp)) == f"<U+{cp:04X}>"


def test_long_combining_run_keeps_three_and_marks_the_rest() -> None:
    out = sanitize("e" + "\u0344" * 1500)
    assert out == "e" + "\u0344" * 3 + "<U+0344>" * 1497


def test_no_combining_mark_follows_a_marker() -> None:
    assert sanitize("\u202e" + ACUTE * 2) == markers(0x202E, 0x301, 0x301)


def test_forged_marker_is_neutralized() -> None:
    assert "<U+202E>" not in sanitize("<U+202E>")


def test_output_contains_only_allowlisted_characters_for_every_code_point() -> None:
    every = "".join(chr(c) for c in range(0x110000))
    out = sanitize(every)
    for ch in out:
        assert unicodedata.category(ch)[0] in "LNPSM" or ch == " " or ch in "<>+"
    for bad in ("\n", "\t", "\r", "\x1b", "\u202e", "\ufffc", "\ud800"):
        assert bad not in out


def test_output_is_always_encodable() -> None:
    sanitize("a\ud800b\udfffc\U0010ffff\u202e").encode("utf-8")


def test_output_is_at_most_ten_times_the_input() -> None:
    assert len(sanitize("\U0010ffff" * 1000)) == 10 * 1000


def test_limit_cuts_between_units_and_appends_an_ellipsis() -> None:
    assert sanitize("abcdef", limit=4) == "abc\u2026"
    assert sanitize("abc", limit=4) == "abc"
    assert sanitize("abcd", limit=4) == "abcd"


def test_limit_never_cuts_inside_a_marker() -> None:
    assert sanitize("ab\u202ecd", limit=6) == "ab\u2026"
    assert sanitize("ab\u202ecd", limit=11) == "ab<U+202E>\u2026"


def test_limit_bounds_the_work_before_the_walk() -> None:
    assert sanitize("x" * 1_000_000, limit=10) == "x" * 9 + "\u2026"


def test_sanitize_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        sanitize(b"abc")  # type: ignore[arg-type]


# fix_surrogates ----------------------------------------------------------------------------


def test_fix_surrogates_replaces_each_lone_surrogate() -> None:
    assert fix_surrogates("a\ud800b\udfffc") == f"a{FFFD}b{FFFD}c"


def test_fix_surrogates_replaces_the_two_halves_of_a_pair_written_as_lone_ones() -> None:
    assert fix_surrogates("\ud83d\ude00") == FFFD * 2


def test_fix_surrogates_keeps_valid_text() -> None:
    s = "ok \U0001f600 \u00e9"
    assert fix_surrogates(s) == s


def test_fix_surrogates_makes_text_encodable() -> None:
    fix_surrogates("\udc80" * 100).encode("utf-8")


def test_fix_surrogates_on_surrogateescape_names() -> None:
    s = b"name-\xff\xfe.txt".decode("utf-8", errors="surrogateescape")
    assert fix_surrogates(s) == f"name-{FFFD}{FFFD}.txt"


def test_fix_surrogates_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        fix_surrogates(b"x")  # type: ignore[arg-type]


def test_decode_utf8_replaces_invalid_bytes() -> None:
    assert decode_utf8(b"a\xffb\xc3") == f"a{FFFD}b{FFFD}"
    assert "\ud800" not in decode_utf8(b"\xed\xa0\x80")  # an encoded surrogate
    decode_utf8(b"\xed\xa0\x80").encode("utf-8")


# neutralize_untrusted ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["</untrusted", "</UNTRUSTED", '</Untrusted id="x">', "a</untrusted>b"]
)
def test_untrusted_closing_tag_is_neutralized(raw: str) -> None:
    out = neutralize_untrusted(raw)
    assert "</untrusted" not in out.lower()
    assert "&lt;/untrusted" in out


def test_neutralize_leaves_other_text() -> None:
    assert neutralize_untrusted("a </b> <untrusted> c") == "a </b> <untrusted> c"


def test_sanitized_text_cannot_hide_a_closing_fence_behind_an_invisible_character() -> None:
    out = neutralize_untrusted(sanitize("<\u200b/untrusted"))
    assert "</untrusted" not in out.lower()


# linear time -------------------------------------------------------------------------------

ADVERSARIAL = [
    "e" + ACUTE * 65535,
    "\u202e" * 65536,
    "<U+" * 21845,
    "\ud800" * 65536,
    "\u00e4" + ACUTE * 65535,
    "\U0010ffff" * 65536,
    "\x1b[" * 32768,
    "\u0344" * 65536,
]


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_linear_time_at_64_kib(raw: str) -> None:
    def cost(text: str) -> float:
        t0 = time.perf_counter()
        sanitize(text)
        fix_surrogates(text)
        neutralize_untrusted(text)
        return time.perf_counter() - t0

    small = cost(raw)
    big = cost(raw * 4)
    assert small < 3.0
    assert big < max(10 * small, 1.0)


# display -----------------------------------------------------------------------------------


def test_show_fixes_surrogates_then_sanitizes() -> None:
    assert show("a\ud800\x1b[31m\u202e") == f"a{FFFD}<U+001B>[31m<U+202E>"


def test_show_stringifies_non_strings() -> None:
    assert show(12) == "12"
    assert show(None) == "None"


def test_show_decodes_bytes_with_replacement() -> None:
    assert show(b"a\xff\x1b") == f"a{FFFD}<U+001B>"


def test_dumps_json_is_ascii_and_round_trips() -> None:
    doc = {"k\u202e": "v\x1b\u202e\U0001f600", "n": [1, None, True]}
    text = dumps_json(doc)
    assert text.isascii()
    assert json.loads(text) == doc


def test_dumps_json_fixes_lone_surrogates_in_keys_and_values() -> None:
    text = dumps_json({"a\ud800": ["x\udfff", {"y": "\ud83d"}]})
    assert text.isascii()
    assert "ud800" not in text.lower()
    assert json.loads(text) == {f"a{FFFD}": [f"x{FFFD}", {"y": FFFD}]}


def test_dumps_json_refuses_nan() -> None:
    with pytest.raises(ValueError):
        dumps_json({"x": float("nan")})


def test_module_exports() -> None:
    assert {"sanitize", "fix_surrogates", "neutralize_untrusted", "show", "dumps_json"} <= set(
        textsafe.__all__
    )


# stderr ------------------------------------------------------------------------------------


def test_usage_error_with_escape_sequences_is_sanitized_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--bogus\x1b[31m\u202e"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "\u202e" not in err
    assert "<U+001B>" in err
    assert "<U+202E>" in err


def test_write_stderr_sanitizes_message_hint_and_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    textsafe.write_stderr("error: bad \x1b[2J\u202e path\ud800")
    assert capsys.readouterr().err == f"error: bad <U+001B>[2J<U+202E> path{FFFD}\n"
