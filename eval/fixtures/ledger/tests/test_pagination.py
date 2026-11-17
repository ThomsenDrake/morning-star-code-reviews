from __future__ import annotations

import pytest

from ledger.errors import InvalidRequest
from ledger.pagination import Page, PageRequest, parse_page_request


def test_defaults() -> None:
    assert parse_page_request({}) == PageRequest(page=1, per_page=20)


def test_offset_of_first_and_third_page() -> None:
    assert PageRequest(page=1, per_page=10).offset == 0
    assert PageRequest(page=3, per_page=10).offset == 20


@pytest.mark.parametrize("query", [{"page": "0"}, {"per_page": "500"}, {"page": "x"}])
def test_rejects_bad_parameters(query: dict[str, str]) -> None:
    with pytest.raises(InvalidRequest):
        parse_page_request(query)


def test_page_count_rounds_up() -> None:
    page = Page(items=[], page=1, per_page=10, total=21)
    assert page.pages == 3
    assert page.has_next
    assert Page(items=[], page=1, per_page=10, total=0).pages == 1
