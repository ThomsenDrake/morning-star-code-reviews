"""Activity that records a failed run: ``outcome.json`` and a banner on the PR (DESIGN 5.2)."""

from __future__ import annotations

import logging
from datetime import timedelta

from mistralai import workflows

from morning_star_review.activities.publish import OUTCOME_JSON
from morning_star_review.core.contract import FailureInput, PrSnapshot, ReviewOutcome
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_model
from morning_star_review.core.redact import redact
from morning_star_review.core.seal import verify
from morning_star_review.github.publish import write_client, write_failure_header

__all__ = ["failed_outcome", "report_failure"]

log = logging.getLogger(__name__)


def failed_outcome(
    inp: FailureInput, snap: PrSnapshot | None, execution_id: str, secrets: list[str]
) -> ReviewOutcome:
    """The ``failed`` outcome: stage, error type and the redacted error text."""
    reason = redact(f"{inp.stage} failed: {inp.error_type}: {inp.error_text}", secrets)
    return ReviewOutcome(
        status="failed",
        reason=reason[:1000],
        pr_url=inp.req.pr_url,
        head_sha=snap.head_sha if snap is not None else None,
        execution_id=execution_id,
    )


@workflows.activity(
    name="msreview_report_failure",
    start_to_close_timeout=timedelta(minutes=2),
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
    # The run may have failed because a payload was tampered with: a snapshot this run
    # did not seal is never used to write to GitHub (the outcome file is still written).
    snap = inp.snap if inp.snap is not None and verify(inp.snap, ctx) else None
    outcome = failed_outcome(inp, snap, ctx.execution_id, ctx.secrets())
    try:
        ctx.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_model(ctx.run_dir / OUTCOME_JSON, outcome)
    except Exception as exc:  # best effort: never mask the original failure
        log.warning("could not write outcome.json: %s", type(exc).__name__)
    try:
        gh = write_client(ctx)
        if gh is None:
            await write_failure_header(inp.req, snap, inp.stage, ctx, None)
        else:
            async with gh:
                await write_failure_header(inp.req, snap, inp.stage, ctx, gh)
    except Exception as exc:
        log.warning("could not mark the failed run on the PR: %s", type(exc).__name__)
    return outcome
