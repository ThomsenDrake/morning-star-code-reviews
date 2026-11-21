"""Where the GitHub App is installed (asked as the App), and the commit of a release."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from morning_star_review.enable.appapi import AppApi
from morning_star_review.enable.gh import Gh, GhError
from morning_star_review.enable.template import ACTION_REPO, is_sha, normalize_version
from morning_star_review.enable.values import EnableUsageError

__all__ = ["InstallCheck", "Installations", "resolve_release"]


@dataclass(frozen=True)
class InstallCheck:
    installed: bool | None  # None: not checked (no App key)
    reason: str = ""  # why not, with the link that fixes it


class Installations:
    """Where the App is installed, asked as the App (``None``: no key, no checks)."""

    def __init__(self, api: AppApi | None) -> None:
        self.api = api

    def _slug(self) -> str | None:
        return self.api.app().slug if self.api is not None else None

    def check(self, full_name: str) -> InstallCheck:
        if self.api is None:
            return InstallCheck(None, "not checked without the App's private key")
        owner, repo = full_name.split("/", 1)
        if self.api.repo_installation(owner, repo) is not None:
            return InstallCheck(True)
        inst = next(
            (i for i in self.api.installations() if _account(i).get("login", "").lower()
             == owner.lower()),
            None,
        )  # fmt: skip
        if inst is None:
            slug = self._slug()
            link = (
                f"https://github.com/apps/{quote(slug)}/installations/new"
                if slug
                else "the App's page (Install App)"
            )
            return InstallCheck(
                False, f"the GitHub App is not installed on {owner}; add it: {link}"
            )
        inst_id = int(inst.get("id", 0))
        link = f"https://github.com/settings/installations/{inst_id}"
        if _account(inst).get("type") == "Organization":
            link = (
                f"https://github.com/organizations/{quote(owner)}/settings/installations/{inst_id}"
            )
        return InstallCheck(
            False, f"the GitHub App cannot see {full_name}; add the repository at {link}"
        )

    def repositories(self) -> list[str]:
        """Every repository the App's installations can see (``owner/repo``)."""
        if self.api is None:
            return []
        out: set[str] = set()
        for inst in self.api.installations():
            out.update(self.api.installation_repos(int(inst.get("id", 0))))
        return sorted(out, key=str.lower)


def _account(inst: dict[str, Any]) -> dict[str, str]:
    acc = inst.get("account")
    if not isinstance(acc, dict):
        return {}
    return {k: str(v) for k, v in acc.items() if isinstance(v, str)}


def resolve_release(gh: Gh, version: str) -> tuple[str, str]:
    """``(vX.Y.Z, commit SHA)`` of a release tag of the Action's public repository."""
    try:
        tag = normalize_version(version)
    except ValueError as exc:
        raise EnableUsageError(str(exc)) from None
    base = f"repos/{ACTION_REPO}/git"
    try:
        data = gh.api(f"{base}/ref/tags/{tag}", allow_404=True)
        if data is None:
            raise EnableUsageError(
                f"{ACTION_REPO} has no release {tag}; pass --version with a released tag"
            )
        obj = data.get("object") if isinstance(data, dict) else None
        for _ in range(3):  # an annotated tag points at a tag object, then the commit
            if not isinstance(obj, dict):
                break
            if obj.get("type") == "commit":
                sha = str(obj.get("sha", ""))
                if is_sha(sha):
                    return tag, sha
                break
            if obj.get("type") != "tag":
                break
            nested = gh.api(f"{base}/tags/{quote(str(obj.get('sha', '')))}")
            obj = nested.get("object") if isinstance(nested, dict) else None
    except GhError as exc:
        raise EnableUsageError(f"could not look up the release {tag}: {exc}") from None
    raise EnableUsageError(f"the tag {tag} of {ACTION_REPO} does not name a commit")
