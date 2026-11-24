"""``finalize``: the deterministic end of a review (DESIGN 8, ADDENDUM D and E).

Inputs are the sealed payloads of the earlier activities plus run-dir data whose
digests the caller has checked (the anchor map and head file contents). No model is
called and nothing is read from the clock: the caller passes the review time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from morning_star_review.core.contract import (
    AnchorMap,
    DismissCommand,
    DroppedCandidate,
    ExploreResult,
    FinalFinding,
    FinalReview,
    MergeResult,
    Prepared,
    PriorFinding,
    PriorState,
    PrSnapshot,
    ResolvedPrior,
    ResolvedRoles,
    ReviewDraft,
    Role,
    Severity,
    Usage,
    VerifyResult,
    severity_at_least,
    usage_key,
)
from morning_star_review.diff.prepass import nothing_to_review, unreviewed_code
from morning_star_review.review import render
from morning_star_review.review.anchoring import line_text
from morning_star_review.review.reconcile import (
    Source,
    apply_verification,
    build_findings,
    combine_prior_status,
    reconcile_priors,
    refound_priors,
    sources_from_draft,
)
from morning_star_review.review.verdict import (
    Assessment,
    assess,
    counts_as_finding,
    is_possible,
)

__all__ = [
    "STATE_VERSION",
    "HeadText",
    "RunInfo",
    "collect_usage",
    "finalize",
    "models_of",
    "readiness",
]

STATE_VERSION = 1
MAX_STATE_FINDINGS = 60
HeadText = Callable[[str], list[str] | None]


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Run facts ``finalize`` cannot derive from its payloads."""

    execution_id: str
    run_label: str
    tool_version: str
    reviewed_at: str  # ISO-8601 UTC, read by the activity (finalize has no clock)
    duration_seconds: float | None = None
    # The run's wind-down had started when finalize ran (the run was finished early).
    run_limit_reached: bool = False
    deadline_minutes: int | None = None  # the run's overall time limit, when known
    # Time the models' rate limits paused the agents, summed over every agent job.
    rate_limit_wait_seconds: float = 0.0


def _inside_repo(path: str) -> bool:
    """A relative path without ``..``: prior-state paths can come from model text."""
    return (
        bool(path)
        and "\x00" not in path
        and not path.startswith("/")
        and (".." not in PurePosixPath(path).parts)
    )


def _cached(head_text: HeadText) -> HeadText:
    """Memoised ``head_text`` that never asks for a path outside the checkout."""
    cache: dict[str, list[str] | None] = {}

    def head(path: str) -> list[str] | None:
        if path not in cache:
            lines = head_text(path) if _inside_repo(path) else None
            cache[path] = None if lines is None else [ln.rstrip("\r\n") for ln in lines]
        return cache[path]

    return head


def collect_usage(
    explores: Sequence[ExploreResult],
    drafts: Sequence[ReviewDraft],
    verify: VerifyResult | None,
) -> dict[str, Usage]:
    """Usage per ``role:model`` (ADDENDUM D), summing repeated keys."""
    entries: list[tuple[Role, Usage]] = [("explorer", e.usage) for e in explores]
    entries += [("formatter", d.formatter_usage) for d in drafts if d.formatter_usage]
    if verify is not None:
        entries.append(("verifier", verify.usage))
        if verify.formatter_usage is not None:
            entries.append(("formatter", verify.formatter_usage))
    out: dict[str, Usage] = {}
    for role, u in entries:
        key = usage_key(role, u.model)
        out[key] = out[key].plus(u) if key in out else u
    return out


def models_of(roles: ResolvedRoles) -> dict[str, Any]:
    """Models and efforts per role, for ``findings.json`` and ``run.json``."""
    return {
        "explorers": [{"model": r.model, "effort": r.effort} for r in roles.explorers],
        "verifier": (
            {"model": roles.verifier.model, "effort": roles.verifier.effort}
            if roles.verifier
            else None
        ),
        "formatter": {"model": roles.formatter_model, "effort": roles.formatter_effort},
    }


def _coverage(
    prep: Prepared, explores: Sequence[ExploreResult], prior: PriorState | None
) -> render.Coverage:
    reviewable = set(prep.reviewable_paths)
    if prep.scope == "carry_over":
        # Nothing changed since a review that covered everything (or did not: then its
        # state lists the files it did not open; a state before 0.3.0 does not).
        if prior is not None and prior.complete:
            covered = reviewable
        elif prior is not None and prior.blockers:
            covered = reviewable - set(prior.not_opened)
        else:
            covered = set()
    else:
        opened = {p for e in explores for p in e.opened_paths}
        covered = (set(prep.inlined_paths) | opened) & reviewable
    return render.Coverage(
        reviewable=len(reviewable),
        covered=len(covered),
        excluded=list(prep.excluded),
        not_opened=sorted(reviewable - covered),
    )


def _state_evidence(f: FinalFinding, head: HeadText) -> str:
    """The flagged head code, so a later review can tell whether it changed."""
    text = None
    if f.anchor in ("inline", "snapped") and not f.carried:
        text = line_text(head(f.path), f.start_line or f.line)
    # Cut without an ellipsis: a cut line must stay a substring of the head line.
    return (text if text and text.strip() else f.evidence)[:300]


_EXPLANATION_MAX, _FIX_MAX = 1000, 500  # PriorFinding limits


def _open_entry(
    f: FinalFinding, recorded: Mapping[str, PriorFinding], head: HeadText
) -> PriorFinding:
    """The state entry of an open finding, with the text a later summary shows."""
    prior = recorded.get(f.id)
    return PriorFinding(
        id=f.id,
        path=f.path,
        line=f.line,
        severity=f.severity,
        title=f.title,
        evidence=prior.evidence if f.carried and prior else _state_evidence(f, head),
        comment_id=f.comment_id,
        thread_id=f.thread_id,
        state=f.state,
        start_line=f.start_line,
        category=f.category,
        explanation=f.explanation[:_EXPLANATION_MAX],
        fix=f.fix[:_FIX_MAX] if f.fix else None,
    )


def _closed(p: PriorFinding, **update: Any) -> PriorFinding:
    """``p`` as a closed record: no texts, the given state and attribution."""
    return p.model_copy(update={"explanation": "", "fix": None, **update})


def _dismisser(p: PriorFinding) -> str | None:
    return p.dismissed_by or p.resolved_by


def _closed_entries(
    snap: PrSnapshot,
    priors: Sequence[ResolvedPrior],
    repeats: Sequence[tuple[FinalFinding, PriorFinding]],
    head: HeadText,
) -> list[PriorFinding]:
    """The PR's record of closed findings: dismissed ones first, then fixed ones,
    each newest first, so the oldest fixed findings are dropped first when the state
    is full. Findings fixed in earlier reviews are kept as recorded."""
    prior = snap.prior
    recorded = {p.id: p for p in prior.findings} if prior else {}
    commands: dict[str, DismissCommand] = {c.id: c for c in snap.dismiss_commands}
    dismissed: list[PriorFinding] = []
    fixed: list[PriorFinding] = []
    earlier: list[PriorFinding] = []
    for r in priors:
        p = recorded.get(r.id)
        if p is None:
            continue
        if r.action == "dismissed" and p.state == "dismissed":
            earlier.append(_closed(p))
        elif r.action == "dismissed":
            c = commands.get(r.id)
            dismissed.append(
                _closed(
                    p,
                    state="dismissed",
                    dismissed_by=c.by if c else _dismisser(p),
                    dismiss_reason=(c.reason or None) if c else None,
                )
            )
        elif r.action in ("resolve", "file_deleted"):
            fixed.append(_closed(p, state="fixed", fixed_in=snap.head_sha))
    for f, same in repeats:
        entry = _open_entry(f, {}, head)
        dismissed.append(
            _closed(
                entry,
                state="dismissed",
                dismissed_by=_dismisser(same),
                dismiss_reason=f"repeats {same.id}",
            )
        )
    old_fixed = [_closed(p) for p in (prior.findings if prior else []) if p.state == "fixed"]
    return [*dismissed, *earlier, *fixed, *old_fixed]


def _state(
    snap: PrSnapshot,
    info: RunInfo,
    assessment: Assessment,
    findings: Sequence[FinalFinding],
    priors: Sequence[ResolvedPrior],
    head: HeadText,
    repeats: Sequence[tuple[FinalFinding, PriorFinding]] = (),
    scope: str = "full",
    not_opened: Sequence[str] = (),
) -> PriorState:
    recorded = {p.id: p for p in snap.prior.findings} if snap.prior else {}
    items: list[PriorFinding] = []
    for f in findings:
        if is_possible(f):
            # Not recorded: a later review would carry it as an open finding (the state
            # has no verification status), and possible issues never count as findings.
            continue
        items.append(_open_entry(f, recorded, head))
    listed = {i.id for i in items}
    for entry in _closed_entries(snap, priors, repeats, head):
        if entry.id not in listed:
            listed.add(entry.id)
            items.append(entry)
    prior = snap.prior
    # A carry-over (and a refresh) reviewed nothing new: the review it carries over
    # still describes the version and configuration that made it.
    carried = scope == "carry_over" and prior is not None
    return PriorState.model_validate(
        {
            "version": STATE_VERSION,
            "tool_version": prior.tool_version if carried and prior else info.tool_version,
            "head_sha": snap.head_sha,
            "merge_base_sha": snap.merge_base_sha,
            # A "findings" review can be incomplete too: a later review must not rely on it.
            "complete": assessment.complete,
            "verdict": assessment.verdict,
            "config_hash": prior.config_hash if carried and prior else snap.config.config_hash,
            "reviewed_at": info.reviewed_at,
            "findings": items[:MAX_STATE_FINDINGS],
            "summary_comment_id": prior.summary_comment_id if prior else None,
            "refused_resolves": list(prior.refused_resolves) if prior else [],
            "blockers": list(assessment.blockers)[:20],
            "not_opened": [p[:500] for p in not_opened][:100],
        }
    )


def _command_notes(snap: PrSnapshot) -> list[str]:
    """Why `/msreview dismiss` commands did nothing (unauthorised, unknown id, fixed)."""
    recorded = {p.id: p for p in snap.prior.findings} if snap.prior else {}
    notes = list(snap.ignored_commands)
    for c in snap.dismiss_commands:
        p = recorded.get(c.id)
        if p is None:
            notes.append(
                f"dismiss command for {c.id} by {c.by}: this pull request has no such finding"
            )
        elif p.state == "fixed":
            notes.append(f"dismiss command for {c.id} by {c.by}: the finding is already fixed")
    return notes[:20]


def readiness(
    findings: Sequence[FinalFinding], complete: bool, blocking_severity: Severity
) -> tuple[bool, int]:
    """(ready to merge, open findings at or above ``blocking_severity``)."""
    blocking = sum(
        1
        for f in findings
        if counts_as_finding(f) and severity_at_least(f.severity, blocking_severity)
    )
    return complete and blocking == 0, blocking


def _sources(
    prep: Prepared,
    drafts: Sequence[ReviewDraft],
    merge: MergeResult | None,
    verify: VerifyResult | None,
) -> tuple[list[Source], list[DroppedCandidate], bool]:
    """Findings to publish, refuted candidates, and whether verification applied."""
    if prep.scope == "carry_over":
        return [], [], False
    if merge is not None:
        verified = prep.roles.verify_enabled and bool(merge.candidates)
        sources, refuted = apply_verification(merge, verify, verified=verified)
        return sources, refuted, verified
    default = prep.roles.explorer_model
    sources = [s for d in drafts for s in sources_from_draft(d, default)]
    if prep.roles.verify_enabled and sources:
        # Verification was configured but no merge result reached finalize, so nothing
        # was verified: every finding is unconfirmed and the verdict needs the verifier.
        sources = [replace(s, verify_status="uncertain") for s in sources]
        return sources, [], True
    return sources, [], False


def _merge_note(merge: MergeResult | None) -> str | None:
    if merge is None or len(merge.explorer_models) < 2:
        return None
    note = (
        f"Ensemble: {merge.input_findings} draft findings from "
        f"{len(merge.explorer_models)} explorers merged into {len(merge.candidates)} candidates"
    )
    if merge.dropped_over_cap:
        note += f" ({merge.dropped_over_cap} more over the cap)"
    return note


def finalize(
    snap: PrSnapshot,
    prep: Prepared,
    explore: ExploreResult | None,
    draft: ReviewDraft | None,
    anchors: AnchorMap,
    head_text: HeadText,
    info: RunInfo,
    *,
    explores: Sequence[ExploreResult] = (),
    drafts: Sequence[ReviewDraft] = (),
    merge: MergeResult | None = None,
    verify: VerifyResult | None = None,
) -> FinalReview:
    """Anchor, reconcile, decide the verdict and render the review (unsealed).

    ``explore``/``draft`` are the primary explorer's; with an ensemble, ``explores``
    and ``drafts`` hold every explorer's (the primary's included) and ``merge`` /
    ``verify`` the pipeline results. ``head_text(path)`` returns the head lines of a
    file (``None`` if it does not exist at head); the caller confines it to the repo.
    """
    head = _cached(head_text)
    all_explores = list(explores) or ([explore] if explore is not None else [])
    all_drafts = list(drafts) or ([draft] if draft is not None else [])
    cfg = snap.config
    pr_paths = {f.path for f in snap.files}

    sources, refuted, verified = _sources(prep, all_drafts, merge, verify)
    priors = reconcile_priors(
        snap.prior,
        combine_prior_status(all_drafts),
        head,
        cfg,
        renamed_from=anchors.renamed_from,
        bot_login=snap.bot_login,
        existing_paths={f.path for f in snap.files if f.status != "removed"},
        commands=snap.dismiss_commands,
    )
    built = build_findings(
        sources,
        anchors=anchors,
        head_text=head,
        pr_paths=pr_paths,
        cfg=cfg,
        priors=priors,
        prior_state=snap.prior,
    )
    priors = refound_priors(priors, built.reported_ids)
    coverage = _coverage(prep, all_explores, snap.prior)
    idle = None if prep.reviewable_paths else nothing_to_review(prep.excluded)
    unreviewed = None if prep.reviewable_paths else unreviewed_code(prep.excluded)
    assessment = assess(
        scope=prep.scope,
        expected_explorers=[r.model for r in prep.roles.explorers],
        explores=all_explores,
        drafts=all_drafts,
        verify_needed=verified,
        verify=verify,
        partial_reason=prep.partial_reason,
        coverage=coverage.as_dict(),
        findings=built.findings,
        priors=priors,
        prior_state=snap.prior,
        structured=len(merge.explorer_models) if merge is not None else len(all_drafts),
        nothing_to_review=idle,
        unreviewed_code=unreviewed,
        run_limit_reached=info.run_limit_reached,
        deadline_minutes=info.deadline_minutes,
        rate_limit_wait_seconds=info.rate_limit_wait_seconds,
    )
    verdict, reasons = assessment.verdict, assessment.reasons
    ready, blocking = readiness(built.findings, assessment.complete, cfg.blocking_severity)
    state = _state(
        snap,
        info,
        assessment,
        built.findings,
        priors,
        head,
        built.repeats,
        prep.scope,
        coverage.not_opened,
    )
    usage = collect_usage(all_explores, all_drafts, verify)
    cost = sum(u.cost_usd for u in usage.values())
    verify_counts: dict[str, int] = {}
    if verified:
        verify_counts = {
            "confirmed": sum(1 for s in sources if s.verify_status == "confirmed"),
            "uncertain": sum(1 for s in sources if s.verify_status == "uncertain"),
            "refuted": len(refuted),
        }
        if verify is not None and verify.state == "NotRun" and not verify.complete:
            # Skipped (the run reached its time limit): not "nothing to verify".
            verify_counts["not_run"] = 1
    details = render.RunDetails(
        execution_id=info.execution_id,
        run_label=info.run_label,
        roles=prep.roles,
        usage=usage,
        cost_usd=cost,
        duration_seconds=info.duration_seconds,
        tool_version=info.tool_version,
        verify_counts=verify_counts,
        merge_note=_merge_note(merge),
    )
    view = render.SummaryInput(
        owner=snap.owner,
        repo=snap.repo,
        number=snap.number,
        head_sha=snap.head_sha,
        verdict=verdict,
        verdict_reasons=reasons,
        complete=assessment.complete,
        scope=prep.scope,
        since_sha=prep.since_sha,
        findings=built.findings,
        priors=priors,
        inline_min_severity=cfg.inline_min_severity,
        coverage=coverage,
        details=details if cfg.show_usage else None,
        over_cap=built.over_cap,
        author_may_dismiss=cfg.author_may_dismiss,
        ready=ready,
        blocking=blocking,
        blocking_severity=cfg.blocking_severity,
        closed=[f for f in state.findings if f.state in ("fixed", "dismissed")],
        command_notes=_command_notes(snap),
    )
    return FinalReview(
        verdict=verdict,
        verdict_reasons=reasons,
        findings=built.findings,
        priors=priors[:60],
        summary_md=render.summary(view),
        report_md=render.report(view, head),
        state=state,
        usage=usage,
        cost_usd=cost,
        coverage=coverage.as_dict(),
        refuted=refuted[:60],
        ready=ready,
        blocking=blocking,
    )
