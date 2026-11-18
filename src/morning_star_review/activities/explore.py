"""Activity that runs one explorer agent in the agent environment (DESIGN 6.2-6.6, ADDENDUM D).

One activity per explorer model, each with its own job directory
(``<run_dir>/explore-<model>/``). With an ensemble the workflow runs them in parallel;
each later explorer starts ``STAGGER_SECONDS`` after the previous one so the first model
calls do not all hit the API at the same moment. A model call refused with HTTP 429 is
retried by the SDK, and a turn that still fails is retried by the runner with a longer
backoff while the explorer's deadline and cost share allow (``agent.runner``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review.activities.agent_launcher import run_agent
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

# The longest explorer budget (25 min cap) plus grace, rescue and process overhead.
EXPLORE_TIMEOUT = timedelta(minutes=35)
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
        res = await run_agent("explore", job_dir, ctx, launch_timeout(job.budgets, EXPLORE_TIMEOUT))
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
            )
        )
        sealed = seal(result, ctx)
        keep_for_finalize(run_dir, sealed)
        return sealed
