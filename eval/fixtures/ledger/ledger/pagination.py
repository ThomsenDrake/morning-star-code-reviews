"""Page parameters for list endpoints. Pages are numbered from 1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from ledger.errors import InvalidRequest

T = TypeVar("T")

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


@dataclass(frozen=True)
class PageRequest:
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        """Rows to skip before this page starts."""
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        """Number of pages; an empty listing still has one (empty) page."""
        if self.total == 0:
            return 1
        return -(-self.total // self.per_page)

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    def meta(self) -> dict[str, object]:
        """Paging metadata for a response body (the items are rendered by the caller)."""
        return {
            "page": self.page,
            "per_page": self.per_page,
            "total": self.total,
            "pages": self.pages,
            "has_next": self.has_next,
        }


def _int_param(query: Mapping[str, str], name: str, default: int) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidRequest(f"{name} must be an integer") from exc


def parse_page_request(query: Mapping[str, str]) -> PageRequest:
    """Read ``page`` and ``per_page`` from a query mapping, with bounds checks."""
    page = _int_param(query, "page", 1)
    per_page = _int_param(query, "per_page", DEFAULT_PER_PAGE)
    if page < 1:
        raise InvalidRequest("page starts at 1")
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise InvalidRequest(f"per_page must be between 1 and {MAX_PER_PAGE}")
    return PageRequest(page=page, per_page=per_page)
