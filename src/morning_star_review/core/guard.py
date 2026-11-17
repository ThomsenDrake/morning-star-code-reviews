"""The check every activity body runs first (DESIGN 5.3), and a safe heartbeat.

``guard`` rejects tasks that do not belong to this run: a foreign execution routed to
our task queue, a request that differs from the one the driver pinned, or a payload
that this process did not seal. temporalio is imported lazily so that ``core`` stays
importable without it.
"""

from __future__ import annotations

from typing import Any

from morning_star_review.core.contract import ReviewRequest, Sealed
from morning_star_review.core.errors import MsreviewPinRejected, MsreviewSealInvalid, for_activity
from morning_star_review.core.runctx import RunContext, run_context
from morning_star_review.core.seal import request_digest, verify

__all__ = ["guard", "heartbeat", "in_activity"]


def in_activity() -> bool:
    """True inside a Temporal activity (False in local in-process mode)."""
    try:
        from temporalio import activity
    except ImportError:  # pragma: no cover - temporalio is a worker dependency
        return False
    return activity.in_activity()


def _current_workflow_id() -> str | None:
    from temporalio import activity

    # None for standalone activities, which never belong to one of our runs.
    return activity.info().workflow_id


def _check(req: ReviewRequest, sealed: tuple[Sealed | None, ...]) -> RunContext:
    ctx = run_context()
    if ctx.on_worker:
        if not in_activity():
            raise MsreviewPinRejected("guard called outside an activity on a worker")
        if _current_workflow_id() != ctx.execution_id:
            raise MsreviewPinRejected("task belongs to a different execution")
    if request_digest(req) != ctx.request_sha256:
        raise MsreviewPinRejected("request does not match the pinned request")
    for item in sealed:
        if item is not None and not verify(item, ctx):
            raise MsreviewSealInvalid(f"{type(item).__name__} is not sealed by this run")
    return ctx


def guard(req: ReviewRequest, *sealed: Sealed | None) -> RunContext:
    """Check the pins and seals; return the run context.

    Raises ``MsreviewPinRejected`` / ``MsreviewSealInvalid`` (or ``MsreviewNoRunContext``).
    Inside an activity these become non-retryable ``ApplicationError``s whose ``type``
    is the class name, so Temporal does not retry a task that can never succeed.
    """
    try:
        return _check(req, sealed)
    except Exception as exc:
        converted = for_activity(exc)
        if converted is exc:
            raise
        raise converted from exc


def heartbeat(details: dict[str, Any] | None = None) -> None:
    """Send a heartbeat when running inside an activity; do nothing otherwise."""
    if not in_activity():
        return
    from temporalio import activity

    if details is None:
        activity.heartbeat()
    else:
        activity.heartbeat(details)
