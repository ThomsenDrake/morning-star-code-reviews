"""Review verdict (DESIGN 8.4 and ADDENDUM D).

``clean`` is a claim that the whole change was reviewed and nothing was found, so it
needs proof: every explorer (and the verifier, when it had work) finished, nothing
was cut short, every reviewable file was covered, and no finding is open. Anything
less is ``incomplete``, never ``clean``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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
    "MAX_REASON_CHARS",
    "RESOLVED_WITHOUT_FIX",
    "decide",
    "incomplete_reasons",
    "is_open",
    "resolved_without_fix",
]

MAX_REASON_CHARS = 300
MAX_REASONS = 20
RESOLVED_WITHOUT_FIX = "resolved without a fix by"


def is_open(f: FinalFinding) -> bool:
    """New or carried, posted inline or summary-only: anything not fixed or dismissed."""
    return f.state in ("open", "summary_only")


def resolved_without_fix(priors: Sequence[ResolvedPrior]) -> list[ResolvedPrior]:
    """Priors whose thread was resolved by someone who may not dismiss findings."""
    return [p for p in priors if p.action == "keep_open" and RESOLVED_WITHOUT_FIX in p.note]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


def _explorer_reasons(
    expected: Sequence[str], explores: Sequence[ExploreResult], has_open: bool
) -> list[str]:
    reasons: list[str] = []
    seen = {e.model for e in explores}
    reasons.extend(f"the {m} explorer produced no result" for m in expected if m not in seen)
    for e in explores:
        exhausted = ", ".join(e.exhausted_by) or "budget"
        if not e.complete:
            detail = f" (out of {exhausted})" if e.budget_exhausted else ""
            state = "" if e.state == "Completed" else f"; state {e.state}"
            reasons.append(f"the {e.model} explorer did not finish its review{detail}{state}")
        elif e.budget_exhausted and not has_open:
            reasons.append(f"the {e.model} explorer ran out of {exhausted} and reported no finding")
    return reasons


def _verifier_reasons(verify_needed: bool, verify: VerifyResult | None) -> list[str]:
    if not verify_needed:
        return []
    if verify is None:
        return ["the verifier did not run"]
    reasons: list[str] = []
    if not verify.complete:
        out = f" (out of {', '.join(verify.exhausted_by)})" if verify.budget_exhausted else ""
        reasons.append(f"the verifier did not finish{out}")
    if verify.source == "parser_lossy":
        reasons.append("the verifier's notes could not be fully parsed")
    return reasons


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
    has_open: bool,
    prior_state: PriorState | None,
    structured: int | None = None,
) -> list[str]:
    """Plain-language reasons that make the review ``incomplete`` (empty = complete).

    ``structured`` is how many explorers' notes became a draft (default: the number of
    ``drafts``); an explorer whose notes were never structured contributed no findings,
    so its result cannot support a ``clean`` verdict.
    """
    if scope == "carry_over":
        # No model ran: completeness is inherited from the review being carried over.
        if prior_state is None or not prior_state.complete or prior_state.verdict == "incomplete":
            return ["the previous review was incomplete and no reviewable file changed since"]
        return []
    reasons = _explorer_reasons(expected_explorers, explores, has_open)
    unstructured = len(explores) - (len(drafts) if structured is None else structured)
    if unstructured > 0:
        count = "1 explorer" if unstructured == 1 else f"{unstructured} explorers"
        reasons.append(f"the notes of {count} were not turned into findings")
    for i, d in enumerate(drafts):
        if d.source == "parser_lossy":
            who = d.explorer_model or (expected_explorers[i] if i < len(expected_explorers) else "")
            reasons.append(
                f"the notes of the {who or 'explorer'} explorer could not be fully parsed"
            )
    reasons.extend(_verifier_reasons(verify_needed, verify))
    if partial_reason:
        reasons.append(f"only part of the change was reviewed: {partial_reason}")
    reviewable, covered = coverage.get("reviewable", 0), coverage.get("covered", 0)
    if covered < reviewable:
        missing = reviewable - covered
        verb = "was" if missing == 1 else "were"
        reasons.append(f"{missing} of {reviewable} reviewable changed files {verb} not opened")
    return [_cap(r) for r in dict.fromkeys(reasons)][:MAX_REASONS]


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
) -> tuple[Verdict, list[str]]:
    """The verdict and its reasons (DESIGN 8.4; ADDENDUM D for the verifier).

    ``verify_needed`` is True when verification is enabled and there was at least one
    candidate to verify. ``findings`` are the final (unrefuted) findings, ``priors`` the
    reconciled prior findings.
    """
    has_open = any(is_open(f) for f in findings)
    reasons = incomplete_reasons(
        scope=scope,
        expected_explorers=expected_explorers,
        explores=explores,
        drafts=drafts,
        verify_needed=verify_needed,
        verify=verify,
        partial_reason=partial_reason,
        coverage=coverage,
        has_open=has_open,
        prior_state=prior_state,
        structured=structured,
    )
    if reasons:
        return "incomplete", reasons
    if has_open or resolved_without_fix(priors):
        return "findings", []
    return "clean", []
