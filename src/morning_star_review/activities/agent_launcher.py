"""Spawns, heartbeats and stops the agent subprocess (DESIGN 6.8).

The agent runs in the separate agent environment (``ctx.agent_python``) as
``python -m morning_star_review.agent <phase> <job_dir>``, in its own process group,
with an environment built from an allowlist (never a copy of ours), a fresh empty
working directory and HOME. It writes ``result.json`` into the job directory.

Exit codes: 0 = ``result.json`` written (any state); 3 = invalid job (not retryable);
anything else = crash (retryable: ``meter.json`` keeps counts and the deadline).

Wall-clock allowance: the agent pauses its own clocks while it waits on a rate limit
(HTTP 429, capacity, 5xx), so the launcher extends the base allowance the same way. On
every heartbeat tick it re-reads ``meter.json``: allowance = base + the rate-limit time
waited during this process + the rest of the wait in progress, capped at the job's
``stop_epoch`` + ``LATEST_MARGIN_S`` (or base + ``RL_FALLBACK_MAX_WAIT_S`` for a job
without run limits). Each new wait is shown once on stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from morning_star_review.activities.common import (
    meter_epoch,
    meter_float,
    progress,
    read_meter,
)
from morning_star_review.core import runclock
from morning_star_review.core.contract import AgentPhase, AgentRunResult
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.guard import heartbeat
from morning_star_review.core.jsonio import FileTooLarge, read_bytes
from morning_star_review.core.proc import child_env
from morning_star_review.core.redact import redact
from morning_star_review.core.runctx import RunContext

__all__ = [
    "AGENT_LOG",
    "EXIT_INVALID_JOB",
    "RESULT_FILE",
    "AgentCrashed",
    "AgentJobInvalid",
    "AgentTimedOut",
    "agent_env",
    "log_tail",
    "run_agent",
]

AGENT_MODULE = "morning_star_review.agent"
RESULT_FILE = "result.json"
METER_FILE = "meter.json"
AGENT_LOG = "agent.log"
EXIT_INVALID_JOB = 3
LOG_TAIL_BYTES = 2048
HEARTBEAT_EVERY = 15.0
KILL_GRACE = 5.0
MAX_RESULT_BYTES = 4 * 1024 * 1024
# The agent stops its last model call / wait by its job's stop_epoch; the process may
# take this much longer to write result.json before it is stopped. The run clock
# keeps this margin between the structure step's stop and the verify cutoff.
LATEST_MARGIN_S = runclock.AGENT_STOP_MARGIN_S
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


class AgentJobInvalid(MsreviewError):  # noqa: N818 - shown as the failure type
    """The agent refused the job directory (exit code 3); a retry cannot fix it."""

    non_retryable = True


class AgentCrashed(MsreviewError):  # noqa: N818 - shown as the failure type
    """The agent process failed without a result (retryable)."""


class AgentTimedOut(MsreviewError):  # noqa: N818 - shown as the failure type
    """The agent process outlived its wall-clock allowance and was stopped (retryable)."""


def agent_env(ctx: RunContext, scratch: str) -> dict[str, str]:
    """Allowlisted environment for the agent: no GitHub tokens, no endpoint overrides.

    Telemetry stays off (``AGENTS_SDK_TELEMETRY_ENABLED=0``,
    ``AGENTS_SDK_STUDIO_TRACING_ENABLED=0``, ``OTEL_ENABLED=false``) unless the operator
    opted in for this run (``ctx.telemetry``); then the three switches are left out.
    """
    return child_env(
        "agent",
        home=scratch,
        tmpdir=scratch,
        telemetry=ctx.telemetry,
        MISTRAL_API_KEY=ctx.mistral_api_key or None,
        # On a self-hosted Actions runner, the job's orphan-process cleanup finds
        # processes by this variable (the agent runs in its own session).
        RUNNER_TRACKING_ID=os.environ.get("RUNNER_TRACKING_ID") or None,
    )


def log_tail(job_dir: Path, ctx: RunContext, limit: int = LOG_TAIL_BYTES) -> str:
    """Last ``limit`` bytes of ``agent.log``, redacted, on one line."""
    path = job_dir / AGENT_LOG
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return ""
    text = redact(data.decode("utf-8", "replace"), ctx.secrets())
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())[-limit:]


def meter_details(job_dir: Path, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Heartbeat details from ``meter.json`` (counts only; never text)."""
    if data is None:
        data = read_meter(job_dir)
    if data is None:
        return {}
    out: dict[str, Any] = {}
    for key, name in (("llm_calls", "llm_calls"), ("tool_calls", "tool_calls")):
        if isinstance(data.get(key), int):
            out[name] = data[key]
    if isinstance(data.get("cost_usd"), int | float):
        out["cost"] = round(float(data["cost_usd"]), 4)
    if "rl_wait_seconds" in data:
        out["rl_wait"] = round(meter_float(data, "rl_wait_seconds"))
    return out


class _Allowance:
    """The agent process's wall-clock allowance, extended by its rate-limit waits.

    ``remaining(meter)`` = base end + rate-limit time waited since the launch (with the
    whole planned wait in progress, as the meter records it) - now, capped at the hard
    end (``latest_epoch`` + ``LATEST_MARGIN_S``, else base + ``RL_FALLBACK_MAX_WAIT_S``).
    Waits recorded before the launch (an earlier attempt) already moved the resumed
    deadline, so only the growth counts.
    """

    def __init__(
        self, timeout_s: float, latest_epoch: float | None, meter: dict[str, Any] | None
    ) -> None:
        self.base_end = time.monotonic() + timeout_s
        self.waited_before = meter_float(meter, "rl_wait_seconds") if meter else 0.0
        if latest_epoch is not None:
            self.hard_end = latest_epoch + LATEST_MARGIN_S
        else:
            self.hard_end = time.time() + timeout_s + runclock.RL_FALLBACK_MAX_WAIT_S

    def remaining(self, meter: dict[str, Any] | None) -> float:
        now = time.time()
        extra = 0.0
        if meter is not None:
            extra = max(0.0, meter_float(meter, "rl_wait_seconds") - self.waited_before)
            until = meter_epoch(meter, "rl_until_epoch")
            if until is not None:
                # ``rl_wait_seconds`` already holds the planned wait in progress
                # (``rl_wait_planned``); only an older meter without it needs the rest.
                planned = meter_float(meter, "rl_wait_planned")
                extra += max(0.0, until - now - planned)
        own = self.base_end + extra - time.monotonic()
        return min(own, self.hard_end - now)


class _WaitNotice:
    """Prints ``msreview: <model> rate-limited (<phase>); waiting ...`` once per wait."""

    def __init__(self, phase: str, model: str | None) -> None:
        self.phase = phase
        self.model = model
        self.last: tuple[object, float] | None = None

    def check(self, meter: dict[str, Any] | None) -> None:
        if meter is None:
            return
        now = time.time()
        until = meter_epoch(meter, "rl_until_epoch")
        if until is None or until <= now:
            return
        key = (meter.get("rl_waits"), until)
        if key == self.last:
            return
        self.last = key
        model = self.model
        if model is None:
            found = meter.get("model")
            model = found if isinstance(found, str) and _SAFE_MODEL.match(found) else "the model"
        # The meter pauses for the whole planned wait when it starts (``rl_wait_planned``
        # of ``rl_wait_seconds``): the total is what was waited before this wait, as in
        # the agent's own log line.
        before = meter_float(meter, "rl_wait_seconds") - meter_float(meter, "rl_wait_planned")
        total = runclock.format_wait(max(0.0, before))
        progress(
            f"msreview: {model} rate-limited ({self.phase}); waiting {round(until - now)}s "
            f"(total waited {total})"
        )


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM to the process group, wait ``KILL_GRACE`` s, then SIGKILL."""
    if proc.returncode is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)
    except TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)


async def _wait(
    proc: asyncio.subprocess.Process,
    job_dir: Path,
    phase: str,
    timeout_s: float,
    *,
    latest_epoch: float | None = None,
    model: str | None = None,
    meter_before: dict[str, Any] | None = None,
) -> int:
    """The exit code; ``TimeoutError`` when the (extended) allowance runs out.

    ``meter_before`` is the meter as it was before the process started.
    """
    allowance = _Allowance(timeout_s, latest_epoch, meter_before)
    notice = _WaitNotice(phase, model)
    meter: dict[str, Any] | None = None
    while True:
        remaining = allowance.remaining(meter)
        if remaining <= 0:
            raise TimeoutError
        try:
            return await asyncio.wait_for(proc.wait(), timeout=min(HEARTBEAT_EVERY, remaining))
        except TimeoutError:
            fresh = read_meter(job_dir)
            heartbeat({"phase": phase, **meter_details(job_dir, fresh or {})})
            # An unreadable meter (being replaced) keeps the last good one.
            meter = fresh if fresh is not None else meter
            notice.check(fresh)


def _read_result(job_dir: Path, phase: str) -> AgentRunResult | None:
    try:
        result = AgentRunResult.model_validate_json(
            read_bytes(job_dir / RESULT_FILE, max_bytes=MAX_RESULT_BYTES)
        )
    except (MsreviewError, FileTooLarge, ValidationError, ValueError):
        return None
    return result if result.phase == phase else None


async def run_agent(
    phase: AgentPhase,
    job_dir: str | Path,
    ctx: RunContext,
    timeout_s: float,
    *,
    latest_epoch: float | None = None,
    model: str | None = None,
) -> AgentRunResult:
    """Run one agent phase on ``job_dir`` and return its ``result.json``.

    ``timeout_s`` is the base allowance; rate-limit waits extend it, up to
    ``latest_epoch`` + ``LATEST_MARGIN_S`` (the job's ``RunLimits.stop_epoch``) when
    given (see the module docstring). ``model`` names the model in the wait lines.

    Heartbeats every 15 s with the meter counts. On cancellation the process group is
    stopped (SIGTERM, 5 s, SIGKILL) and the cancellation re-raised. Raises
    ``AgentJobInvalid`` (exit 3), ``AgentTimedOut`` or ``AgentCrashed``.
    """
    jd = Path(job_dir)
    (jd / RESULT_FILE).unlink(missing_ok=True)  # never read a result of an earlier attempt
    meter_before = read_meter(jd)  # waits of an earlier attempt do not extend this one
    scratch = tempfile.mkdtemp(prefix="msreview-agent-")
    try:
        with (jd / AGENT_LOG).open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                ctx.agent_python,
                "-m",
                AGENT_MODULE,
                phase,
                str(jd),
                cwd=scratch,
                env=agent_env(ctx, scratch),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            launched = time.monotonic()
            try:
                code = await _wait(
                    proc,
                    jd,
                    phase,
                    timeout_s,
                    latest_epoch=latest_epoch,
                    model=model,
                    meter_before=meter_before,
                )
            except TimeoutError:
                await _stop(proc)
                ran = int(time.monotonic() - launched)
                raise AgentTimedOut(
                    f"{phase} agent stopped after {ran} s: {log_tail(jd, ctx)}"
                ) from None
            except BaseException:  # cancellation, or anything else: never leave it running
                await _stop(proc)
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if code == 0:
        result = _read_result(jd, phase)
        if result is None:
            raise AgentCrashed(f"{phase} agent exited 0 without a valid result.json")
        return result
    if code == EXIT_INVALID_JOB:
        raise AgentJobInvalid(f"{phase} agent refused the job: {log_tail(jd, ctx)}")
    raise AgentCrashed(f"{phase} agent exited with code {code}: {log_tail(jd, ctx)}")
