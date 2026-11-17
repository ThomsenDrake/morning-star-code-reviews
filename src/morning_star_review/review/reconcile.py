"""Finding ids, de-duplication, caps, verification policy and prior reconciliation.

DESIGN 8.2 and 8.3, plus ADDENDUM D (publishing policy for verified candidates):

- ``confirmed`` candidates are published normally (inline when anchored and severe
  enough);
- ``uncertain`` candidates go to the summary only, unless at least two explorers
  reported them and they are ``high`` or worse: then they go inline, tagged
  "(unconfirmed)";
- ``refuted`` candidates are dropped and kept only for the report JSON.

Prior findings are reconciled deterministically: a model's claim that something was
fixed is advisory, and only a change to the flagged code resolves a thread.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field

from morning_star_review.core.contract import (
    SEVERITY_ORDER,
    AnchorMap,
    DraftFinding,
    DroppedCandidate,
    FinalFinding,
    MergeResult,
    PriorFinding,
    PriorState,
    PriorStatus,
    PriorStatusValue,
    ResolvedConfig,
    ResolvedPrior,
    ReviewDraft,
    VerifyEntry,
    VerifyResult,
    VerifyStatus,
    severity_at_least,
)
from morning_star_review.core.ids import finding_id, norm
from morning_star_review.review import suggest
from morning_star_review.review.anchoring import anchor_finding, line_text, resolve_path
from morning_star_review.review.verdict import RESOLVED_WITHOUT_FIX

__all__ = [
    "MAX_FINDINGS",
    "Built",
    "Source",
    "apply_verification",
    "build_findings",
    "carried_finding",
    "combine_prior_status",
    "inline_allowed_by_verification",
    "reconcile_priors",
    "refound_priors",
    "sources_from_draft",
    "truncate",
]

MAX_FINDINGS = 50
HeadText = Callable[[str], list[str] | None]

# Field limits of FinalFinding (DraftFinding allows longer explanations and fixes).
_TITLE_MAX, _EVIDENCE_MAX, _EXPLANATION_MAX, _FIX_MAX, _NOTE_MAX = 160, 500, 2000, 1000, 200
_DROPPED_REASON_MAX, _PRIOR_NOTE_MAX = 300, 300
_VERIFY_RANK: dict[VerifyStatus | None, int] = {"confirmed": 0, None: 0, "uncertain": 1}


def truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, with an ellipsis when cut."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class Source:
    """A finding to finalize: a draft finding with its votes and verification status."""

    draft: DraftFinding
    votes: tuple[str, ...] = ()
    verify_status: VerifyStatus | None = None
    candidate_id: str | None = None
    # The line the suggestion was written for (the merged candidate's line). When the
    # verifier's corrections move the finding elsewhere, the suggestion no longer fits.
    suggestion_line: int | None = None


@dataclass(frozen=True, slots=True)
class Built:
    """Output of ``build_findings``: at most 50 findings, plus how many did not fit."""

    findings: list[FinalFinding] = field(default_factory=list)
    over_cap: int = 0
    dropped_dismissed: int = 0  # new findings matching a dismissed prior
    reported_ids: frozenset[str] = frozenset()  # ids reported in this review (not dismissed)


def sources_from_draft(draft: ReviewDraft | None, model: str | None = None) -> list[Source]:
    """Sources for the single-explorer path (no merge, no verification)."""
    if draft is None:
        return []
    who = draft.explorer_model or model
    votes = (who,) if who else ()
    return [Source(draft=f, votes=votes) for f in draft.findings]


def _corrected(draft: DraftFinding, entry: VerifyEntry | None) -> DraftFinding:
    """Apply the verifier's optional corrections (line, evidence, severity)."""
    if entry is None:
        return draft
    update: dict[str, object] = {}
    if entry.line is not None and entry.line >= 1 and entry.line != draft.line:
        update["line"] = entry.line
        if draft.end_line is not None and draft.line is not None:
            update["end_line"] = draft.end_line + entry.line - draft.line
    if entry.evidence and entry.evidence.strip():
        update["evidence"] = entry.evidence
    if entry.severity is not None:
        update["severity"] = entry.severity
    if not update:
        return draft
    return DraftFinding.model_validate({**draft.model_dump(), **update})


def apply_verification(
    merge: MergeResult, verify: VerifyResult | None, *, verified: bool
) -> tuple[list[Source], list[DroppedCandidate]]:
    """Split merged candidates into sources to publish and refuted candidates.

    ``verified`` says whether verification was part of the run. When it was but no
    result exists (or a candidate was never addressed), the candidate is ``uncertain``.
    """
    entries = {e.candidate_id: e for e in verify.entries} if verify else {}
    sources: list[Source] = []
    dropped: list[DroppedCandidate] = []
    for c in merge.candidates:
        entry = entries.get(c.candidate_id)
        status: VerifyStatus | None = None
        if verified:
            status = entry.status if entry else "uncertain"
        if status == "refuted":
            dropped.append(
                DroppedCandidate(
                    candidate_id=c.candidate_id,
                    path=c.path,
                    line=c.line,
                    severity=c.severity,
                    title=c.title,
                    votes=list(c.votes),
                    reason=truncate(entry.reason if entry else "", _DROPPED_REASON_MAX),
                )
            )
            continue
        draft = _corrected(c.to_draft(), entry)
        sources.append(Source(draft, tuple(c.votes), status, c.candidate_id, c.line))
    return sources, dropped


def inline_allowed_by_verification(f: FinalFinding) -> bool:
    """ADDENDUM D: uncertain findings go inline only with 2+ votes and high severity."""
    if f.verify_status != "uncertain":
        return True
    return len(f.votes) >= 2 and severity_at_least(f.severity, "high")


def _to_final(
    src: Source,
    anchors: AnchorMap,
    head: HeadText,
    pr_paths: set[str],
    suggestions: bool,
) -> FinalFinding:
    d = src.draft
    path = resolve_path(d.path, pr_paths, anchors.renamed_from)
    head_lines = head(path) if path in pr_paths else None
    where = anchor_finding(d, anchors, head_lines, pr_paths)
    on_diff = where.kind in ("inline", "snapped")
    anchor_text = (line_text(head_lines, where.line) if on_diff else None) or (
        norm(d.evidence) or norm(d.title)
    )
    notes: list[str] = []
    suggestion = None
    moved = on_diff and src.suggestion_line is not None and where.line != src.suggestion_line
    if suggestions and d.suggestion is not None and moved:
        notes.append("suggestion dropped: the verifier moved the finding to other lines")
    elif suggestions and d.suggestion is not None:
        checked = suggest.check(
            d.suggestion,
            path=where.path,
            anchor=where.kind,
            line=where.github_line,
            start_line=where.start_line,
            evidence=d.evidence,
            head_lines=head_lines,
            anchor_file=anchors.files.get(where.path),
            range_rejected=where.range_rejected,
        )
        suggestion = checked.suggestion
        if checked.reason:
            notes.append(truncate(checked.reason, _NOTE_MAX))
    return FinalFinding(
        id=finding_id(where.path, anchor_text),
        path=truncate(where.path, 500),
        line=where.github_line,
        start_line=where.start_line,
        anchor=where.kind,
        severity=d.severity,
        category=d.category,
        title=truncate(d.title, _TITLE_MAX),
        evidence=truncate(d.evidence, _EVIDENCE_MAX),
        explanation=truncate(d.explanation, _EXPLANATION_MAX),
        fix=truncate(d.fix, _FIX_MAX) if d.fix else None,
        state="open",
        votes=list(dict.fromkeys(src.votes))[:5],
        verify_status=src.verify_status,
        suggestion=suggestion,
        notes=notes,
    )


def _better_status(a: VerifyStatus | None, b: VerifyStatus | None) -> VerifyStatus | None:
    if "confirmed" in (a, b):
        return "confirmed"
    return a if a is not None else b


def _absorb(kept: FinalFinding, other: FinalFinding) -> FinalFinding:
    """Same id twice: keep ``kept`` (the more severe) and mention the other's title."""
    explanation = kept.explanation
    if norm(other.title) != norm(kept.title) and f"Also: {other.title}" not in explanation:
        explanation = truncate(f"{explanation}\n\nAlso: {other.title}", _EXPLANATION_MAX)
    votes = list(dict.fromkeys([*kept.votes, *other.votes]))[:5]
    return kept.model_copy(
        update={
            "explanation": explanation,
            "votes": votes,
            "verify_status": _better_status(kept.verify_status, other.verify_status),
            "notes": [*kept.notes, *other.notes][:4],
        }
    )


def _dedupe(findings: Iterable[FinalFinding]) -> list[FinalFinding]:
    by_id: dict[str, FinalFinding] = {}
    for f in findings:
        current = by_id.get(f.id)
        if current is None:
            by_id[f.id] = f
        elif SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[current.severity]:
            by_id[f.id] = _absorb(f, current)
        else:
            by_id[f.id] = _absorb(current, f)
    return list(by_id.values())


def carried_finding(prior: PriorFinding) -> FinalFinding:
    """A prior finding that stays open although no explorer reported it again."""
    has_thread = prior.comment_id is not None or prior.thread_id is not None
    return FinalFinding(
        id=prior.id,
        path=prior.path,
        line=prior.line,
        anchor="inline" if has_thread else "file",
        severity=prior.severity,
        category="other",
        title=prior.title,
        evidence=prior.evidence,
        explanation="",
        state="open" if has_thread else "summary_only",
        carried=True,
        thread_id=prior.thread_id,
        comment_id=prior.comment_id,
    )


def _sort_key(f: FinalFinding) -> tuple[int, int, str, int, str]:
    line = f.line if f.line is not None else 1 << 30
    return (SEVERITY_ORDER[f.severity], _VERIFY_RANK[f.verify_status], f.path, line, f.id)


def _has_thread(p: PriorFinding) -> bool:
    return p.comment_id is not None or p.thread_id is not None


def _assign_states(findings: list[FinalFinding], cfg: ResolvedConfig) -> list[FinalFinding]:
    """Inline posting candidates (DESIGN 8.2) under the verification policy."""
    budget = cfg.max_inline_comments
    out: list[FinalFinding] = []
    for f in findings:
        if f.carried:
            state = (
                "open" if f.comment_id is not None or f.thread_id is not None else "summary_only"
            )
        elif (
            budget > 0
            and f.anchor in ("inline", "snapped")
            and severity_at_least(f.severity, cfg.inline_min_severity)
            and inline_allowed_by_verification(f)
        ):
            state, budget = "open", budget - 1
        else:
            state = "summary_only"
        out.append(f if f.state == state else f.model_copy(update={"state": state}))
    return out


def build_findings(
    sources: Sequence[Source],
    *,
    anchors: AnchorMap,
    head_text: HeadText,
    pr_paths: set[str],
    cfg: ResolvedConfig,
    priors: Sequence[ResolvedPrior] = (),
    prior_state: PriorState | None = None,
) -> Built:
    """Anchor, id, de-duplicate, match against priors, sort, cap and assign states.

    New findings matching a dismissed prior are dropped; those matching an open prior
    with a thread are ``carried`` (never posted again). Open priors that nobody
    reported again are carried as well, so their threads stay listed.
    """
    findings = _dedupe(_to_final(s, anchors, head_text, pr_paths, cfg.suggestions) for s in sources)
    action = {p.id: p.action for p in priors}
    recorded = {p.id: p for p in prior_state.findings} if prior_state else {}
    kept: list[FinalFinding] = []
    dismissed = 0
    for f in findings:
        prior = recorded.get(f.id)
        if prior is not None and action.get(f.id) == "dismissed":
            dismissed += 1
            continue
        if (
            prior is not None
            and _has_thread(prior)
            and action.get(f.id) in ("keep_open", "resolve")
        ):
            f = f.model_copy(
                update={
                    "carried": True,
                    "thread_id": prior.thread_id,
                    "comment_id": prior.comment_id,
                }
            )
        kept.append(f)
    seen = frozenset(f.id for f in kept)
    kept.extend(
        carried_finding(recorded[p.id])
        for p in priors
        if p.action == "keep_open" and p.id in recorded and p.id not in seen
    )
    kept.sort(key=_sort_key)
    capped = kept[:MAX_FINDINGS]
    return Built(
        findings=_assign_states(capped, cfg),
        over_cap=len(kept) - len(capped),
        dropped_dismissed=dismissed,
        reported_ids=seen,
    )


def refound_priors(
    priors: Sequence[ResolvedPrior], reported_ids: frozenset[str]
) -> list[ResolvedPrior]:
    """A prior about to be resolved stays open when the same id was reported again."""
    return [
        p.model_copy(update={"action": "keep_open", "note": "reported again in this review"})
        if p.action == "resolve" and p.id in reported_ids
        else p
        for p in priors
    ]


_STATUS_PRIORITY: dict[PriorStatusValue, int] = {"still_open": 0, "fixed": 1, "unsure": 2}


def combine_prior_status(drafts: Sequence[ReviewDraft | None]) -> list[PriorStatus]:
    """One status per prior id across explorers; ``still_open`` wins, then ``fixed``."""
    best: dict[str, PriorStatus] = {}
    for d in drafts:
        for st in d.prior_status if d else ():
            cur = best.get(st.id)
            if cur is None or _STATUS_PRIORITY[st.status] < _STATUS_PRIORITY[cur.status]:
                best[st.id] = st
    return list(best.values())


def _evidence_present(evidence: str, lines: list[str]) -> bool:
    """Does the flagged code still occur in the head file? Unknown evidence counts as yes.

    "Occurs" means the normalised evidence is a substring of a (normalised) head line,
    or of the whole file for multi-line evidence. The reverse direction does not
    count: a line that was shortened by the fix contains less than the evidence.
    """
    e = norm(evidence)
    if not e:
        return True
    if len([ln for ln in evidence.splitlines() if ln.strip()]) > 1:
        return e in norm("\n".join(lines))
    return any(e in norm(ln) for ln in lines)


def _prior(p: PriorFinding, action: str, note: str) -> ResolvedPrior:
    return ResolvedPrior.model_validate(
        {
            "id": p.id,
            "title": p.title,
            "path": p.path,
            "action": action,
            "note": truncate(note, _PRIOR_NOTE_MAX),
        }
    )


def _reconcile_one(
    p: PriorFinding,
    said: PriorStatusValue | None,
    head_text: HeadText,
    renamed_from: dict[str, str],
    bot_login: str | None,
    existing_paths: Collection[str],
) -> ResolvedPrior | None:
    if p.state == "fixed":
        return None  # resolved by an earlier review
    if p.state == "dismissed":
        return _prior(p, "dismissed", "dismissed earlier")
    human = p.thread_resolved and p.resolved_by is not None and p.resolved_by != bot_login
    if human and p.resolver_can_dismiss:
        return _prior(p, "dismissed", f"dismissed by {p.resolved_by}")
    path = renamed_from.get(p.path, p.path)
    lines = head_text(path)
    if lines is None:
        if path in existing_paths:
            # The file is still there but cannot be read (binary, too large): the
            # flagged code may be unchanged, so the thread must not be resolved.
            return _prior(p, "keep_open", "the file could not be read at head")
        return _prior(p, "file_deleted", "the file was deleted")
    if not _evidence_present(p.evidence, [ln.rstrip("\r\n") for ln in lines]):
        if said == "still_open":
            return _prior(
                p, "keep_open", "the flagged code changed, but the model reports it is still open"
            )
        return _prior(p, "resolve", "the flagged code changed")
    if human:
        # Resolved by someone who may not dismiss findings, and the code is unchanged.
        return _prior(p, "keep_open", f"{RESOLVED_WITHOUT_FIX} {p.resolved_by}")
    if said == "fixed":
        return _prior(p, "keep_open", "model reports fixed but the flagged code is unchanged")
    return _prior(p, "keep_open", "the flagged code is unchanged")


def reconcile_priors(
    prior: PriorState | None,
    drafts_status: Sequence[PriorStatus],
    head_text: HeadText,
    cfg: ResolvedConfig | None = None,
    *,
    renamed_from: dict[str, str] | None = None,
    bot_login: str | None = None,
    existing_paths: Collection[str] = (),
) -> list[ResolvedPrior]:
    """Decide what happens to each finding of the previous review (DESIGN 8.3).

    ``resolver_can_dismiss`` already folds in ``cfg.author_may_dismiss`` (``fetch_pr``
    computes it), so ``cfg`` is accepted for the contract but not needed here.
    ``existing_paths`` are files known to exist at head (PR files not removed): when
    ``head_text`` returns ``None`` for one of them, the file is unreadable, not deleted.
    """
    del cfg
    if prior is None:
        return []
    said = {s.id: s.status for s in drafts_status}
    renames = renamed_from or {}
    existing = frozenset(existing_paths)
    out: list[ResolvedPrior] = []
    for p in prior.findings:
        r = _reconcile_one(p, said.get(p.id), head_text, renames, bot_login, existing)
        if r is not None:
            out.append(r)
    return out
