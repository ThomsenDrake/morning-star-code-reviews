"""Amounts are integer cents everywhere; these helpers convert at the edges."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from ledger.errors import InvalidRequest

SUPPORTED_CURRENCIES = ("EUR", "USD", "GBP")
_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
MAX_AMOUNT_CENTS = 100_000_000_00  # one hundred million in major units


def parse_amount(text: str) -> int:
    """Parse a decimal string such as ``"12.50"`` into cents.

    At most two decimal places are accepted; the result must be positive and
    below ``MAX_AMOUNT_CENTS``.
    """
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise InvalidRequest(f"not an amount: {text!r}") from exc
    if not value.is_finite():
        raise InvalidRequest("amount must be a finite number")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise InvalidRequest("amount has more than two decimal places")
    cents = int(value * 100)
    if cents <= 0:
        raise InvalidRequest("amount must be positive")
    if cents >= MAX_AMOUNT_CENTS:
        raise InvalidRequest("amount is too large")
    return cents


def format_amount(cents: int, currency: str) -> str:
    """Render cents for people, e.g. ``format_amount(-1250, "EUR") == "-€12.50"``."""
    symbol = _SYMBOLS.get(currency, currency + " ")
    sign = "-" if cents < 0 else ""
    major, minor = divmod(abs(cents), 100)
    return f"{sign}{symbol}{major:,}.{minor:02d}"


def check_currency(currency: str) -> str:
    """Return the upper-cased currency code or raise if it is not supported."""
    code = currency.strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        raise InvalidRequest(f"unsupported currency: {currency!r}")
    return code


def prorate(amount_cents: int, used_days: int, period_days: int) -> int:
    """Share of ``amount_cents`` for ``used_days`` out of a ``period_days`` period.

    Rounds half to even, so splitting a period in two never creates a cent.
    ``used_days`` must lie between 0 and ``period_days`` inclusive.
    """
    if period_days <= 0:
        raise InvalidRequest("period must be at least one day")
    if not 0 <= used_days <= period_days:
        raise InvalidRequest("used days must lie within the period")
    share = Decimal(amount_cents) * Decimal(used_days) / Decimal(period_days)
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def split_evenly(cents: int, parts: int) -> list[int]:
    """Split ``cents`` into ``parts`` amounts that differ by at most one cent.

    The first ``cents % parts`` amounts receive the extra cent, so the parts
    always add up to the original amount.
    """
    if parts <= 0:
        raise InvalidRequest("parts must be positive")
    base, extra = divmod(cents, parts)
    return [base + 1 if index < extra else base for index in range(parts)]
