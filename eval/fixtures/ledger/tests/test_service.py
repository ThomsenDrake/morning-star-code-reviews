from __future__ import annotations

import pytest

from ledger.errors import InsufficientFunds, InvalidRequest
from ledger.models import Account, User
from ledger.pagination import PageRequest
from ledger.service import LedgerService


def test_deposit_and_withdraw(service: LedgerService, account: Account) -> None:
    service.deposit(account, "25.00")
    service.withdraw(account, "5.50", "coffee")
    assert service.balance(account) == 1950


def test_withdraw_needs_funds(service: LedgerService, account: Account) -> None:
    with pytest.raises(InsufficientFunds):
        service.withdraw(account, "1.00")


def test_transfer_moves_money(service: LedgerService, account: Account, bob: User) -> None:
    other = service.open_account(bob, "Savings", "EUR")
    service.deposit(account, "10")
    debit, credit = service.transfer(account, other, "4")
    assert (debit.amount_cents, credit.amount_cents) == (-400, 400)
    assert service.balance(other) == 400


def test_transfer_rejects_currency_mismatch(
    service: LedgerService, account: Account, bob: User
) -> None:
    other = service.open_account(bob, "Dollars", "USD")
    service.deposit(account, "10")
    with pytest.raises(InvalidRequest):
        service.transfer(account, other, "1")


def test_statement_pages(service: LedgerService, account: Account) -> None:
    for index in range(1, 26):
        service.deposit(account, str(index))
    first = service.statement(account, PageRequest(page=1, per_page=10))
    last = service.statement(account, PageRequest(page=3, per_page=10))
    assert [tx.amount_cents for tx in first.transactions][:2] == [100, 200]
    assert len(last.transactions) == 5
    assert first.total == 25


def test_close_requires_zero_balance(service: LedgerService, account: Account) -> None:
    service.deposit(account, "1")
    with pytest.raises(InvalidRequest):
        service.close(account)
    service.withdraw(account, "1")
    service.close(account)
    assert service.get_account(account.id).closed


def test_open_account_validates_name(service: LedgerService, alice: User) -> None:
    with pytest.raises(InvalidRequest):
        service.open_account(alice, "   ", "EUR")
