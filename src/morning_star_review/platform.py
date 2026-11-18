"""A thin adapter over the Mistral Workflows API, so the driver can run against a fake.

Only the calls the driver needs: whoami, deployment status, start, status, terminate
and the list of running executions for a pull request. ``MistralPlatform`` uses the
``mistralai`` client (imported lazily); tests use their own ``Platform``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

__all__ = [
    "API_BASE",
    "TERMINAL_STATUSES",
    "AlreadyStarted",
    "DeploymentState",
    "ExecutionState",
    "MistralPlatform",
    "Platform",
    "PlatformAuthError",
    "PlatformError",
    "Whoami",
    "error_status",
    "transient_status",
    "transient_worker_error",
]

API_BASE = "https://api.mistral.ai"
WHOAMI_PATH = "/v1/workflows/workers/whoami"
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"})
_DUPLICATE_START = "WF_1101"  # "Workflow execution with the same id already started"


def transient_status(status: int) -> bool:
    """An HTTP status worth retrying: 408, 429 (rate limit) and every 5xx."""
    return status in (408, 429) or status >= 500


def error_status(exc: object) -> int | None:
    """The HTTP status an exception carries (``status_code``, or an ``int``/``HTTPStatus``
    ``status`` as on the Workflows SDK's ``WorkflowsException``); None if it has none."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
    return None


def transient_worker_error(exc: object) -> bool:
    """True when the worker task failed with a rate limit or a server error.

    Spec registration in the Workflows SDK retries only 502/503/504 and raises a
    ``WorkflowsException`` whose ``status`` is the HTTP status (429 included); such a
    worker can be started again. An exception without a status is not transient.
    """
    status = error_status(exc)
    return status is not None and transient_status(status)


class PlatformError(Exception):
    """A Workflows API call failed; ``status`` is the HTTP status when there was one.

    Every adapter call raises it (or a subclass) for API failures; ``retryable`` is
    True for network errors (no status), 408, 429 and 5xx.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or transient_status(self.status)


class PlatformAuthError(PlatformError):
    """401/403: the key is wrong or has no Workflows access."""


class AlreadyStarted(PlatformError):  # noqa: N818 - reads like the condition it reports
    """409 ``WF_1101``: an execution with this id already exists (treat as started)."""


@dataclass(frozen=True)
class Whoami:
    namespace: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class DeploymentState:
    name: str
    is_active: bool
    active_worker_count: int
    last_heartbeat: datetime | None


@dataclass(frozen=True)
class ExecutionState:
    execution_id: str
    status: str
    result: Any = None
    deployment_name: str | None = None


class Platform(Protocol):
    async def whoami(self) -> Whoami: ...

    async def deployment(self, name: str) -> DeploymentState | None: ...

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None: ...

    async def execution(self, execution_id: str) -> ExecutionState: ...

    async def terminate(self, execution_id: str) -> None: ...

    async def running_for_pr(self, workflow: str, pr_url: str) -> list[str]: ...

    async def deployments(self, prefix: str, limit: int = 100) -> list[DeploymentState]: ...

    async def aclose(self) -> None: ...


def _status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _body_of(exc: BaseException) -> str:
    body = getattr(exc, "body", "")
    return body if isinstance(body, str) else ""


def _wrap(exc: BaseException, what: str) -> PlatformError:
    status = _status_of(exc)
    message = f"{what} failed" + (f" (HTTP {status})" if status else f": {type(exc).__name__}")
    if status in (401, 403):
        return PlatformAuthError(message, status)
    if status == 409 and _DUPLICATE_START in _body_of(exc):
        return AlreadyStarted(message, status)
    return PlatformError(message, status)


def _datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


class MistralPlatform:
    """``Platform`` over ``mistralai.client.Mistral`` (server ``https://api.mistral.ai``)."""

    def __init__(
        self,
        api_key: str,
        *,
        server_url: str = API_BASE,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        from mistralai.client import Mistral

        self._key = api_key
        self._server_url = server_url.rstrip("/")
        self._client = Mistral(api_key=api_key, server_url=self._server_url)
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=20.0))

    def __repr__(self) -> str:  # never show the key
        return f"MistralPlatform(server_url={self._server_url!r})"

    async def whoami(self) -> Whoami:
        async with self._http_factory() as http:
            try:
                resp = await http.get(
                    self._server_url + WHOAMI_PATH,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            except httpx.HTTPError as exc:
                raise PlatformError(f"whoami failed: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            raise PlatformAuthError(f"whoami failed (HTTP {resp.status_code})", resp.status_code)
        if resp.status_code != 200:
            raise PlatformError(f"whoami failed (HTTP {resp.status_code})", resp.status_code)
        # A 200 with an unusable body is not a transient error: not retryable.
        try:
            data = resp.json()
        except ValueError:
            raise PlatformError("whoami returned invalid JSON", resp.status_code) from None
        namespace = data.get("namespace") if isinstance(data, dict) else None
        if not isinstance(namespace, str) or not namespace:
            raise PlatformError("whoami returned no namespace", resp.status_code)
        return Whoami(namespace=namespace, raw=data)

    async def deployment(self, name: str) -> DeploymentState | None:
        try:
            d = await self._client.workflows.deployments.get_deployment_async(name=name)
        except Exception as exc:
            if _status_of(exc) == 404:
                return None
            raise _wrap(exc, "get_deployment") from None
        return DeploymentState(
            name=d.name,
            is_active=bool(d.is_active),
            active_worker_count=int(d.active_worker_count or 0),
            last_heartbeat=_datetime(d.last_heartbeat),
        )

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None:
        try:
            await self._client.workflows.execute_workflow_async(
                workflow_identifier=workflow,
                deployment_name=deployment,
                execution_id=execution_id,
                input=payload,
            )
        except Exception as exc:
            raise _wrap(exc, "execute_workflow") from None

    async def execution(self, execution_id: str) -> ExecutionState:
        try:
            e = await self._client.workflows.executions.get_workflow_execution_async(
                execution_id=execution_id
            )
        except Exception as exc:
            raise _wrap(exc, "get_workflow_execution") from None
        deployment = e.deployment_name if isinstance(e.deployment_name, str) else None
        return ExecutionState(
            execution_id=e.execution_id,
            status=str(e.status or "UNKNOWN"),
            result=e.result,
            deployment_name=deployment,
        )

    async def terminate(self, execution_id: str) -> None:
        try:
            await self._client.workflows.executions.terminate_workflow_execution_async(
                execution_id=execution_id
            )
        except Exception as exc:
            raise _wrap(exc, "terminate_workflow_execution") from None

    async def running_for_pr(self, workflow: str, pr_url: str) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        for _ in range(10):  # at most 500 executions; far more than one PR ever has
            try:
                page = await self._client.workflows.runs.list_runs_async(
                    workflow_identifier=workflow,
                    status="RUNNING",
                    search_key=[f"pr_url==:{pr_url}"],
                    page_size=50,
                    next_page_token=token,
                )
            except Exception as exc:
                raise _wrap(exc, "list_runs") from None
            if page is None:
                break
            ids.extend(e.execution_id for e in page.result.executions)
            nxt = page.result.next_page_token
            token = nxt if isinstance(nxt, str) and nxt else None
            if token is None:
                break
        return ids

    async def deployments(self, prefix: str, limit: int = 100) -> list[DeploymentState]:
        out: list[DeploymentState] = []
        cursor: str | None = None
        for _ in range(20):
            try:
                page = await self._client.workflows.deployments.list_deployments_async(
                    active_only=False, search=prefix, limit=limit, cursor=cursor
                )
            except Exception as exc:
                raise _wrap(exc, "list_deployments") from None
            items = getattr(page, "deployments", None) or getattr(page, "items", None) or []
            for d in items:
                name = getattr(d, "name", "")
                if isinstance(name, str) and name.startswith(prefix):
                    out.append(
                        DeploymentState(
                            name=name,
                            is_active=bool(getattr(d, "is_active", False)),
                            active_worker_count=int(getattr(d, "active_worker_count", 0) or 0),
                            last_heartbeat=_datetime(getattr(d, "last_heartbeat", None)),
                        )
                    )
            nxt = getattr(page, "next_cursor", None) or getattr(page, "cursor", None)
            cursor = nxt if isinstance(nxt, str) and nxt else None
            if cursor is None:
                break
        return out

    async def aclose(self) -> None:
        # The generated client manages its own httpx clients; nothing to close here.
        return None
