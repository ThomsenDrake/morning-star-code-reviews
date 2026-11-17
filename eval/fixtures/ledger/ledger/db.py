"""SQLite storage. Every query binds its values as parameters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from ledger.errors import NotFound
from ledger.models import Account, Transaction, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'basic',
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tx_by_account ON transactions(account_id, id);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        owner_id=row["owner_id"],
        name=row["name"],
        currency=row["currency"],
        plan=row["plan"],
        closed=bool(row["closed"]),
    )


def _row_to_transaction(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        account_id=row["account_id"],
        amount_cents=row["amount_cents"],
        memo=row["memo"],
        created_at=row["created_at"],
    )


class Database:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any exception."""
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # Users ---------------------------------------------------------------

    def create_user(self, name: str, is_admin: bool = False) -> User:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users (name, is_admin) VALUES (?, ?)", (name, int(is_admin))
            )
        return User(id=int(cur.lastrowid or 0), name=name, is_admin=is_admin)

    def get_user(self, user_id: int) -> User:
        row = self._conn.execute(
            "SELECT id, name, is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"user {user_id} not found")
        return User(id=row["id"], name=row["name"], is_admin=bool(row["is_admin"]))

    # Accounts ------------------------------------------------------------

    def create_account(self, owner_id: int, name: str, currency: str) -> Account:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (owner_id, name, currency) VALUES (?, ?, ?)",
                (owner_id, name, currency),
            )
        return self.get_account(int(cur.lastrowid or 0))

    def get_account(self, account_id: int) -> Account:
        row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise NotFound(f"account {account_id} not found")
        return _row_to_account(row)

    def set_plan(self, account_id: int, plan: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE accounts SET plan = ? WHERE id = ?", (plan, account_id))

    def close_account(self, account_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE accounts SET closed = 1 WHERE id = ?", (account_id,))

    # Transactions --------------------------------------------------------

    def insert_transaction(self, account_id: int, amount_cents: int, memo: str) -> Transaction:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO transactions (account_id, amount_cents, memo, created_at)"
                " VALUES (?, ?, ?, ?)",
                (account_id, amount_cents, memo, _now()),
            )
            row = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _row_to_transaction(row)

    def list_transactions(self, account_id: int, limit: int, offset: int) -> list[Transaction]:
        rows = self._conn.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (account_id, limit, offset),
        ).fetchall()
        return [_row_to_transaction(row) for row in rows]

    def count_transactions(self, account_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)
        ).fetchone()
        return int(row["n"])

    def balance(self, account_id: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row["total"])
