"""Worker readiness needs our worker's own heartbeat, not a stale ``is_active`` (G5)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_driver_lifecycle import (  # noqa: E402
    FAST,
    SECRETS,
    FakeClock,
    FakePlatform,
    Worker,
    _run_root,  # noqa: F401 - autouse: the fake publish step writes outcome.json there
    hooks,
    make_cfg,
    no_skip,
    rate_limited,
)

from morning_star_review import driver  # noqa: E402
from morning_star_review.platform import DeploymentState, PlatformError  # noqa: E402

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


def dep(active: bool = True, workers: int = 1, hb: datetime | None = NOW) -> DeploymentState:
    return DeploymentState("msreview-cli-x-pr7", active, workers, hb)


def registered(
    d: DeploymentState | None, before: DeploymentState | None = None, *, known: bool = True
) -> bool:
    return driver.worker_registered(d, since=NOW, before=before, skew_s=5.0, before_known=known)


def test_inactive_or_missing_deployments_are_not_ready() -> None:
    assert not registered(None)
    assert not registered(dep(active=False))
    assert not registered(dep(workers=0))


def test_a_stale_active_deployment_is_not_ready() -> None:
    stale = dep(hb=NOW - timedelta(seconds=1))
    # The previous worker's state, seen again: same count, same heartbeat. Not ours.
    assert not registered(stale, before=stale)
    assert not registered(dep(hb=None), before=dep(hb=None))
    # The state before our start is unknown: a heartbeat older than our start (beyond
    # the clock skew allowed) is not ours either.
    assert not registered(dep(hb=NOW - timedelta(seconds=30)), known=False)
    assert not registered(dep(hb=None), known=False)


def test_a_new_worker_is_ready() -> None:
    # No active worker before ours started: any active worker now is ours.
    assert registered(dep(hb=NOW - timedelta(seconds=30)), before=None)
    assert registered(dep(hb=None), before=dep(active=False, workers=0, hb=None))
    # One more worker than the stale one counted before.
    stale = dep(hb=NOW - timedelta(seconds=40))
    assert registered(dep(workers=2, hb=stale.last_heartbeat), before=stale)


def test_a_heartbeat_after_our_start_is_ready() -> None:
    before = dep(hb=NOW - timedelta(seconds=1))
    assert registered(dep(hb=NOW + timedelta(seconds=1)), before=before)
    # Within the clock skew (the server clock may run behind ours).
    assert registered(
        dep(hb=NOW - timedelta(seconds=2)), before=dep(hb=NOW - timedelta(seconds=10))
    )
    assert registered(dep(hb=NOW - timedelta(seconds=2)), known=False)
    naive = (NOW + timedelta(seconds=1)).replace(tzinfo=None)  # naive = UTC
    assert registered(dep(hb=naive), before=before)


class StalePlatform(FakePlatform):
    """The deployment still shows the previous worker until ours heartbeats.

    Its heartbeat is too old for a live worker (``choose_deployment`` keeps the stable
    name), yet ``is_active`` and the worker count have not expired: the Action e2e case.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stale_hb = datetime.now(UTC) - timedelta(seconds=40)
        self.registered = False
        self.started_after_registration: list[bool] = []
        self.on_start = lambda eid: self.started_after_registration.append(self.registered)

    async def deployment(self, name: str) -> DeploymentState | None:
        self.calls.append(("deployment", name))
        if self.registered and self.worker_running:
            return DeploymentState(name, True, 1, datetime.now(UTC))
        return DeploymentState(name, True, 1, self.stale_hb)


class LateWorker:
    """Registers (first heartbeat) only after ``delay`` seconds."""

    def __init__(self, platform: StalePlatform, delay: float | None) -> None:
        self.platform = platform
        self.delay = delay

    async def __call__(self) -> None:
        self.platform.worker_running = True
        try:
            if self.delay is not None:
                await asyncio.sleep(self.delay)
                self.platform.registered = True
            await asyncio.Event().wait()
        finally:
            self.platform.worker_running = False


def _hooks(p: FakePlatform, w: Any, **kw: Any) -> driver.Hooks:
    return driver.Hooks(
        platform=p, worker=w, timing=FAST, install_signals=False, precheck=no_skip, **kw
    )


async def test_start_waits_for_our_workers_heartbeat(tmp_path: Path) -> None:
    p = StalePlatform()
    outcome, code = await driver.run_review(
        make_cfg(tmp_path), SECRETS, _hooks(p, LateWorker(p, 0.05))
    )
    assert outcome.status == "posted" and code == driver.EXIT_OK
    assert p.started_after_registration == [True]
    names = {c[1] for c in p.calls if c[0] == "deployment"}
    assert len(names) == 1  # the stable per-PR name: the stale worker is not live
    polls = [c for c in p.calls if c[0] == "deployment"]
    # choose_deployment, the state before our worker, then readiness polls until ready.
    assert len(polls) > 3


async def test_a_stale_deployment_alone_times_out(tmp_path: Path) -> None:
    p = StalePlatform()
    outcome, code = await driver.run_review(
        make_cfg(tmp_path), SECRETS, _hooks(p, LateWorker(p, None))
    )
    assert code == driver.EXIT_FAILED
    assert "not ready" in (outcome.reason or "")
    assert p.started == [] and p.terminated == []


# ----------------------------------------------------------------------------- slow, not dead
# API errors extend the readiness wait; a rate-limited worker start is retried.

READY_TIMING = driver.Timing(
    **{
        **FAST.__dict__,
        "readiness_timeout_s": 90.0,
        "readiness_poll_s": 1.0,
        "start_retry_s": 2.0,
        "readiness_error_budget_s": 600.0,
    }
)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The driver's monotonic clock; only the hooks' sleeps advance it."""
    c = FakeClock()
    monkeypatch.setattr(driver, "_now", c.now)
    return c


class ErrorsWhileStarting(FakePlatform):
    """Deployment reads fail with 429 for ``seconds`` (fake time) once the worker runs."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds
        self.until: float | None = None
        self.refused = 0

    async def deployment(self, name: str) -> DeploymentState | None:
        if self.worker_running:
            if self.until is None:
                self.until = self.clock.t + self.seconds
            if self.clock.t < self.until:
                self.calls.append(("deployment", name))
                self.refused += 1
                raise rate_limited("get_deployment")
        return await super().deployment(name)


async def test_readiness_is_extended_by_time_lost_to_api_errors(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    clock = fake_clock
    # 200 s of 429s: more than the 90 s readiness timeout, within the error budget.
    p = ErrorsWhileStarting(clock, 200.0)
    h = hooks(p, Worker(p), sleep=clock.sleep, timing=READY_TIMING)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert code == driver.EXIT_OK and outcome.status == "posted"
    assert p.refused >= 5 and p.started == [outcome.execution_id]


async def test_readiness_extension_is_bounded_by_its_budget(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    clock = fake_clock
    p = ErrorsWhileStarting(clock, 100_000.0)
    w = Worker(p)
    h = hooks(p, w, sleep=clock.sleep, timing=READY_TIMING)
    start = clock.t
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert code == driver.EXIT_FAILED and "not ready within 90s" in (outcome.reason or "")
    assert "lost to Workflows API errors" in (outcome.reason or "")
    waited = clock.t - start
    budget = READY_TIMING.readiness_timeout_s + READY_TIMING.readiness_error_budget_s
    assert budget - 60 <= waited <= budget + READY_TIMING.api_backoff_max_s
    assert p.started == [] and w.cancelled


def _register_429() -> BaseException:
    """What the Workflows SDK raises when spec registration gets a 429 (not retried)."""
    from http import HTTPStatus

    from mistralai.workflows.exceptions import ErrorCode, WorkflowsException

    return WorkflowsException(
        message="Failed to register workflow specs",
        status=HTTPStatus.TOO_MANY_REQUESTS,
        code=ErrorCode.POST_WORKFLOWS_REGISTER_ERROR,
    )


class FailingWorker(Worker):
    """Fails with ``error`` on its first ``failures`` starts, then runs normally."""

    def __init__(self, platform: FakePlatform, error: BaseException, failures: int) -> None:
        super().__init__(platform)
        self.error = error
        self.failures = failures

    async def __call__(self) -> None:
        if self.starts < self.failures:
            self.starts += 1
            raise self.error
        await super().__call__()


async def test_a_worker_rate_limited_at_registration_is_started_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = FakePlatform()
    w = FailingWorker(p, _register_429(), failures=2)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == driver.EXIT_OK and outcome.status == "posted"
    assert w.starts == 3 and p.started == [outcome.execution_id]
    err = capsys.readouterr().err
    assert "starting it again" in err and "(2 of 5)" in err


async def test_worker_restarts_are_limited(tmp_path: Path) -> None:
    p = FakePlatform()
    w = FailingWorker(p, _register_429(), failures=100)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == driver.EXIT_FAILED and "worker failed" in (outcome.reason or "")
    assert w.starts == 1 + FAST.worker_restarts
    assert p.started == []


@pytest.mark.parametrize(
    "error",
    [ValueError("bad config"), PlatformError("register failed (HTTP 400)", 400)],
)
async def test_a_worker_that_fails_for_good_is_not_started_again(
    tmp_path: Path, error: BaseException
) -> None:
    p = FakePlatform()
    w = FailingWorker(p, error, failures=1)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == driver.EXIT_FAILED and "worker failed" in (outcome.reason or "")
    assert w.starts == 1 and p.started == []
