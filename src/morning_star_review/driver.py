"""Worker lifecycle and workflow execution control for one review run (DESIGN 11).

One driver process serves exactly one review: it starts its own Workflows worker on a
per-PR deployment, starts the execution routed to that deployment, waits for it, and
always terminates the execution and stops the worker before it exits. ``--local`` runs
the workflow in-process instead (no server).

A slow run is fine, a run that dies of rate limits is not: the driver plans the run
clock (``core.runclock``: wind-down W, hard deadline H) and passes it to the activities
in the run context. Workflows API rate limits and server errors (status None, 408, 429,
5xx) are waited out with a backoff instead of ending the run; only H ends it. At H the
driver terminates the execution and still records the failure on the PR in-process
(the deadline backstop).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import math
import os
import secrets as pysecrets
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from morning_star_review.core.contract import (
    MAX_USAGE_KEYS,
    FailureInput,
    Mode,
    OperatorPolicy,
    PrSnapshot,
    ReviewOutcome,
    ReviewRequest,
    Role,
    RunOverrides,
    Severity,
    Stage,
    Usage,
    severity_at_least,
    usage_key,
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
from morning_star_review.core.runclock import SNAPSHOT_FILE, RunClock, plan_clock
from morning_star_review.platform import (
    API_BASE,
    TERMINAL_STATUSES,
    AlreadyStarted,
    DeploymentState,
    ExecutionState,
    Platform,
    PlatformAuthError,
    PlatformError,
    transient_worker_error,
)

if TYPE_CHECKING:
    from morning_star_review.activities.fetch_pr import EarlySkip
    from morning_star_review.core.runctx import RunContext, TokenBox

__all__ = [
    "CI_CANCELLED",
    "CI_NO_KEY",
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
    "report_ci_usage_failure",
    "run_review",
    "usage_from_run_dir",
    "worker_registered",
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

# ci mode: the key comes only from the Action's `mistral-api-key` input (MISTRAL_API_KEY),
# and GitHub does not enforce `required: true` for composite-action inputs.
CI_NO_KEY = (
    "no Mistral API key: the Action input `mistral-api-key` is empty (for example, the "
    "MISTRAL_API_KEY secret is missing, or the workflow does not pass it to the action)"
)
# ci mode: Actions stops the step with a signal when someone cancels the workflow run.
CI_CANCELLED = "cancelled (the workflow run was cancelled)"

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
    # Test knob: start the wind-down this many seconds after the start (runclock).
    wind_down_after_s: float | None = None
    # CI deferred publish: when set, the run writes a publish plan and copies it here.
    defer_publish_dir: Path | None = None


@dataclass
class DriverSecrets:
    mistral_api_key: str = field(repr=False)
    github_token: str | None = field(default=None, repr=False)
    read_token: str | None = field(default=None, repr=False)
    bot_login: str | None = None
    cannot_post_reason: str | None = None
    # CLI: re-mints the App installation token (None when it cannot be minted).
    github_token_refresh: Callable[[], Awaitable[str | None]] | None = field(
        default=None, repr=False
    )
    github_token_minted_epoch: float | None = None

    def values(self) -> list[str]:
        return [v for v in (self.mistral_api_key, self.github_token, self.read_token) if v]


@dataclass
class Timing:
    """Polling and timeout knobs (tests shrink them)."""

    readiness_timeout_s: float = 90.0
    readiness_poll_s: float = 1.0
    live_heartbeat_s: float = 25.0  # a deployment heartbeat newer than this = live worker
    # Readiness accepts a heartbeat this much older than our worker's start (the server's
    # clock may run behind ours); later heartbeats (every 10 s) cover larger skews.
    clock_skew_s: float = 5.0
    precheck_timeout_s: float = 20.0  # the GitHub pre-check before any worker starts
    busy_wait_s: float = 30.0
    start_retry_s: float = 2.0  # first backoff after a retryable API error
    poll_s: float = 3.0
    terminate_timeout_s: float = 8.0
    terminate_retry_s: float = 15.0  # retryable terminate errors are retried this long
    worker_stop_timeout_s: float = 15.0
    # Workflows API errors that may pass (status None, 408, 429, 5xx) are retried with a
    # backoff from ``start_retry_s`` doubling up to ``api_backoff_max_s``, within these
    # budgets (and never past the hard deadline):
    api_backoff_max_s: float = 30.0
    preflight_retry_s: float = 300.0  # whoami and the deployment reads before the worker
    start_retry_budget_s: float = 600.0  # the execution start
    readiness_error_budget_s: float = 600.0  # extends readiness by the time lost to errors
    worker_restarts: int = 5  # a worker that fails with a 429/5xx before it is ready
    # Polling tolerates such errors until the hard deadline (interval up to the max).
    wind_down_check_s: float = 30.0  # the wind-down announcer checks the clock this often
    backstop_timeout_s: float = 120.0  # the deadline backstop (record the failure)


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
    """True when the open findings meet the ``fail-on`` threshold.

    Possible issues (unconfirmed, summary-only) do not count, as for the verdict.
    """
    if fail_on == "never":
        return False
    open_findings = outcome.counted_findings()
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
    # After SIGHUP the terminal may be gone; a failed write must not stop the cleanup.
    with contextlib.suppress(OSError, ValueError):
        print(line, file=sys.stderr, flush=True)


def _minutes(seconds: float) -> str:
    """``90``, ``0.5``: minutes for the progress lines."""
    return f"{round(seconds / 60, 1):g}"


def api_backoff(timing: Timing, failures: int, base: float | None = None) -> float:
    """The wait after ``failures`` retryable Workflows API errors in a row.

    ``base`` (default ``start_retry_s``, 2 s) doubles per failure up to
    ``api_backoff_max_s`` (30 s).
    """
    first = timing.start_retry_s if base is None else base
    exponent = min(max(0, failures - 1), 30)
    return min(timing.api_backoff_max_s, first * 2**exponent)


async def retry_api[T](
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    timing: Timing,
    budget_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    until: float | None = None,
) -> T:
    """``await call()``, retrying retryable ``PlatformError`` s with ``api_backoff``.

    Gives up (re-raises the last error) when the next wait would end more than
    ``budget_s`` after the first attempt, or after ``until`` (a ``_now()`` value: the
    hard deadline). Errors that are not retryable are raised at once.
    """
    begun = _now()
    limit = begun + budget_s if until is None else min(begun + budget_s, until)
    failures = 0
    while True:
        try:
            return await call()
        except PlatformError as exc:
            if not exc.retryable:
                raise
            failures += 1
            delay = api_backoff(timing, failures)
            if _now() + delay > limit:
                raise
            if failures == 1:
                _say(f"msreview: Workflows API error ({exc}); retrying {what} with a backoff")
            await sleep(delay)


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


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _heartbeat_age(d: DeploymentState, now: datetime) -> float | None:
    hb = _aware(d.last_heartbeat)
    return None if hb is None else (now - hb).total_seconds()


def _has_worker(d: DeploymentState | None) -> bool:
    return d is not None and d.is_active and d.active_worker_count > 0


def worker_registered(
    d: DeploymentState | None,
    *,
    since: datetime,
    before: DeploymentState | None,
    skew_s: float,
    before_known: bool = True,
) -> bool:
    """True when ``d`` shows that *our* worker registered (started at ``since``).

    ``is_active`` and the worker count outlive a worker that just stopped (the server
    expires them later), so they alone prove nothing. Either evidence counts:

    - more active workers than ``before`` (the deployment just before our worker
      started) had: a worker registered since, and no other driver's worker was live;
    - a heartbeat newer than ``before``'s and no older than ``since`` minus the clock
      skew allowed (the server's clock may run behind ours).

    ``before_known=False`` (that read failed) leaves only the heartbeat rule.
    """
    if d is None or not _has_worker(d):
        return False
    if before_known:
        had = before.active_worker_count if before is not None and _has_worker(before) else 0
        if d.active_worker_count > had:
            return True
    hb = _aware(d.last_heartbeat)
    if hb is None:
        return False
    old = _aware(before.last_heartbeat) if before is not None else None
    if old is not None and hb <= old:
        return False
    return hb >= since - timedelta(seconds=skew_s)


def is_live(d: DeploymentState | None, timing: Timing, now: datetime | None = None) -> bool:
    """A deployment with an active worker that heartbeated recently (another driver)."""
    if d is None or not _has_worker(d):
        return False
    age = _heartbeat_age(d, now or datetime.now(UTC))
    return age is None or age < timing.live_heartbeat_s


async def choose_deployment(
    platform: Platform,
    mode: Mode,
    ref: PrRef,
    timing: Timing,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    until: float | None = None,
) -> str:
    """The stable per-PR name, or a ``-x<hex>`` variant when another worker is live on it.

    Rate limits and server errors on the deployment read are retried for up to
    ``timing.preflight_retry_s`` (and not past ``until``, a ``_now()`` value).
    """

    async def state(name: str) -> DeploymentState | None:
        return await retry_api(
            lambda: platform.deployment(name),
            what="get_deployment",
            timing=timing,
            budget_s=timing.preflight_retry_s,
            sleep=sleep,
            until=until,
        )

    name = deployment_name(mode, ref)
    if not is_live(await state(name), timing):
        return name
    _say(f"msreview: deployment {name} has a live worker; waiting up to {timing.busy_wait_s:g}s")
    await sleep(timing.busy_wait_s)
    if not is_live(await state(name), timing):
        return name
    return deployment_name(mode, ref, suffix="x" + pysecrets.token_hex(2))


async def _served_by_live_worker(platform: Platform, eid: str, timing: Timing) -> bool:
    """True when ``eid`` runs on a deployment whose worker heartbeats: another live run.

    An unknown deployment (or an error that will not pass) counts as not live, so a
    true orphan is still terminated. A rate limit or server error proves nothing:
    it counts as live, so a slow run of another driver is never terminated.
    """
    try:
        state = await platform.execution(eid)
        if not state.deployment_name:
            return False
        return is_live(await platform.deployment(state.deployment_name), timing)
    except PlatformError as exc:
        return exc.retryable


async def sweep_orphans(
    platform: Platform, ref: PrRef, pr_url: str, timing: Timing | None = None
) -> list[str]:
    """CI only: terminate RUNNING executions of earlier CI runs for this PR.

    Only orphans: an execution whose deployment still has a live worker belongs to a
    run in progress (a workflow_dispatch review next to the pull_request one, say) and
    is left alone (security review PL-5).
    """
    timing = timing or Timing()
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
        if await _served_by_live_worker(platform, eid, timing):
            _say(f"msreview: execution {eid} may have a live worker (another run); not terminated")
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


# A sealed PrSnapshot is far below this (fetch_pr bounds the PR data it keeps).
_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024


def deadline_stage(run_dir: Path) -> Stage:
    """The stage a run reached, from the directories its activities created."""
    try:
        names = {p.name for p in run_dir.iterdir()}
    except OSError:
        return "fetch_pr"
    if "verify" in names or "verify-format" in names:
        return "verify"
    if any(n.startswith("format-") for n in names):
        return "structure"
    if any(n.startswith("explore-") for n in names):
        return "explore"
    if SNAPSHOT_FILE in names:
        return "prepare"
    return "fetch_pr"


def sealed_snapshot(run_dir: Path, ctx: RunContext) -> PrSnapshot | None:
    """``<run_dir>/snapshot.json`` (fetch_pr's sealed snapshot) if this run sealed it."""
    from morning_star_review.core.jsonio import read_bytes
    from morning_star_review.core.seal import verify

    path = run_dir / SNAPSHOT_FILE
    try:
        if path.is_symlink() or not path.is_file():
            return None
        snap = PrSnapshot.model_validate_json(read_bytes(path, max_bytes=_SNAPSHOT_MAX_BYTES))
    except Exception as exc:  # unreadable, too large or not a snapshot
        log.info("deadline backstop: no usable snapshot (%s)", type(exc).__name__)
        return None
    return snap if verify(snap, ctx) else None


# Agent job directories under the run dir hold the runner's ``meter.json``:
# explore-<model>, format-<model>, verify and verify-format (activities/*).
_JOB_DIR_PREFIXES = ("explore-", "format-", "verify")
_PHASE_ROLES: dict[str, Role] = {"explore": "explorer", "verify": "verifier", "format": "formatter"}
_METER_FILE = "meter.json"
_METER_MAX_BYTES = 256 * 1024
_METER_INTS = (
    "llm_calls",
    "tool_calls",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def _non_negative(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else 0.0


def _meter_usage(path: Path) -> tuple[str, Usage] | None:
    """``(usage key, usage)`` from one agent job's ``meter.json``; None if unusable."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _METER_MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    role = _PHASE_ROLES.get(str(data.get("phase")))
    model = data.get("model")
    if role is None or not isinstance(model, str) or not model or len(model) > 100:
        return None
    counts = {k: data[k] for k in _METER_INTS if type(data.get(k)) is int and data[k] >= 0}
    reasons = data.get("finish_reasons")
    rl_waits = data.get("rl_waits")
    try:
        usage = Usage(
            model=model,
            **counts,
            cost_usd=round(_non_negative(data.get("cost_usd")), 6),
            seconds=round(_non_negative(data.get("seconds")), 3),
            rate_limit_waits=rl_waits if type(rl_waits) is int and rl_waits >= 0 else 0,
            rate_limit_wait_seconds=round(_non_negative(data.get("rl_wait_seconds")), 3),
            finish_reasons=[str(r)[:20] for r in reasons][-200:]
            if isinstance(reasons, list)
            else [],
        )
    except ValueError:
        return None
    if usage.llm_calls == 0 and usage.cost_usd == 0:
        return None
    return usage_key(role, model), usage


def usage_from_run_dir(run_dir: Path) -> dict[str, Usage]:
    """Usage per ``role:model`` from the agent jobs' meters in ``run_dir``.

    For runs that ended before ``finalize`` added up the usage (cancelled, terminated,
    failed): the meters still count every model call that was made. Only our own job
    directories are read (never the repository checkout).
    """
    out: dict[str, Usage] = {}
    try:
        jobs = sorted(p for p in run_dir.iterdir() if p.name.startswith(_JOB_DIR_PREFIXES))
    except OSError:
        return out
    for job in jobs:
        if job.is_symlink() or not job.is_dir():
            continue
        found = _meter_usage(job / _METER_FILE)
        if found is None:
            continue
        key, usage = found
        if key in out:
            out[key] = out[key].plus(usage)
        elif len(out) < MAX_USAGE_KEYS:
            out[key] = usage
    return out


def with_metered_usage(outcome: ReviewOutcome, run_dir: Path) -> ReviewOutcome:
    """``outcome`` with the meters' usage and cost when it reports none of its own."""
    if outcome.usage:
        return outcome
    usage = usage_from_run_dir(run_dir)
    if not usage:
        return outcome
    cost = round(sum(u.cost_usd for u in usage.values()), 6)
    return outcome.model_copy(update={"usage": usage, "cost_usd": cost})


# ----------------------------------------------------------------------------- the run


class RunFailed(MsreviewError):  # noqa: N818 - reads like the condition it reports
    """The run cannot finish normally (worker death, readiness timeout, deadline)."""


WorkerFactory = Callable[[], Awaitable[None]]
Precheck = Callable[[ReviewRequest, "DriverSecrets"], Awaitable["EarlySkip | None"]]


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
    # The GitHub skip pre-check before a worker starts (default: GitHub's REST API).
    precheck: Precheck | None = None
    # Wall clock (epoch seconds) of the run clock and the wind-down announcer.
    wall_clock: Callable[[], float] = time.time


async def _default_precheck(req: ReviewRequest, secrets: DriverSecrets) -> EarlySkip | None:
    from morning_star_review.activities.fetch_pr import early_skip
    from morning_star_review.github.http import GitHubHttp

    # The same credentials as fetch_pr's reads: the App token, else the read token.
    async with GitHubHttp(secrets.github_token or secrets.read_token, timeout_s=10.0) as gh:
        return await early_skip(req, gh, secrets.bot_login)


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
        # The run clock (wall-clock epochs) the activities get in the run context.
        self.start_epoch = hooks.wall_clock()
        self.clock: RunClock = plan_clock(self.start_epoch, cfg.deadline_s, cfg.wind_down_after_s)
        self.token_box: TokenBox | None = None
        if secrets.github_token and secrets.github_token_refresh is not None:
            from morning_star_review.core.runctx import TokenBox

            minted = secrets.github_token_minted_epoch
            self.token_box = TokenBox(
                token=secrets.github_token,
                minted_epoch=self.start_epoch if minted is None else minted,
                refresh=secrets.github_token_refresh,
            )
        self.ctx: RunContext | None = None
        self.eid = execution_id(cfg.mode, ref, cfg.expected_head_sha, pysecrets.token_hex(4))
        self.deployment: str | None = None
        self.platform: Platform | None = None
        self.worker: asyncio.Task[None] | None = None
        self.factory: WorkerFactory | None = None
        self.worker_restarts = 0
        self.start_attempted = False
        self.terminal = False
        self.deadline_hit = False  # the hard deadline ended the run
        self.stopping = False  # cleanup has begun; a signal must not interrupt it
        self.run_root = cfg.run_root or default_run_root(cfg.mode)
        self.ready_seconds: float | None = None
        # The deployment as it was just before our worker started, and when it started.
        self.before_worker: DeploymentState | None = None
        self.before_known = False
        self.worker_started_at: datetime | None = None
        self.announcer: asyncio.Task[None] | None = None

    @property
    def run_dir(self) -> Path:
        return self.run_root / self.eid

    def secret_values(self) -> list[str]:
        """Every secret to redact, including GitHub tokens re-minted during the run."""
        values = self.secrets.values()
        if self.token_box is not None:
            values += [t for t in self.token_box.history if t and t not in values]
        return values

    def fail(self, reason: str) -> ReviewOutcome:
        return failed_outcome(self.req, self.eid, reason, self.secret_values())

    def deadline_reason(self) -> str:
        return f"the review did not finish within the {self.cfg.deadline_s / 60:g}-minute deadline"

    def context(self, on_worker: bool) -> RunContext:
        """The run context (kept: the deadline backstop checks seals with its key)."""
        from morning_star_review.core.runctx import RunContext
        from morning_star_review.core.seal import request_digest

        self.ctx = RunContext(
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
            run_started_epoch=self.clock.start_epoch,
            wind_down_epoch=self.clock.wind_down_epoch,
            hard_epoch=self.clock.hard_epoch,
            defer_publish=self.cfg.defer_publish_dir is not None,
            token_box=self.token_box,
        )
        return self.ctx

    def remaining(self) -> float:
        return self.deadline - _now()

    # ------------------------------------------------------------------ run clock

    def start_clock(self) -> None:
        """Say the deadline and the wind-down, and announce the wind-down when it comes."""
        wind_down_s = self.clock.wind_down_epoch - self.clock.start_epoch
        _say(
            f"msreview: deadline {_minutes(self.clock.deadline_s)} min; "
            f"wind-down at {_minutes(wind_down_s)} min"
        )
        if self.announcer is None:
            self.announcer = asyncio.create_task(self._announce_wind_down())

    async def _announce_wind_down(self) -> None:
        clock = self.clock
        while (left := clock.wind_down_epoch - self.hooks.wall_clock()) > 0:
            await asyncio.sleep(min(left, max(0.001, self.timing.wind_down_check_s)))
        used = clock.wind_down_epoch - clock.start_epoch
        _say(
            f"msreview: wind-down: {_minutes(used)} of {_minutes(clock.deadline_s)} minutes "
            "used; finishing the review with what it has"
        )

    def cancel_clock(self) -> None:
        if self.announcer is not None:
            self.announcer.cancel()
            self.announcer = None

    async def stop_clock(self) -> None:
        announcer, self.announcer = self.announcer, None
        if announcer is None:
            return
        announcer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await announcer

    # ------------------------------------------------------------------ deadline backstop

    async def deadline_backstop(self, outcome: ReviewOutcome) -> ReviewOutcome:
        """After the hard deadline, record the failure as ``report_failure`` would.

        The execution is gone (terminated or cancelled), so its ``report_failure`` never
        ran: the driver writes ``outcome.json`` and marks the PR (failure banner and
        label; a failure plan in CI deferred mode) in-process. An outcome an activity of
        this run already wrote wins and is returned instead; otherwise the outcome stays
        the driver's ``failed`` one. Never raises.
        """
        found = outcome_from_run_dir(self.run_dir, self.eid)
        if found is not None:
            return found
        ctx = self.ctx
        if ctx is None:
            return outcome
        stage = deadline_stage(self.run_dir)
        try:
            recorded = await asyncio.wait_for(
                self._record_deadline_failure(ctx, stage),
                max(0.001, self.timing.backstop_timeout_s),
            )
        except Exception as exc:  # TimeoutError included; best effort
            detail = redact(f"{type(exc).__name__}: {exc}", self.secret_values())[:300]
            _say(f"msreview: could not record the deadline failure on the PR ({detail})")
            return outcome
        _say(f"msreview: recorded the deadline failure (the run reached stage {stage})")
        head = getattr(recorded, "head_sha", None)
        if isinstance(head, str) and head and outcome.head_sha is None:
            return outcome.model_copy(update={"head_sha": head})
        return outcome

    async def _record_deadline_failure(self, ctx: RunContext, stage: Stage) -> Any:
        # Imported here: the activity modules load the Workflows SDK.
        from morning_star_review.activities import report_failure

        inp = FailureInput(
            req=self.req,
            snap=sealed_snapshot(self.run_dir, ctx),
            stage=stage,
            error_type="MsreviewDeadline",
            error_text=self.deadline_reason(),
        )
        return await report_failure.record_failure(inp, ctx)

    # ------------------------------------------------------------------ local mode

    async def run_local(self) -> ReviewOutcome:
        from morning_star_review.core.runctx import set_run_context

        set_run_context(self.context(on_worker=False))
        runner = self.hooks.local_runner
        if runner is None:
            _configure_workflows_sdk(None, self.secrets.mistral_api_key, local=True)
            runner = _default_local_runner
        _say(f"msreview: execution {self.eid} (local, in-process)")
        self.start_clock()
        try:
            result = await asyncio.wait_for(runner(self.req), max(1.0, self.remaining()))
        except TimeoutError:
            self.deadline_hit = True
            self.stopping = True  # the backstop is the cleanup now: no signal cancels it
            return await self.deadline_backstop(self.fail(self.deadline_reason()))
        except Exception as exc:
            found = outcome_from_run_dir(self.run_dir, self.eid)
            if found is not None:
                return found
            return self.fail(f"{type(exc).__name__}: {exc}")
        finally:
            await self.stop_clock()
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
        """whoami with the key; a refused key ends the run before any worker starts.

        Rate limits and server errors are retried for up to ``preflight_retry_s``.
        """
        platform = self._platform()
        try:
            who = await retry_api(
                platform.whoami,
                what="whoami",
                timing=self.timing,
                budget_s=self.timing.preflight_retry_s,
                sleep=self.hooks.sleep,
                until=self.deadline,
            )
        except PlatformAuthError as exc:
            raise DriverError(
                f"the Mistral API key was refused by the Workflows API ({exc}); check "
                "the key and that its workspace has Workflows access"
            ) from None
        except PlatformError as exc:
            raise DriverError(f"cannot reach the Mistral Workflows API ({exc})") from None
        return who.namespace

    async def early_skip(self) -> ReviewOutcome | None:
        """The cheap GitHub checks of ``fetch_pr`` before any worker or execution exists.

        A head that moved (superseded), a closed PR or a draft (without ``--force``), or
        a head already reviewed completely with the same configuration ends the run
        here with the skipped outcome ``fetch_pr`` would give. Any problem with the
        check (network, rate limit, permissions) lets the run go on: ``fetch_pr``
        decides again on the worker and reports real errors.
        """
        check = self.hooks.precheck or _default_precheck
        try:
            early = await asyncio.wait_for(
                check(self.req, self.secrets), self.timing.precheck_timeout_s
            )
        except Exception as exc:  # TimeoutError included
            detail = redact(f"{type(exc).__name__}: {exc}", self.secrets.values())[:300]
            log.info("skip pre-check unavailable: %s", detail)
            return None
        if early is None:
            return None
        from morning_star_review.github.publish import SKIP_REASONS

        _say(f"msreview: skip={early.skip} head={early.head_sha[:7]} (no worker started)")
        return ReviewOutcome(
            status="superseded" if early.skip == "superseded" else "skipped",
            reason=SKIP_REASONS[early.skip],
            verdict=early.prior_verdict,
            pr_url=self.req.pr_url,
            head_sha=early.head_sha[:64],
            execution_id=self.eid,
            notes=["Decided before starting a worker: no workflow execution was started."],
        )

    async def run_on_worker(self) -> ReviewOutcome:
        from morning_star_review.core.runctx import set_run_context

        namespace = await self.preflight()
        early = await self.early_skip()
        if early is not None:
            return early
        platform = self._platform()
        try:
            self.deployment = await choose_deployment(
                platform,
                self.cfg.mode,
                self.ref,
                self.timing,
                self.hooks.sleep,
                until=self.deadline,
            )
        except PlatformError as exc:
            raise DriverError(f"cannot read the deployment status ({exc})") from None
        _say(
            f"msreview: execution {self.eid} on deployment {self.deployment} "
            f"(workspace namespace {namespace[:8]})"
        )
        self.start_clock()
        if self.cfg.mode == "ci":
            await sweep_orphans(platform, self.ref, self.req.pr_url, self.timing)
        set_run_context(self.context(on_worker=True))
        factory = self.hooks.worker
        configure = self.hooks.configure_sdk
        if configure is None:
            configure = factory is None
        if configure:
            _configure_workflows_sdk(self.deployment, self.secrets.mistral_api_key, local=False)
        if factory is None:
            factory = _default_worker
        self.factory = factory
        try:
            outcome = await self._execute(factory, configure)
        finally:
            await self.cleanup()
        if self.deadline_hit and self.start_attempted:
            # The execution is terminated now; its own report_failure never ran.
            outcome = await self.deadline_backstop(outcome)
        return outcome

    async def _execute(self, factory: WorkerFactory, configure: bool) -> ReviewOutcome:
        """Start the worker, wait until it is ready, start the execution, wait for it."""
        try:
            self.before_known, self.before_worker = await self._deployment_now()
            if self.before_known and is_live(self.before_worker, self.timing):
                # Another run's worker registered after choose_deployment read the state.
                self._move_to_fresh_deployment(configure)
                self.before_known, self.before_worker = await self._deployment_now()
            self.worker_started_at = datetime.now(UTC)
            self.worker = asyncio.create_task(self._guarded_worker(factory))
            await self._wait_ready()
            if await self._shared_with_another_worker():
                await self._restart_on_fresh_deployment(factory, configure)
            await self._start()
            state = await self._wait_done()
            return self._outcome_of(state)
        except RunFailed as exc:
            return self.fail(str(exc))
        except PlatformError as exc:
            return self.fail(f"Workflows API error: {exc}")

    def _move_to_fresh_deployment(self, configure: bool) -> None:
        """Switch to a ``-x<hex>`` deployment of our own (no worker of ours runs yet)."""
        old = self._deployment_name()
        self.deployment = deployment_name(
            self.cfg.mode, self.ref, suffix="x" + pysecrets.token_hex(2)
        )
        _say(
            f"msreview: another msreview worker joined deployment {old}; "
            f"moving to {self.deployment}"
        )
        if configure:
            _configure_workflows_sdk(self.deployment, self.secrets.mistral_api_key, local=False)

    async def _shared_with_another_worker(self) -> bool:
        """True when a worker other than ours registered on our deployment meanwhile.

        Two runs for the same PR that start within seconds both pick the stable name
        before either worker registers. Workers on one task queue get each other's
        tasks, and every task handed to the wrong worker costs an attempt (security
        review PL-1). The count is compared with the one read before our worker
        started, so a stale count from an earlier, stopped worker does not matter.
        """
        if not self.before_known:
            return False
        try:
            now = await self._platform().deployment(self._deployment_name())
        except PlatformError:
            return False
        if now is None or not _has_worker(now):
            return False
        before = self.before_worker
        had = before.active_worker_count if before is not None and _has_worker(before) else 0
        return now.active_worker_count > had + 1

    async def _restart_on_fresh_deployment(self, factory: WorkerFactory, configure: bool) -> None:
        """Stop our worker, then start it again on a ``-x<hex>`` deployment of our own."""
        worker, self.worker = self.worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(worker, self.timing.worker_stop_timeout_s)
        self._move_to_fresh_deployment(configure)
        self.before_known, self.before_worker = await self._deployment_now()
        self.worker_started_at = datetime.now(UTC)
        self.worker = asyncio.create_task(self._guarded_worker(factory))
        await self._wait_ready()

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

    async def _deployment_now(self) -> tuple[bool, DeploymentState | None]:
        """(read, state) of the deployment before our worker starts."""
        try:
            return True, await self._platform().deployment(self._deployment_name())
        except PlatformError as exc:
            if not exc.retryable:
                raise
            return False, None

    async def _wait_ready(self) -> None:
        """Wait until our worker has registered: a heartbeat newer than its start.

        A stale ``is_active`` from a worker that stopped moments ago does not count;
        otherwise the start could be sent before our worker exists. The readiness
        timeout counts only time in which the deployment reads work: time lost to
        retryable API errors (and worker restarts) extends it, by at most
        ``readiness_error_budget_s``. A worker that fails with a rate limit or a
        server error before it is ready is started again (``worker_restarts``).
        """
        name = self._deployment_name()
        platform = self._platform()
        timing = self.timing
        begun = _now()
        extension = 0.0
        errors = 0

        def limit() -> float:
            return begun + timing.readiness_timeout_s + extension

        while True:
            if self.remaining() <= 0:
                self.deadline_hit = True
                raise RunFailed(self.deadline_reason())
            if self.worker is not None and self.worker.done():
                failure = self._worker_failure()
                if not self._may_restart_worker(limit()):
                    raise failure
                self.worker_restarts += 1
                delay = api_backoff(timing, self.worker_restarts)
                detail = redact(str(failure), self.secret_values())[:300]
                _say(
                    f"msreview: {detail}; starting it again in {delay:g}s "
                    f"({self.worker_restarts} of {timing.worker_restarts})"
                )
                extension = min(timing.readiness_error_budget_s, extension + delay)
                await self.hooks.sleep(delay)
                await self._start_worker_again()
                continue
            t0 = _now()
            try:
                d = await platform.deployment(name)
            except PlatformError as exc:
                if not exc.retryable:
                    raise
                errors += 1
                delay = api_backoff(timing, errors)
                lost = max(0.0, _now() - t0) + delay
                extension = min(timing.readiness_error_budget_s, extension + lost)
                if errors == 1:
                    _say(f"msreview: Workflows API error ({exc}) while waiting for the worker")
                if _now() + delay > limit():
                    raise RunFailed(self._not_ready(extension)) from None
                await self.hooks.sleep(delay)
                continue
            errors = 0
            if worker_registered(
                d,
                since=self.worker_started_at or datetime.now(UTC),
                before=self.before_worker,
                skew_s=timing.clock_skew_s,
                before_known=self.before_known,
            ):
                self.ready_seconds = _now() - begun
                _say(f"msreview: worker ready in {self.ready_seconds:.1f}s")
                return
            if _now() >= limit():
                raise RunFailed(self._not_ready(extension))
            await self.hooks.sleep(timing.readiness_poll_s)

    def _not_ready(self, extension: float) -> str:
        text = f"the Workflows worker was not ready within {self.timing.readiness_timeout_s:g}s"
        if extension >= 1:
            text += f" (plus {extension:.0f}s lost to Workflows API errors)"
        return text

    def _may_restart_worker(self, limit: float) -> bool:
        """True when our worker failed with a 429/5xx and may be started again."""
        worker = self.worker
        if worker is None or not worker.done() or worker.cancelled() or self.factory is None:
            return False
        if self.worker_restarts >= self.timing.worker_restarts or _now() >= limit:
            return False
        return transient_worker_error(worker.exception())

    async def _start_worker_again(self) -> None:
        """Start the worker again on the same deployment (the old one has stopped)."""
        if self.factory is None:
            raise RunFailed("the Workflows worker did not start")
        self.worker = None
        # The state before the new worker: a heartbeat the failed one left is not ours.
        self.before_known, self.before_worker = await self._deployment_now()
        self.worker_started_at = datetime.now(UTC)
        self.worker = asyncio.create_task(self._guarded_worker(self.factory))

    async def _start(self) -> None:
        """Start the execution; rate limits and server errors are retried with the same
        id for up to ``start_retry_budget_s`` (409 ``WF_1101``: already started)."""
        name = self._deployment_name()
        platform = self._platform()
        payload = self.req.model_dump(mode="json")

        async def attempt() -> None:
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

        await retry_api(
            attempt,
            what="execute_workflow",
            timing=self.timing,
            budget_s=self.timing.start_retry_budget_s,
            sleep=self.hooks.sleep,
            until=self.deadline,
        )

    async def _wait_done(self) -> ExecutionState:
        """Poll the execution until it ends or the hard deadline passes.

        Retryable API errors (rate limits, server errors) never end the run: the poll
        interval backs off to ``api_backoff_max_s`` while they last and returns to
        ``poll_s`` after a successful read. Other errors are raised.
        """
        platform = self._platform()
        errors = 0
        while True:
            if self.worker is not None and self.worker.done():
                raise self._worker_failure()
            if self.remaining() <= 0:
                self.deadline_hit = True
                raise RunFailed(self.deadline_reason())
            try:
                state = await platform.execution(self.eid)
            except PlatformError as exc:
                if not exc.retryable:
                    raise
                errors += 1
                if errors == 1:
                    _say(f"msreview: Workflows API error ({exc}); still waiting, with a backoff")
                delay = api_backoff(self.timing, errors, base=self.timing.poll_s)
            else:
                if errors:
                    _say(f"msreview: the Workflows API answers again after {errors} errors")
                errors = 0
                if state.status in TERMINAL_STATUSES:
                    self.terminal = True
                    return state
                delay = self.timing.poll_s
            await self.hooks.sleep(min(delay, max(0.0, self.remaining())))

    def _outcome_of(self, state: ExecutionState) -> ReviewOutcome:
        found = outcome_from_run_dir(self.run_dir, self.eid)
        if state.status == "COMPLETED":
            # Only the outcome our own publish/report_failure activity wrote counts.
            # The workflow's result is not sealed: a rogue worker on the (derivable)
            # task queue could complete it with a forged "posted, clean" outcome
            # (security review PL-3).
            if found is None:
                return self.fail(
                    "the workflow completed, but this run's publish step left no outcome"
                )
            return found
        if found is not None and found.status == "failed":
            return found
        return self.fail(f"the workflow execution ended {state.status}")

    async def cleanup(self) -> None:
        """Step 11: terminate a non-terminal execution, then stop the worker."""
        self.stopping = True
        if self.start_attempted and not self.terminal and self.platform is not None:
            platform = self.platform

            async def terminate() -> None:
                await asyncio.wait_for(
                    platform.terminate(self.eid), self.timing.terminate_timeout_s
                )

            try:
                # A rate-limited terminate is tried again briefly: a terminate that never
                # lands leaves the execution running without a worker until it times out.
                await retry_api(
                    terminate,
                    what="terminate_workflow_execution",
                    timing=self.timing,
                    budget_s=self.timing.terminate_retry_s,
                    sleep=self.hooks.sleep,
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
        await self.stop_clock()


# ----------------------------------------------------------------------------- signals


class _SignalGuard:
    """Step 12: the first SIGTERM/SIGINT/SIGHUP/SIGQUIT cancels the main task; later ones
    are ignored.

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
        # SIGHUP (closed terminal, dropped SSH session) and SIGQUIT would otherwise end
        # the process at once: no terminate, no worker stop, agents left running.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
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


# Set on an exception once report_ci_usage_failure has reported it.
_CI_REPORTED = "_msreview_ci_reported"
_CI_REASON_LIMIT = 500


def report_ci_usage_failure(
    error: BaseException | str,
    *,
    secrets: Sequence[str] = (),
    stop_commands: Any = None,
    pr_url: str = "",
    env: Mapping[str, str] | None = None,
) -> None:
    """``msreview ci`` stops with exit 2 (usage or preflight): tell GitHub Actions why.

    Writes ``status=failed`` (the other outputs empty) to ``$GITHUB_OUTPUT``, a short
    step summary, and ``::error title=Morning Star Code Review::<reason>`` with the
    reason redacted. An exception is reported once (``run_review`` reports its own
    ``DriverError``; the CLI may pass the same one again). Best effort: a problem
    writing these is printed and never changes the exit code.
    """
    from morning_star_review import outputs
    from morning_star_review.core.redact import secret_env_values

    if isinstance(error, BaseException):
        if getattr(error, _CI_REPORTED, False):
            return
        with contextlib.suppress(AttributeError, TypeError):
            setattr(error, _CI_REPORTED, True)
    src = os.environ if env is None else env
    known = [*secrets, *secret_env_values(src)]
    reason = redact(str(error).strip() or "the review could not start", known)
    reason = " ".join(reason.split())[:_CI_REASON_LIMIT]
    try:
        out = src.get("GITHUB_OUTPUT", "")
        if out:
            outputs.write_github_output(
                out,
                {
                    "status": "failed",
                    "verdict": "",
                    "findings": "0",
                    "execution-id": "",
                    "report-path": "",
                    "deployment": "",
                },
            )
        summary = src.get("GITHUB_STEP_SUMMARY", "")
        if summary:
            lines = [
                "### Morning Star Code Review",
                "",
                "Status: **failed** (the review did not start)",
                "",
                reason,
                "",
            ]
            if pr_url:
                lines += [f"Pull request: {redact(pr_url, known)[:300]}", ""]
            outputs.write_step_summary(summary, "\n".join(lines))
    except OSError as exc:
        _say(f"msreview: could not write the Actions outputs ({type(exc).__name__})")
    if isinstance(stop_commands, outputs.StopCommands):
        stop_commands.resume()  # an annotation is ignored while commands are stopped
    title = "Morning Star Code Review"
    print(f"::error title={title}::{outputs.escape_command(reason)}", flush=True)


async def run_review(
    cfg: DriverConfig, secrets: DriverSecrets, hooks: Hooks | None = None
) -> tuple[ReviewOutcome, int]:
    """Run one review and return ``(outcome, exit code)`` (DESIGN 11).

    Raises ``DriverError`` for usage, configuration and preflight problems (exit 2);
    in ci mode it reports them to GitHub Actions first (``report_ci_usage_failure``).
    Every other problem becomes a ``failed`` outcome. Reports and CI outputs are
    written before returning.
    """
    try:
        return await _run_review(cfg, secrets, hooks or Hooks())
    except DriverError as exc:
        if cfg.mode == "ci":
            report_ci_usage_failure(
                exc,
                secrets=secrets.values(),
                stop_commands=cfg.stop_commands,
                pr_url=cfg.pr_url,
            )
        raise


async def _run_review(
    cfg: DriverConfig, secrets: DriverSecrets, hooks: Hooks
) -> tuple[ReviewOutcome, int]:
    from morning_star_review import outputs
    from morning_star_review.core.runctx import clear_run_context

    if cfg.fail_on not in FAIL_ON_VALUES:
        raise DriverError(f"fail-on must be one of {', '.join(FAIL_ON_VALUES)}")
    if not secrets.mistral_api_key:
        raise DriverError(CI_NO_KEY if cfg.mode == "ci" else "no Mistral API key")
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
            # The runner sends SIGINT and the step's trap forwards it as SIGTERM: in ci
            # mode the signal name says nothing to the user.
            outcome = run.fail(CI_CANCELLED if cfg.mode == "ci" else f"cancelled by {name}")
            code = EXIT_CANCELLED
        # A run that ended before finalize (cancelled, terminated, failed) still spent
        # what its agents' meters recorded; report that instead of "usage: none".
        outcome = with_metered_usage(outcome, run.run_dir)
        try:
            outputs.emit(cfg, outcome, run.run_dir, deployment=run.deployment)
        except Exception as exc:  # reporting problems never change the review result
            _say(f"msreview: could not write the reports ({type(exc).__name__}: {exc})")
    finally:
        run.cancel_clock()
        guard.remove()
        clear_run_context()
        if not cfg.keep_run_dir:
            shutil.rmtree(run.run_dir, ignore_errors=True)
            if cfg.run_root is None:
                with contextlib.suppress(OSError):
                    run.run_root.rmdir()  # only if empty
    return outcome, code
