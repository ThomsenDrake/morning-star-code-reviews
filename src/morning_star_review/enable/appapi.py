"""The GitHub App's own view (App JWT over ``httpx``): who it is and where it is installed.

``GET /user/installations`` does not accept the token of a ``gh`` login (only GitHub
App user tokens), so installations are read as the App: ``GET /app``,
``GET /repos/{o}/{r}/installation`` and ``GET /app/installations``. Listing an
installation's repositories needs an installation token: msreview mints one with only
``metadata: read``, uses it for ``GET /installation/repositories`` and revokes it.
The JWT and tokens go in request headers, never on a command line.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Protocol
from urllib.parse import quote

from morning_star_review.enable.gh import GhError
from morning_star_review.enable.values import AppIdentity, EnableUsageError

__all__ = ["AppApi", "HttpAppApi"]

_JWT_REUSE_S = 300  # a JWT lives 10 minutes (make_app_jwt); renew it after 5
_MAX_PAGES = 50


class AppApi(Protocol):
    def app(self) -> AppIdentity: ...

    def repo_installation(self, owner: str, repo: str) -> dict[str, Any] | None: ...

    def installations(self) -> list[dict[str, Any]]: ...

    def installation_repos(self, installation_id: int) -> list[str]: ...

    def close(self) -> None: ...


class HttpAppApi:
    """``AppApi`` over ``https://api.github.com`` (20-second timeouts)."""

    def __init__(
        self,
        client_id: str,
        private_key: bytes,
        *,
        base_url: str | None = None,
        transport: Any = None,  # httpx.BaseTransport (tests)
    ) -> None:
        import httpx

        from morning_star_review.github.http import API_BASE, USER_AGENT

        self._client_id = client_id
        self._key = bytearray(private_key)
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._app: AppIdentity | None = None
        self._installations: list[dict[str, Any]] | None = None
        self._http = httpx.Client(
            base_url=base_url or API_BASE,
            timeout=20.0,
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )

    def __repr__(self) -> str:  # never show the key or a token
        return f"HttpAppApi(client_id={self._client_id!r})"

    def close(self) -> None:
        self._key[:] = b"\0" * len(self._key)
        self._jwt = None
        self._http.close()

    def _bearer(self) -> str:
        from morning_star_review.github.app_auth import make_app_jwt

        now = time.time()
        if self._jwt is None or now - self._jwt_at > _JWT_REUSE_S:
            try:
                self._jwt = make_app_jwt(self._client_id, bytes(self._key), int(now))
            except Exception:
                raise EnableUsageError("the App private key could not sign a token") from None
            self._jwt_at = now
        return self._jwt

    def _call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        allow_404: bool = False,
        body: dict[str, Any] | None = None,
    ) -> Any:
        import httpx

        auth = {"Authorization": f"Bearer {token or self._bearer()}"}
        try:
            resp = self._http.request(method, path, headers=auth, json=body)
        except httpx.HTTPError as exc:
            raise GhError(f"{method} {path.split('?')[0]} failed ({type(exc).__name__})") from None
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code == 401 and token is None:
            raise EnableUsageError(
                "GitHub refused the App credentials (HTTP 401): the client id and the "
                "private key do not belong to the same App, or the key was deleted"
            )
        if resp.status_code >= 300:
            raise GhError(
                f"{method} {path.split('?')[0]}: HTTP {resp.status_code}", resp.status_code
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise GhError(f"{method} {path.split('?')[0]}: the response is not JSON") from None

    def _paged(self, path: str, key: str | None, *, token: str | None = None) -> list[Any]:
        out: list[Any] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self._call("GET", f"{path}?per_page=100&page={page}", token=token)
            items = data.get(key) if key and isinstance(data, dict) else data
            if not isinstance(items, list):
                raise GhError(f"GET {path}: unexpected response")
            out.extend(items)
            if len(items) < 100:
                break
        return out

    def app(self) -> AppIdentity:
        if self._app is None:
            data = self._call("GET", "/app")
            if not isinstance(data, dict) or not isinstance(data.get("slug"), str):
                raise GhError("GET /app: unexpected response")
            app_id = data.get("id")
            self._app = AppIdentity(
                client_id=self._client_id,
                slug=data["slug"],
                app_id=app_id if isinstance(app_id, int) else None,
                name=str(data.get("name") or data["slug"]),
            )
        return self._app

    def repo_installation(self, owner: str, repo: str) -> dict[str, Any] | None:
        path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation"
        data = self._call("GET", path, allow_404=True)
        return data if isinstance(data, dict) else None

    def installations(self) -> list[dict[str, Any]]:
        if self._installations is None:
            items = self._paged("/app/installations", None)
            self._installations = [i for i in items if isinstance(i, dict)]
        return self._installations

    def installation_repos(self, installation_id: int) -> list[str]:
        minted = self._call(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            body={"permissions": {"metadata": "read"}},
        )
        token = minted.get("token") if isinstance(minted, dict) else None
        if not isinstance(token, str):
            raise GhError("POST access_tokens: unexpected response")
        try:
            items = self._paged("/installation/repositories", "repositories", token=token)
        finally:
            # Best effort: the token would expire within the hour anyway.
            with contextlib.suppress(GhError):
                self._call("DELETE", "/installation/token", token=token)
            del token
        return [str(r["full_name"]) for r in items if isinstance(r, dict) and r.get("full_name")]
