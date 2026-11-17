"""GitHubHttp: headers, pagination, retries, rate limits and error mapping."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from morning_star_review.core.errors import MsreviewGitHubLasting
from morning_star_review.github import http as gh_http
from morning_star_review.github.http import (
    GitHubGraphQLError,
    GitHubHttp,
    GitHubLasting,
    GitHubRateLimited,
    GitHubTransient,
    rate_limit_wait,
)

TOKEN = "ghs_secretvalue0123456789"


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def client(handler: Callable[[httpx.Request], httpx.Response], token: str | None = TOKEN):
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    gh = GitHubHttp(token, transport=httpx.MockTransport(handler), sleep=sleep)
    return gh, slept


async def test_headers_and_json() -> None:
    rec = Recorder([httpx.Response(200, json={"ok": True})])
    gh, _ = client(rec)
    assert await gh.get_json("/repos/o/r") == {"ok": True}
    req = rec.requests[0]
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert req.headers["User-Agent"].startswith("morning-star-code-reviews/")
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(gh)


async def test_anonymous_has_no_auth_header() -> None:
    rec = Recorder([httpx.Response(200, json=[])])
    gh, _ = client(rec, token=None)
    await gh.get_json("/x")
    assert "Authorization" not in rec.requests[0].headers
    assert not gh.has_token
    with pytest.raises(GitHubGraphQLError):
        await gh.graphql("query { viewer { login } }", {})
    assert len(rec.requests) == 1  # no GraphQL request without a token


async def test_paginate_follows_link_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < 3:
            headers["link"] = (
                f'<https://api.github.com/items?page={page + 1}&per_page=100>; rel="next", '
                '<https://api.github.com/items?page=3>; rel="last"'
            )
        return httpx.Response(200, json=[page], headers=headers)

    gh, _ = client(handler)
    assert await gh.paginate("/items") == [1, 2, 3]
    assert await gh.paginate("/items", max_pages=2) == [1, 2]


async def test_paginate_refuses_foreign_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[1], headers={"link": '<https://evil.example/items?page=2>; rel="next"'}
        )

    gh, _ = client(handler)
    with pytest.raises(GitHubLasting):
        await gh.paginate("/items")


async def test_paginate_refuses_scheme_downgrade() -> None:
    rec = Recorder(
        [
            httpx.Response(
                200, json=[1], headers={"link": '<http://api.github.com/items?page=2>; rel="next"'}
            )
        ]
    )
    gh, _ = client(rec)
    with pytest.raises(GitHubLasting):
        await gh.paginate("/items")
    assert len(rec.requests) == 1  # the token never went out over plain http


async def test_get_retries_5xx_then_succeeds() -> None:
    rec = Recorder([httpx.Response(502), httpx.Response(503), httpx.Response(200, json=1)])
    gh, slept = client(rec)
    assert await gh.get_json("/x") == 1
    assert slept == [1.0, 4.0]


async def test_get_gives_up_after_three_retries() -> None:
    rec = Recorder([httpx.Response(500)])
    gh, slept = client(rec)
    with pytest.raises(GitHubTransient):
        await gh.get_json("/x")
    assert len(rec.requests) == 4
    assert sum(slept) == pytest.approx(15.0)


async def test_get_retries_connection_errors() -> None:
    rec = Recorder([httpx.ConnectError("boom"), httpx.Response(200, json=2)])
    gh, _ = client(rec)
    assert await gh.get_json("/x") == 2


async def test_writes_are_not_retried_on_5xx() -> None:
    rec = Recorder([httpx.Response(500)])
    gh, _ = client(rec)
    with pytest.raises(GitHubTransient):
        await gh.post_json("/x", {"a": 1})
    assert len(rec.requests) == 1
    with pytest.raises(GitHubTransient):
        await gh.graphql("mutation { x }", {})
    assert len(rec.requests) == 2


async def test_rate_limit_retry_after_short_wait() -> None:
    rec = Recorder(
        [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(201, json={"id": 1})]
    )
    gh, slept = client(rec)
    # A rate-limited write was rejected before it ran, so repeating it is safe.
    assert await gh.post_json("/x", {}) == {"id": 1}
    assert slept == [7.0]


async def test_rate_limit_long_wait_raises() -> None:
    rec = Recorder([httpx.Response(403, headers={"retry-after": "600"})])
    gh, slept = client(rec)
    with pytest.raises(GitHubRateLimited) as info:
        await gh.get_json("/x")
    assert info.value.retry_after_s == 600
    assert slept == []
    assert not GitHubRateLimited.non_retryable


def test_rate_limit_detection() -> None:
    now = 1_000_000.0
    remaining0 = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 30)}
    )
    assert rate_limit_wait(remaining0, now) == pytest.approx(31.0)
    secondary = httpx.Response(
        403, json={"message": "You have exceeded a secondary rate limit. Please wait."}
    )
    assert rate_limit_wait(secondary, now) == 60.0
    forbidden = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    assert rate_limit_wait(forbidden, now) is None
    assert rate_limit_wait(httpx.Response(200), now) is None


async def test_repeated_rate_limits_give_up() -> None:
    rec = Recorder([httpx.Response(429, headers={"retry-after": "1"})])
    gh, slept = client(rec)
    with pytest.raises(GitHubRateLimited):
        await gh.get_json("/x")
    assert len(slept) == gh_http.MAX_RATE_WAITS


async def test_4xx_is_lasting_and_redacted() -> None:
    rec = Recorder([httpx.Response(403, json={"message": f"bad token {TOKEN}", "errors": []})])
    gh, _ = client(rec)
    with pytest.raises(GitHubLasting) as info:
        await gh.get_json("/x")
    assert info.value.status == 403
    assert isinstance(info.value, MsreviewGitHubLasting)
    assert info.value.non_retryable
    assert TOKEN not in str(info.value)
    assert len(rec.requests) == 1


async def test_allow_404_and_delete() -> None:
    rec = Recorder([httpx.Response(404, json={"message": "Not Found"})])
    gh, _ = client(rec)
    assert await gh.get_json("/x", allow_404=True) is None
    assert await gh.delete("/x") is False
    with pytest.raises(GitHubLasting):
        await gh.get_json("/x")


async def test_transient_4xx_codes_retry() -> None:
    rec = Recorder([httpx.Response(409), httpx.Response(200, json=3)])
    gh, _ = client(rec)
    assert await gh.get_json("/x") == 3


async def test_graphql_errors_carry_types() -> None:
    body = {"data": None, "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}
    rec = Recorder([httpx.Response(200, json=body)])
    gh, _ = client(rec)
    with pytest.raises(GitHubGraphQLError) as info:
        await gh.graphql("mutation($id: ID!) { x }", {"id": "1"})
    assert info.value.forbidden
    assert info.value.types == ["FORBIDDEN"]


async def test_graphql_query_returns_data_and_retries() -> None:
    rec = Recorder([httpx.Response(502), httpx.Response(200, json={"data": {"a": 1}})])
    gh, _ = client(rec)
    assert await gh.graphql("query { a }", {}) == {"a": 1}


async def test_set_test_transport_is_used() -> None:
    gh_http.set_test_transport(httpx.MockTransport(lambda r: httpx.Response(200, json="fake")))
    async with GitHubHttp(None) as gh:
        assert await gh.get_json("/anything") == "fake"
