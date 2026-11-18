"""GitHub REST/GraphQL HTTP client with retries and rate-limit handling.

Reads (GET and GraphQL queries) retry on 5xx and connection errors. Writes and
mutations are never retried here on such errors, because GitHub may have applied
them; the whole ``publish`` is idempotent and is retried by the activity instead.
A rate-limited request was rejected by GitHub before it did anything, so every
method waits and repeats it: a rate limit makes a run slower, it does not fail it.
The waits for one request may add up to ``MAX_RATE_WAIT_TOTAL_S``, and never end
after ``wait_until_epoch`` (in a run: shortly before the run's hard deadline). A wait
past either bound raises ``GitHubRateLimited`` at once.

The token is only ever placed in the ``Authorization`` header. Errors name the
method and path, never headers.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from morning_star_review._version import __version__
from morning_star_review.core.errors import MsreviewError, MsreviewGitHubLasting
from morning_star_review.core.redact import redact

__all__ = [
    "API_BASE",
    "GitHubGraphQLError",
    "GitHubHttp",
    "GitHubLasting",
    "GitHubRateLimited",
    "GitHubTransient",
    "set_test_transport",
]

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
USER_AGENT = f"morning-star-code-reviews/{__version__}"
MAX_RATE_WAIT_S = 60.0  # assumed wait for an exhausted limit without a reset time
MAX_RATE_WAIT_TOTAL_S = 900.0  # rate-limit waits of one request, in total
MIN_RATE_WAIT_S = 1.0  # a "Retry-After: 0" still waits (and counts) this long
READ_RETRY_DELAYS_S = (1.0, 4.0, 10.0)  # 3 retries over ~15 s
_TRANSIENT_4XX = frozenset({408, 409, 425})
_SECONDARY = re.compile(r"secondary rate limit|abuse detection", re.IGNORECASE)
_MESSAGE_LIMIT = 300

Sleep = Callable[[float], Awaitable[None]]

_test_transport: httpx.AsyncBaseTransport | None = None


def set_test_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Make every new ``GitHubHttp`` without an explicit transport use ``transport``.

    Test hook for in-process workflow tests (DESIGN 15.4); ``None`` restores the
    network transport.
    """
    global _test_transport
    _test_transport = transport


class GitHubTransient(MsreviewError):  # noqa: N818 - reads like the condition it reports
    """5xx, connection error or 408/409/425 that a later attempt may not hit (retryable)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubRateLimited(MsreviewError):  # noqa: N818 - DESIGN name
    """Rate limited for longer than we wait in-process (retryable at activity level)."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class GitHubLasting(MsreviewGitHubLasting):
    """A 4xx that retrying will not fix. ``status`` and GitHub's message are kept."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        github_message: str = "",
        errors: list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.github_message = github_message
        self.errors = errors or []

    def mentions(self, *words: str) -> bool:
        """True when GitHub's message or error details mention any of ``words``."""
        text = (self.github_message + " " + repr(self.errors)).lower()
        return any(w.lower() in text for w in words)


class GitHubGraphQLError(MsreviewError):
    """GraphQL ``errors`` in an HTTP 200 response. ``types`` holds e.g. ``FORBIDDEN``."""

    def __init__(self, message: str, types: list[str]) -> None:
        super().__init__(message)
        self.types = types

    @property
    def forbidden(self) -> bool:
        return "FORBIDDEN" in self.types


def _cap(text: str, limit: int = _MESSAGE_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _github_message(resp: httpx.Response) -> tuple[str, list[Any]]:
    data = _json_or_none(resp)
    if isinstance(data, dict):
        message = data.get("message")
        errors = data.get("errors")
        return (
            str(message) if message is not None else "",
            errors if isinstance(errors, list) else [],
        )
    return "", []


def _parse_retry_after(value: str, now: float) -> float | None:
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - now)


def rate_limit_wait(resp: httpx.Response, now: float | None = None) -> float | None:
    """Seconds to wait if ``resp`` is a rate-limit response, else ``None``.

    429 always counts. A 403 counts with ``Retry-After``, with
    ``X-RateLimit-Remaining: 0`` (wait until ``X-RateLimit-Reset``) or with the
    secondary-limit wording; other 403s are permission errors.
    """
    if resp.status_code not in (403, 429):
        return None
    current = time.time() if now is None else now
    retry_after = resp.headers.get("retry-after")
    if retry_after is not None:
        parsed = _parse_retry_after(retry_after, current)
        if parsed is not None:
            return parsed
    if resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "")
        if reset.isdigit():
            return max(1.0, float(reset) - current + 1.0)
        return MAX_RATE_WAIT_S
    message, _ = _github_message(resp)
    if resp.status_code == 429 or _SECONDARY.search(message):
        return 60.0  # GitHub's advice for secondary limits without a header
    return None


def _run_wait_until() -> float | None:
    """``RunClock.github_wait_until()`` of the current run context, if it has a clock."""
    try:
        # Lazy: the client is also used outside a run (CLI helpers, ci-publish).
        from morning_star_review.core import runclock
        from morning_star_review.core.runctx import run_context

        clock = runclock.from_context(run_context())
    except Exception:  # no run context in this process
        return None
    return clock.github_wait_until() if clock is not None else None


_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def next_link(resp: httpx.Response) -> str | None:
    """The ``rel="next"`` URL of a paginated response, if any."""
    match = _NEXT_LINK.search(resp.headers.get("link", ""))
    return match.group(1) if match else None


class GitHubHttp:
    """Async GitHub API client. Use as ``async with GitHubHttp(token) as gh``."""

    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        timeout_s: float = 30.0,
        wait_until_epoch: float | None = None,
    ) -> None:
        """``wait_until_epoch``: no rate-limit wait ends after this wall-clock time.

        ``None`` takes it from the run context when one with a run clock is set
        (``RunClock.github_wait_until``), else rate-limit waits are only bounded by
        ``MAX_RATE_WAIT_TOTAL_S`` per request.
        """
        self._token = token
        self._wait_until = wait_until_epoch if wait_until_epoch is not None else _run_wait_until()
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).netloc
        self._scheme = urlsplit(self._base).scheme
        self._sleep: Sleep = sleep or asyncio.sleep
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            transport=transport or _test_transport,
            timeout=timeout_s,
            follow_redirects=False,
        )

    def __repr__(self) -> str:  # never show the token
        return f"GitHubHttp(base_url={self._base!r}, token={'set' if self._token else 'none'})"

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubHttp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ requests

    def _redact(self, text: str) -> str:
        return redact(text, [self._token] if self._token else [])

    def _check_url(self, url: str) -> None:
        # Pagination follows Link headers; never send the token to another host, or
        # to the same host over a downgraded scheme.
        parts = urlsplit(url)
        if parts.netloc and parts.netloc != self._host:
            raise GitHubLasting(f"refusing to follow a link to {parts.netloc}", status=0)
        if parts.scheme and parts.scheme != self._scheme:
            raise GitHubLasting(f"refusing to follow a {parts.scheme}: link", status=0)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        retry_errors: bool,
    ) -> httpx.Response:
        """One logical request: rate-limit waits always, error retries only if asked."""
        self._check_url(url)
        label = f"{method} {urlsplit(url).path or url}"
        attempt = 0
        waited = 0.0
        while True:
            try:
                resp = await self._client.request(method, url, params=params, json=body)
            except httpx.TransportError as exc:
                if retry_errors and attempt < len(READ_RETRY_DELAYS_S):
                    await self._sleep(READ_RETRY_DELAYS_S[attempt])
                    attempt += 1
                    continue
                raise GitHubTransient(f"{label}: {type(exc).__name__}") from None
            wait = rate_limit_wait(resp)
            if wait is not None:
                wait = max(wait, MIN_RATE_WAIT_S)
                self._check_rate_wait(label, wait, waited)
                log.warning("GitHub rate limit on %s; waiting %.0fs", label, wait)
                await self._sleep(wait)
                waited += wait
                continue
            status = resp.status_code
            if status >= 500 or status in _TRANSIENT_4XX:
                if retry_errors and attempt < len(READ_RETRY_DELAYS_S):
                    await self._sleep(READ_RETRY_DELAYS_S[attempt])
                    attempt += 1
                    continue
                raise GitHubTransient(f"{label}: HTTP {status}", status=status)
            if status >= 400:
                message, errors = _github_message(resp)
                text = self._redact(_cap(message))
                raise GitHubLasting(
                    f"{label}: HTTP {status}" + (f" ({text})" if text else ""),
                    status=status,
                    github_message=text,
                    errors=errors[:20],
                )
            if 300 <= status < 400:
                raise GitHubLasting(f"{label}: unexpected redirect {status}", status=status)
            return resp

    def _check_rate_wait(self, label: str, wait: float, waited: float) -> None:
        """Raise ``GitHubRateLimited`` when waiting ``wait`` more seconds is not allowed."""
        if waited + wait > MAX_RATE_WAIT_TOTAL_S:
            raise GitHubRateLimited(f"{label}: rate limited for {wait:.0f}s", retry_after_s=wait)
        if self._wait_until is not None and time.time() + wait > self._wait_until:
            raise GitHubRateLimited(
                f"{label}: rate limited for {wait:.0f}s, past the run's time limit",
                retry_after_s=wait,
            )

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None, *, allow_404: bool = False
    ) -> Any:
        """GET and decode JSON. With ``allow_404``, a 404 returns ``None``."""
        try:
            resp = await self._send("GET", path, params=params, retry_errors=True)
        except GitHubLasting as exc:
            if allow_404 and exc.status == 404:
                return None
            raise
        return _json_or_none(resp)

    async def paginate(
        self, path: str, params: dict[str, Any] | None = None, max_pages: int = 30
    ) -> list[Any]:
        """All items of a list endpoint (``per_page=100``), following ``Link: next``."""
        query = {"per_page": 100, **(params or {})}
        items: list[Any] = []
        url: str | None = path
        pages = 0
        while url is not None and pages < max_pages:
            resp = await self._send(
                "GET", url, params=query if pages == 0 else None, retry_errors=True
            )
            data = _json_or_none(resp)
            if not isinstance(data, list):
                raise GitHubLasting(f"GET {path}: expected a list", status=resp.status_code)
            items.extend(data)
            url = next_link(resp)
            pages += 1
        return items

    async def post_json(self, path: str, body: dict[str, Any]) -> Any:
        resp = await self._send("POST", path, body=body, retry_errors=False)
        return _json_or_none(resp)

    async def patch_json(self, path: str, body: dict[str, Any]) -> Any:
        resp = await self._send("PATCH", path, body=body, retry_errors=False)
        return _json_or_none(resp)

    async def delete(self, path: str, *, allow_404: bool = True) -> bool:
        """DELETE; returns False when the resource was already gone (404)."""
        try:
            await self._send("DELETE", path, retry_errors=False)
        except GitHubLasting as exc:
            if allow_404 and exc.status == 404:
                return False
            raise
        return True

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a query or mutation; raise ``GitHubGraphQLError`` on GraphQL errors.

        Queries are retried like GETs; mutations are not retried on errors.
        """
        if not self._token:
            raise GitHubGraphQLError("GraphQL needs a token", types=["UNAUTHENTICATED"])
        is_mutation = query.lstrip().startswith("mutation")
        resp = await self._send(
            "POST",
            "/graphql",
            body={"query": query, "variables": variables},
            retry_errors=not is_mutation,
        )
        data = _json_or_none(resp)
        if not isinstance(data, dict):
            raise GitHubGraphQLError("GraphQL response is not an object", types=["INVALID"])
        errors = data.get("errors")
        if errors:
            items = errors if isinstance(errors, list) else [errors]
            types = [str(e.get("type", "")) for e in items if isinstance(e, dict)]
            messages = [str(e.get("message", "")) for e in items if isinstance(e, dict)]
            text = self._redact(_cap("; ".join(m for m in messages if m) or "error"))
            raise GitHubGraphQLError(f"GraphQL: {text}", types=[t for t in types if t])
        result = data.get("data")
        if not isinstance(result, dict):
            raise GitHubGraphQLError("GraphQL response has no data", types=["INVALID"])
        return result
