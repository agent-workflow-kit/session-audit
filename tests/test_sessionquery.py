"""``sessionquery.whole_ms``: the shared rule for an untyped meta value as epoch milliseconds."""

from __future__ import annotations

from session_audit.sessionquery import whole_ms


def test_whole_ms_ignores_digit_text_too_long_to_be_an_epoch() -> None:
    assert whole_ms("9" * 5000) is None


def test_whole_ms_still_reads_plausible_digit_text() -> None:
    assert whole_ms("1700000000000") == 1700000000000
