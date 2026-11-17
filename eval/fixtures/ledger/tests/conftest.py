from __future__ import annotations

from collections.abc import Iterator

import pytest

from ledger.db import Database
from ledger.models import Account, User
from ledger.service import LedgerService


@pytest.fixture
def service() -> Iterator[LedgerService]:
    db = Database()
    yield LedgerService(db)
    db.close()


@pytest.fixture
def alice(service: LedgerService) -> User:
    return service.db.create_user("alice")


@pytest.fixture
def bob(service: LedgerService) -> User:
    return service.db.create_user("bob")


@pytest.fixture
def admin(service: LedgerService) -> User:
    return service.db.create_user("root", is_admin=True)


@pytest.fixture
def account(service: LedgerService, alice: User) -> Account:
    return service.open_account(alice, "Main", "eur")
