"""Worker readiness needs our worker's own heartbeat, not a stale ``is_active`` (G5)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_driver_lifecycle import (  # noqa: E402
    FAST,
    SECRETS,
    FakePlatform,
    _run_root,  # noqa: F401 - autouse: the fake publish step writes outcome.json there
    make_cfg,
    no_skip,
)

from morning_star_review import driver  # noqa: E402
from morning_star_review.platform import DeploymentState  # noqa: E402

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
