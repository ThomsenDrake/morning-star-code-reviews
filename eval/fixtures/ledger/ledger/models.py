"""Plain data objects passed between the storage, service and handler layers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    id: int
    name: str
    is_admin: bool = False


@dataclass(frozen=True)
class Plan:
    code: str
    monthly_cents: int
    included_transactions: int


PLANS: dict[str, Plan] = {
    "basic": Plan("basic", monthly_cents=0, included_transactions=50),
    "plus": Plan("plus", monthly_cents=900, included_transactions=500),
    "business": Plan("business", monthly_cents=2900, included_transactions=5000),
}


@dataclass(frozen=True)
class Account:
    id: int
    owner_id: int
    name: str
    currency: str
    plan: str = "basic"
    closed: bool = False


@dataclass(frozen=True)
class Transaction:
    id: int
    account_id: int
    amount_cents: int
    memo: str
    created_at: str


@dataclass
class Statement:
    account: Account
    balance_cents: int
    transactions: list[Transaction] = field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 20
