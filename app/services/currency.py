"""Currency conversion.

The marketplace quotes budgets in whatever currency the client chose — a Mumbai client posts in
INR, a Berlin one in EUR — while a freelancer thinks in one home currency. Comparing a budget to a
floor, or showing it for a quick read, only makes sense once both are in the *same* currency.

Rates here are a static, approximate table (USD per one unit of each currency). That is deliberate
for v1: a budget floor is a coarse gate and a display figure an at-a-glance aid, so a rate that is a
few percent stale changes nothing that matters, and a hardcoded table needs no network call on the
hot path. When precision starts to matter, swap ``_USD_RATES`` for a refreshed feed behind the same
``convert`` signature — nothing else has to change.
"""

from __future__ import annotations

# USD value of one unit of each currency. Approximate; see the module docstring.
_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "AUD": 0.66,
    "CAD": 0.73,
    "NZD": 0.61,
    "SGD": 0.74,
    "HKD": 0.128,
    "INR": 0.012,
    "PKR": 0.0036,
    "BDT": 0.0091,
    "LKR": 0.0033,
    "PHP": 0.017,
    "IDR": 0.000063,
    "MYR": 0.21,
    "THB": 0.028,
    "VND": 0.000039,
    "CNY": 0.138,
    "JPY": 0.0066,
    "KRW": 0.00073,
    "AED": 0.272,
    "SAR": 0.267,
    "ZAR": 0.053,
    "NGN": 0.00065,
    "KES": 0.0078,
    "EGP": 0.020,
    "BRL": 0.18,
    "MXN": 0.058,
    "ARS": 0.0011,
    "TRY": 0.029,
    "RUB": 0.011,
    "UAH": 0.024,
    "PLN": 0.25,
    "SEK": 0.094,
    "NOK": 0.091,
    "DKK": 0.145,
    "CHF": 1.11,
    "ILS": 0.27,
}


def _rate(currency: str | None) -> float | None:
    if not currency:
        return None
    return _USD_RATES.get(currency.strip().upper())


def convert(
    amount: float | None, from_currency: str | None, to_currency: str | None
) -> float | None:
    """Convert ``amount`` from one currency to another.

    Returns ``None`` when the amount is missing or either currency is unknown — the caller decides
    what a missing conversion means (skip a filter, hide a display figure), which is safer than
    inventing a number by comparing across currencies. A same-currency conversion is a no-op.
    """
    if amount is None:
        return None
    from_c = (from_currency or "").strip().upper()
    to_c = (to_currency or "").strip().upper()
    if from_c and from_c == to_c:
        return amount
    from_rate = _rate(from_c)
    to_rate = _rate(to_c)
    if from_rate is None or to_rate is None:
        return None
    return amount * from_rate / to_rate


def is_supported(currency: str | None) -> bool:
    """Whether a currency has a conversion rate — i.e. ``convert`` can reach or leave it."""
    return _rate(currency) is not None
