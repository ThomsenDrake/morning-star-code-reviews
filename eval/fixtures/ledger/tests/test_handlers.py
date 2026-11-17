from __future__ import annotations

from ledger.handlers import Request, dispatch
from ledger.models import Account, User
from ledger.service import LedgerService


def _req(user: User, account: Account, **body: str) -> Request:
    return Request(user=user, params={"account_id": str(account.id)}, body=dict(body))


def test_owner_can_read(service: LedgerService, alice: User, account: Account) -> None:
    status, body = dispatch(service, "GET /accounts/{account_id}", _req(alice, account))
    assert status == 200
    assert body["balance"] == "€0.00"


def test_admin_can_read(service: LedgerService, admin: User, account: Account) -> None:
    status, _ = dispatch(service, "GET /accounts/{account_id}", _req(admin, account))
    assert status == 200


def test_stranger_cannot_read(service: LedgerService, bob: User, account: Account) -> None:
    status, body = dispatch(service, "GET /accounts/{account_id}", _req(bob, account))
    assert status == 403
    assert "error" in body


def test_admin_cannot_move_money(service: LedgerService, admin: User, account: Account) -> None:
    route = "POST /accounts/{account_id}/deposits"
    status, _ = dispatch(service, route, _req(admin, account, amount="5"))
    assert status == 403


def test_deposit_then_list(service: LedgerService, alice: User, account: Account) -> None:
    route = "POST /accounts/{account_id}/deposits"
    assert dispatch(service, route, _req(alice, account, amount="7.25"))[0] == 201
    request = _req(alice, account)
    request.params["per_page"] = "5"
    status, body = dispatch(service, "GET /accounts/{account_id}/transactions", request)
    assert status == 200
    assert body["total"] == 1
    assert body["items"][0]["amount"] == "€7.25"


def test_unknown_route(service: LedgerService, alice: User, account: Account) -> None:
    assert dispatch(service, "DELETE /accounts", _req(alice, account))[0] == 404


def test_bad_amount_is_400(service: LedgerService, alice: User, account: Account) -> None:
    route = "POST /accounts/{account_id}/deposits"
    assert dispatch(service, route, _req(alice, account, amount="lots"))[0] == 400
