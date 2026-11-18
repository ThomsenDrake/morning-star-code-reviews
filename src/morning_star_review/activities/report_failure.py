"""Activity that records a failed run: ``outcome.json`` and a banner on the PR (DESIGN 5.2).

In the Action's deferred mode the banner is not written here: a failure plan is
(``deferred.py``), and the publish step marks the PR with a freshly minted token.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from mistralai import workflows

from morning_star_review import deferred
from morning_star_review.activities.publish import OUTCOME_JSON
from morning_star_review.core.contract import FailureInput, PrSnapshot, ReviewOutcome
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_model
from morning_star_review.core.redact import redact
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.seal import verify
from morning_star_review.github.publish import fresh_write_client, write_failure_header

__all__ = ["failed_outcome", "failure_reason", "record_failure", "report_failure"]

log = logging.getLogger(__name__)


def failure_reason(stage: str, error_type: str, error_text: str) -> str:
    """``<stage> failed: <type>: <text>``, with the type named once.

    ``failure_text`` already starts the text with the error type
    (``MsreviewBadRequest: ...``); the type is added only when the text lacks it.
    """
    text = error_text.strip()
    if error_type and not (text == error_type or text.startswith(f"{error_type}:")):
        text = f"{error_type}: {text}" if text else error_type
    return f"{stage} failed: {text}"


def failed_outcome(
    inp: FailureInput, snap: PrSnapshot | None, execution_id: str, secrets: list[str]
) -> ReviewOutcome:
    """The ``failed`` outcome: stage, error type and the redacted error text."""
    reason = redact(failure_reason(inp.stage, inp.error_type, inp.error_text), secrets)
    return ReviewOutcome(
        status="failed",
        reason=reason[:1000],
        pr_url=inp.req.pr_url,
        head_sha=snap.head_sha if snap is not None else None,
        execution_id=execution_id,
    )


@workflows.activity(
    name="msreview_report_failure",
    # GitHub rate-limit waits (up to 15 minutes per request) happen inside the attempt.
    start_to_close_timeout=timedelta(minutes=20),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
)
async def report_failure(inp: FailureInput) -> ReviewOutcome:
    """Write ``outcome.json`` (status ``failed``) and mark our summary as stale.

    Best effort after the guard: it never raises, so the workflow re-raises the
    original error. The PR banner is a fixed template (stage and short sha only) and
    the label becomes ``msreview: incomplete`` (ADDENDUM C).
    """
    ctx = guard(inp.req)
    # Sealed inputs are checked at the activity boundary (record_failure checks again,
    # for in-process callers): an unverified snapshot is dropped, not trusted.
    if inp.snap is not None and not verify(inp.snap, ctx):
        inp = inp.model_copy(update={"snap": None})
    return await record_failure(inp, ctx)


async def record_failure(inp: FailureInput, ctx: RunContext) -> ReviewOutcome:
    """The body of ``report_failure`` for a caller that already holds ``ctx``.

    The driver's deadline backstop calls it in-process. Never raises.
    """
    # The run may have failed because a payload was tampered with: a snapshot this run
    # did not seal is never used to write to GitHub (the outcome file is still written).
    snap = inp.snap if inp.snap is not None and verify(inp.snap, ctx) else None
    outcome = failed_outcome(inp, snap, ctx.execution_id, ctx.secrets())
    try:
        ctx.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_model(ctx.run_dir / OUTCOME_JSON, outcome)
    except Exception as exc:  # best effort: never mask the original failure
        log.warning("could not write outcome.json: %s", type(exc).__name__)
    if ctx.defer_publish:
        _write_failure_plan(inp.model_copy(update={"snap": snap}), outcome, ctx)
        return outcome
    if snap is None or not snap.can_post:
        return outcome  # nothing of ours to mark on the PR
    try:
        gh = await fresh_write_client(ctx)
        if gh is None:
            await write_failure_header(inp.req, snap, inp.stage, ctx, None)
        else:
            async with gh:
                await write_failure_header(inp.req, snap, inp.stage, ctx, gh)
    except Exception as exc:
        log.warning("could not mark the failed run on the PR: %s", type(exc).__name__)
    return outcome


def _write_failure_plan(inp: FailureInput, outcome: ReviewOutcome, ctx: RunContext) -> None:
    """Deferred mode: the publish step marks the PR from this plan. Never raises."""
    try:
        plan = deferred.DeferredPlan(
            kind="failure", execution_id=ctx.execution_id, failure=inp, outcome=outcome
        )
        ctx.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        deferred.write_plan(ctx.run_dir, plan)
    except Exception as exc:
        log.warning("could not write the failure plan: %s", type(exc).__name__)
