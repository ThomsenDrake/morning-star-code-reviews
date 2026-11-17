"""Activity that fetches the pull request snapshot from GitHub (DESIGN 5.2, 9.4)."""

from __future__ import annotations

from datetime import timedelta

from mistralai import workflows

from morning_star_review.activities.common import activity_errors, mark_started, progress
from morning_star_review.core.contract import PrSnapshot, ReviewRequest
from morning_star_review.core.guard import guard
from morning_star_review.core.seal import seal
from morning_star_review.github.fetch import build_snapshot, read_client

__all__ = ["fetch_pr"]


@workflows.activity(
    name="msreview_fetch_pr",
    start_to_close_timeout=timedelta(minutes=3),
    retry_policy_max_attempts=6,
    retry_policy_backoff_coefficient=3.0,
)
async def fetch_pr(req: ReviewRequest) -> PrSnapshot:
    """Read the PR, its files, the base-branch config and our earlier state.

    Writes ``<run_dir>/pr_files.json``; decides ``skip`` and ``can_post``.
    """
    ctx = guard(req)
    with activity_errors():
        run_dir = ctx.run_dir
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        mark_started(run_dir)
        async with read_client(ctx) as gh:
            snap = await build_snapshot(req, ctx, gh, run_dir)
        if snap.skip is not None:
            progress(f"msreview: skip={snap.skip} head={snap.head_sha[:7]}")
        return seal(snap, ctx)
