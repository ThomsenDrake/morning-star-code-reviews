"""Activity that anchors, reconciles and renders the final review (DESIGN 8, ADDENDUM D/E).

Deterministic: ``review.finalize`` over the sealed payloads, the anchor map (digest
checked) and head file contents read from the checkout (confined to it). The only
inputs from outside the payloads are the clock (review time and duration) and the
tool version.

Every explorer's result and draft reaches this activity by seal (``FinalizeInput``):
``explore`` and ``structure`` save a copy of what they return in the run directory
(``keep_for_finalize``), and ``load_kept`` reads it back and checks the seal. That
keeps this input well under the payload limit for an ensemble (DESIGN 2.3).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mistralai import workflows
from pydantic import ValidationError

from morning_star_review._version import __version__
from morning_star_review.activities.common import (
    activity_errors,
    blocking,
    head_text_reader,
    require_dir,
    started_epoch,
)
from morning_star_review.activities.merge import load_anchors
from morning_star_review.core.contract import (
    ExploreResult,
    FinalizeInput,
    FinalReview,
    ReviewDraft,
    Sealed,
)
from morning_star_review.core.errors import MsreviewSealInvalid
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import FileTooLarge, read_bytes, write_model
from morning_star_review.core.seal import seal
from morning_star_review.review import RunInfo
from morning_star_review.review import finalize as review_finalize

__all__ = [
    "KEPT_DIR",
    "finalize",
    "finalize_sync",
    "keep_for_finalize",
    "load_kept",
    "primary_prior_claims_win",
    "run_info",
]

KEPT_DIR = "payloads"  # <run_dir>/payloads/<Type>-<seal>.json
KEPT_MAX_BYTES = 2 * 1024 * 1024  # a payload above this could not have travelled anyway


def _kept_path(run_dir: Path, kind: type[Sealed], seal_hex: str) -> Path:
    if len(seal_hex) != 64 or any(ch not in "0123456789abcdef" for ch in seal_hex):
        raise MsreviewSealInvalid(f"{kind.__name__} reference is not a seal")
    return run_dir / KEPT_DIR / f"{kind.__name__}-{seal_hex}.json"


def keep_for_finalize(run_dir: Path, sealed: Sealed) -> None:
    """Save a sealed payload for ``finalize``, named by its type and seal.

    Each attempt of an activity seals (and so names) its own result, so a retried or
    late attempt never overwrites the copy of the result the workflow received.
    """
    path = _kept_path(run_dir, type(sealed), sealed.seal)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_model(path, sealed)


def load_kept[S: Sealed](run_dir: Path, kind: type[S], seal_hex: str) -> S:
    """The payload ``keep_for_finalize`` saved under ``seal_hex`` (seal not yet verified)."""
    path = _kept_path(run_dir, kind, seal_hex)  # a missing file: MsreviewRunDirMissing
    try:
        model = kind.model_validate_json(read_bytes(path, max_bytes=KEPT_MAX_BYTES))
    except (FileTooLarge, ValidationError):
        raise MsreviewSealInvalid(f"saved {kind.__name__} is not a valid payload") from None
    if model.seal != seal_hex:
        raise MsreviewSealInvalid(f"saved {kind.__name__} does not carry the expected seal")
    return model


def primary_prior_claims_win(drafts: Sequence[ReviewDraft], primary: str) -> list[ReviewDraft]:
    """``drafts`` where the primary explorer's prior-status claims win conflicts.

    Every explorer's claims about prior findings count. Where the primary explorer
    made a definite claim (``fixed`` or ``still_open``) about a prior finding, the other
    explorers' claims about it are dropped; claims about the rest are combined as
    usual (``review.reconcile.combine_prior_status``). The primary's ``unsure`` is no
    claim. Returns copies; their seals were checked before.
    """
    decided = {
        s.id
        for d in drafts
        if d.explorer_model == primary
        for s in d.prior_status
        if s.status != "unsure"
    }
    if not decided:
        return list(drafts)
    return [
        d
        if d.explorer_model == primary
        else d.model_copy(
            update={"prior_status": [s for s in d.prior_status if s.id not in decided]}
        )
        for d in drafts
    ]


def run_info(inp: FinalizeInput, execution_id: str, now: float) -> RunInfo:
    started = started_epoch(Path(inp.snap.run_dir))
    return RunInfo(
        execution_id=execution_id,
        run_label=inp.req.run_label,
        tool_version=__version__,
        reviewed_at=datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        duration_seconds=round(now - started, 1) if started is not None else None,
    )


def finalize_sync(
    inp: FinalizeInput,
    execution_id: str,
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
) -> FinalReview:
    """The deterministic finalize step over the loaded payloads (unsealed result)."""
    require_dir(inp.prep.job_root)
    anchors = load_anchors(inp.prep)
    return review_finalize(
        inp.snap,
        inp.prep,
        None,
        None,
        anchors,
        head_text_reader(inp.prep.repo_dir),
        run_info(inp, execution_id, time.time()),
        explores=explores,
        drafts=primary_prior_claims_win(drafts, inp.prep.roles.explorer_model),
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
    ctx = guard(inp.req, inp.snap, inp.prep, inp.merge, inp.verify)
    with activity_errors():
        run_dir = require_dir(inp.prep.job_root)
        explores = [load_kept(run_dir, ExploreResult, s) for s in inp.explore_seals]
        drafts = [load_kept(run_dir, ReviewDraft, s) for s in inp.draft_seals]
        guard(inp.req, *explores, *drafts)  # each loaded payload carries this run's seal
        final = await blocking(
            lambda: finalize_sync(inp, ctx.execution_id, explores, drafts), {"stage": "finalize"}
        )
        return seal(final, ctx)
