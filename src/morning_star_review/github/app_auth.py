"""GitHub App JWT and installation token minting.

Only the mint child calls these with a real private key (DESIGN 9.2): the key never
enters the driver or worker process. The App-level calls take a ``GitHubHttp`` whose
token is the App JWT (``GitHubHttp(make_app_jwt(...))``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import jwt

from morning_star_review.github.http import GitHubHttp, GitHubLasting

__all__ = [
    "OPTIONAL_PERMISSIONS",
    "REQUIRED_PERMISSIONS",
    "AppInfo",
    "Installation",
    "MintedToken",
    "find_installation",
    "get_app",
    "has_level",
    "make_app_jwt",
    "mint_installation_token",
    "missing_permissions",
    "requested_permissions",
]

# What a review needs. Issues: write is optional (verdict labels, ADDENDUM C).
REQUIRED_PERMISSIONS: dict[str, str] = {"contents": "read", "pull_requests": "write"}
OPTIONAL_PERMISSIONS: dict[str, str] = {"issues": "write", "metadata": "read"}
_LEVEL = {"read": 1, "write": 2, "admin": 3}


@dataclass(frozen=True)
class AppInfo:
    slug: str
    name: str

    @property
    def bot_login(self) -> str:
        return f"{self.slug}[bot]"


@dataclass(frozen=True)
class Installation:
    id: int
    account: str
    permissions: dict[str, str] = field(default_factory=dict)
    repository_selection: str = ""


@dataclass(frozen=True)
class MintedToken:
    token: str = field(repr=False)
    expires_at: str
    permissions: dict[str, str] = field(default_factory=dict)


def make_app_jwt(client_id: str, pem: bytes, now: int) -> str:
    """RS256 App JWT: ``iss`` = client id, valid from ``now - 60`` for 10 minutes.

    The backdated ``iat`` absorbs clock skew; ``exp`` stays under GitHub's 10-minute
    maximum.
    """
    if not client_id.strip():
        raise ValueError("client id is empty")
    payload = {"iat": now - 60, "exp": now + 540, "iss": client_id.strip()}
    return jwt.encode(payload, pem, algorithm="RS256")


def has_level(granted: dict[str, str], name: str, level: str) -> bool:
    """True when ``granted[name]`` is at least ``level`` (read < write < admin)."""
    return _LEVEL.get(granted.get(name, ""), 0) >= _LEVEL[level]


def requested_permissions(granted: dict[str, str]) -> dict[str, str]:
    """Permissions to request for a token: the required ones plus optional granted ones.

    Requesting a permission the installation lacks makes GitHub refuse the token, so
    optional ones are only asked for when granted.
    """
    out = dict(REQUIRED_PERMISSIONS)
    for name, level in OPTIONAL_PERMISSIONS.items():
        if has_level(granted, name, level):
            out[name] = level
    return out


def missing_permissions(granted: dict[str, str]) -> list[str]:
    """Required permissions the installation does not grant (``"pull_requests: write"``)."""
    return [
        f"{name}: {level}"
        for name, level in REQUIRED_PERMISSIONS.items()
        if not has_level(granted, name, level)
    ]


def _str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


async def get_app(client: GitHubHttp) -> AppInfo:
    """``GET /app`` with a JWT client."""
    data = await client.get_json("/app")
    if not isinstance(data, dict) or not data.get("slug"):
        raise GitHubLasting("GET /app: unexpected response", status=200)
    return AppInfo(slug=str(data["slug"]), name=str(data.get("name") or data["slug"]))


async def find_installation(client: GitHubHttp, owner: str, repo: str) -> Installation | None:
    """``GET /repos/{o}/{r}/installation`` with a JWT client; ``None`` when not installed."""
    data = await client.get_json(
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation", allow_404=True
    )
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise GitHubLasting("GET installation: unexpected response", status=200)
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    return Installation(
        id=int(data["id"]),
        account=str((account or {}).get("login", "")),
        permissions=_str_dict(data.get("permissions")),
        repository_selection=str(data.get("repository_selection", "")),
    )


async def mint_installation_token(
    client: GitHubHttp,
    installation_id: int,
    repo: str,
    permissions: dict[str, str],
) -> MintedToken:
    """``POST /app/installations/{id}/access_tokens`` scoped to one repository.

    ``repo`` is the repository name without the owner.
    """
    data = await client.post_json(
        f"/app/installations/{installation_id}/access_tokens",
        {"repositories": [repo], "permissions": permissions},
    )
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise GitHubLasting("POST access_tokens: unexpected response", status=201)
    return MintedToken(
        token=data["token"],
        expires_at=str(data.get("expires_at", "")),
        permissions=_str_dict(data.get("permissions")),
    )
