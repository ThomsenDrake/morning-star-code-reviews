"""Activity that publishes the final review to GitHub, or returns a report (DESIGN 9.4, 9.6)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review.activities.common import activity_errors
from morning_star_review.core.contract import PublishInput, ReviewOutcome
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_model
from morning_star_review.github import publish as gh_publish

__all__ = ["OUTCOME_JSON", "publish"]

OUTCOME_JSON = "outcome.json"


@workflows.activity(
    name="msreview_publish",
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=6,
    retry_policy_backoff_coefficient=3.0,
)
async def publish(inp: PublishInput) -> ReviewOutcome:
    """Superseded check, then idempotent GitHub writes, or the report outcome.

    Safe to repeat: every write is keyed by our markers. Writes ``outcome.json``.
    """
    ctx = guard(inp.req, inp.snap, inp.final)
    with activity_errors():
        gh = gh_publish.write_client(ctx)
        if gh is None:
            outcome = await gh_publish.publish(inp.req, inp.snap, inp.final, ctx, None)
        else:
            async with gh:
                outcome = await gh_publish.publish(inp.req, inp.snap, inp.final, ctx, gh)
        write_model(Path(inp.snap.run_dir) / OUTCOME_JSON, outcome)
        return outcome
