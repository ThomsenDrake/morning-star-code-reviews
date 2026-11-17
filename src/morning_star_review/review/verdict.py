"""Review verdict (DESIGN 8.4 and ADDENDUM D, with the precedence fixed after the first
live evaluation).

Precedence:

1. ``findings`` when any finding is open: new or carried, inline or summary-only, or a
   prior thread resolved without a fix. A coverage gap or an unfinished explorer does
   not hide findings that were made; the reasons still list the gap.
2. ``incomplete`` when nothing is open and the review was not complete.
3. ``clean`` otherwise.

``clean`` is a claim that the whole change was reviewed and nothing was found, so it
needs proof. The review is complete (it has no *blockers*) only when the primary
explorer finished within its budget, every other explorer finished (see below), the
notes of every explorer became a draft without loss, the verifier finished when it had
work, nothing was cut short, and every reviewable file was opened.

A secondary ensemble explorer that ran out of budget but still produced a draft that
parsed without loss does not block ``clean``: the primary explorer reviewed the change
and the secondary's findings were kept. It is recorded as a *note* in the reasons. So is the
reason a full review had nothing to review (every changed file excluded, no model ran).

``Assessment.complete`` (no blockers) does not depend on the verdict. Store it as
``PriorState.complete``: a ``findings`` review can still be incomplete, and a later
incremental review must not rely on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from morning_star_review.core.contract import (
    ExploreResult,
    FinalFinding,
    PriorState,
    ResolvedPrior,
    ReviewDraft,
    Scope,
    Verdict,
    VerifyResult,
)

__all__ = [
    "MAX_REASONS",
    "MAX_REASON_CHARS",
    "RESOLVED_WITHOUT_FIX",
    "Assessment",
    "assess",
    "decide",
    "incomplete_reasons",
    "is_open",
    "resolved_without_fix",
]

MAX_REASON_CHARS = 300
MAX_REASONS = 20
RESOLVED_WITHOUT_FIX = "resolved without a fix by"
# Draft sources that hold everything the explorer wrote ("empty" = it wrote nothing).
_FULL_DRAFT = ("parser", "parser+formatter")


def is_open(f: FinalFinding) -> bool:
    """New or carried, posted inline or summary-only: anything not fixed or dismissed."""
    return f.state in ("open", "summary_only")


def resolved_without_fix(priors: Sequence[ResolvedPrior]) -> list[ResolvedPrior]:
    """Priors whose thread was resolved by someone who may not dismiss findings."""
    return [p for p in priors if p.action == "keep_open" and RESOLVED_WITHOUT_FIX in p.note]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


def _bounded(reasons: Sequence[str]) -> list[str]:
    return [_cap(r) for r in dict.fromkeys(reasons)][:MAX_REASONS]


@dataclass(frozen=True, slots=True)
class Assessment:
    """The verdict, plus why the review is not complete (blockers) and notes.

    Blockers make the review incomplete: they rule out ``clean`` and must set
    ``PriorState.complete`` to False. Notes are informational only.
    """

    verdict: Verdict
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.blockers

    @property
    def reasons(self) -> list[str]:
        """``verdict_reasons``: blockers first, then notes, each bounded."""
        return _bounded([*self.blockers, *self.notes])


def _draft_of(
    model: str, drafts: Sequence[ReviewDraft], expected: Sequence[str]
) -> ReviewDraft | None:
    """The draft built from ``model``'s notes (by model id, else by position)."""
    for d in drafts:
        if d.explorer_model == model:
            return d
    if model in expected:
        i = list(expected).index(model)
        if i < len(drafts) and drafts[i].explorer_model is None:
            return drafts[i]
    return None


def _explorer_gaps(
    expected: Sequence[str], explores: Sequence[ExploreResult], drafts: Sequence[ReviewDraft]
) -> tuple[list[str], list[str]]:
    """(blockers, notes) about the explorers themselves."""
    blockers: list[str] = []
    notes: list[str] = []
    seen = {e.model for e in explores}
    blockers.extend(f"the {m} explorer produced no result" for m in expected if m not in seen)
    primary = expected[0] if expected else (explores[0].model if explores else None)
    for e in explores:
        if e.complete and not e.budget_exhausted:
            continue
        out_of = ", ".join(e.exhausted_by) or "budget"
        draft = _draft_of(e.model, drafts, expected)
        if (
            e.model != primary
            and e.budget_exhausted
            and draft is not None
            and draft.source in _FULL_DRAFT
        ):
            notes.append(
                f"the {e.model} explorer ran out of {out_of}; its findings were kept, and as a "
                "secondary explorer it does not make the review incomplete"
            )
        elif not e.complete:
            detail = f" (out of {out_of})" if e.budget_exhausted else ""
            state = "" if e.state == "Completed" else f"; state {e.state}"
            blockers.append(f"the {e.model} explorer did not finish its review{detail}{state}")
        else:
            blockers.append(f"the {e.model} explorer ran out of {out_of} and stopped early")
    return blockers, notes


def _draft_gaps(
    expected: Sequence[str],
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    structured: int | None,
) -> list[str]:
    blockers: list[str] = []
    unstructured = len(explores) - (len(drafts) if structured is None else structured)
    if unstructured > 0:
        count = "1 explorer" if unstructured == 1 else f"{unstructured} explorers"
        blockers.append(f"the notes of {count} were not turned into findings")
    for i, d in enumerate(drafts):
        if d.source == "parser_lossy":
            who = d.explorer_model or (expected[i] if i < len(expected) else "")
            blockers.append(
                f"the notes of the {who or 'explorer'} explorer could not be fully parsed"
            )
    return blockers


def _verifier_gaps(verify_needed: bool, verify: VerifyResult | None) -> list[str]:
    if not verify_needed:
        return []
    if verify is None:
        return ["the verifier did not run"]
    blockers: list[str] = []
    if not verify.complete:
        out = f" (out of {', '.join(verify.exhausted_by)})" if verify.budget_exhausted else ""
        blockers.append(f"the verifier did not finish{out}")
    if verify.source == "parser_lossy":
        blockers.append("the verifier's notes could not be fully parsed")
    return blockers


def _coverage_gaps(partial_reason: str | None, coverage: Mapping[str, int]) -> list[str]:
    blockers: list[str] = []
    if partial_reason:
        blockers.append(f"only part of the change was reviewed: {partial_reason}")
    reviewable, covered = coverage.get("reviewable", 0), coverage.get("covered", 0)
    if covered < reviewable:
        missing = reviewable - covered
        verb = "was" if missing == 1 else "were"
        blockers.append(f"{missing} of {reviewable} reviewable changed files {verb} not opened")
    return blockers


def _gaps(
    *,
    scope: Scope,
    expected_explorers: Sequence[str],
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    verify_needed: bool,
    verify: VerifyResult | None,
    partial_reason: str | None,
    coverage: Mapping[str, int],
    prior_state: PriorState | None,
    structured: int | None,
    nothing_to_review: str | None = None,
    unreviewed_code: str | None = None,
) -> tuple[list[str], list[str]]:
    """(blockers, notes), each in plain language."""
    if scope == "carry_over":
        # No model ran: completeness is inherited from the review being carried over.
        if prior_state is None or not prior_state.complete or prior_state.verdict == "incomplete":
            return ["the previous review was incomplete and no reviewable file changed since"], []
        return [], []
    if (
        nothing_to_review
        and scope == "full"
        and not explores
        and not partial_reason
        and coverage.get("reviewable", 0) == 0
    ):
        # Every changed file is excluded, so no model ran. Lock files, generated code
        # and binaries can still change what runs: then nothing was reviewed, which
        # is not "clean" (the PR author chooses the names that get files excluded).
        if unreviewed_code:
            return [
                f"nothing was reviewed: {nothing_to_review}, and the excluded files "
                f"({unreviewed_code}) can change what runs"
            ], []
        # Only inert files (media, snapshots, renames) changed: nothing to miss.
        return [], [f"nothing to review: {nothing_to_review}"]
    blockers, notes = _explorer_gaps(expected_explorers, explores, drafts)
    blockers.extend(_draft_gaps(expected_explorers, explores, drafts, structured))
    blockers.extend(_verifier_gaps(verify_needed, verify))
    blockers.extend(_coverage_gaps(partial_reason, coverage))
    return blockers, notes


def incomplete_reasons(
    *,
    scope: Scope,
    expected_explorers: Sequence[str],
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    verify_needed: bool,
    verify: VerifyResult | None,
    partial_reason: str | None,
    coverage: Mapping[str, int],
    prior_state: PriorState | None,
    structured: int | None = None,
    nothing_to_review: str | None = None,
    unreviewed_code: str | None = None,
) -> list[str]:
    """Plain-language reasons why the review is not complete (empty = complete).

    ``structured`` is how many explorers' notes became a draft (default: the number of
    ``drafts``); an explorer whose notes were never structured contributed no findings,
    so its result cannot support a ``clean`` verdict. ``nothing_to_review`` is why a
    full review had no reviewable file (every changed file excluded); no explorer ran
    then, and the review is complete with that reason as a note, unless
    ``unreviewed_code`` names excluded files that can change what runs (lock files,
    generated code, binaries): then that is a blocker.
    """
    blockers, _ = _gaps(
        scope=scope,
        expected_explorers=expected_explorers,
        explores=explores,
        drafts=drafts,
        verify_needed=verify_needed,
        verify=verify,
        partial_reason=partial_reason,
        coverage=coverage,
        prior_state=prior_state,
        structured=structured,
        nothing_to_review=nothing_to_review,
        unreviewed_code=unreviewed_code,
    )
    return _bounded(blockers)


def assess(
    *,
    scope: Scope,
    expected_explorers: Sequence[str],
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    verify_needed: bool,
    verify: VerifyResult | None,
    partial_reason: str | None,
    coverage: Mapping[str, int],
    findings: Sequence[FinalFinding],
    priors: Sequence[ResolvedPrior],
    prior_state: PriorState | None = None,
    structured: int | None = None,
    nothing_to_review: str | None = None,
    unreviewed_code: str | None = None,
) -> Assessment:
    """The verdict with its blockers and notes (see the module docstring).

    ``verify_needed`` is True when verification is enabled and there was at least one
    candidate to verify. ``findings`` are the final (unrefuted) findings, ``priors`` the
    reconciled prior findings. ``expected_explorers`` lists the configured explorer
    models, primary first. ``nothing_to_review``: see ``incomplete_reasons``.
    """
    blockers, notes = _gaps(
        scope=scope,
        expected_explorers=expected_explorers,
        explores=explores,
        drafts=drafts,
        verify_needed=verify_needed,
        verify=verify,
        partial_reason=partial_reason,
        coverage=coverage,
        prior_state=prior_state,
        structured=structured,
        nothing_to_review=nothing_to_review,
        unreviewed_code=unreviewed_code,
    )
    verdict: Verdict
    if any(is_open(f) for f in findings) or resolved_without_fix(priors):
        verdict = "findings"
    elif blockers:
        verdict = "incomplete"
    else:
        verdict = "clean"
    return Assessment(verdict=verdict, blockers=tuple(blockers), notes=tuple(notes))


def decide(
    *,
    scope: Scope,
    expected_explorers: Sequence[str],
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    verify_needed: bool,
    verify: VerifyResult | None,
    partial_reason: str | None,
    coverage: Mapping[str, int],
    findings: Sequence[FinalFinding],
    priors: Sequence[ResolvedPrior],
    prior_state: PriorState | None = None,
    structured: int | None = None,
    nothing_to_review: str | None = None,
    unreviewed_code: str | None = None,
) -> tuple[Verdict, list[str]]:
    """``(verdict, verdict_reasons)``; see ``assess``.

    The verdict alone does not tell whether the review was complete (a ``findings``
    review may not be): use ``assess(...).complete`` for ``PriorState.complete``.
    """
    a = assess(
        scope=scope,
        expected_explorers=expected_explorers,
        explores=explores,
        drafts=drafts,
        verify_needed=verify_needed,
        verify=verify,
        partial_reason=partial_reason,
        coverage=coverage,
        findings=findings,
        priors=priors,
        prior_state=prior_state,
        structured=structured,
        nothing_to_review=nothing_to_review,
        unreviewed_code=unreviewed_code,
    )
    return a.verdict, a.reasons
