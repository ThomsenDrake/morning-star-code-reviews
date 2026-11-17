from __future__ import annotations

import pytest

from ledger import money
from ledger.errors import InvalidRequest


@pytest.mark.parametrize(
    ("text", "cents"), [("1", 100), ("12.5", 1250), ("0.01", 1), (" 3.20 ", 320)]
)
def test_parse_amount(text: str, cents: int) -> None:
    assert money.parse_amount(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "0", "-1", "1.001", "NaN", "1e12"])
def test_parse_amount_rejects(text: str) -> None:
    with pytest.raises(InvalidRequest):
        money.parse_amount(text)


def test_format_amount() -> None:
    assert money.format_amount(123456, "EUR") == "€1,234.56"
    assert money.format_amount(-5, "USD") == "-$0.05"
    assert money.format_amount(100, "CHF") == "CHF 1.00"


def test_prorate() -> None:
    assert money.prorate(3000, 10, 30) == 1000
    assert money.prorate(900, 0, 30) == 0
    assert money.prorate(900, 30, 30) == 900
    assert money.prorate(100, 1, 8) == 12  # 12.5 rounds half to even


def test_prorate_rejects_days_outside_period() -> None:
    with pytest.raises(InvalidRequest):
        money.prorate(900, 31, 30)
    with pytest.raises(InvalidRequest):
        money.prorate(900, 1, 0)


def test_split_evenly() -> None:
    assert money.split_evenly(100, 3) == [34, 33, 33]
    assert sum(money.split_evenly(1001, 7)) == 1001


def test_check_currency() -> None:
    assert money.check_currency(" usd ") == "USD"
    with pytest.raises(InvalidRequest):
        money.check_currency("XYZ")
