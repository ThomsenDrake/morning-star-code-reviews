"""Activity that merges explorer drafts into review candidates (ADDENDUM D).

Deterministic: ``review.merge.merge_drafts`` over the sealed drafts, the anchor map
(digest checked) and the head versions of the PR files. No model is called.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review.activities.common import activity_errors, head_text_reader, require_dir
from morning_star_review.activities.prepare import ANCHORS_JSON
from morning_star_review.core.contract import AnchorMap, MergeInput, MergeResult, Prepared
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import read_verified
from morning_star_review.core.seal import seal
from morning_star_review.review.merge import merge_drafts

__all__ = ["load_anchors", "merge", "merge_sync", "pr_paths"]


def load_anchors(prep: Prepared) -> AnchorMap:
    """``anchors.json`` of the run, checked against the digest in ``prep``."""
    run_dir = require_dir(prep.job_root)
    return AnchorMap.model_validate_json(read_verified(run_dir / ANCHORS_JSON, prep.anchors_sha256))


def pr_paths(prep: Prepared, anchors: AnchorMap) -> set[str]:
    """Head paths of the PR's files known to this run (anchored, reviewable or excluded)."""
    return {*anchors.files, *prep.reviewable_paths, *(e.path for e in prep.excluded)}


def merge_sync(inp: MergeInput) -> MergeResult:
    """The pure merge step (unsealed result)."""
    anchors = load_anchors(inp.prep)
    head_text = head_text_reader(Path(inp.prep.repo_dir))
    return merge_drafts(inp.drafts, anchors, head_text, pr_paths=pr_paths(inp.prep, anchors))


@workflows.activity(
    name="msreview_merge",
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
)
async def merge(inp: MergeInput) -> MergeResult:
    """Merge one draft per explorer into at most 60 candidates with votes."""
    ctx = guard(inp.req, inp.prep, *inp.drafts)
    with activity_errors():
        return seal(merge_sync(inp), ctx)
