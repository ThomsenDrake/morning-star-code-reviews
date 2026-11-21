"""The online check of the Mistral key (``--verify``, the default).

``GET /v1/workflows/workers/whoami``: no worker is started and nothing is stored. The
App's client id and private key are checked with ``appapi.HttpAppApi.app``.
"""

from __future__ import annotations

from typing import Protocol

from morning_star_review.enable.values import EnableUsageError

__all__ = ["HttpVerifier", "Verifier"]

_SKIP = "; pass --no-verify to store it without this check"


class Verifier(Protocol):
    def mistral(self, key: bytes) -> None: ...


class HttpVerifier:
    """The real checks (``httpx``, 20-second timeouts)."""

    def mistral(self, key: bytes) -> None:
        import httpx

        from morning_star_review.platform import API_BASE, WHOAMI_PATH

        try:
            resp = httpx.get(
                API_BASE + WHOAMI_PATH,
                headers={"Authorization": "Bearer " + key.decode("ascii")},
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise EnableUsageError(
                f"could not check the Mistral API key ({type(exc).__name__}){_SKIP}"
            ) from None
        if resp.status_code in (401, 403):
            raise EnableUsageError(
                f"Mistral refused the API key (HTTP {resp.status_code}): use a key of a "
                "workspace with Workflows access"
            )
        if resp.status_code != 200:
            raise EnableUsageError(
                f"could not check the Mistral API key (HTTP {resp.status_code}){_SKIP}"
            )
