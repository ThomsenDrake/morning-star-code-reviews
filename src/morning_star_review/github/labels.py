"""Status labels (ADDENDUM C): one living ``msreview: <status>`` label per PR.

The label says whether the PR is ready to merge: ``msreview: ready to merge`` (the
review is complete and no open finding is at or above the blocking severity),
``msreview: findings`` (open findings at or above it) or ``msreview: incomplete``.
``msreview: clean`` (before 0.3.0) is removed where it is still set.

Everything here is best effort: a failure returns a note for the outcome and never
fails the run. Labels need the Issues: write permission.
"""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import quote

from morning_star_review.github.http import (
    GitHubGraphQLError,
    GitHubHttp,
    GitHubLasting,
    GitHubRateLimited,
    GitHubTransient,
)

__all__ = [
    "LABEL_SPECS",
    "LEGACY_LABELS",
    "SAFE_TO_REVIEW",
    "STATUS_LABELS",
    "Status",
    "ensure_label",
    "remove_label",
    "set_status_label",
    "status_of",
]

log = logging.getLogger(__name__)

Status = Literal["ready", "findings", "incomplete"]

STATUS_LABELS: dict[Status, str] = {
    "ready": "msreview: ready to merge",
    "findings": "msreview: findings",
    "incomplete": "msreview: incomplete",
}
LEGACY_LABELS = ("msreview: clean",)  # removed wherever it is still set
SAFE_TO_REVIEW = "msreview: safe-to-review"

# name -> (color, description)
LABEL_SPECS: dict[str, tuple[str, str]] = {
    "msreview: ready to merge": (
        "2DA44E",
        "Morning Star Code Review: no open findings that block merging",
    ),
    "msreview: findings": ("D97706", "Morning Star Code Review: open findings to fix or dismiss"),
    "msreview: incomplete": ("6E7781", "Morning Star Code Review could not finish a full review"),
    SAFE_TO_REVIEW: ("0969DA", "Approve one Morning Star Code Review run for this head commit"),
}


def status_of(ready: bool, blocking: int) -> Status:
    """The label status: ready, else findings when some block merging, else incomplete."""
    if ready:
        return "ready"
    return "findings" if blocking else "incomplete"


_GITHUB_ERRORS = (GitHubLasting, GitHubTransient, GitHubRateLimited, GitHubGraphQLError)


def _repo_path(owner: str, repo: str) -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _name(label: str) -> str:
    return quote(label, safe="")


async def ensure_label(gh: GitHubHttp, owner: str, repo: str, name: str) -> None:
    """Create the repository label ``name`` if it does not exist (with our color)."""
    base = _repo_path(owner, repo)
    if await gh.get_json(f"{base}/labels/{_name(name)}", allow_404=True) is not None:
        return
    color, description = LABEL_SPECS.get(name, ("6E7781", ""))
    try:
        await gh.post_json(
            f"{base}/labels", {"name": name, "color": color, "description": description}
        )
    except GitHubLasting as exc:
        # Someone created it between our GET and POST: fine.
        if not (exc.status == 422 and exc.mentions("already_exists", "already exists")):
            raise


async def current_labels(gh: GitHubHttp, owner: str, repo: str, number: int) -> list[str]:
    items = await gh.paginate(f"{_repo_path(owner, repo)}/issues/{number}/labels", max_pages=3)
    return [str(i["name"]) for i in items if isinstance(i, dict) and i.get("name")]


async def remove_label(gh: GitHubHttp, owner: str, repo: str, number: int, name: str) -> bool:
    """Remove ``name`` from the PR; False when it was not there."""
    return await gh.delete(f"{_repo_path(owner, repo)}/issues/{number}/labels/{_name(name)}")


async def set_status_label(
    gh: GitHubHttp, owner: str, repo: str, number: int, status: Status
) -> list[str]:
    """Put the ``status`` label on the PR and remove the other status labels.

    Returns notes describing what failed (empty on success). Never raises for
    GitHub errors.
    """
    wanted = STATUS_LABELS[status]
    try:
        present = await current_labels(gh, owner, repo, number)
        if wanted not in present:
            await ensure_label(gh, owner, repo, wanted)
            await gh.post_json(
                f"{_repo_path(owner, repo)}/issues/{number}/labels", {"labels": [wanted]}
            )
        for other in (*STATUS_LABELS.values(), *LEGACY_LABELS):
            if other != wanted and other in present:
                await remove_label(gh, owner, repo, number, other)
    except _GITHUB_ERRORS as exc:
        log.warning("could not update the status label: %s", exc)
        return [f"could not update the status label ({type(exc).__name__}: {exc})"[:300]]
    return []
