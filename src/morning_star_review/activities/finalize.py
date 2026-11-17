"""Activity that anchors, reconciles and renders the final review (DESIGN 8, ADDENDUM D/E).

Deterministic: ``review.finalize`` over the sealed payloads, the anchor map (digest
checked) and head file contents read from the checkout (confined to it). The only
inputs from outside the payloads are the clock (review time and duration) and the
tool version.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review._version import __version__
from morning_star_review.activities.common import (
    activity_errors,
    blocking,
    head_text_reader,
    require_dir,
    started_epoch,
)
from morning_star_review.activities.merge import load_anchors
from morning_star_review.core.contract import FinalizeInput, FinalReview
from morning_star_review.core.guard import guard
from morning_star_review.core.seal import seal
from morning_star_review.review import RunInfo
from morning_star_review.review import finalize as review_finalize

__all__ = ["finalize", "finalize_sync", "run_info"]


def run_info(inp: FinalizeInput, execution_id: str, now: float) -> RunInfo:
    started = started_epoch(Path(inp.snap.run_dir))
    return RunInfo(
        execution_id=execution_id,
        run_label=inp.req.run_label,
        tool_version=__version__,
        reviewed_at=datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        duration_seconds=round(now - started, 1) if started is not None else None,
    )


def finalize_sync(inp: FinalizeInput, execution_id: str) -> FinalReview:
    """The deterministic finalize step (unsealed result)."""
    require_dir(inp.prep.job_root)
    anchors = load_anchors(inp.prep)
    return review_finalize(
        inp.snap,
        inp.prep,
        inp.explore,
        inp.draft,
        anchors,
        head_text_reader(inp.prep.repo_dir),
        run_info(inp, execution_id, time.time()),
        explores=inp.explores,
        drafts=inp.drafts,
        merge=inp.merge,
        verify=inp.verify,
    )


@workflows.activity(
    name="msreview_finalize",
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
)
async def finalize(inp: FinalizeInput) -> FinalReview:
    """Anchoring, ids, dedupe, prior reconciliation, verdict and rendering."""
    ctx = guard(
        inp.req,
        inp.snap,
        inp.prep,
        inp.explore,
        inp.draft,
        *inp.explores,
        *inp.drafts,
        inp.merge,
        inp.verify,
    )
    with activity_errors():
        final = await blocking(lambda: finalize_sync(inp, ctx.execution_id), {"stage": "finalize"})
        return seal(final, ctx)
