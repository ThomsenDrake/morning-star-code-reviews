"""Activity that publishes the final review to GitHub, or returns a report (DESIGN 9.4, 9.6).

In the Action's deferred mode (``RunContext.defer_publish``) it writes the publish plan
instead (``deferred.py``): the publish step posts it with a token minted after the
review, so a slow run never posts with an expired token.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review import deferred
from morning_star_review.activities.common import activity_errors
from morning_star_review.core.contract import PublishInput, ReviewOutcome
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_model
from morning_star_review.core.runctx import RunContext
from morning_star_review.github import publish as gh_publish

__all__ = ["DEFERRED_REASON", "OUTCOME_JSON", "publish"]

OUTCOME_JSON = "outcome.json"
DEFERRED_REASON = "Posting deferred to the publish step."


@workflows.activity(
    name="msreview_publish",
    # GitHub rate-limit waits (up to 15 minutes per request) happen inside the attempt.
    start_to_close_timeout=timedelta(minutes=20),
    retry_policy_max_attempts=6,
    retry_policy_backoff_coefficient=3.0,
)
async def publish(inp: PublishInput) -> ReviewOutcome:
    """Superseded check, then idempotent GitHub writes, or the report outcome.

    Safe to repeat: every write is keyed by our markers. Writes ``outcome.json``.
    """
    ctx = guard(inp.req, inp.snap, inp.final)
    with activity_errors():
        if inp.snap.skip is not None or not inp.snap.can_post:
            # Skipped, or report mode: nothing is written to GitHub.
            outcome = await gh_publish.publish(inp.req, inp.snap, inp.final, ctx, None)
        elif ctx.defer_publish:
            outcome = defer(inp, ctx)
        else:
            gh = await gh_publish.fresh_write_client(ctx)
            if gh is None:
                # The token expired and could not be re-minted: a report.
                expired = gh_publish.report_reason("token_expired")
                outcome = await gh_publish.publish(
                    inp.req, inp.snap, inp.final, ctx, None, no_post_reason=expired
                )
            else:
                async with gh:
                    outcome = await gh_publish.publish(inp.req, inp.snap, inp.final, ctx, gh)
        write_model(Path(inp.snap.run_dir) / OUTCOME_JSON, outcome)
        return outcome


def defer(inp: PublishInput, ctx: RunContext) -> ReviewOutcome:
    """Write the report files and the review plan; no GitHub call (deferred mode)."""
    if inp.final is None:  # PublishInput allows this only for skipped snapshots
        raise ValueError("final review missing for a snapshot that is not skipped")
    outcome = gh_publish.report_outcome(
        inp.req, inp.snap, inp.final, DEFERRED_REASON, execution_id=ctx.execution_id
    )
    gh_publish.write_report_files(inp.snap.run_dir, inp.snap, inp.final, outcome)
    plan = deferred.DeferredPlan(
        kind="review", execution_id=ctx.execution_id, review=inp, outcome=outcome
    )
    deferred.write_plan(ctx.run_dir, plan)
    return outcome
