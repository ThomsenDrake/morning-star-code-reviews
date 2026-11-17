"""A pull request whose file list cannot fit a workflow payload fails early and clearly.

Security review PL-2: past ~2 MB, Temporal refuses to schedule the next activity, the
workflow task fails in a loop until the driver's deadline, and report_failure never
runs. ``check_payload_budget`` (called by fetch_pr and prepare) turns that into a
non-retryable ``MsreviewTooLarge``, which the workflow reports like any other failure.
"""

from __future__ import annotations

import pytest

from morning_star_review.activities.common import PAYLOAD_BUDGET_BYTES, check_payload_budget
from morning_star_review.core.contract import ExploreInput, OperatorPolicy, PrFile, ReviewRequest
from morning_star_review.core.errors import MsreviewTooLarge

REQ = ReviewRequest(
    pr_url="https://github.com/octo/ledger/pull/7",
    repo="octo/ledger",
    mode="ci",
    policy=OperatorPolicy(trust_repo_config_models=True),
    run_label="gha:x/1/1",
)


def _inputs(make, n: int, path_len: int, *, renamed: bool):  # type: ignore[no-untyped-def]
    paths = [f"{i:05d}/" + "p" * (path_len - 6) for i in range(n)]
    files = [
        PrFile(
            path=p,
            previous_path=("old/" + p)[:path_len] if renamed else None,
            status="renamed" if renamed else "modified",
            additions=1,
            deletions=1,
            has_patch=True,
        )
        for p in paths
    ]
    snap = make.snapshot(paths=paths[:1], files=files, changed_files=n)
    prep = make.prepared(
        paths=paths[:1],
        inlined_paths=paths[:20],
        on_demand_paths=paths[20:],
        reviewable_paths=paths,
    )
    return snap, prep


def test_a_monorepo_move_that_would_overflow_is_refused(make) -> None:  # type: ignore[no-untyped-def]
    snap, prep = _inputs(make, 3000, 150, renamed=True)
    assert len(ExploreInput(req=REQ, snap=snap, prep=prep).model_dump_json()) > 2_000_000
    check_payload_budget(snap)  # the snapshot alone still fits
    with pytest.raises(MsreviewTooLarge, match="too large to review"):
        check_payload_budget(snap, prep)
    assert MsreviewTooLarge.non_retryable


def test_what_passes_the_budget_fits_every_activity_input(make) -> None:  # type: ignore[no-untyped-def]
    snap, prep = _inputs(make, 3000, 100, renamed=False)
    check_payload_budget(snap, prep)
    explore = ExploreInput(req=REQ, snap=snap, prep=prep)
    # The budget leaves room for the request, seals, merge and verification.
    assert len(explore.model_dump_json().encode()) < PAYLOAD_BUDGET_BYTES + 100_000 < 1_600_000
