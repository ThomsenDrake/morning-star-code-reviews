"""Activity that runs one explorer agent in the agent environment (DESIGN 6.2-6.6, ADDENDUM D).

One activity per explorer model, each with its own job directory
(``<run_dir>/explore-<model>/``). With an ensemble the workflow runs them in parallel;
each later explorer starts ``STAGGER_SECONDS`` after the previous one so the first model
calls do not all hit the API at the same moment.

Rate limits slow an explorer down, they never drop it. A model call refused with HTTP
429 (or capacity / 5xx) is retried by the SDK a few times; a turn that still fails is
retried by the runner with an exponential backoff for as long as the run allows, and
the explorer's own clocks pause while it waits (``agent.runner``, ``agent.ratelimit``).
The launcher extends the process allowance by the same waits. Only the run's limits
(``RunClock.limits("explore")``: tools close at the wind-down, the last call ends at
``stop_epoch``) bound the waiting. In a run with a verifier these limits come earlier,
so a rate-limited explorer cannot crowd out the verifier's reserved window. An agent
process stopped at that limit still gives a result: its partial notes and metered
usage (state ``Error``, stop reason ``run_deadline``), so the explorer stays in the
ensemble.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review.activities.agent_launcher import AgentTimedOut, run_agent
from morning_star_review.activities.common import (
    JOB_JSON,
    activity_errors,
    agent_budgets,
    agent_job,
    agent_line,
    agent_state,
    cap_bytes,
    cap_paths,
    cost_share,
    deadline_run_result,
    exhaust_reasons,
    job_dir_name,
    launch_timeout,
    link_diffs,
    load_changed_files,
    progress,
    read_partial_notes,
    require_dir,
    usage_for,
)
from morning_star_review.activities.finalize import keep_for_finalize
from morning_star_review.activities.prepare import PROMPT_TXT
from morning_star_review.core import runclock
from morning_star_review.core.contract import (
    AgentJob,
    AgentRunResult,
    ExploreInput,
    ExploreResult,
    ModelRole,
    PresetName,
    ResolvedBudgets,
    ResolvedRoles,
)
from morning_star_review.core.errors import MsreviewBadRequest
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import read_verified, write_model
from morning_star_review.core.models import tool_call_limit
from morning_star_review.core.redact import redact
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.seal import seal

__all__ = [
    "EXPLORE_TIMEOUT",
    "STAGGER_SECONDS",
    "explore",
    "explore_job",
    "explore_result",
    "explorer_role",
    "explorer_tool_calls",
]

# Above any allowed run deadline: rate-limit waits may stretch an explorer far beyond its
# own budget. Liveness is the 60 s heartbeat timeout; the run's limits end the waiting.
EXPLORE_TIMEOUT: timedelta = runclock.ACTIVITY_CEILING
STAGGER_SECONDS = 2.0
NOTES_MAX_CHARS = 65_536  # ExploreResult.notes bound
NOTES_MAX_BYTES = 65_536  # also bounds the payload for non-ASCII notes
OPENED_MAX = 300  # one tool call opens at most one file; the tool budget is <= 200
OPENED_MAX_BYTES = 32 * 1024
ERROR_MAX = 2000


def explorer_role(roles: ResolvedRoles, explorer: ModelRole | None) -> tuple[int, ModelRole]:
    """(index, role) of the requested explorer; the primary when ``explorer`` is None."""
    explorers = roles.explorers
    if explorer is None:
        return 0, explorers[0]
    for i, role in enumerate(explorers):
        if role.model == explorer.model:
            return i, role
    raise MsreviewBadRequest(f"explorer {explorer.model!r} is not configured for this run")


def explorer_tool_calls(b: ResolvedBudgets, model: str, preset: PresetName | None) -> int:
    """Tool-call budget of one explorer on ``model``.

    An explicit budget (flag, Action input, operator config or trusted repository file)
    applies as set. Otherwise the preset's explorer budget, else the per-model default.
    """
    explicit = "max_tool_calls" in b.explicit
    return tool_call_limit(model, b.max_tool_calls, explicit=explicit, preset=preset)


def explore_job(
    inp: ExploreInput, ctx: RunContext, role: ModelRole, run_dir: Path, job_dir: Path
) -> AgentJob:
    prep, snap = inp.prep, inp.snap
    b = prep.budgets
    refs = load_changed_files(run_dir)
    link_diffs(run_dir, job_dir, refs)
    budgets = agent_budgets(
        b,
        max_tool_calls=explorer_tool_calls(b, role.model, snap.config.preset),
        seconds=b.explorer_minutes * 60,
        max_cost_usd=cost_share(prep.roles, b.max_cost_usd, "explorer"),
    )
    clock = runclock.from_context(ctx)
    return agent_job(
        phase="explore",
        execution_id=ctx.execution_id,
        repo_dir=prep.repo_dir,
        head_sha=snap.head_sha,
        merge_base_sha=snap.merge_base_sha,
        input_path=run_dir / PROMPT_TXT,
        input_sha256=prep.prompt_sha256,
        variant="incremental" if prep.scope == "incremental" else "full",
        role=role,
        budgets=budgets,
        changed_files=refs,
        # With a verifier, explorers end early enough for it to start (runclock).
        run_limits=(
            clock.limits("explore", verify=prep.roles.verify_enabled) if clock is not None else None
        ),
        cooldown_path=runclock.cooldown_path(run_dir, role.model) if clock is not None else None,
    )


def explore_result(
    res: AgentRunResult, model: str, known_paths: set[str], secrets: list[str], partial: str = ""
) -> ExploreResult:
    """The payload for one explorer run (unsealed), with every field bounded."""
    notes, cut = cap_bytes(res.notes or partial, NOTES_MAX_CHARS, NOTES_MAX_BYTES)
    error = redact(res.error, secrets)[:ERROR_MAX] if res.error else None
    return ExploreResult(
        notes=notes,
        state=agent_state(res.state),
        stop_reason=res.stop_reason[:200] if res.stop_reason else None,
        rescued=res.rescued,
        complete=res.complete and not cut,  # cut notes lost their end
        budget_exhausted=res.budget_exhausted,
        exhausted_by=exhaust_reasons(res.exhausted_by),
        opened_paths=cap_paths(res.opened_paths, known_paths, OPENED_MAX, OPENED_MAX_BYTES),
        usage=usage_for(res.usage, model),
        error=error,
    )


@workflows.activity(
    name="msreview_explore",
    start_to_close_timeout=EXPLORE_TIMEOUT,
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
)
async def explore(inp: ExploreInput) -> ExploreResult:
    """Run one explorer (phase ``explore``) and return its notes and run facts.

    A retried attempt resumes the persisted meter and deadline (``meter.json``).
    """
    ctx = guard(inp.req, inp.snap, inp.prep)
    with activity_errors():
        if inp.prep.scope == "carry_over":
            raise MsreviewBadRequest("a carry-over review has nothing to explore")
        index, role = explorer_role(inp.prep.roles, inp.explorer)
        run_dir = require_dir(inp.prep.job_root)
        read_verified(run_dir / PROMPT_TXT, inp.prep.prompt_sha256)
        if index:
            await asyncio.sleep(index * STAGGER_SECONDS)
        job_dir = run_dir / job_dir_name("explore", role.model)
        job_dir.mkdir(mode=0o700, exist_ok=True)
        job = explore_job(inp, ctx, role, run_dir, job_dir)
        write_model(job_dir / JOB_JSON, job)
        stop = job.run_limits.stop_epoch if job.run_limits is not None else None
        try:
            res = await run_agent(
                "explore",
                job_dir,
                ctx,
                launch_timeout(job.budgets),
                latest_epoch=stop,
                model=role.model,
            )
        except AgentTimedOut:
            if stop is None or time.time() < stop:
                raise  # retryable: a retry resumes the meter and the deadline
            # The run's time limit: keep what the explorer has instead of dropping it.
            res = deadline_run_result("explore", job_dir, role.model)
        known = {f.path for f in inp.snap.files}
        partial = "" if res.notes else read_partial_notes(job_dir)
        result = explore_result(res, role.model, known, ctx.secrets(), partial)
        progress(
            agent_line(
                "explore",
                role.model,
                state=result.state,
                complete=result.complete,
                rescued=result.rescued,
                budget_exhausted=result.budget_exhausted,
                exhausted_by=result.exhausted_by,
                stop_reason=result.stop_reason,
                rl_wait=result.usage.rate_limit_wait_seconds,
            )
        )
        sealed = seal(result, ctx)
        keep_for_finalize(run_dir, sealed)
        return sealed
