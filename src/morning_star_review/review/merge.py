"""Deterministic merge of explorer drafts into voted candidates (ADDENDUM D).

Each explorer of an ensemble writes its own draft. The same bug usually shows up in
several drafts with slightly different lines, evidence and titles. Two findings are
the same when they are on the same path, in the same hunk with anchored lines at most
three apart, and either their evidence matches (one contains the other, length ratio
at least 0.6) or their titles share at least half of their significant words.

A merged candidate keeps the highest severity and the longest explanation; its
``votes`` are the models that reported it. No model is involved.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from morning_star_review.core.contract import (
    SEVERITY_ORDER,
    AnchorMap,
    Candidate,
    DraftFinding,
    MergeResult,
    ReviewDraft,
)
from morning_star_review.core.ids import candidate_id, evidence_match
from morning_star_review.review.anchoring import (
    Anchored,
    anchor_finding,
    evidence_variants,
    hunk_of,
    resolve_path,
)

__all__ = [
    "LINE_WINDOW",
    "MAX_CANDIDATES",
    "TITLE_OVERLAP",
    "HeadText",
    "draft_models",
    "merge_drafts",
    "same_finding",
    "title_similarity",
    "title_tokens",
]

MAX_CANDIDATES = 60
LINE_WINDOW = 3
TITLE_OVERLAP = 0.5

HeadText = Callable[[str], list[str] | None]

_WORD = re.compile(r"[a-z0-9]+")
# Words that say nothing about which bug a title describes.
_STOPWORDS = frozenset(
    (
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "not",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "was",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
    )
)


def title_tokens(title: str) -> frozenset[str]:
    """Significant lower-case words of a title."""
    return frozenset(w for w in _WORD.findall(title.lower()) if w not in _STOPWORDS)


def title_similarity(a: str, b: str) -> float:
    """Share of words the two titles have in common (Dice coefficient, 0..1)."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def _evidence_similar(a: str, b: str) -> bool:
    return any(evidence_match(x, y) for x in evidence_variants(a) for y in evidence_variants(b))


@dataclass(frozen=True, slots=True)
class _Item:
    model: str
    rank: tuple[int, int, int]  # (severity, draft index, finding index): processing order
    finding: DraftFinding
    where: Anchored
    hunk: int | None  # hunk of the anchored line, None when not on a changed line


def _near(a: _Item, b: _Item) -> bool:
    if a.where.path != b.where.path:
        return False
    la, lb = a.where.line, b.where.line
    if a.hunk is not None and b.hunk is not None:
        return (
            a.hunk == b.hunk and la is not None and lb is not None and abs(la - lb) <= LINE_WINDOW
        )
    if a.hunk is None and b.hunk is None:  # neither on a changed line: compare model lines
        if la is None or lb is None:
            return la is None and lb is None
        return abs(la - lb) <= LINE_WINDOW
    return False


def _similar(a: _Item, b: _Item) -> bool:
    return _evidence_similar(a.finding.evidence, b.finding.evidence) or (
        title_similarity(a.finding.title, b.finding.title) >= TITLE_OVERLAP
    )


def same_finding(a: _Item, b: _Item) -> bool:
    """True when two anchored draft findings describe the same issue."""
    return _near(a, b) and _similar(a, b)


@dataclass
class _Cluster:
    items: list[_Item] = field(default_factory=list)

    def distance(self, item: _Item) -> int:
        lines = [m.where.line for m in self.items if m.where.line is not None]
        if item.where.line is None or not lines:
            return 0
        return min(abs(item.where.line - n) for n in lines)


def draft_models(drafts: Sequence[ReviewDraft]) -> list[str]:
    """The votes key of each draft: its explorer model, made unique by position."""
    models: list[str] = []
    for i, d in enumerate(drafts):
        name = d.explorer_model or f"explorer-{i + 1}"
        if name in models:
            name = f"{name[:90]}#{i + 1}"  # still a valid ModelId (at most 100 chars)
        models.append(name)
    return models


def _items(
    drafts: Sequence[ReviewDraft],
    models: list[str],
    anchors: AnchorMap,
    head_text: HeadText,
    pr_paths: set[str],
) -> list[_Item]:
    cache: dict[str, list[str] | None] = {}

    def head(path: str) -> list[str] | None:
        if path not in cache:
            cache[path] = head_text(path) if path in pr_paths else None
        return cache[path]

    items: list[_Item] = []
    for di, (draft, model) in enumerate(zip(drafts, models, strict=True)):
        for fi, f in enumerate(draft.findings):
            path = resolve_path(f.path, pr_paths, anchors.renamed_from)
            where = anchor_finding(f, anchors, head(path), pr_paths)
            on_diff = where.kind in ("inline", "snapped")
            hunk = hunk_of(anchors.files.get(where.path), where.line) if on_diff else None
            items.append(_Item(model, (SEVERITY_ORDER[f.severity], di, fi), f, where, hunk))
    return sorted(items, key=lambda it: it.rank)


def _cluster(items: list[_Item]) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for item in items:
        matching = [c for c in clusters if any(same_finding(item, m) for m in c.items)]
        if matching:
            min(matching, key=lambda c: c.distance(item)).items.append(item)  # first on ties
        else:
            clusters.append(_Cluster([item]))
    return clusters


def _longest(values: list[str | None]) -> str | None:
    best: str | None = None
    for v in values:
        if v and v.strip() and (best is None or len(v) > len(best)):
            best = v
    return best


def _span(item: _Item) -> tuple[int | None, int]:
    f = item.finding
    length = f.end_line - f.line if f.end_line is not None and f.line is not None else 0
    return item.where.line, max(length, 0)


def _placed_for_suggestion(item: _Item) -> bool:
    """ADDENDUM E rule 1 as seen by merge: on the model's own line, or snapped exactly.

    The merged candidate carries the snapped line, so ``finalize`` sees it as
    ``inline`` and could no longer tell that the model's line was off.
    """
    return item.where.kind == "inline" or (item.where.kind == "snapped" and item.where.exact)


def _suggestion(cluster: _Cluster) -> str | None:
    """The first suggestion written for exactly the representative's lines.

    A suggestion replaces specific lines, so one written by another explorer for a
    neighbouring line must not be attached to the representative's location.
    """
    span = _span(cluster.items[0])
    return next(
        (
            m.finding.suggestion
            for m in cluster.items
            if m.finding.suggestion and _span(m) == span and _placed_for_suggestion(m)
        ),
        None,
    )


def _combine(cluster: _Cluster, models: list[str]) -> tuple[DraftFinding, list[str]]:
    rep = cluster.items[0]  # most severe, earliest draft: items arrive in rank order
    f, where = rep.finding, rep.where
    on_diff = where.kind in ("inline", "snapped")
    line = where.line if on_diff else f.line
    end_line = f.end_line
    if end_line is not None and f.line is not None and line is not None:
        end_line += line - f.line  # keep the requested range length; finalize re-checks it
    members = [m.finding for m in cluster.items]
    evidence = f.evidence if f.evidence.strip() else _longest([m.evidence for m in members])
    fix = f.fix if f.fix and f.fix.strip() else _longest([m.fix for m in members])
    merged = f.model_copy(
        update={
            "path": where.path,
            "line": line,
            "end_line": end_line,
            "evidence": evidence or "",
            "explanation": _longest([m.explanation for m in members]) or f.explanation,
            "fix": fix,
            "suggestion": _suggestion(cluster),
        }
    )
    voted = {m.model for m in cluster.items}
    return DraftFinding.model_validate(merged.model_dump()), [m for m in models if m in voted]


def merge_drafts(
    drafts: Sequence[ReviewDraft],
    anchors: AnchorMap,
    head_text: HeadText,
    *,
    pr_paths: set[str] | None = None,
    cap: int = MAX_CANDIDATES,
) -> MergeResult:
    """Merge one draft per explorer into at most ``cap`` candidates (unsealed).

    ``head_text(path)`` returns the head lines of a PR file (``None`` when unknown);
    it is only called for paths in ``pr_paths`` (default: the files of ``anchors``).
    Candidates are ordered by severity, then votes, then location; ids are ``c1``...
    """
    paths = set(anchors.files) if pr_paths is None else pr_paths
    models = draft_models(drafts)
    combined = [
        _combine(c, models) for c in _cluster(_items(drafts, models, anchors, head_text, paths))
    ]
    combined.sort(
        key=lambda fv: (SEVERITY_ORDER[fv[0].severity], -len(fv[1]), fv[0].path, fv[0].line or 0)
    )
    kept = combined[: max(cap, 0)]
    candidates = [
        Candidate.from_draft(f, candidate_id(i), votes) for i, (f, votes) in enumerate(kept)
    ]
    return MergeResult(
        candidates=candidates,
        explorer_models=models,
        input_findings=sum(len(d.findings) for d in drafts),
        dropped_over_cap=len(combined) - len(kept),
    )
