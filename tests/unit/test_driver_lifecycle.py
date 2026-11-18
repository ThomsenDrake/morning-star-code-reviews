"""Driver lifecycle against a fake Workflows platform (DESIGN 11, WP7 acceptance)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from morning_star_review import driver
from morning_star_review.core.contract import (
    FinalFinding,
    OperatorPolicy,
    ReviewOutcome,
    RunOverrides,
)
from morning_star_review.core.naming import deployment_name, parse_pr_url
from morning_star_review.platform import (
    AlreadyStarted,
    DeploymentState,
    ExecutionState,
    PlatformAuthError,
    PlatformError,
    Whoami,
)

PR = "https://github.com/octo-org/widgets/pull/7"
REF = parse_pr_url(PR)
KEY = "mk-test-not-a-real-key-0123456789"
FAST = driver.Timing(
    readiness_timeout_s=0.5,
    readiness_poll_s=0.01,
    busy_wait_s=0.01,
    start_retry_s=0.01,
    poll_s=0.01,
    terminate_timeout_s=1.0,
    worker_stop_timeout_s=1.0,
)


class FakePlatform:
    """Records every call; deployments become active once the fake worker runs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.worker_running = False
        self.never_ready = False
        self.live_names: set[str] = set()
        self.start_errors: list[PlatformError] = []
        self.statuses: list[str] = ["RUNNING", "RUNNING", "COMPLETED"]
        self.result: dict[str, Any] | None = None
        self.started: list[str] = []
        self.terminated: list[str] = []
        self.running: list[str] = []
        self.whoami_error: PlatformError | None = None
        self.on_start: Any = None
        self.writes_outcome = True  # False: a result no activity of ours wrote
        self.foreign: dict[str, str] = {}  # other runs' executions -> their deployment

    async def whoami(self) -> Whoami:
        self.calls.append(("whoami", None))
        if self.whoami_error is not None:
            raise self.whoami_error
        return Whoami(namespace="0123456789abcdef:fedcba", raw={})

    async def deployment(self, name: str) -> DeploymentState | None:
        self.calls.append(("deployment", name))
        if name in self.live_names:
            return DeploymentState(name, True, 1, datetime.now(UTC))
        if self.worker_running and not self.never_ready:
            return DeploymentState(name, True, 1, datetime.now(UTC))
        return None

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None:
        self.calls.append(("start", execution_id))
        if self.start_errors:
            raise self.start_errors.pop(0)
        self.started.append(execution_id)
        if self.on_start is not None:
            self.on_start(execution_id)

    async def execution(self, execution_id: str) -> ExecutionState:
        self.calls.append(("execution", execution_id))
        if execution_id in self.foreign:
            return ExecutionState(execution_id, "RUNNING", None, self.foreign[execution_id])
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        result = None
        if status == "COMPLETED":
            result = self.result or outcome_dict(execution_id)
            if self.writes_outcome and RUN_ROOT is not None:
                # What our own publish activity does on the worker: write outcome.json.
                run_dir = RUN_ROOT / execution_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "outcome.json").write_text(json.dumps(result))
        return ExecutionState(execution_id, status, result)

    async def terminate(self, execution_id: str) -> None:
        self.calls.append(("terminate", execution_id))
        self.terminated.append(execution_id)

    async def running_for_pr(self, workflow: str, pr_url: str) -> list[str]:
        self.calls.append(("running_for_pr", pr_url))
        return list(self.running)

    async def deployments(self, prefix: str, limit: int = 100) -> list[DeploymentState]:
        return []

    async def aclose(self) -> None:
        return None


RUN_ROOT: Path | None = None


@pytest.fixture(autouse=True)
def _run_root(tmp_path: Path) -> Iterator[None]:
    """The run root ``make_cfg`` uses, for the fake publish step's outcome.json."""
    global RUN_ROOT
    RUN_ROOT = tmp_path / "runs"
    yield
    RUN_ROOT = None


def outcome_dict(eid: str, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": "posted",
        "verdict": "clean",
        "pr_url": PR,
        "execution_id": eid,
    }
    data.update(kw)
    return ReviewOutcome.model_validate(data).model_dump(mode="json")


def make_cfg(tmp_path: Path, **kw: Any) -> driver.DriverConfig:
    data: dict[str, Any] = {
        "mode": "cli",
        "pr_url": PR,
        "expected_head_sha": None,
        "post": "auto",
        "force": False,
        "overrides": RunOverrides(),
        "policy": OperatorPolicy(trust_repo_config_models=False),
        "run_label": "cli:test",
        "deadline_s": 30,
        "out_dir": None,
        "fail_on": "never",
        "agent_python": "/nonexistent/python",
        "run_root": tmp_path / "runs",
    }
    data.update(kw)
    return driver.DriverConfig(**data)


class Worker:
    """A fake worker: marks the platform ready, then runs until cancelled."""

    def __init__(self, platform: FakePlatform, *, crash_after: float | None = None) -> None:
        self.platform = platform
        self.crash_after = crash_after
        self.cancelled = False
        self.starts = 0

    async def __call__(self) -> None:
        self.starts += 1
        self.platform.worker_running = True
        try:
            if self.crash_after is not None:
                await asyncio.sleep(self.crash_after)
                raise SystemExit(1)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.platform.worker_running = False


async def no_skip(req: Any, secrets: Any) -> None:
    """The GitHub pre-check: nothing to skip (no network in these tests)."""


def hooks(platform: FakePlatform, worker: Any, **kw: Any) -> driver.Hooks:
    kw.setdefault("precheck", no_skip)
    kw.setdefault("timing", FAST)
    return driver.Hooks(platform=platform, worker=worker, install_signals=False, **kw)


SECRETS = driver.DriverSecrets(mistral_api_key=KEY)


async def test_happy_path_routes_and_stops_the_worker(tmp_path: Path) -> None:
    p = FakePlatform()
    w = Worker(p)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == 0
    assert outcome.status == "posted"
    assert p.started == [outcome.execution_id]
    assert outcome.execution_id.startswith("msreview-cli-octo-org-widgets-pr7-latest-")
    assert p.terminated == []  # terminal executions are not terminated
    assert w.cancelled
    names = [c[1] for c in p.calls if c[0] == "deployment"]
    assert names[0] == deployment_name("cli", REF)
    kinds = [c[0] for c in p.calls]
    assert kinds.index("whoami") < kinds.index("deployment") < kinds.index("start")


async def test_readiness_timeout_fails_without_starting(tmp_path: Path) -> None:
    p = FakePlatform()
    p.never_ready = True
    w = Worker(p)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == driver.EXIT_FAILED
    assert outcome.status == "failed"
    assert "not ready" in (outcome.reason or "")
    assert p.started == [] and p.terminated == []
    assert w.cancelled


async def test_duplicate_start_409_is_attached(tmp_path: Path) -> None:
    p = FakePlatform()
    p.start_errors = [AlreadyStarted("execute_workflow failed (HTTP 409)", 409)]
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == 0 and outcome.status == "posted"
    assert [c[0] for c in p.calls].count("start") == 1
    assert p.terminated == []


async def test_start_is_retried_with_the_same_id(tmp_path: Path) -> None:
    p = FakePlatform()
    p.start_errors = [PlatformError("execute_workflow failed (HTTP 502)", 502)]
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == 0
    ids = [c[1] for c in p.calls if c[0] == "start"]
    assert len(ids) == 2 and ids[0] == ids[1] == outcome.execution_id


async def test_start_4xx_is_not_retried_and_terminates(tmp_path: Path) -> None:
    p = FakePlatform()
    p.start_errors = [PlatformError("execute_workflow failed (HTTP 422)", 422)]
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED
    assert "HTTP 422" in (outcome.reason or "")
    assert [c[0] for c in p.calls].count("start") == 1
    # The start may have reached the server: the driver still terminates the id.
    assert p.terminated == [outcome.execution_id]


async def test_worker_crash_terminates_the_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = FakePlatform()
    p.statuses = ["RUNNING"]
    w = Worker(p, crash_after=0.05)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == driver.EXIT_FAILED
    assert "worker exited" in (outcome.reason or "")
    assert p.terminated == [outcome.execution_id]
    # No worker of ours is left to report a late activity completion: no note.
    assert driver.TERMINATED_ACTIVITY_NOTE not in capsys.readouterr().err


async def test_deadline_terminates_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = FakePlatform()
    p.statuses = ["RUNNING"]
    w = Worker(p)
    cfg = make_cfg(tmp_path, deadline_s=1)
    outcome, code = await driver.run_review(cfg, SECRETS, hooks(p, w))
    assert code == driver.EXIT_FAILED
    assert "deadline" in (outcome.reason or "")
    assert p.terminated == [outcome.execution_id]
    assert w.cancelled
    err = capsys.readouterr().err
    assert f"terminated execution {outcome.execution_id}" in err
    # The worker was still running: the Temporal core's late-completion WARN is explained.
    assert err.count(driver.TERMINATED_ACTIVITY_NOTE) == 1


class SecondSignalDuringTerminate(FakePlatform):
    """Sends a second signal while the cleanup's terminate call runs."""

    async def terminate(self, execution_id: str) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.05)  # the handler runs (and must ignore it) meanwhile
        await super().terminate(execution_id)


async def test_signal_terminates_exactly_once(tmp_path: Path) -> None:
    p = SecondSignalDuringTerminate()
    p.statuses = ["RUNNING"]
    w = Worker(p)

    def on_start(eid: str) -> None:
        # SIGTERM; the second signal (SIGINT) comes during the cleanup and is ignored.
        # (A timer for it could fire after the handlers are removed: a flaky test.)
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    p.on_start = on_start
    h = driver.Hooks(platform=p, worker=w, timing=FAST, install_signals=True, precheck=no_skip)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, h)
    assert code == driver.EXIT_CANCELLED
    assert outcome.status == "failed"
    assert "SIGTERM" in (outcome.reason or "")
    assert p.terminated == [outcome.execution_id]
    assert w.cancelled
    # Handlers are removed afterwards: the default SIGINT handler is back.
    assert signal.getsignal(signal.SIGINT) is not None


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGQUIT])
async def test_hangup_cleans_up_like_sigterm(tmp_path: Path, sig: signal.Signals) -> None:
    # Security review PL-4: a closed terminal (SIGHUP) must not leave the execution
    # running and the worker (with its agents) alive.
    p = FakePlatform()
    p.statuses = ["RUNNING"]
    w = Worker(p)

    def on_start(eid: str) -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), sig)

    p.on_start = on_start
    h = driver.Hooks(platform=p, worker=w, timing=FAST, install_signals=True, precheck=no_skip)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, h)
    assert code == driver.EXIT_CANCELLED
    assert sig.name in (outcome.reason or "")
    assert p.terminated == [outcome.execution_id]
    assert w.cancelled


async def test_cancel_in_ci_says_the_workflow_run_was_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Live: Actions' Cancel arrives as SIGINT, which the step's trap forwards as SIGTERM;
    # "cancelled by SIGTERM" told users nothing.
    output, summary = tmp_path / "gh-output", tmp_path / "gh-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    p = FakePlatform()
    p.statuses = ["RUNNING"]
    w = Worker(p)

    def on_start(eid: str) -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    p.on_start = on_start
    h = driver.Hooks(platform=p, worker=w, timing=FAST, install_signals=True, precheck=no_skip)
    cfg = make_cfg(tmp_path, mode="ci", policy=OperatorPolicy(trust_repo_config_models=True))
    outcome, code = await driver.run_review(cfg, SECRETS, h)
    assert code == driver.EXIT_CANCELLED and outcome.status == "failed"
    assert outcome.reason == "cancelled (the workflow run was cancelled)"
    assert p.terminated == [outcome.execution_id] and w.cancelled
    out = capsys.readouterr().out
    assert "::error title=Morning Star Code Review::cancelled (the workflow run" in out
    assert "SIGTERM" not in out and "SIGTERM" not in summary.read_text(encoding="utf-8")


async def test_empty_key_in_ci_names_the_action_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    p = FakePlatform()
    cfg = make_cfg(tmp_path, mode="ci", policy=OperatorPolicy(trust_repo_config_models=True))
    with pytest.raises(driver.DriverError, match="Action input `mistral-api-key` is empty"):
        await driver.run_review(cfg, driver.DriverSecrets(mistral_api_key=""), hooks(p, Worker(p)))
    with pytest.raises(driver.DriverError, match=r"^no Mistral API key$"):
        await driver.run_review(
            make_cfg(tmp_path), driver.DriverSecrets(mistral_api_key=""), hooks(p, Worker(p))
        )
    assert p.calls == []


async def test_refused_key_is_a_preflight_error(tmp_path: Path) -> None:
    p = FakePlatform()
    p.whoami_error = PlatformAuthError("whoami failed (HTTP 401)", 401)
    w = Worker(p)
    with pytest.raises(driver.DriverError, match="refused"):
        await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert w.starts == 0
    assert [c[0] for c in p.calls] == ["whoami"]


async def test_live_deployment_falls_back_to_a_suffixed_name(tmp_path: Path) -> None:
    p = FakePlatform()
    stable = deployment_name("cli", REF)
    p.live_names = {stable}
    _outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == 0
    used = [c[1] for c in p.calls if c[0] == "deployment"]
    assert used[:2] == [stable, stable]  # checked, waited, re-checked
    assert used[2].startswith(stable + "-x")


def test_stale_heartbeat_is_not_live() -> None:
    old = datetime.now(UTC) - timedelta(seconds=60)
    assert not driver.is_live(DeploymentState("d", True, 1, old), driver.Timing())
    assert driver.is_live(DeploymentState("d", True, 1, datetime.now(UTC)), driver.Timing())
    assert not driver.is_live(DeploymentState("d", True, 0, datetime.now(UTC)), driver.Timing())
    assert not driver.is_live(None, driver.Timing())


async def test_ci_sweeps_only_its_own_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    p = FakePlatform()
    mine = "msreview-ci-octo-org-widgets-pr7-abcdef1-0a0b0c0d"
    other_pr = "msreview-ci-octo-org-widgets-pr70-abcdef1-0a0b0c0d"
    cli_run = "msreview-cli-octo-org-widgets-pr7-abcdef1-0a0b0c0d"
    p.running = [mine, other_pr, cli_run]
    cfg = make_cfg(tmp_path, mode="ci", policy=OperatorPolicy(trust_repo_config_models=True))
    outcome, code = await driver.run_review(cfg, SECRETS, hooks(p, Worker(p)))
    assert code == 0
    assert p.terminated == [mine]
    assert outcome.execution_id.startswith("msreview-ci-")


async def test_ci_sweep_spares_executions_with_a_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Security review PL-5: a second CI run for the PR must not terminate a live run.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    p = FakePlatform()
    live = "msreview-ci-octo-org-widgets-pr7-abcdef1-0a0b0c0d"
    orphan = "msreview-ci-octo-org-widgets-pr7-abcdef1-1a1b1c1d"
    p.running = [live, orphan]
    p.foreign = {live: "msreview-ci-octo-org-widgets-1445a6-pr7-x1234", orphan: "gone"}
    p.live_names = {"msreview-ci-octo-org-widgets-1445a6-pr7-x1234"}
    cfg = make_cfg(tmp_path, mode="ci", policy=OperatorPolicy(trust_repo_config_models=True))
    _outcome, code = await driver.run_review(cfg, SECRETS, hooks(p, Worker(p)))
    assert code == 0
    assert p.terminated == [orphan]


async def test_failed_execution_uses_report_failure_outcome(tmp_path: Path) -> None:
    p = FakePlatform()
    p.statuses = ["RUNNING", "FAILED"]
    cfg = make_cfg(tmp_path, keep_run_dir=True)

    def on_start(eid: str) -> None:
        run_dir = tmp_path / "runs" / eid
        run_dir.mkdir(parents=True)
        data = outcome_dict(eid, status="failed", verdict=None, reason="explore failed: boom")
        (run_dir / "outcome.json").write_text(ReviewOutcome.model_validate(data).model_dump_json())

    p.on_start = on_start
    outcome, code = await driver.run_review(cfg, SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED
    assert outcome.reason == "explore failed: boom"


async def test_failed_execution_without_outcome_file(tmp_path: Path) -> None:
    p = FakePlatform()
    p.statuses = ["TIMED_OUT"]
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED
    assert "TIMED_OUT" in (outcome.reason or "")
    assert p.terminated == []


async def test_invalid_completed_result_is_a_failure(tmp_path: Path) -> None:
    p = FakePlatform()
    p.statuses = ["COMPLETED"]
    p.result = {"not": "an outcome"}
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED
    assert "left no outcome" in (outcome.reason or "")


async def test_a_forged_completed_result_is_not_trusted(tmp_path: Path) -> None:
    # Security review PL-3: a rogue worker on the task queue completes the workflow
    # with "posted, clean"; our publish activity never ran and wrote no outcome.json.
    p = FakePlatform()
    p.statuses = ["COMPLETED"]
    p.writes_outcome = False
    p.result = outcome_dict("msreview-cli-somebody-else-1", report_markdown="# forged")
    cfg = make_cfg(tmp_path, fail_on="findings")
    outcome, code = await driver.run_review(cfg, SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED
    assert outcome.status == "failed" and outcome.execution_id != "msreview-cli-somebody-else-1"
    assert "forged" not in (outcome.report_markdown or "")


async def test_run_dir_is_removed_unless_kept(tmp_path: Path) -> None:
    p = FakePlatform()

    def on_start(eid: str) -> None:
        (tmp_path / "runs" / eid / "repo").mkdir(parents=True)

    p.on_start = on_start
    outcome, _ = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert not (tmp_path / "runs" / outcome.execution_id).exists()


def _finding(severity: str, state: str = "open", verify_status: str | None = None) -> FinalFinding:
    return FinalFinding.model_validate(
        {
            "id": "f0123456789ab",
            "path": "a.py",
            "line": 1,
            "anchor": "inline",
            "severity": severity,
            "category": "correctness",
            "title": "t",
            "evidence": "x",
            "explanation": "e",
            "state": state,
            "verify_status": verify_status,
        }
    )


@pytest.mark.parametrize(
    ("severity", "fail_on", "code"),
    [
        ("medium", "never", 0),
        ("medium", "findings", 3),
        ("medium", "high", 0),
        ("high", "high", 3),
        ("high", "critical", 0),
        ("critical", "critical", 3),
    ],
)
def test_fail_on_thresholds(severity: str, fail_on: str, code: int) -> None:
    outcome = ReviewOutcome.model_validate(
        {**outcome_dict("e"), "verdict": "findings", "findings": [_finding(severity)]}
    )
    assert driver.exit_code_for(outcome, fail_on) == code


def test_fixed_findings_do_not_trigger_fail_on() -> None:
    outcome = ReviewOutcome.model_validate(
        {**outcome_dict("e"), "findings": [_finding("critical", state="fixed")]}
    )
    assert driver.exit_code_for(outcome, "findings") == 0


@pytest.mark.parametrize("fail_on", ["findings", "high", "critical"])
def test_possible_issues_do_not_trigger_fail_on(fail_on: str) -> None:
    # Unconfirmed and summary-only: the verdict ignores it, and so does fail-on.
    possible = _finding("critical", state="summary_only", verify_status="uncertain")
    outcome = ReviewOutcome.model_validate(outcome_dict("e", findings=[possible]))
    assert outcome.verdict == "clean" and outcome.possible_issues == 1
    assert driver.exit_code_for(outcome, fail_on) == 0
    # Posted inline as "(unconfirmed)": it counts.
    inline = possible.model_copy(update={"state": "open"})
    outcome = ReviewOutcome.model_validate(
        {**outcome_dict("e"), "verdict": "findings", "findings": [inline]}
    )
    assert driver.exit_code_for(outcome, fail_on) == 3


async def test_local_mode_runs_in_process(tmp_path: Path) -> None:
    seen: list[Any] = []

    async def runner(req: Any) -> Any:
        from morning_star_review.core.runctx import run_context

        ctx = run_context()
        seen.append((req.mode, ctx.on_worker))
        return outcome_dict(ctx.execution_id, status="report", reason="report mode")

    h = driver.Hooks(timing=FAST, install_signals=False, local_runner=runner)
    outcome, code = await driver.run_review(make_cfg(tmp_path, mode="local"), SECRETS, h)
    assert code == 0 and outcome.status == "report"
    assert seen == [("local", False)]


@pytest.mark.parametrize("telemetry", [False, True])
async def test_telemetry_opt_in_reaches_the_run_context(tmp_path: Path, telemetry: bool) -> None:
    seen: list[bool] = []

    async def runner(req: Any) -> Any:
        from morning_star_review.core.runctx import run_context

        ctx = run_context()
        seen.append(ctx.telemetry)
        return outcome_dict(ctx.execution_id, status="report", reason="report mode")

    h = driver.Hooks(timing=FAST, install_signals=False, local_runner=runner)
    cfg = make_cfg(tmp_path, mode="local", telemetry=telemetry)
    await driver.run_review(cfg, SECRETS, h)
    assert seen == [telemetry]


async def test_local_mode_failure_reads_outcome_file(tmp_path: Path) -> None:
    async def runner(req: Any) -> Any:
        from morning_star_review.core.runctx import run_context

        ctx = run_context()
        ctx.run_dir.mkdir(parents=True)
        data = outcome_dict(
            ctx.execution_id, status="failed", verdict=None, reason="prepare failed"
        )
        (ctx.run_dir / "outcome.json").write_text(json_of(data))
        raise RuntimeError("raw local exception")

    h = driver.Hooks(timing=FAST, install_signals=False, local_runner=runner)
    outcome, code = await driver.run_review(make_cfg(tmp_path, mode="local"), SECRETS, h)
    assert code == 1 and outcome.reason == "prepare failed"


async def test_local_mode_deadline(tmp_path: Path) -> None:
    async def runner(req: Any) -> Any:
        await asyncio.sleep(10)

    h = driver.Hooks(timing=FAST, install_signals=False, local_runner=runner)
    outcome, code = await driver.run_review(
        make_cfg(tmp_path, mode="local", deadline_s=1), SECRETS, h
    )
    assert code == 1 and "deadline" in (outcome.reason or "")


def json_of(data: dict[str, Any]) -> str:
    return ReviewOutcome.model_validate(data).model_dump_json()


async def test_usage_errors(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError):
        await driver.run_review(make_cfg(tmp_path, pr_url="not a url"), SECRETS, driver.Hooks())
    with pytest.raises(driver.DriverError):
        await driver.run_review(make_cfg(tmp_path, fail_on="sometimes"), SECRETS, driver.Hooks())
    with pytest.raises(driver.DriverError):
        await driver.run_review(make_cfg(tmp_path), driver.DriverSecrets(""), driver.Hooks())


def test_failure_reasons_are_redacted(tmp_path: Path) -> None:
    req, _ = driver.build_request(make_cfg(tmp_path))
    out = driver.failed_outcome(req, "e", f"boom {KEY}", [KEY])
    assert KEY not in (out.reason or "")


class FlakyDeployments(FakePlatform):
    def __init__(self, errors: list[PlatformError]) -> None:
        super().__init__()
        self.errors = errors

    async def deployment(self, name: str) -> DeploymentState | None:
        if self.errors:
            self.calls.append(("deployment", name))
            raise self.errors.pop(0)
        return await super().deployment(name)


async def test_deployment_lookup_retries_transient_errors(tmp_path: Path) -> None:
    p = FlakyDeployments([PlatformError("get_deployment failed (HTTP 502)", 502)])
    _outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == 0


async def test_deployment_lookup_4xx_is_a_usage_error(tmp_path: Path) -> None:
    p = FlakyDeployments([PlatformError("get_deployment failed (HTTP 400)", 400)])
    w = Worker(p)
    with pytest.raises(driver.DriverError, match="deployment status"):
        await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert w.starts == 0


def test_process_env_hygiene(tmp_path: Path) -> None:
    env = {"AGENT": "1", "HTTP": "x", "WORKER": "y", "PATH": "/bin"}
    before = Path.cwd()
    driver.prepare_process_env(telemetry=False, env=env)
    assert Path.cwd() != before and not any(Path.cwd().iterdir())  # empty cwd: no .env
    assert env == {"PATH": "/bin", "OTEL_ENABLED": "false"}
    tele = {"SERVER_URL": "https://api.mistral.ai/"}
    driver.prepare_process_env(telemetry=True, env=tele)
    assert "OTEL_ENABLED" not in tele
    with pytest.raises(driver.DriverError, match="SERVER_URL"):
        driver.prepare_process_env(telemetry=False, env={"SERVER_URL": "https://evil.example"})
    allowed = {"SERVER_URL": "https://staging.example", "MSREVIEW_ALLOW_CUSTOM_SERVER": "1"}
    driver.prepare_process_env(telemetry=False, env=allowed)


def test_process_env_quiets_the_sdk_http_loggers() -> None:
    # The Workflows SDK polls through httpx2/httpcore2; at INFO every 3 s poll is a log line.
    names = ("httpx", "httpcore", "httpx2", "httpcore2.connection", "temporalio")
    roots = ("httpx", "httpcore", "httpx2", "httpcore2", "temporalio", "mistralai")
    saved = {n: logging.getLogger(n).level for n in roots}
    try:
        driver.prepare_process_env(telemetry=False, env={})
        assert all(not logging.getLogger(n).isEnabledFor(logging.INFO) for n in names)
        driver.prepare_process_env(telemetry=False, env={}, verbose=True)
        assert all(logging.getLogger(n).isEnabledFor(logging.INFO) for n in names)
    finally:
        for n, level in saved.items():
            logging.getLogger(n).setLevel(level)


class SlowTerminate(FakePlatform):
    """``terminate`` takes a while; a signal is sent while it is in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.terminate_finished = False

    async def terminate(self, execution_id: str) -> None:
        self.calls.append(("terminate", execution_id))
        loop = asyncio.get_running_loop()
        loop.call_later(0.02, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.2)
        self.terminated.append(execution_id)
        self.terminate_finished = True


async def test_signal_during_cleanup_does_not_interrupt_terminate(tmp_path: Path) -> None:
    p = SlowTerminate()
    p.statuses = ["RUNNING"]
    w = Worker(p)
    h = driver.Hooks(platform=p, worker=w, timing=FAST, install_signals=True, precheck=no_skip)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=1), SECRETS, h)
    # The deadline started the cleanup; the SIGTERM that came during the terminate call
    # must not cancel it, and the worker must still be stopped.
    assert p.terminate_finished and p.terminated == [outcome.execution_id]
    assert code == driver.EXIT_FAILED and "deadline" in (outcome.reason or "")
    assert w.cancelled


@pytest.fixture
def sdk_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The Workflows SDK config singleton, restored after the test."""
    from mistralai.workflows.core.config.config import config as wf_config

    monkeypatch.setattr(wf_config.worker, "local_retry_backoff_coefficient", None)
    monkeypatch.setattr(wf_config.worker, "deployment_name", wf_config.worker.deployment_name)
    monkeypatch.setattr(wf_config.common, "mistral_api_key", wf_config.common.mistral_api_key)
    monkeypatch.setenv("MISTRAL_API_KEY", "placeholder")
    monkeypatch.delenv("DEPLOYMENT_NAME", raising=False)
    return wf_config


def test_local_mode_drops_the_sdk_client_side_retry_wait(sdk_config: Any) -> None:
    driver._configure_workflows_sdk(None, KEY, local=True)
    assert sdk_config.worker.local_retry_backoff_coefficient == 0.0
    assert driver.LOCAL_RETRY_BACKOFF_COEFFICIENT == 0.0
    assert "DEPLOYMENT_NAME" not in os.environ


def test_worker_mode_keeps_the_sdk_retry_settings(sdk_config: Any) -> None:
    driver._configure_workflows_sdk("msreview-cli-o-r-pr1", KEY, local=False)
    assert sdk_config.worker.local_retry_backoff_coefficient is None
    assert sdk_config.worker.deployment_name == "msreview-cli-o-r-pr1"


async def test_local_mode_surfaces_a_fatal_activity_error_quickly(sdk_config: Any) -> None:
    """The SDK retries even non-retryable errors outside a worker; no waits between them."""
    import time

    from mistralai import workflows

    from morning_star_review.core.errors import MsreviewError

    class FatalError(MsreviewError):
        non_retryable = True

    attempts: list[float] = []

    @workflows.activity(
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy_max_attempts=6,
        retry_policy_backoff_coefficient=3.0,
        _skip_registering=True,
    )
    async def msreview_test_fatal_activity() -> None:
        attempts.append(time.monotonic())
        raise FatalError("fatal")

    driver._configure_workflows_sdk(None, KEY, local=True)
    begun = time.monotonic()
    with pytest.raises(FatalError):
        await msreview_test_fatal_activity()
    assert len(attempts) == 6  # the SDK still re-runs it: only the waits are gone
    assert time.monotonic() - begun < 1.0  # was ~3.1 s at 0.1, ~6 min at the default


class CrowdedPlatform(FakePlatform):
    """A second run's worker registers on the stable deployment right after ours."""

    def __init__(self, *, live_before: bool = False) -> None:
        super().__init__()
        self.started_on: list[str] = []
        self.live_before = live_before
        self.deployment_reads = 0

    async def deployment(self, name: str) -> DeploymentState | None:
        self.calls.append(("deployment", name))
        stable = name == deployment_name("cli", REF)
        self.deployment_reads += 1
        if stable and self.live_before and self.deployment_reads >= 2:
            # Registered between choose_deployment and our pre-start read.
            return DeploymentState(name, True, 1, datetime.now(UTC))
        if self.worker_running:
            return DeploymentState(name, True, 2 if stable else 1, datetime.now(UTC))
        return None

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None:
        self.started_on.append(deployment)
        await super().start(
            workflow=workflow, deployment=deployment, execution_id=execution_id, payload=payload
        )


async def test_a_second_worker_on_our_deployment_moves_us_away(tmp_path: Path) -> None:
    # Security review PL-1: two runs for one PR picked the same stable deployment.
    p = CrowdedPlatform()
    w = Worker(p)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == 0 and outcome.status == "posted"
    assert w.starts == 2  # stopped on the shared deployment, started again on our own
    assert len(p.started_on) == 1
    assert p.started_on[0].startswith(deployment_name("cli", REF).rsplit("-pr", 1)[0])
    assert p.started_on[0] != deployment_name("cli", REF) and "-x" in p.started_on[0]


async def test_a_worker_that_registered_before_ours_is_not_shared(tmp_path: Path) -> None:
    p = CrowdedPlatform(live_before=True)
    w = Worker(p)
    _outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, w))
    assert code == 0
    assert w.starts == 1  # moved before our worker started
    assert p.started_on and "-x" in p.started_on[0]


# ----------------------------------------------------------------------------- slow, not dead
# Workflows API rate limits and server errors make the run wait; they do not end it.


class FakeClock:
    """The driver's monotonic clock (``driver._now``); only the hooks' sleeps advance it."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)  # let the worker task run


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(driver, "_now", c.now)
    return c


def rate_limited(what: str = "get_workflow_execution") -> PlatformError:
    return PlatformError(f"{what} failed (HTTP 429)", 429)


class RateLimitedPolls(FakePlatform):
    """``execution`` answers 429 for ``seconds`` (fake time) from the first poll."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds
        self.until: float | None = None
        self.refused = 0

    async def execution(self, execution_id: str) -> ExecutionState:
        if self.until is None:
            self.until = self.clock.t + self.seconds
        if self.clock.t < self.until:
            self.calls.append(("execution", execution_id))
            self.refused += 1
            raise rate_limited()
        return await super().execution(execution_id)


async def test_polling_waits_out_five_minutes_of_rate_limits(
    tmp_path: Path, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    p = RateLimitedPolls(clock, 300.0)
    h = hooks(p, Worker(p), sleep=clock.sleep)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert code == 0 and outcome.status == "posted"
    assert p.refused >= 10 and p.terminated == []
    assert p.until is not None and clock.t >= p.until  # five fake minutes went by
    polls = clock.sleeps[-(p.refused + 2) :]
    # The interval backs off to the cap while the errors last, then returns to poll_s.
    assert max(polls) == FAST.api_backoff_max_s == 30.0
    assert polls[0] == FAST.poll_s * 2**0 and polls[1] == FAST.poll_s * 2
    assert polls[-2:] == [FAST.poll_s, FAST.poll_s]
    err = capsys.readouterr().err
    assert "still waiting, with a backoff" in err
    assert f"answers again after {p.refused} errors" in err


async def test_polling_stops_at_the_hard_deadline_while_rate_limited(
    tmp_path: Path, clock: FakeClock
) -> None:
    p = RateLimitedPolls(clock, 10_000.0)
    h = hooks(p, Worker(p), sleep=clock.sleep)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=1200), SECRETS, h)
    assert code == driver.EXIT_FAILED and "20-minute deadline" in (outcome.reason or "")
    assert p.terminated == [outcome.execution_id]
    assert clock.t <= 1000.0 + 1200 + 1e-6  # no sleep runs past the deadline


class BrokenPolls(FakePlatform):
    async def execution(self, execution_id: str) -> ExecutionState:
        self.calls.append(("execution", execution_id))
        raise PlatformError("get_workflow_execution failed (HTTP 404)", 404)


async def test_a_poll_error_that_will_not_pass_fails_as_before(tmp_path: Path) -> None:
    p = BrokenPolls()
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks(p, Worker(p)))
    assert code == driver.EXIT_FAILED and outcome.status == "failed"
    assert "Workflows API error" in (outcome.reason or "") and "HTTP 404" in (outcome.reason or "")
    assert [c[0] for c in p.calls].count("execution") == 1
    assert p.terminated == [outcome.execution_id]


class FlakyWhoami(FakePlatform):
    def __init__(self, errors: list[PlatformError]) -> None:
        super().__init__()
        self.errors = errors

    async def whoami(self) -> Whoami:
        if self.errors:
            self.calls.append(("whoami", None))
            raise self.errors.pop(0)
        return await super().whoami()


SLOW_BACKOFF = driver.Timing(**{**FAST.__dict__, "start_retry_s": 2.0})


async def test_preflight_waits_out_rate_limits(tmp_path: Path, clock: FakeClock) -> None:
    p = FlakyWhoami([rate_limited("whoami") for _ in range(3)])
    h = hooks(p, Worker(p), sleep=clock.sleep, timing=SLOW_BACKOFF)
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, h)
    assert code == 0 and outcome.status == "posted"
    assert [c[0] for c in p.calls].count("whoami") == 4
    assert clock.sleeps[:3] == [2.0, 4.0, 8.0]  # 2 s doubling


async def test_preflight_gives_up_after_its_retry_budget(tmp_path: Path, clock: FakeClock) -> None:
    p = FlakyWhoami([rate_limited("whoami") for _ in range(1000)])
    w = Worker(p)
    h = hooks(p, w, sleep=clock.sleep, timing=SLOW_BACKOFF)
    with pytest.raises(driver.DriverError, match="cannot reach the Mistral Workflows API"):
        await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert 270.0 <= sum(clock.sleeps) <= SLOW_BACKOFF.preflight_retry_s == 300.0
    assert max(clock.sleeps) == 30.0  # the backoff cap
    assert w.starts == 0


async def test_start_waits_out_rate_limits_with_the_same_id(
    tmp_path: Path, clock: FakeClock
) -> None:
    p = FakePlatform()
    p.start_errors = [rate_limited("execute_workflow") for _ in range(4)]
    h = hooks(p, Worker(p), sleep=clock.sleep, timing=SLOW_BACKOFF)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert code == 0 and outcome.status == "posted"
    ids = [c[1] for c in p.calls if c[0] == "start"]
    assert len(ids) == 5 and set(ids) == {outcome.execution_id}
    assert [s for s in clock.sleeps if s >= 2.0][:4] == [2.0, 4.0, 8.0, 16.0]


async def test_start_gives_up_after_its_retry_budget(tmp_path: Path, clock: FakeClock) -> None:
    p = FakePlatform()
    p.start_errors = [rate_limited("execute_workflow") for _ in range(1000)]
    h = hooks(p, Worker(p), sleep=clock.sleep, timing=SLOW_BACKOFF)
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=3600), SECRETS, h)
    assert code == driver.EXIT_FAILED and "HTTP 429" in (outcome.reason or "")
    assert 570.0 <= sum(s for s in clock.sleeps if s >= 2.0) <= 600.0
    assert p.terminated == [outcome.execution_id]  # the start may have reached the server


class RateLimitedSweepReads(FakePlatform):
    """Other runs' executions; reads about some of them are refused."""

    def __init__(self, refuse: dict[str, PlatformError]) -> None:
        super().__init__()
        self.refuse = refuse

    async def execution(self, execution_id: str) -> ExecutionState:
        if execution_id in self.refuse:
            self.calls.append(("execution", execution_id))
            raise self.refuse[execution_id]
        return await super().execution(execution_id)

    async def deployment(self, name: str) -> DeploymentState | None:
        if name in self.refuse:
            self.calls.append(("deployment", name))
            raise self.refuse[name]
        return await super().deployment(name)


async def test_sweep_does_not_terminate_when_a_read_is_rate_limited() -> None:
    prefix = "msreview-ci-octo-org-widgets-pr7-abcdef1-"
    slow, busy, gone, orphan = (prefix + x for x in ("0a0b0c0d", "1a1b1c1d", "2a2b2c2d", "3a3b"))
    p = RateLimitedSweepReads(
        {
            "dep-slow": rate_limited("get_deployment"),  # the deployment read is refused
            busy: PlatformError("get_workflow_execution failed (HTTP 503)", 503),
            "dep-gone": PlatformError("get_deployment failed (HTTP 400)", 400),
        }
    )
    p.running = [slow, busy, gone, orphan]
    p.foreign = {slow: "dep-slow", gone: "dep-gone", orphan: "dep-orphan"}
    terminated = await driver.sweep_orphans(p, REF, PR, FAST)
    # A rate limit or a server error proves nothing: that run may be alive and slow.
    assert terminated == p.terminated == [gone, orphan]


async def test_run_context_carries_the_clock_and_the_token_box(tmp_path: Path) -> None:
    from morning_star_review.core.runctx import RunContext, run_context

    seen: list[RunContext] = []

    async def runner(req: Any) -> Any:
        seen.append(run_context())
        return outcome_dict(seen[0].execution_id)

    async def refresh() -> str | None:
        return "ghs_refreshed_token_abcdefghijklmnop"

    secrets = driver.DriverSecrets(
        mistral_api_key=KEY,
        github_token="ghs_first_token_abcdefghijklmnopqr",
        github_token_refresh=refresh,
        github_token_minted_epoch=999_000.0,
    )
    cfg = make_cfg(
        tmp_path,
        mode="local",
        deadline_s=5400,
        wind_down_after_s=600.0,
        defer_publish_dir=tmp_path / "plans",
    )
    h = driver.Hooks(
        local_runner=runner, timing=FAST, install_signals=False, wall_clock=lambda: 1_000_000.0
    )
    outcome, code = await driver.run_review(cfg, secrets, h)
    assert code == 0 and outcome.status == "posted"
    ctx = seen[0]
    assert (ctx.run_started_epoch, ctx.wind_down_epoch, ctx.hard_epoch) == (
        1_000_000.0,
        1_000_600.0,
        1_005_400.0,
    )
    assert ctx.defer_publish is True
    box = ctx.token_box
    assert box is not None and box.refresh is refresh and box.minted_epoch == 999_000.0
    assert ctx.current_github_token() == "ghs_first_token_abcdefghijklmnopqr"
    assert "ghs_first_token_abcdefghijklmnopqr" not in repr(ctx)

    # No refresh (ci mode, or no App): no box; no defer dir: posting is not deferred.
    seen.clear()
    plain = driver.DriverSecrets(mistral_api_key=KEY, github_token="ghs_plain_token_abcdefghijk")
    await driver.run_review(make_cfg(tmp_path, mode="local", deadline_s=5400), plain, h)
    assert seen[0].token_box is None and seen[0].defer_publish is False
    assert seen[0].current_github_token() == "ghs_plain_token_abcdefghijk"
    assert seen[0].hard_epoch == 1_005_400.0 and seen[0].wind_down_epoch == 1_004_500.0


async def test_the_wind_down_is_announced_at_w(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = [1_000_000.0]
    before: list[str] = []

    async def runner(req: Any) -> Any:
        await asyncio.sleep(0.05)
        before.append(capsys.readouterr().err)
        now[0] += 301.0  # past W (5 minutes after the start)
        await asyncio.sleep(0.1)
        return outcome_dict("e")

    timing = driver.Timing(**{**FAST.__dict__, "wind_down_check_s": 0.01})
    h = driver.Hooks(
        local_runner=runner, timing=timing, install_signals=False, wall_clock=lambda: now[0]
    )
    cfg = make_cfg(tmp_path, mode="local", deadline_s=5400, wind_down_after_s=300.0)
    await driver.run_review(cfg, SECRETS, h)
    after = capsys.readouterr().err
    assert "msreview: deadline 90 min; wind-down at 5 min" in before[0]
    line = "msreview: wind-down: 5 of 90 minutes used; finishing the review with what it has"
    assert line not in before[0]
    assert after.count(line) == 1


async def test_the_wind_down_announcer_stops_with_the_run(tmp_path: Path) -> None:
    p = FakePlatform()
    h = hooks(p, Worker(p))
    before = {t for t in asyncio.all_tasks() if not t.done()}
    outcome, code = await driver.run_review(make_cfg(tmp_path, deadline_s=5400), SECRETS, h)
    assert code == 0 and outcome.status == "posted"
    await asyncio.sleep(0)
    left = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert left <= {asyncio.current_task()}


class RateLimitedTerminate(FakePlatform):
    def __init__(self, refusals: int) -> None:
        super().__init__()
        self.refusals = refusals

    async def terminate(self, execution_id: str) -> None:
        if self.refusals:
            self.refusals -= 1
            self.calls.append(("terminate", execution_id))
            raise rate_limited("terminate_workflow_execution")
        await super().terminate(execution_id)


async def test_a_rate_limited_terminate_is_tried_again(tmp_path: Path) -> None:
    p = RateLimitedTerminate(2)
    p.statuses = ["RUNNING"]
    outcome, code = await driver.run_review(
        make_cfg(tmp_path, deadline_s=1), SECRETS, hooks(p, Worker(p))
    )
    assert code == driver.EXIT_FAILED and "deadline" in (outcome.reason or "")
    assert [c[0] for c in p.calls].count("terminate") == 3
    assert p.terminated == [outcome.execution_id]
