"""Spawns, heartbeats and stops the agent subprocess (DESIGN 6.8).

The agent runs in the separate agent environment (``ctx.agent_python``) as
``python -m morning_star_review.agent <phase> <job_dir>``, in its own process group,
with an environment built from an allowlist (never a copy of ours), a fresh empty
working directory and HOME. It writes ``result.json`` into the job directory.

Exit codes: 0 = ``result.json`` written (any state); 3 = invalid job (not retryable);
anything else = crash (retryable: ``meter.json`` keeps counts and the deadline).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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


def meter_details(job_dir: Path) -> dict[str, Any]:
    """Heartbeat details from ``meter.json`` (counts only; never text)."""
    try:
        data = json.loads((job_dir / METER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, name in (("llm_calls", "llm_calls"), ("tool_calls", "tool_calls")):
        if isinstance(data.get(key), int):
            out[name] = data[key]
    if isinstance(data.get("cost_usd"), int | float):
        out["cost"] = round(float(data["cost_usd"]), 4)
    return out


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
    proc: asyncio.subprocess.Process, job_dir: Path, phase: str, timeout_s: float
) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            return await asyncio.wait_for(proc.wait(), timeout=min(HEARTBEAT_EVERY, remaining))
        except TimeoutError:
            heartbeat({"phase": phase, **meter_details(job_dir)})


def _read_result(job_dir: Path, phase: str) -> AgentRunResult | None:
    try:
        result = AgentRunResult.model_validate_json(
            read_bytes(job_dir / RESULT_FILE, max_bytes=MAX_RESULT_BYTES)
        )
    except (MsreviewError, FileTooLarge, ValidationError, ValueError):
        return None
    return result if result.phase == phase else None


async def run_agent(
    phase: AgentPhase, job_dir: str | Path, ctx: RunContext, timeout_s: float
) -> AgentRunResult:
    """Run one agent phase on ``job_dir`` and return its ``result.json``.

    Heartbeats every 15 s with the meter counts. On cancellation the process group is
    stopped (SIGTERM, 5 s, SIGKILL) and the cancellation re-raised. Raises
    ``AgentJobInvalid`` (exit 3), ``AgentTimedOut`` or ``AgentCrashed``.
    """
    jd = Path(job_dir)
    (jd / RESULT_FILE).unlink(missing_ok=True)  # never read a result of an earlier attempt
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
            try:
                code = await _wait(proc, jd, phase, timeout_s)
            except TimeoutError:
                await _stop(proc)
                raise AgentTimedOut(
                    f"{phase} agent stopped after {int(timeout_s)} s: {log_tail(jd, ctx)}"
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
