"""Authorization rules. Handlers call these before touching an account."""

from __future__ import annotations

from ledger.errors import Forbidden
from ledger.models import Account, User


def can_view_account(user: User, account: Account) -> bool:
    """Owners and admins may read an account."""
    return user.is_admin or account.owner_id == user.id


def can_modify_account(user: User, account: Account) -> bool:
    """Only the owner may move money, and never on a closed account."""
    return account.owner_id == user.id and not account.closed


def require_view(user: User, account: Account) -> None:
    if not can_view_account(user, account):
        raise Forbidden("you may not view this account")


def require_modify(user: User, account: Account) -> None:
    if not can_modify_account(user, account):
        raise Forbidden("you may not change this account")


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise Forbidden("administrators only")
