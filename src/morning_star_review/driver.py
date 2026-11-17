"""Worker lifecycle and workflow execution control for one review run (DESIGN 11).

One driver process serves exactly one review: it starts its own Workflows worker on a
per-PR deployment, starts the execution routed to that deployment, waits for it, and
always terminates the execution and stops the worker before it exits. ``--local`` runs
the workflow in-process instead (no server).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import secrets as pysecrets
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from morning_star_review.core.contract import (
    Mode,
    OperatorPolicy,
    ReviewOutcome,
    ReviewRequest,
    RunOverrides,
    Severity,
    severity_at_least,
)
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.naming import (
    WORKFLOW_NAME,
    PrRef,
    deployment_name,
    execution_id,
    execution_id_prefix,
    parse_pr_url,
)
from morning_star_review.core.redact import redact
from morning_star_review.platform import (
    API_BASE,
    TERMINAL_STATUSES,
    AlreadyStarted,
    DeploymentState,
    ExecutionState,
    Platform,
    PlatformAuthError,
    PlatformError,
)

__all__ = [
    "EXIT_CANCELLED",
    "EXIT_FAILED",
    "EXIT_FAIL_ON",
    "EXIT_OK",
    "EXIT_USAGE",
    "DriverConfig",
    "DriverError",
    "DriverSecrets",
    "FailOn",
    "Timing",
    "exit_code_for",
    "fail_on_triggered",
    "prepare_process_env",
    "run_review",
]

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2  # usage, configuration or preflight error
EXIT_FAIL_ON = 3
EXIT_CANCELLED = 130

# Shown after terminating an execution while our worker still runs one of its activities.
TERMINATED_ACTIVITY_NOTE = (
    "msreview: a Temporal 'Activity not found on completion' warning may follow; it is "
    "expected after a termination and harmless"
)

# Wait multiplier of the Workflows SDK's client-side activity retry in --local mode.
LOCAL_RETRY_BACKOFF_COEFFICIENT = 0.0

FailOn = Literal["never", "findings", "high", "critical"]
FAIL_ON_VALUES: tuple[str, ...] = ("never", "findings", "high", "critical")

# Bare names that crash or reconfigure the Workflows config singleton at import.
_POISON_ENV = ("AGENT", "HTTP", "COMMON", "WORKER", "TEMPORAL")
# The Workflows SDK talks HTTP through the httpx2/httpcore2 forks, which log every request at INFO.
_QUIET_LOGGERS = (
    "temporalio",
    "mistralai.workflows",
    "mistralai",
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
)


class DriverError(MsreviewError):
    """A usage, configuration or preflight problem (exit code 2). Safe to show."""


@dataclass
class DriverConfig:
    mode: Mode
    pr_url: str
    expected_head_sha: str | None
    post: Literal["auto", "never"]
    force: bool
    overrides: RunOverrides
    policy: OperatorPolicy
    run_label: str
    deadline_s: int
    out_dir: Path | None
    fail_on: FailOn
    verbose: bool = False
    telemetry: bool = False
    agent_python: str = ""
    run_root: Path | None = None  # default: a fresh temporary directory
    keep_run_dir: bool = False
    stop_commands: Any = None  # outputs.StopCommands while the Actions log is muted


@dataclass
class DriverSecrets:
    mistral_api_key: str = field(repr=False)
    github_token: str | None = field(default=None, repr=False)
    read_token: str | None = field(default=None, repr=False)
    bot_login: str | None = None
    cannot_post_reason: str | None = None

    def values(self) -> list[str]:
        return [v for v in (self.mistral_api_key, self.github_token, self.read_token) if v]


@dataclass
class Timing:
    """Polling and timeout knobs (tests shrink them)."""

    readiness_timeout_s: float = 90.0
    readiness_poll_s: float = 1.0
    live_heartbeat_s: float = 25.0  # a deployment heartbeat newer than this = live worker
    busy_wait_s: float = 30.0
    start_attempts: int = 3
    start_retry_s: float = 2.0
    poll_s: float = 3.0
    terminate_timeout_s: float = 8.0
    worker_stop_timeout_s: float = 15.0
    max_poll_errors: int = 10


def prepare_process_env(
    *, telemetry: bool, verbose: bool = False, env: MutableMapping[str, str] | None = None
) -> None:
    """Step 1 of DESIGN 11, before ``mistralai.workflows`` is imported.

    The Workflows config reads ``.env`` from the cwd at import, so the process moves to
    an empty directory; variables that break or redirect the config are removed or
    refused; telemetry stays off unless asked for.
    """
    target = os.environ if env is None else env
    cwd = tempfile.mkdtemp(prefix="msreview-cwd-")
    os.chdir(cwd)
    atexit.register(shutil.rmtree, cwd, True)
    for name in _POISON_ENV:
        target.pop(name, None)
    server = target.get("SERVER_URL", "").strip().rstrip("/")
    if server and server != API_BASE and target.get("MSREVIEW_ALLOW_CUSTOM_SERVER") != "1":
        raise DriverError(
            f"SERVER_URL is set to a server other than {API_BASE}; unset it or set "
            "MSREVIEW_ALLOW_CUSTOM_SERVER=1"
        )
    if not telemetry:
        target["OTEL_ENABLED"] = "false"
    level = logging.INFO if verbose else logging.WARNING
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(level)


def fail_on_triggered(outcome: ReviewOutcome, fail_on: str) -> bool:
    """True when the open findings meet the ``fail-on`` threshold."""
    if fail_on == "never":
        return False
    open_findings = [f for f in outcome.findings if f.state in ("open", "summary_only")]
    if fail_on == "findings":
        return bool(open_findings)
    threshold: Severity = "critical" if fail_on == "critical" else "high"
    return any(severity_at_least(f.severity, threshold) for f in open_findings)


def exit_code_for(outcome: ReviewOutcome, fail_on: str) -> int:
    """Exit codes of DESIGN 11.14 (130 is decided by the caller)."""
    if outcome.status == "failed":
        return EXIT_FAILED
    if fail_on_triggered(outcome, fail_on):
        return EXIT_FAIL_ON
    return EXIT_OK


def _now() -> float:
    return time.monotonic()


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- requests


def build_request(cfg: DriverConfig) -> tuple[ReviewRequest, PrRef]:
    """The workflow input; raises ``DriverError`` for a bad URL or invalid settings."""
    try:
        ref = parse_pr_url(cfg.pr_url)
    except ValueError as exc:
        raise DriverError(str(exc)) from None
    try:
        req = ReviewRequest(
            pr_url=ref.canonical_url,
            repo=ref.full_name,
            mode=cfg.mode,
            expected_head_sha=cfg.expected_head_sha or None,
            post=cfg.post,
            force=cfg.force,
            overrides=cfg.overrides,
            policy=cfg.policy,
            run_label=cfg.run_label[:200],
        )
    except ValueError as exc:
        raise DriverError(f"invalid review settings: {exc}") from None
    return req, ref


def _heartbeat_age(d: DeploymentState, now: datetime) -> float | None:
    if d.last_heartbeat is None:
        return None
    hb = d.last_heartbeat if d.last_heartbeat.tzinfo else d.last_heartbeat.replace(tzinfo=UTC)
    return (now - hb).total_seconds()


def is_live(d: DeploymentState | None, timing: Timing, now: datetime | None = None) -> bool:
    """A deployment with an active worker that heartbeated recently (another driver)."""
    if d is None or not d.is_active or d.active_worker_count <= 0:
        return False
    age = _heartbeat_age(d, now or datetime.now(UTC))
    return age is None or age < timing.live_heartbeat_s


async def choose_deployment(
    platform: Platform,
    mode: Mode,
    ref: PrRef,
    timing: Timing,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """The stable per-PR name, or a ``-x<hex>`` variant when another worker is live on it."""

    async def state(name: str) -> DeploymentState | None:
        for attempt in range(1, 4):  # a brief API hiccup should not end the run
            try:
                return await platform.deployment(name)
            except PlatformError as exc:
                if not exc.retryable or attempt == 3:
                    raise
                await sleep(timing.start_retry_s * attempt)
        return None  # unreachable

    name = deployment_name(mode, ref)
    if not is_live(await state(name), timing):
        return name
    _say(f"msreview: deployment {name} has a live worker; waiting up to {timing.busy_wait_s:g}s")
    await sleep(timing.busy_wait_s)
    if not is_live(await state(name), timing):
        return name
    return deployment_name(mode, ref, suffix="x" + pysecrets.token_hex(2))


async def sweep_orphans(platform: Platform, ref: PrRef, pr_url: str) -> list[str]:
    """CI only: terminate RUNNING executions of earlier CI runs for this PR."""
    prefix = execution_id_prefix("ci", ref)
    terminated: list[str] = []
    try:
        running = await platform.running_for_pr(WORKFLOW_NAME, pr_url)
    except PlatformError as exc:
        _say(f"msreview: orphan sweep skipped ({exc})")
        return terminated
    for eid in running:
        if not eid.startswith(prefix):
            continue
        try:
            await platform.terminate(eid)
        except PlatformError as exc:
            _say(f"msreview: could not terminate orphan {eid} ({exc})")
            continue
        _say(f"msreview: terminated orphan execution {eid}")
        terminated.append(eid)
    return terminated


def default_run_root(mode: Mode, env: Mapping[str, str] | None = None) -> Path:
    src = os.environ if env is None else env
    runner_temp = src.get("RUNNER_TEMP", "").strip()
    if mode == "ci" and runner_temp:
        return Path(runner_temp) / "msreview" / "runs"
    return Path(tempfile.mkdtemp(prefix="msreview-runs-"))


def failed_outcome(req: ReviewRequest, eid: str, reason: str, secrets: list[str]) -> ReviewOutcome:
    return ReviewOutcome(
        status="failed",
        reason=redact(reason, secrets)[:1000],
        pr_url=req.pr_url,
        execution_id=eid,
    )


def outcome_from_run_dir(run_dir: Path, eid: str) -> ReviewOutcome | None:
    """``outcome.json`` written by ``publish`` or ``report_failure`` for this execution."""
    path = run_dir / "outcome.json"
    try:
        raw = path.read_text(encoding="utf-8")
        outcome = ReviewOutcome.model_validate_json(raw)
    except (OSError, ValueError):
        return None
    return outcome if outcome.execution_id == eid else None


# ----------------------------------------------------------------------------- the run


class RunFailed(MsreviewError):  # noqa: N818 - reads like the condition it reports
    """The run cannot finish normally (worker death, readiness timeout, deadline)."""


WorkerFactory = Callable[[], Awaitable[None]]


@dataclass
class Hooks:
    """Injection points; production uses the defaults."""

    platform: Platform | None = None
    worker: WorkerFactory | None = None
    timing: Timing = field(default_factory=Timing)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    install_signals: bool = True
    local_runner: Callable[[ReviewRequest], Awaitable[Any]] | None = None
    # Point the real Workflows SDK config at the run (default: only with the real worker).
    configure_sdk: bool | None = None


async def _default_worker() -> None:
    from mistralai import workflows

    from morning_star_review.workflow import CodeReviewWorkflow

    await workflows.run_worker([CodeReviewWorkflow])


async def _default_local_runner(req: ReviewRequest) -> Any:
    from mistralai import workflows

    from morning_star_review.workflow import CodeReviewWorkflow

    return await workflows.execute_workflow(CodeReviewWorkflow, params=req)


def _configure_workflows_sdk(deployment: str | None, api_key: str, *, local: bool) -> None:
    """Point the (import-time) Workflows config at this run's deployment and key."""
    os.environ["MISTRAL_API_KEY"] = api_key
    if deployment:
        os.environ["DEPLOYMENT_NAME"] = deployment
    from mistralai.workflows.core.config.config import config as wf_config
    from pydantic import SecretStr

    wf_config.common.mistral_api_key = SecretStr(api_key)
    if deployment:
        wf_config.worker.deployment_name = deployment
    if local:
        # Outside a worker the SDK retries every activity error client-side (tenacity,
        # up to the activity's max attempts), even non-retryable ones, and waits
        # coefficient * 2**n between attempts. Our own layers already wait where waiting
        # helps (GitHub reads back off in-process, the agent SDK retries 429s), so the
        # SDK wait only delays a fatal error: drop it.
        wf_config.worker.local_retry_backoff_coefficient = LOCAL_RETRY_BACKOFF_COEFFICIENT


class _Run:
    def __init__(
        self,
        cfg: DriverConfig,
        secrets: DriverSecrets,
        hooks: Hooks,
        req: ReviewRequest,
        ref: PrRef,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.hooks = hooks
        self.timing = hooks.timing
        self.req = req
        self.ref = ref
        self.started_at = _now()
        self.deadline = self.started_at + cfg.deadline_s
        self.eid = execution_id(cfg.mode, ref, cfg.expected_head_sha, pysecrets.token_hex(4))
        self.deployment: str | None = None
        self.platform: Platform | None = None
        self.worker: asyncio.Task[None] | None = None
        self.start_attempted = False
        self.terminal = False
        self.stopping = False  # cleanup has begun; a signal must not interrupt it
        self.run_root = cfg.run_root or default_run_root(cfg.mode)
        self.ready_seconds: float | None = None

    @property
    def run_dir(self) -> Path:
        return self.run_root / self.eid

    def fail(self, reason: str) -> ReviewOutcome:
        return failed_outcome(self.req, self.eid, reason, self.secrets.values())

    def context(self, on_worker: bool) -> Any:
        from morning_star_review.core.runctx import RunContext
        from morning_star_review.core.seal import request_digest

        return RunContext(
            execution_id=self.eid,
            request_sha256=request_digest(self.req),
            seal_key=pysecrets.token_bytes(32),
            run_root=str(self.run_root),
            mode=self.cfg.mode,
            on_worker=on_worker,
            github_token=self.secrets.github_token,
            read_token=self.secrets.read_token,
            bot_login=self.secrets.bot_login,
            mistral_api_key=self.secrets.mistral_api_key,
            agent_python=self.cfg.agent_python,
            cannot_post_reason=self.secrets.cannot_post_reason,
            telemetry=self.cfg.telemetry,
        )

    def remaining(self) -> float:
        return self.deadline - _now()

    # ------------------------------------------------------------------ local mode

    async def run_local(self) -> ReviewOutcome:
        from morning_star_review.core.runctx import set_run_context

        set_run_context(self.context(on_worker=False))
        runner = self.hooks.local_runner
        if runner is None:
            _configure_workflows_sdk(None, self.secrets.mistral_api_key, local=True)
            runner = _default_local_runner
        _say(f"msreview: execution {self.eid} (local, in-process)")
        try:
            result = await asyncio.wait_for(runner(self.req), max(1.0, self.remaining()))
        except TimeoutError:
            minutes = self.cfg.deadline_s / 60
            return self.fail(f"the review did not finish within the {minutes:g}-minute deadline")
        except Exception as exc:
            found = outcome_from_run_dir(self.run_dir, self.eid)
            if found is not None:
                return found
            return self.fail(f"{type(exc).__name__}: {exc}")
        self.terminal = True
        if isinstance(result, ReviewOutcome):
            return result
        try:
            return ReviewOutcome.model_validate(result)
        except ValueError:
            return self.fail("the workflow returned an invalid result")

    # ------------------------------------------------------------------ worker mode

    def _platform(self) -> Platform:
        if self.platform is None:
            if self.hooks.platform is not None:
                self.platform = self.hooks.platform
            else:
                from morning_star_review.platform import MistralPlatform

                self.platform = MistralPlatform(self.secrets.mistral_api_key)
        return self.platform

    async def preflight(self) -> str:
        """whoami with the key; a refused key ends the run before any worker starts."""
        try:
            who = await self._platform().whoami()
        except PlatformAuthError as exc:
            raise DriverError(
                f"the Mistral API key was refused by the Workflows API ({exc}); check "
                "the key and that its workspace has Workflows access"
            ) from None
        except PlatformError as exc:
            raise DriverError(f"cannot reach the Mistral Workflows API ({exc})") from None
        return who.namespace

    async def run_on_worker(self) -> ReviewOutcome:
        from morning_star_review.core.runctx import set_run_context

        namespace = await self.preflight()
        platform = self._platform()
        try:
            self.deployment = await choose_deployment(
                platform, self.cfg.mode, self.ref, self.timing, self.hooks.sleep
            )
        except PlatformError as exc:
            raise DriverError(f"cannot read the deployment status ({exc})") from None
        _say(
            f"msreview: execution {self.eid} on deployment {self.deployment} "
            f"(workspace namespace {namespace[:8]})"
        )
        if self.cfg.mode == "ci":
            await sweep_orphans(platform, self.ref, self.req.pr_url)
        set_run_context(self.context(on_worker=True))
        factory = self.hooks.worker
        configure = self.hooks.configure_sdk
        if configure is None:
            configure = factory is None
        if configure:
            _configure_workflows_sdk(self.deployment, self.secrets.mistral_api_key, local=False)
        if factory is None:
            factory = _default_worker
        try:
            self.worker = asyncio.create_task(self._guarded_worker(factory))
            await self._wait_ready()
            await self._start()
            state = await self._wait_done()
            return self._outcome_of(state)
        except RunFailed as exc:
            return self.fail(str(exc))
        except PlatformError as exc:
            return self.fail(f"Workflows API error: {exc}")
        finally:
            await self.cleanup()

    async def _guarded_worker(self, factory: WorkerFactory) -> None:
        try:
            await factory()
        except SystemExit as exc:  # run_worker exits the process on terminal errors
            raise RunFailed(f"the Workflows worker exited (code {exc.code})") from None

    def _worker_failure(self) -> RunFailed:
        if self.worker is None:
            return RunFailed("the Workflows worker did not start")
        if self.worker.cancelled():
            return RunFailed("the Workflows worker was cancelled")
        exc = self.worker.exception()
        if exc is None:
            return RunFailed("the Workflows worker stopped unexpectedly")
        if isinstance(exc, RunFailed):
            return exc
        return RunFailed(f"the Workflows worker failed: {type(exc).__name__}: {exc}")

    def _deployment_name(self) -> str:
        if self.deployment is None:
            raise RunFailed("no deployment was chosen")
        return self.deployment

    async def _wait_ready(self) -> None:
        name = self._deployment_name()
        platform = self._platform()
        limit = _now() + self.timing.readiness_timeout_s
        begun = _now()
        while True:
            if self.worker is not None and self.worker.done():
                raise self._worker_failure()
            try:
                d = await platform.deployment(name)
            except PlatformError as exc:
                if not exc.retryable:
                    raise
                d = None
            if d is not None and d.is_active and d.active_worker_count > 0:
                self.ready_seconds = _now() - begun
                _say(f"msreview: worker ready in {self.ready_seconds:.1f}s")
                return
            if _now() >= limit:
                limit_s = self.timing.readiness_timeout_s
                raise RunFailed(f"the Workflows worker was not ready within {limit_s:g}s")
            await self.hooks.sleep(self.timing.readiness_poll_s)

    async def _start(self) -> None:
        name = self._deployment_name()
        platform = self._platform()
        payload = self.req.model_dump(mode="json")
        for attempt in range(1, self.timing.start_attempts + 1):
            self.start_attempted = True
            try:
                await platform.start(
                    workflow=WORKFLOW_NAME,
                    deployment=name,
                    execution_id=self.eid,
                    payload=payload,
                )
            except AlreadyStarted:
                _say(f"msreview: execution {self.eid} already started; attaching")
                return
            except PlatformError as exc:
                if not exc.retryable or attempt == self.timing.start_attempts:
                    raise
                await self.hooks.sleep(self.timing.start_retry_s * attempt)
                continue
            return

    async def _wait_done(self) -> ExecutionState:
        platform = self._platform()
        errors = 0
        while True:
            if self.worker is not None and self.worker.done():
                raise self._worker_failure()
            if self.remaining() <= 0:
                minutes = self.cfg.deadline_s / 60
                raise RunFailed(f"the review did not finish within the {minutes:g}-minute deadline")
            try:
                state = await platform.execution(self.eid)
            except PlatformError as exc:
                errors += 1
                if not exc.retryable or errors > self.timing.max_poll_errors:
                    raise
            else:
                errors = 0
                if state.status in TERMINAL_STATUSES:
                    self.terminal = True
                    return state
            await self.hooks.sleep(min(self.timing.poll_s, max(0.0, self.remaining())))

    def _outcome_of(self, state: ExecutionState) -> ReviewOutcome:
        if state.status == "COMPLETED":
            try:
                return ReviewOutcome.model_validate(state.result)
            except ValueError:
                found = outcome_from_run_dir(self.run_dir, self.eid)
                return found or self.fail("the workflow returned an invalid result")
        found = outcome_from_run_dir(self.run_dir, self.eid)
        if found is not None and found.status == "failed":
            return found
        return self.fail(f"the workflow execution ended {state.status}")

    async def cleanup(self) -> None:
        """Step 11: terminate a non-terminal execution, then stop the worker."""
        self.stopping = True
        if self.start_attempted and not self.terminal and self.platform is not None:
            try:
                await asyncio.wait_for(
                    self.platform.terminate(self.eid), self.timing.terminate_timeout_s
                )
                _say(f"msreview: terminated execution {self.eid}")
                if self.worker is not None and not self.worker.done():
                    # The Temporal core (Rust) prints its own WARN when an activity that
                    # was still running reports back to the terminated execution. It
                    # writes to stderr directly, so it cannot be filtered here; say why.
                    _say(TERMINATED_ACTIVITY_NOTE)
            except (PlatformError, TimeoutError) as exc:
                _say(f"msreview: could not terminate execution {self.eid} ({exc or 'timeout'})")
            self.terminal = True
        worker, self.worker = self.worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(worker, self.timing.worker_stop_timeout_s)
        elif worker is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                worker.result()  # retrieve it so asyncio does not log "never retrieved"


# ----------------------------------------------------------------------------- signals


class _SignalGuard:
    """Step 12: the first SIGTERM/SIGINT cancels the main task; later ones are ignored.

    Ignoring later signals keeps a second Ctrl-C (or Actions' SIGTERM after SIGINT) from
    interrupting the terminate call that the cancellation runs. A first signal that
    arrives while the cleanup is already running (after a deadline or a worker crash) is
    recorded but does not cancel: the cleanup is doing what the signal asks for.
    """

    def __init__(self, stopping: Callable[[], bool] | None = None) -> None:
        self.received: int | None = None
        self.cancelled_task = False
        self._stopping = stopping
        self._loop: asyncio.AbstractEventLoop | None = None
        self._installed: list[int] = []

    def install(self, task: asyncio.Task[Any]) -> None:
        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._on_signal, sig, task)
            except (NotImplementedError, RuntimeError, ValueError):
                continue  # not the main thread, or no signal support
            self._installed.append(sig)

    def _on_signal(self, signum: int, task: asyncio.Task[Any]) -> None:
        if self.received is not None:
            return
        self.received = signum
        name = signal.Signals(signum).name
        if self._stopping is not None and self._stopping():
            _say(f"msreview: received {name} while stopping; finishing the cleanup")
            return
        _say(f"msreview: received {name}; stopping")
        self.cancelled_task = True
        task.cancel()

    def remove(self) -> None:
        if self._loop is None:
            return
        for sig in self._installed:
            with contextlib.suppress(Exception):
                self._loop.remove_signal_handler(sig)
        self._installed.clear()


# ----------------------------------------------------------------------------- entry


async def run_review(
    cfg: DriverConfig, secrets: DriverSecrets, hooks: Hooks | None = None
) -> tuple[ReviewOutcome, int]:
    """Run one review and return ``(outcome, exit code)`` (DESIGN 11).

    Raises ``DriverError`` for usage, configuration and preflight problems (exit 2);
    every other problem becomes a ``failed`` outcome. Reports and CI outputs are written
    before returning.
    """
    from morning_star_review import outputs
    from morning_star_review.core.runctx import clear_run_context

    hooks = hooks or Hooks()
    if cfg.fail_on not in FAIL_ON_VALUES:
        raise DriverError(f"fail-on must be one of {', '.join(FAIL_ON_VALUES)}")
    if not secrets.mistral_api_key:
        raise DriverError("no Mistral API key")
    if cfg.deadline_s <= 0:
        raise DriverError("the deadline must be positive")
    req, ref = build_request(cfg)
    run = _Run(cfg, secrets, hooks, req, ref)
    guard = _SignalGuard(stopping=lambda: run.stopping)
    task = asyncio.current_task()
    if hooks.install_signals and task is not None:
        guard.install(task)
    code: int
    try:
        try:
            if cfg.mode == "local":
                outcome = await run.run_local()
            else:
                outcome = await run.run_on_worker()
            code = exit_code_for(outcome, cfg.fail_on)
        except asyncio.CancelledError:
            if not guard.cancelled_task or guard.received is None:
                raise
            if task is not None:
                task.uncancel()
            name = signal.Signals(guard.received).name
            outcome = run.fail(f"cancelled by {name}")
            code = EXIT_CANCELLED
        try:
            outputs.emit(cfg, outcome, run.run_dir, deployment=run.deployment)
        except Exception as exc:  # reporting problems never change the review result
            _say(f"msreview: could not write the reports ({type(exc).__name__}: {exc})")
    finally:
        guard.remove()
        clear_run_context()
        if not cfg.keep_run_dir:
            shutil.rmtree(run.run_dir, ignore_errors=True)
            if cfg.run_root is None:
                with contextlib.suppress(OSError):
                    run.run_root.rmdir()  # only if empty
    return outcome, code
