"""``MistralPlatform``: error mapping, whoami parsing and pagination (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from morning_star_review.platform import (
    AlreadyStarted,
    MistralPlatform,
    PlatformAuthError,
    PlatformError,
    _wrap,
)

KEY = "mk-test-not-a-real-key-0123456789"


class ApiError(Exception):
    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.body = body


def test_error_mapping() -> None:
    assert isinstance(_wrap(ApiError(401), "x"), PlatformAuthError)
    assert isinstance(_wrap(ApiError(403), "x"), PlatformAuthError)
    dup = _wrap(ApiError(409, '{"code":"WF_1101","detail":"already started"}'), "start")
    assert isinstance(dup, AlreadyStarted)
    other = _wrap(ApiError(409, '{"code":"OTHER"}'), "start")
    assert type(other) is PlatformError and not other.retryable
    assert _wrap(ApiError(503), "x").retryable
    assert _wrap(ApiError(429), "x").retryable
    assert _wrap(RuntimeError("boom"), "x").retryable  # no status: network-level problem
    assert "boom" not in str(_wrap(RuntimeError("boom"), "x"))


def platform_with(handler: Any) -> MistralPlatform:
    transport = httpx.MockTransport(handler)
    return MistralPlatform(KEY, http_factory=lambda: httpx.AsyncClient(transport=transport))


async def test_whoami_ok_and_header() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"namespace": "ns-1234567890", "tls": True})

    who = await platform_with(handler).whoami()
    assert who.namespace == "ns-1234567890"
    assert seen[0].url.path == "/v1/workflows/workers/whoami"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(platform_with(handler))


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={}), PlatformAuthError),
        (httpx.Response(500, text="oops"), PlatformError),
        (httpx.Response(200, text="not json"), PlatformError),
        (httpx.Response(200, json={"tls": True}), PlatformError),
    ],
)
async def test_whoami_errors(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        await platform_with(lambda req: response).whoami()


def _fake_client(**methods: Any) -> Any:
    ns = SimpleNamespace(**methods)
    return SimpleNamespace(
        workflows=SimpleNamespace(deployments=ns, executions=ns, runs=ns, **methods)
    )


async def test_deployment_404_is_none_and_fields_are_mapped() -> None:
    p = MistralPlatform(KEY)

    async def missing(**kw: Any) -> Any:
        raise ApiError(404)

    p._client = _fake_client(get_deployment_async=missing)
    assert await p.deployment("msreview-x") is None
    hb = datetime.now(UTC)

    async def found(**kw: Any) -> Any:
        return SimpleNamespace(
            name=kw["name"], is_active=True, active_worker_count=2, last_heartbeat=hb
        )

    p._client = _fake_client(get_deployment_async=found)
    d = await p.deployment("msreview-x")
    assert d is not None and (d.name, d.is_active, d.active_worker_count, d.last_heartbeat) == (
        "msreview-x",
        True,
        2,
        hb,
    )


async def test_running_for_pr_pages_and_filters() -> None:
    p = MistralPlatform(KEY)
    calls: list[dict[str, Any]] = []

    def page(ids: list[str], token: str | None) -> Any:
        execs = [SimpleNamespace(execution_id=i) for i in ids]
        return SimpleNamespace(result=SimpleNamespace(executions=execs, next_page_token=token))

    async def list_runs(**kw: Any) -> Any:
        calls.append(kw)
        return page(["a", "b"], "t2") if kw["next_page_token"] is None else page(["c"], None)

    p._client = _fake_client(list_runs_async=list_runs)
    assert await p.running_for_pr("wf", "https://github.com/o/r/pull/1") == ["a", "b", "c"]
    assert calls[0]["status"] == "RUNNING"
    assert calls[0]["search_key"] == ["pr_url==:https://github.com/o/r/pull/1"]


async def test_start_maps_duplicate_to_already_started() -> None:
    p = MistralPlatform(KEY)

    async def execute(**kw: Any) -> Any:
        raise ApiError(409, "WF_1101: Workflow execution with the same id already started")

    p._client = _fake_client(execute_workflow_async=execute)
    with pytest.raises(AlreadyStarted):
        await p.start(workflow="wf", deployment="d", execution_id="e", payload={})
