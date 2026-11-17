"""guard() and heartbeat() with and without a Temporal activity context (DESIGN 5.3)."""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from morning_star_review.core.contract import (
    OperatorPolicy,
    PriorStatus,
    ReviewDraft,
    ReviewRequest,
)
from morning_star_review.core.errors import (
    MsreviewForeignTask,
    MsreviewNoRunContext,
    MsreviewPinRejected,
    MsreviewSealInvalid,
)
from morning_star_review.core.guard import guard, heartbeat, in_activity
from morning_star_review.core.runctx import RunContext, clear_run_context, set_run_context
from morning_star_review.core.seal import request_digest, seal

EXEC_ID = "msreview-ci-acme-widgets-pr7-0123abc-1a2b3c4d"


@pytest.fixture(autouse=True)
def _no_global_context() -> Iterator[None]:
    clear_run_context()
    yield
    clear_run_context()


def make_request(force: bool = False) -> ReviewRequest:
    return ReviewRequest(
        pr_url="https://github.com/acme/widgets/pull/7",
        repo="acme/widgets",
        mode="ci",
        force=force,
        policy=OperatorPolicy(trust_repo_config_models=True),
        run_label="gha:acme/widgets/1/1",
    )


def install(on_worker: bool, req: ReviewRequest | None = None) -> RunContext:
    ctx = RunContext(
        execution_id=EXEC_ID,
        request_sha256=request_digest(req or make_request()),
        seal_key=secrets.token_bytes(32),
        run_root="/tmp/runs",
        mode="ci",
        on_worker=on_worker,
        github_token=None,
        read_token=None,
        bot_login=None,
        mistral_api_key="k" * 32,
        agent_python="/usr/bin/python3",
    )
    set_run_context(ctx)
    return ctx


def draft(ctx: RunContext | None = None) -> ReviewDraft:
    d = ReviewDraft(
        findings=[],
        prior_status=[PriorStatus(id="f1", status="fixed")],
        summary="ok",
        source="empty",
        marker_count=0,
    )
    return seal(d, ctx) if ctx is not None else d


def activity_env(workflow_id: str = EXEC_ID) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=workflow_id)
    return env


# --------------------------------------------------------------------------------------
# Local mode (on_worker=False): no activity context, raw exceptions
# --------------------------------------------------------------------------------------


def test_local_mode_accepts_matching_request_and_seals() -> None:
    ctx = install(on_worker=False)
    assert not in_activity()
    assert guard(make_request(), draft(ctx), None) is ctx
    assert guard(make_request()) is ctx


def test_local_mode_rejects_a_different_request() -> None:
    install(on_worker=False)
    with pytest.raises(MsreviewPinRejected):
        guard(make_request(force=True))


def test_local_mode_rejects_unsealed_or_tampered_payloads() -> None:
    ctx = install(on_worker=False)
    with pytest.raises(MsreviewSealInvalid, match="ReviewDraft"):
        guard(make_request(), draft())
    tampered = draft(ctx).model_copy(update={"summary": "changed"})
    with pytest.raises(MsreviewSealInvalid):
        guard(make_request(), tampered)


def test_no_context_raises() -> None:
    with pytest.raises(MsreviewNoRunContext):
        guard(make_request())


# --------------------------------------------------------------------------------------
# Worker mode (on_worker=True): pins on the workflow id, non-retryable ApplicationError
# --------------------------------------------------------------------------------------


def test_worker_mode_inside_our_activity_passes() -> None:
    ctx = install(on_worker=True)
    result = activity_env().run(guard, make_request(), draft(ctx))
    assert result is ctx


def test_worker_mode_rejects_a_foreign_execution_retryably() -> None:
    # Security review PL-1: a sibling run's worker on the same task queue may get our
    # task; rejected retryably, Temporal hands it out again (perhaps to our worker).
    install(on_worker=True)
    with pytest.raises(MsreviewForeignTask) as info:
        activity_env("someone-elses-execution").run(guard, make_request())
    assert isinstance(info.value, MsreviewPinRejected)
    assert not info.value.non_retryable


def test_worker_mode_rejects_request_mismatch_and_bad_seals_non_retryably() -> None:
    ctx = install(on_worker=True)
    with pytest.raises(ApplicationError) as pin:
        activity_env().run(guard, make_request(force=True))
    assert pin.value.type == "MsreviewPinRejected" and pin.value.non_retryable
    tampered = draft(ctx).model_copy(update={"summary": "x"})
    with pytest.raises(ApplicationError) as sealed:
        activity_env().run(guard, make_request(), tampered)
    assert sealed.value.type == "MsreviewSealInvalid" and sealed.value.non_retryable


def test_worker_mode_outside_an_activity_is_rejected() -> None:
    install(on_worker=True)
    with pytest.raises(MsreviewPinRejected, match="outside an activity"):
        guard(make_request())


def test_worker_mode_without_context_inside_activity_is_non_retryable() -> None:
    with pytest.raises(ApplicationError) as info:
        activity_env().run(guard, make_request())
    assert info.value.type == "MsreviewNoRunContext" and info.value.non_retryable


# --------------------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------------------


def test_heartbeat_outside_activity_is_a_no_op() -> None:
    heartbeat({"llm_calls": 1})
    heartbeat()


def test_heartbeat_inside_activity_sends_details() -> None:
    seen: list[tuple[Any, ...]] = []
    env = activity_env()
    env.on_heartbeat = lambda *details: seen.append(details)

    def body() -> None:
        heartbeat({"llm_calls": 3, "tool_calls": 7, "cost": 0.12})
        heartbeat()

    env.run(body)
    assert seen == [({"llm_calls": 3, "tool_calls": 7, "cost": 0.12},), ()]
