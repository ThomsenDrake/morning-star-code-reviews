"""Platform smoke test against a real Workflows workspace (DESIGN 15.5).

Opt-in: ``MSREVIEW_PLATFORM_TESTS=1``, ``MISTRAL_API_KEY`` and
``MSREVIEW_PLATFORM_NAMESPACE`` (the workspace namespace the key must belong to; the
test refuses to start a worker anywhere else). It starts exactly one worker, through
the real driver, and checks on that worker:

(a) readiness via ``get_deployment``;
(b) a routed execution completes (fake GitHub transport, stub agent);
(d) a second start with the same id is refused with 409 ``WF_1101`` (attach);
(c) a foreign execution on the same deployment fails with ``MsreviewPinRejected``;
(f) the orphan sweep finds and terminates a RUNNING execution with the CI prefix;
(e) SIGTERM mid-run terminates the driver's execution and the worker ends cleanly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import secrets
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from fakegh import BOT
from harness import Harness
from wfkit import PR_URL, SQLI, TOKEN, notes_for

from morning_star_review import driver
from morning_star_review.core.contract import OperatorPolicy, ReviewOutcome, RunOverrides
from morning_star_review.core.naming import NAME_PREFIX, execution_id, parse_pr_url
from morning_star_review.core.runctx import clear_run_context, run_context, set_run_context
from morning_star_review.platform import (
    TERMINAL_STATUSES,
    AlreadyStarted,
    DeploymentState,
    ExecutionState,
    MistralPlatform,
    Whoami,
)

pytestmark = pytest.mark.timeout(1500)

SIGTERM_AFTER_S = 25.0
REF = parse_pr_url(PR_URL)


def _now() -> float:
    return time.monotonic()


class SmokePlatform:
    """Delegates to the real platform; runs the extra checks before the driver's start."""

    def __init__(self, inner: MistralPlatform, h: Harness, report: dict[str, Any]) -> None:
        self.inner = inner
        self.h = h
        self.report = report
        self.first_deployment_poll: float | None = None
        self.driver_eid: str | None = None
        self.extra_ids: list[str] = []  # executions the prelude started (cleaned up after)

    async def whoami(self) -> Whoami:
        return await self.inner.whoami()

    async def deployment(self, name: str) -> DeploymentState | None:
        if self.first_deployment_poll is None:
            self.first_deployment_poll = _now()
        return await self.inner.deployment(name)

    async def execution(self, execution_id: str) -> ExecutionState:
        return await self.inner.execution(execution_id)

    async def terminate(self, execution_id: str) -> None:
        self.report.setdefault("terminate_calls", []).append(execution_id)
        await self.inner.terminate(execution_id)

    async def running_for_pr(self, workflow: str, pr_url: str) -> list[str]:
        return await self.inner.running_for_pr(workflow, pr_url)

    async def deployments(self, prefix: str, limit: int = 100) -> list[DeploymentState]:
        return await self.inner.deployments(prefix, limit)

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def wait_terminal(self, eid: str, timeout_s: float) -> ExecutionState:
        limit = _now() + timeout_s
        while True:
            state = await self.inner.execution(eid)
            if state.status in TERMINAL_STATUSES or _now() > limit:
                return state
            await asyncio.sleep(2)

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None:
        if self.driver_eid is None:
            self.driver_eid = execution_id
            self.report["deployment"] = deployment
            if self.first_deployment_poll is not None:
                self.report["readiness_s"] = round(_now() - self.first_deployment_poll, 1)
            await self.prelude(workflow, deployment, payload)
            self.h.spec("explore", notes=notes_for(self.h.repo, SQLI), sleep=900)
            loop = asyncio.get_running_loop()
            loop.call_later(SIGTERM_AFTER_S, os.kill, os.getpid(), signal.SIGTERM)
            self.report["sigterm_scheduled_at"] = time.time()
        await self.inner.start(
            workflow=workflow, deployment=deployment, execution_id=execution_id, payload=payload
        )

    def _use_context(self, eid: str, base: Any) -> None:
        clear_run_context()
        set_run_context(dataclasses.replace(base, execution_id=eid))

    async def prelude(self, workflow: str, deployment: str, payload: Any) -> None:
        base = run_context()
        r = self.report
        # (b) + (d): a routed execution with its own pinned context.
        eid_a = execution_id("ci", REF, None, secrets.token_hex(4))
        self.extra_ids.append(eid_a)
        self._use_context(eid_a, base)
        self.h.spec("explore", notes=notes_for(self.h.repo, SQLI), opened_paths=["app/db.py"])
        t0 = _now()
        await self.inner.start(
            workflow=workflow, deployment=deployment, execution_id=eid_a, payload=payload
        )
        try:
            await self.inner.start(
                workflow=workflow, deployment=deployment, execution_id=eid_a, payload=payload
            )
            r["duplicate_start"] = "accepted"
        except AlreadyStarted:
            r["duplicate_start"] = "409 WF_1101"
        state = await self.wait_terminal(eid_a, 600)
        r["routed"] = {"id": eid_a, "status": state.status, "seconds": round(_now() - t0, 1)}
        if state.status == "COMPLETED":
            outcome = ReviewOutcome.model_validate(state.result)
            r["routed"].update(outcome=outcome.status, verdict=outcome.verdict)
        # (c): a foreign execution on our deployment (its id is not the pinned one).
        eid_f = f"{NAME_PREFIX}-foreign-{secrets.token_hex(4)}"
        self.extra_ids.append(eid_f)
        t0 = _now()
        await self.inner.start(
            workflow=workflow, deployment=deployment, execution_id=eid_f, payload=payload
        )
        state = await self.wait_terminal(eid_f, 180)
        history = (
            await self.inner._client.workflows.executions.get_workflow_execution_history_async(
                execution_id=eid_f
            )
        )
        text = json.dumps(history, default=str)
        r["foreign"] = {
            "id": eid_f,
            "status": state.status,
            "seconds": round(_now() - t0, 1),
            "pin_rejected": "MsreviewPinRejected" in text,
        }
        # (f): an orphan with the CI prefix, kept RUNNING by a slow stub agent.
        eid_o = execution_id("ci", REF, None, secrets.token_hex(4))
        self.extra_ids.append(eid_o)
        self._use_context(eid_o, base)
        self.h.spec("explore", notes=notes_for(self.h.repo, SQLI), sleep=900)
        await self.inner.start(
            workflow=workflow, deployment=deployment, execution_id=eid_o, payload=payload
        )
        t0 = _now()
        listed = False
        while _now() - t0 < 90:
            if eid_o in await self.inner.running_for_pr(workflow, PR_URL):
                listed = True
                break
            await asyncio.sleep(2)
        swept = await driver.sweep_orphans(self.inner, REF, PR_URL)
        state = await self.wait_terminal(eid_o, 120)
        r["orphan"] = {
            "id": eid_o,
            "listed_after_s": round(_now() - t0, 1) if listed else None,
            "swept": eid_o in swept,
            "status": state.status,
        }
        self._use_context(base.execution_id, base)


async def test_platform_lifecycle(harness: Harness, tmp_path: Path) -> None:
    key = os.environ.get("MISTRAL_API_KEY", "")
    expected_ns = os.environ.get("MSREVIEW_PLATFORM_NAMESPACE", "")
    if not key or not expected_ns:
        pytest.skip("needs MISTRAL_API_KEY and MSREVIEW_PLATFORM_NAMESPACE")
    inner = MistralPlatform(key)
    who = await inner.whoami()
    assert who.namespace == expected_ns, "refusing to start a worker in another workspace"
    report: dict[str, Any] = {"started_at": time.time()}
    smoke = SmokePlatform(inner, harness, report)
    worker_state: dict[str, Any] = {}

    async def worker() -> None:
        worker_state["started"] = True
        try:
            await driver._default_worker()
        except asyncio.CancelledError:
            worker_state["cancelled"] = True
            raise
        finally:
            worker_state["ended"] = _now()

    cfg = driver.DriverConfig(
        mode="ci",
        pr_url=PR_URL,
        expected_head_sha=None,
        post="auto",
        force=True,  # later executions must not skip as already_reviewed
        overrides=RunOverrides(preset="single"),
        policy=OperatorPolicy(trust_repo_config_models=True),
        run_label="platform-smoke",
        deadline_s=1200,
        out_dir=tmp_path / "report",
        fail_on="never",
        agent_python=str(harness.agent_python),
        run_root=tmp_path / "runs",
    )
    sec = driver.DriverSecrets(mistral_api_key=key, github_token=TOKEN, bot_login=BOT)
    hooks = driver.Hooks(platform=smoke, worker=worker, configure_sdk=True)
    t0 = _now()
    try:
        outcome, code = await driver.run_review(cfg, sec, hooks)
    finally:
        # Never leave an execution of this test running on the platform.
        for eid in smoke.extra_ids:
            state = await inner.execution(eid)
            if state.status not in TERMINAL_STATUSES:
                await inner.terminate(eid)
                report.setdefault("cleanup_terminated", []).append(eid)
    report["driver_seconds"] = round(_now() - t0, 1)
    eid_b = smoke.driver_eid
    assert eid_b is not None
    final = await inner.execution(eid_b)
    report["sigterm"] = {
        "id": eid_b,
        "exit_code": code,
        "status": final.status,
        "terminate_calls": report.get("terminate_calls", []).count(eid_b),
        "worker_cancelled": worker_state.get("cancelled", False),
    }
    out_path = os.environ.get("MSREVIEW_PLATFORM_REPORT")
    if out_path:
        Path(out_path).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))

    assert report["deployment"].startswith(f"{NAME_PREFIX}-ci-")
    assert report["routed"]["status"] == "COMPLETED"
    assert report["routed"]["outcome"] == "posted"
    assert report["duplicate_start"] == "409 WF_1101"
    assert report["foreign"]["status"] == "FAILED"
    assert report["foreign"]["pin_rejected"]
    assert report["orphan"]["swept"] and report["orphan"]["status"] == "TERMINATED"
    assert code == driver.EXIT_CANCELLED and outcome.status == "failed"
    assert final.status == "TERMINATED"
    assert report["sigterm"]["terminate_calls"] == 1
    assert worker_state.get("ended") is not None
