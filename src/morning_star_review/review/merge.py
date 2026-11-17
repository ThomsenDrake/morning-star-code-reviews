"""Deterministic merge of explorer drafts into voted candidates (ADDENDUM D).

Each explorer of an ensemble writes its own draft. The same bug usually shows up in
several drafts with slightly different lines, ranges, evidence and titles. Two
findings are the same when they are on the same path and near each other, and they
are similar:

- near: their line ranges (``line``..``end_line``) overlap or are at most three lines
  apart, and, on the diff, they are in the same hunk. One explorer may flag a whole
  function while another flags the one line inside it.
- similar: their evidence matches (one contains the other, length ratio at least
  0.6), or their titles share at least 40% of their significant words, or they have
  the same category and their ranges overlap by at least half of the smaller range.

A merged candidate keeps the highest severity, the longest explanation and the best
anchor (on the diff with exact evidence first); its ``votes`` are the models that
reported it. No model is involved.
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
    MAX_RANGE_SPAN,
    Anchored,
    anchor_finding,
    evidence_variants,
    hunk_of,
    resolve_path,
)

__all__ = [
    "LINE_WINDOW",
    "MAX_CANDIDATES",
    "RANGE_OVERLAP",
    "TITLE_OVERLAP",
    "HeadText",
    "draft_models",
    "merge_drafts",
    "same_finding",
    "title_similarity",
    "title_tokens",
]

MAX_CANDIDATES = 60
LINE_WINDOW = 3  # ranges at most this many lines apart are near
TITLE_OVERLAP = 0.4  # Dice coefficient of significant title words
RANGE_OVERLAP = 0.5  # share of the smaller range, for same-category findings

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
    lines: tuple[int, int] | None  # (first, last) line the finding covers, see _lines_of


def _lines_of(where: Anchored, f: DraftFinding) -> tuple[int, int] | None:
    """The lines a finding covers: its anchored range, else the range the model asked for.

    A range that anchoring could not place (or a finding off the diff) still tells
    which code the model meant; it moves with a snapped start. Ranges longer than
    ``MAX_RANGE_SPAN`` count as their first line only.
    """
    start = where.line
    if start is None:
        return None
    end = where.end_line
    if end is None and f.line is not None and f.end_line is not None:
        end = start + (f.end_line - f.line)
    if end is None or end < start or end - start > MAX_RANGE_SPAN:
        return start, start
    return start, end


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Lines between two ranges; zero or less when they overlap (minus the overlap + 1)."""
    return max(a[0], b[0]) - min(a[1], b[1])


def _overlap_share(a: tuple[int, int] | None, b: tuple[int, int] | None) -> float:
    """Overlapping lines as a share of the smaller range (0..1)."""
    if a is None or b is None or _gap(a, b) > 0:
        return 0.0
    smaller = min(a[1] - a[0], b[1] - b[0]) + 1
    return (1 - _gap(a, b)) / smaller


def _near(a: _Item, b: _Item) -> bool:
    # Same hunk when both are on the diff; both off the diff otherwise (hunk None).
    if a.where.path != b.where.path or a.hunk != b.hunk:
        return False
    if a.lines is None or b.lines is None:
        return a.lines is None and b.lines is None
    return _gap(a.lines, b.lines) <= LINE_WINDOW


def _similar(a: _Item, b: _Item) -> bool:
    fa, fb = a.finding, b.finding
    return (
        _evidence_similar(fa.evidence, fb.evidence)
        or title_similarity(fa.title, fb.title) >= TITLE_OVERLAP
        or (fa.category == fb.category and _overlap_share(a.lines, b.lines) >= RANGE_OVERLAP)
    )


def same_finding(a: _Item, b: _Item) -> bool:
    """True when two anchored draft findings describe the same issue."""
    return _near(a, b) and _similar(a, b)


@dataclass
class _Cluster:
    items: list[_Item] = field(default_factory=list)

    def distance(self, item: _Item) -> int:
        """Smallest line gap to a member (negative when ranges overlap: more is closer)."""
        if item.lines is None:
            return 0
        gaps = [_gap(item.lines, m.lines) for m in self.items if m.lines is not None]
        return min(gaps, default=0)


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
            rank = (SEVERITY_ORDER[f.severity], di, fi)
            items.append(_Item(model, rank, f, where, hunk, _lines_of(where, f)))
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


def _anchor_quality(item: _Item) -> int:
    """0: on the diff with exact evidence, 1: on the diff, 2: off the diff."""
    on_diff = item.where.kind in ("inline", "snapped")
    return 0 if on_diff and item.where.exact else 1 if on_diff else 2


def _best_anchor(cluster: _Cluster) -> _Item:
    """The member whose location the candidate takes (rank order on ties).

    An explorer that flags a whole function next to one that quotes the exact
    line yields the exact line, whichever is more severe.
    """
    return min(cluster.items, key=_anchor_quality)


def _suggestion(cluster: _Cluster, anchor: _Item) -> str | None:
    """The first suggestion written for exactly the anchor's lines.

    A suggestion replaces specific lines, so one written by another explorer for a
    neighbouring line must not be attached to the candidate's location.
    """
    span = _span(anchor)
    return next(
        (
            m.finding.suggestion
            for m in cluster.items
            if m.finding.suggestion and _span(m) == span and _placed_for_suggestion(m)
        ),
        None,
    )


def _combine(cluster: _Cluster, models: list[str]) -> tuple[DraftFinding, list[str]]:
    """One candidate per cluster.

    The claim (severity, category, title, fix) comes from the representative, the
    most severe member of the earliest draft (items arrive in rank order). The
    location and evidence come from the best anchor. The explanation is the longest.
    """
    rep, anchor = cluster.items[0], _best_anchor(cluster)
    f, at, where = rep.finding, anchor.finding, anchor.where
    on_diff = where.kind in ("inline", "snapped")
    line = where.line if on_diff else at.line
    end_line = at.end_line
    if end_line is not None and at.line is not None and line is not None:
        end_line += line - at.line  # keep the requested range length; finalize re-checks it
    members = [m.finding for m in cluster.items]
    evidence = at.evidence if at.evidence.strip() else _longest([m.evidence for m in members])
    fix = f.fix if f.fix and f.fix.strip() else _longest([m.fix for m in members])
    merged = f.model_copy(
        update={
            "path": where.path,
            "line": line,
            "end_line": end_line,
            "evidence": evidence or "",
            "explanation": _longest([m.explanation for m in members]) or f.explanation,
            "fix": fix,
            "suggestion": _suggestion(cluster, anchor),
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
