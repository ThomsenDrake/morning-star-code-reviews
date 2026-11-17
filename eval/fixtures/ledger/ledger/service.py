"""Business rules on top of the storage layer.

The service never checks who is calling; handlers do that with ``ledger.auth``
before they call in.
"""

from __future__ import annotations

from ledger import money
from ledger.db import Database
from ledger.errors import InsufficientFunds, InvalidRequest
from ledger.models import PLANS, Account, Statement, Transaction, User
from ledger.pagination import PageRequest


class LedgerService:
    """One service per worker thread.

    Each instance owns its own SQLite connection and is never shared between
    threads, so a balance check and the insert that follows it cannot
    interleave with another request.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def open_account(self, owner: User, name: str, currency: str) -> Account:
        name = name.strip()
        if not name:
            raise InvalidRequest("account name must not be empty")
        if len(name) > 80:
            raise InvalidRequest("account name is too long")
        return self.db.create_account(owner.id, name, money.check_currency(currency))

    def get_account(self, account_id: int) -> Account:
        return self.db.get_account(account_id)

    def balance(self, account: Account) -> int:
        return self.db.balance(account.id)

    def deposit(self, account: Account, amount_text: str, memo: str = "") -> Transaction:
        """Credit the account. The amount is a positive decimal string."""
        self._check_open(account)
        amount = money.parse_amount(amount_text)
        return self.db.insert_transaction(account.id, amount, memo or "deposit")

    def withdraw(self, account: Account, amount_text: str, memo: str = "") -> Transaction:
        """Debit the account; the balance may not go below zero."""
        self._check_open(account)
        amount = money.parse_amount(amount_text)
        if self.db.balance(account.id) < amount:
            raise InsufficientFunds("balance too low")
        return self.db.insert_transaction(account.id, -amount, memo or "withdrawal")

    def transfer(
        self, source: Account, target: Account, amount_text: str, memo: str = ""
    ) -> tuple[Transaction, Transaction]:
        """Move money between two open accounts with the same currency."""
        if source.id == target.id:
            raise InvalidRequest("cannot transfer to the same account")
        if source.currency != target.currency:
            raise InvalidRequest("currencies differ")
        self._check_open(source)
        self._check_open(target)
        amount = money.parse_amount(amount_text)
        if self.db.balance(source.id) < amount:
            raise InsufficientFunds("balance too low")
        label = memo or f"transfer {source.id}->{target.id}"
        debit = self.db.insert_transaction(source.id, -amount, label)
        credit = self.db.insert_transaction(target.id, amount, label)
        return debit, credit

    def statement(self, account: Account, request: PageRequest) -> Statement:
        """One page of transactions, oldest first, plus the current balance."""
        rows = self.db.list_transactions(account.id, request.limit, request.offset)
        return Statement(
            account=account,
            balance_cents=self.db.balance(account.id),
            transactions=rows,
            total=self.db.count_transactions(account.id),
            page=request.page,
            per_page=request.per_page,
        )

    def close(self, account: Account) -> None:
        """Close an account. Only an account with a zero balance can be closed."""
        if self.db.balance(account.id) != 0:
            raise InvalidRequest("balance must be zero before closing")
        self.db.close_account(account.id)

    def monthly_fee(self, account: Account) -> int:
        """Full monthly fee of the account's plan, in cents."""
        return PLANS[account.plan].monthly_cents

    @staticmethod
    def _check_open(account: Account) -> None:
        if account.closed:
            raise InvalidRequest("account is closed")
