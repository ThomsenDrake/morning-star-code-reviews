"""The ``morning-star-code-review`` Mistral Workflow (DESIGN 5.1, ADDENDUM D).

Shape::

    fetch_pr -> prepare -> explore[m1] | explore[m2] | ...   (parallel, one per explorer)
             -> structure[m_i] for each explorer               (parallel)
             -> merge -> verify                                (when the pipeline is on)
             -> finalize -> publish

Workflow code does no I/O, reads no clock, environment or randomness: everything that
touches the outside world is an activity. Any failure runs ``report_failure`` (best
effort) and is re-raised, so the execution ends FAILED.

With an ensemble, an explorer (or its structure step) that fails with a retryable error
is left out and the run continues with the others; the verdict then cannot be
``clean`` (an expected explorer has no result). A verifier that fails the same way
leaves every candidate unconfirmed. Pin, seal, digest and bad-request failures always
fail the run.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Sequence
from datetime import timedelta

from mistralai import workflows

with workflows.workflow.unsafe.imports_passed_through():
    from morning_star_review.activities import explore as _explore
    from morning_star_review.activities import fetch_pr as _fetch_pr
    from morning_star_review.activities import finalize as _finalize
    from morning_star_review.activities import merge as _merge
    from morning_star_review.activities import prepare as _prepare
    from morning_star_review.activities import publish as _publish
    from morning_star_review.activities import report_failure as _report_failure
    from morning_star_review.activities import structure as _structure
    from morning_star_review.activities import verify as _verify
    from morning_star_review.core.contract import (
        ExploreInput,
        ExploreResult,
        FailureInput,
        FinalizeInput,
        MergeInput,
        MergeResult,
        Prepared,
        PrepareInput,
        PrSnapshot,
        PublishInput,
        ReviewDraft,
        ReviewOutcome,
        ReviewRequest,
        Stage,
        StructureInput,
        VerifyInput,
        VerifyResult,
    )
    from morning_star_review.core.errors import failure_text, failure_type

__all__ = [
    "EXECUTION_TIMEOUT",
    "FATAL_FAILURES",
    "SEARCH_KEYS",
    "WF_DISPLAY_NAME",
    "WF_NAME",
    "CodeReviewWorkflow",
    "is_fatal",
]

WF_NAME = "morning-star-code-review"
WF_DISPLAY_NAME = "Morning Star Code Review"
WF_DESCRIPTION = (
    "Review one GitHub pull request; post as the user's GitHub App or return a Markdown report."
)
# Explorers and then the verifier may each run up to their 25-minute caps (plus grace,
# rescue and formatter time); the driver's own deadline normally ends a run much sooner.
EXECUTION_TIMEOUT = timedelta(minutes=90)
SEARCH_KEYS = ["pr_url", "repo"]

# Failure types that mean "this run must not continue" (never tolerated in an ensemble).
FATAL_FAILURES = frozenset(
    {
        "MsreviewPinRejected",
        "MsreviewSealInvalid",
        "MsreviewRunDirMissing",
        "MsreviewDigestMismatch",
        "MsreviewBadRequest",
        "MsreviewNoRunContext",
        "MsreviewGitHubLasting",
        "GitHubLasting",
        "AgentJobInvalid",
    }
)


def is_fatal(exc: BaseException) -> bool:
    """True when ``exc`` (raw or wrapped by Temporal) must fail the whole run."""
    return failure_type(exc) in FATAL_FAILURES


async def _settled[T](aws: Sequence[Awaitable[T]]) -> tuple[list[T], list[Exception]]:
    """Run ``aws`` concurrently; (results in order, failures). Cancellation propagates."""
    outcomes = await asyncio.gather(*aws, return_exceptions=True)
    ok: list[T] = []
    failed: list[Exception] = []
    for item in outcomes:
        if isinstance(item, Exception):
            failed.append(item)
        elif isinstance(item, BaseException):  # CancelledError and friends
            raise item
        else:
            ok.append(item)
    return ok, failed


def _keep_going(ok: Sequence[object], failed: Sequence[Exception]) -> None:
    """Raise unless the ensemble may continue without the failed members."""
    for exc in failed:
        if is_fatal(exc):
            raise exc
    if failed and not ok:
        raise failed[0]


@workflows.workflow.define(
    name=WF_NAME,
    workflow_display_name=WF_DISPLAY_NAME,
    workflow_description=WF_DESCRIPTION,
    execution_timeout=EXECUTION_TIMEOUT,
    search_keys=SEARCH_KEYS,
)
class CodeReviewWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, req: ReviewRequest) -> ReviewOutcome:
        snap: PrSnapshot | None = None
        stage: Stage = "fetch_pr"
        try:
            snap = await _fetch_pr.fetch_pr(req)
            if snap.skip is not None:
                stage = "publish"
                return await _publish.publish(PublishInput(req=req, snap=snap, final=None))
            stage = "prepare"
            prep = await _prepare.prepare(PrepareInput(req=req, snap=snap))
            explores: list[ExploreResult] = []
            drafts: list[ReviewDraft] = []
            merged: MergeResult | None = None
            verified: VerifyResult | None = None
            if prep.scope != "carry_over":
                stage = "explore"
                explores = await self._explore(req, snap, prep)
                stage = "structure"
                drafts = await self._structure(req, prep, explores)
                if prep.roles.uses_pipeline:
                    stage = "merge"
                    merged = await _merge.merge(MergeInput(req=req, prep=prep, drafts=drafts))
                    if prep.roles.verify_enabled and merged.candidates:
                        stage = "verify"
                        verified = await self._verify(req, snap, prep, merged)
            stage = "finalize"
            final = await _finalize.finalize(
                self._finalize_input(req, snap, prep, explores, drafts, merged, verified)
            )
            stage = "publish"
            return await _publish.publish(PublishInput(req=req, snap=snap, final=final))
        except Exception as exc:  # ActivityError on a worker; the raw exception locally
            failure = FailureInput(
                req=req,
                snap=snap,
                stage=stage,
                error_type=failure_type(exc)[:100],
                error_text=failure_text(exc),
            )
            # Best effort: the original error is what ends the execution.
            with contextlib.suppress(Exception):
                await _report_failure.report_failure(failure)
            raise

    async def _explore(
        self, req: ReviewRequest, snap: PrSnapshot, prep: Prepared
    ) -> list[ExploreResult]:
        if not prep.roles.ensemble:
            return [await _explore.explore(ExploreInput(req=req, snap=snap, prep=prep))]
        ok, failed = await _settled(
            [
                _explore.explore(ExploreInput(req=req, snap=snap, prep=prep, explorer=role))
                for role in prep.roles.explorers
            ]
        )
        _keep_going(ok, failed)
        return ok

    async def _structure(
        self, req: ReviewRequest, prep: Prepared, explores: list[ExploreResult]
    ) -> list[ReviewDraft]:
        if len(explores) == 1 and not prep.roles.ensemble:
            inp = StructureInput(req=req, prep=prep, explore=explores[0])
            return [await _structure.structure(inp)]
        ok, failed = await _settled(
            [_structure.structure(StructureInput(req=req, prep=prep, explore=e)) for e in explores]
        )
        _keep_going(ok, failed)
        return ok

    async def _verify(
        self, req: ReviewRequest, snap: PrSnapshot, prep: Prepared, merged: MergeResult
    ) -> VerifyResult | None:
        try:
            return await _verify.verify(VerifyInput(req=req, snap=snap, prep=prep, merge=merged))
        except Exception as exc:
            if is_fatal(exc):
                raise
            return None  # every candidate stays unconfirmed; the verdict says why

    @staticmethod
    def _finalize_input(
        req: ReviewRequest,
        snap: PrSnapshot,
        prep: Prepared,
        explores: list[ExploreResult],
        drafts: list[ReviewDraft],
        merged: MergeResult | None,
        verified: VerifyResult | None,
    ) -> FinalizeInput:
        if prep.roles.uses_pipeline:
            # Every explorer's result goes in the lists; repeating the primary's in
            # explore/draft would only grow the payload.
            return FinalizeInput(
                req=req,
                snap=snap,
                prep=prep,
                explore=None,
                draft=None,
                explores=explores,
                drafts=drafts,
                merge=merged,
                verify=verified,
            )
        return FinalizeInput(
            req=req,
            snap=snap,
            prep=prep,
            explore=explores[0] if explores else None,
            draft=drafts[0] if drafts else None,
        )
