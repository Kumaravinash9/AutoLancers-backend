"""Currency conversion — the floor comparison and the display figures both ride on this."""

from __future__ import annotations

import pytest

from app.services.currency import convert, is_supported


class TestConvert:
    def test_same_currency_is_a_noop(self):
        assert convert(500, "USD", "USD") == 500

    def test_case_and_whitespace_insensitive(self):
        assert convert(500, " usd ", "Usd") == 500

    def test_converts_across_currencies_via_usd(self):
        # 12,500 INR at ~0.012 USD is ~150 USD — well under a 500 USD floor, the bug's
        # canonical case.
        usd = convert(12_500, "INR", "USD")
        assert usd is not None
        assert 100 < usd < 200

    def test_round_trips_approximately(self):
        there = convert(1000, "USD", "EUR")
        back = convert(there, "EUR", "USD")
        assert back == pytest.approx(1000, rel=1e-9)

    def test_unknown_currency_is_none_not_a_guess(self):
        assert convert(100, "USD", "ZZZ") is None
        assert convert(100, "ZZZ", "USD") is None

    def test_missing_amount_is_none(self):
        assert convert(None, "USD", "INR") is None


class TestIsSupported:
    def test_known_and_unknown(self):
        assert is_supported("inr") is True
        assert is_supported("ZZZ") is False
        assert is_supported(None) is False
