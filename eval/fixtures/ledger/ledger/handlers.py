"""Request handlers: turn a ``Request`` into a ``(status, body)`` pair.

Every handler loads the account, checks permissions with ``ledger.auth`` and
only then calls the service. Errors become JSON-style bodies in ``dispatch``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ledger import auth, money
from ledger.errors import InvalidRequest, LedgerError
from ledger.models import Account, Transaction, User
from ledger.pagination import parse_page_request
from ledger.service import LedgerService

Body = dict[str, Any]
Response = tuple[int, Body]


@dataclass
class Request:
    user: User
    params: dict[str, str] = field(default_factory=dict)  # path and query parameters
    body: dict[str, str] = field(default_factory=dict)


def _account_id(request: Request, name: str = "account_id") -> int:
    raw = request.params.get(name, "")
    if not (raw.isascii() and raw.isdigit()):
        raise InvalidRequest(f"{name} must be a positive integer")
    return int(raw)


def _render_transaction(tx: Transaction, currency: str) -> Body:
    return {
        "id": tx.id,
        "amount": money.format_amount(tx.amount_cents, currency),
        "amount_cents": tx.amount_cents,
        "memo": tx.memo,
        "created_at": tx.created_at,
    }


def _render_account(account: Account, balance_cents: int) -> Body:
    return {
        "id": account.id,
        "name": account.name,
        "currency": account.currency,
        "plan": account.plan,
        "closed": account.closed,
        "balance": money.format_amount(balance_cents, account.currency),
    }


def get_account(service: LedgerService, request: Request) -> Response:
    account = service.get_account(_account_id(request))
    auth.require_view(request.user, account)
    return 200, _render_account(account, service.balance(account))


def list_transactions(service: LedgerService, request: Request) -> Response:
    account = service.get_account(_account_id(request))
    auth.require_view(request.user, account)
    page_request = parse_page_request(request.params)
    statement = service.statement(account, page_request)
    items = [_render_transaction(tx, account.currency) for tx in statement.transactions]
    return 200, {
        "account": _render_account(account, statement.balance_cents),
        "items": items,
        "page": statement.page,
        "per_page": statement.per_page,
        "total": statement.total,
    }


def deposit(service: LedgerService, request: Request) -> Response:
    account = service.get_account(_account_id(request))
    auth.require_modify(request.user, account)
    tx = service.deposit(account, request.body.get("amount", ""), request.body.get("memo", ""))
    return 201, _render_transaction(tx, account.currency)


def withdraw(service: LedgerService, request: Request) -> Response:
    account = service.get_account(_account_id(request))
    auth.require_modify(request.user, account)
    tx = service.withdraw(account, request.body.get("amount", ""), request.body.get("memo", ""))
    return 201, _render_transaction(tx, account.currency)


def transfer(service: LedgerService, request: Request) -> Response:
    source = service.get_account(_account_id(request))
    target = service.get_account(_account_id(request, "target_id"))
    auth.require_modify(request.user, source)
    debit, _credit = service.transfer(
        source, target, request.body.get("amount", ""), request.body.get("memo", "")
    )
    return 201, _render_transaction(debit, source.currency)


def close_account(service: LedgerService, request: Request) -> Response:
    account = service.get_account(_account_id(request))
    auth.require_modify(request.user, account)
    service.close(account)
    return 200, {"id": account.id, "closed": True}


Handler = Callable[[LedgerService, Request], Response]

ROUTES: dict[str, Handler] = {
    "GET /accounts/{account_id}": get_account,
    "GET /accounts/{account_id}/transactions": list_transactions,
    "POST /accounts/{account_id}/deposits": deposit,
    "POST /accounts/{account_id}/withdrawals": withdraw,
    "POST /accounts/{account_id}/transfers": transfer,
    "POST /accounts/{account_id}/close": close_account,
}


def dispatch(service: LedgerService, route: str, request: Request) -> Response:
    """Run the handler for ``route`` and map ledger errors to status codes."""
    handler = ROUTES.get(route)
    if handler is None:
        return 404, {"error": f"no route {route!r}"}
    try:
        return handler(service, request)
    except LedgerError as exc:
        return exc.status, {"error": str(exc)}
