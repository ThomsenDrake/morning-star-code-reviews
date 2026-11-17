"""Repository permission checks: who may dismiss a finding by resolving its thread."""

from __future__ import annotations

import logging
from urllib.parse import quote

from morning_star_review.github.http import GitHubHttp, GitHubLasting

__all__ = ["DISMISS_LEVELS", "PermissionCache", "can_dismiss", "collaborator_permission"]

# ``permission`` is the legacy field (admin/write/read/none: maintain shows as write,
# triage as read); ``role_name`` carries the precise role.
DISMISS_LEVELS = frozenset({"admin", "maintain", "write"})

log = logging.getLogger(__name__)


async def collaborator_permission(gh: GitHubHttp, owner: str, repo: str, user: str) -> str:
    """The user's role on the repository: ``admin``, ``maintain``, ``write``, ``triage``,
    ``read`` or ``none`` (also for unknown users)."""
    data = await gh.get_json(
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/collaborators/{quote(user, safe='')}/permission",
        allow_404=True,
    )
    if not isinstance(data, dict):
        return "none"
    role = data.get("role_name")
    if isinstance(role, str) and role in DISMISS_LEVELS | {"triage", "read"}:
        return role
    permission = data.get("permission")
    return permission if isinstance(permission, str) and permission else "none"


def can_dismiss(permission: str, user: str, pr_author: str, author_may_dismiss: bool) -> bool:
    """DESIGN 9.4: write access or more, and the PR author only if allowed."""
    if permission not in DISMISS_LEVELS:
        return False
    return author_may_dismiss or user.lower() != pr_author.lower()


class PermissionCache:
    """Caches ``collaborator_permission`` per user for one fetch."""

    def __init__(self, gh: GitHubHttp, owner: str, repo: str) -> None:
        self._gh = gh
        self._owner = owner
        self._repo = repo
        self._cache: dict[str, str] = {}

    async def get(self, user: str) -> str:
        """The user's role; ``none`` when GitHub refuses the lookup (fail closed).

        A refused lookup (e.g. 403 for a token without access to the endpoint) must
        not abort the whole review; it only means the resolution does not dismiss.
        """
        key = user.lower()
        if key not in self._cache:
            try:
                role = await collaborator_permission(self._gh, self._owner, self._repo, user)
            except GitHubLasting as exc:
                log.warning("permission lookup refused (HTTP %s); treating as none", exc.status)
                role = "none"
            self._cache[key] = role
        return self._cache[key]
