"""Deterministic anchoring of draft findings to diff lines (DESIGN 8.1).

Models give a path, a line and an evidence string; any of them can be slightly off.
Anchoring never asks a model to repair anything: it normalises the path, checks the
line against GitHub's own patch, snaps to the nearest line whose text matches the
evidence, and otherwise degrades to a file-level or outside-the-PR finding.

``head_lines`` are the lines of the head version of the file, without line endings;
line ``n`` is ``head_lines[n - 1]``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from morning_star_review.core.contract import AnchorFile, AnchorKind, AnchorMap, DraftFinding
from morning_star_review.core.ids import evidence_match, norm

__all__ = [
    "MAX_RANGE_SPAN",
    "SNAP_WINDOW",
    "Anchored",
    "anchor",
    "anchor_finding",
    "evidence_variants",
    "hunk_of",
    "is_anchorable",
    "line_text",
    "normalize_path",
    "resolve_path",
    "same_hunk",
]

SNAP_WINDOW = 5  # search line-5..line+5 for the evidence (step 5)
MAX_RANGE_SPAN = 20  # end - start of an inline range (step 9)
_PATH_STRIP = " \t`'\"*"
_DIFF_PREFIXES = ("a/", "b/")


@dataclass(frozen=True, slots=True)
class Anchored:
    """Where a finding lands.

    ``line`` is the primary line (the evidence line) and ``end_line`` the end of an
    accepted range. For ``file`` findings ``line`` is the model's line, kept only for
    the summary link; for ``outside`` findings it is ``None``.
    """

    path: str
    kind: AnchorKind
    line: int | None
    end_line: int | None = None
    exact: bool = False  # norm(head[line]) == norm(evidence)
    range_rejected: bool = False  # the draft asked for a range that cannot be placed

    @property
    def github_line(self) -> int | None:
        """The ``line`` of a GitHub review comment (the end of a range)."""
        return self.end_line if self.end_line is not None else self.line

    @property
    def start_line(self) -> int | None:
        """The ``start_line`` of a GitHub review comment, or ``None`` for one line."""
        return self.line if self.end_line is not None else None


def normalize_path(path: str) -> str:
    """Strip quotes, backticks, ``./`` and leading ``/`` from a model-written path."""
    p = path.strip().strip(_PATH_STRIP).replace("\\", "/")
    while True:
        before = p
        if p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p == before:
            return p


def resolve_path(path: str, pr_paths: set[str], renamed_from: dict[str, str]) -> str:
    """Map a model-written path to a PR path when possible; else return it normalised.

    Tries, in order: the path itself, an old name through ``renamed_from``, the path
    without a ``a/``/``b/`` diff prefix, and a unique PR path ending in ``/<path>``.
    """
    p = normalize_path(path)
    if p in pr_paths:
        return p
    if p in renamed_from:
        return renamed_from[p]
    for prefix in _DIFF_PREFIXES:
        if p.startswith(prefix):
            rest = p[len(prefix) :]
            if rest in pr_paths:
                return rest
            if rest in renamed_from:
                return renamed_from[rest]
    if p:
        matches = sorted(q for q in pr_paths if q.endswith("/" + p))
        if len(matches) == 1:
            return matches[0]
    return p


def is_anchorable(af: AnchorFile | None, line: int | None) -> bool:
    """True when ``line`` is a RIGHT-side line of GitHub's patch for the file."""
    if af is None or line is None:
        return False
    i = bisect.bisect_left(af.right_lines, line)
    return i < len(af.right_lines) and af.right_lines[i] == line


def hunk_of(af: AnchorFile | None, line: int | None) -> int | None:
    """Index of the hunk containing ``line`` (RIGHT side), or ``None``."""
    if af is None or line is None:
        return None
    for i, (start, end) in enumerate(af.hunks):
        if start <= line <= end:
            return i
    return None


def same_hunk(af: AnchorFile | None, a: int, b: int) -> bool:
    ha = hunk_of(af, a)
    return ha is not None and ha == hunk_of(af, b)


def line_text(head_lines: list[str] | None, line: int | None) -> str | None:
    """Head text of ``line`` (1-based), or ``None`` when out of range."""
    if head_lines is None or line is None or not 1 <= line <= len(head_lines):
        return None
    return head_lines[line - 1].rstrip("\r\n")


def evidence_variants(evidence: str) -> list[str]:
    """The evidence, plus its first non-blank line when it spans several lines."""
    variants = [evidence] if norm(evidence) else []
    lines = [ln for ln in evidence.splitlines() if norm(ln)]
    if len(lines) > 1:
        variants.append(lines[0])
    return variants


def _matches(text: str | None, variants: list[str]) -> bool:
    return text is not None and any(evidence_match(text, v) for v in variants)


def _exact(text: str | None, variants: list[str]) -> bool:
    return text is not None and any(norm(text) == norm(v) for v in variants)


def _snap_near(
    af: AnchorFile, head_lines: list[str] | None, line: int, variants: list[str]
) -> int | None:
    """Nearest anchorable line within ``SNAP_WINDOW`` whose text matches (earlier on ties)."""
    for distance in range(1, SNAP_WINDOW + 1):
        for cand in (line - distance, line + distance):
            if is_anchorable(af, cand) and _matches(line_text(head_lines, cand), variants):
                return cand
    return None


def _snap_unique(af: AnchorFile, head_lines: list[str] | None, variants: list[str]) -> int | None:
    """The only anchorable line of the file whose text matches, if exactly one does."""
    found = [n for n in af.right_lines if _matches(line_text(head_lines, n), variants)]
    return found[0] if len(found) == 1 else None


def _place(
    af: AnchorFile, head_lines: list[str] | None, line: int | None, evidence: str
) -> tuple[AnchorKind, int | None]:
    """Steps 4-8 of DESIGN 8.1 for a file that is part of the PR."""
    variants = evidence_variants(evidence)
    if variants:
        if is_anchorable(af, line) and _matches(line_text(head_lines, line), variants):
            return "inline", line
        if line is not None:
            near = _snap_near(af, head_lines, line, variants)
            if near is not None:
                return "snapped", near
        unique = _snap_unique(af, head_lines, variants)
        if unique is not None:
            return "snapped", unique
    if is_anchorable(af, line):
        return "inline", line
    return "file", line


def anchor_finding(
    draft: DraftFinding,
    anchors: AnchorMap,
    head_lines: list[str] | None,
    pr_paths: set[str],
    renamed_from: dict[str, str] | None = None,
) -> Anchored:
    """Anchor one draft finding (DESIGN 8.1 steps 1-9)."""
    renames = anchors.renamed_from if renamed_from is None else renamed_from
    path = resolve_path(draft.path, pr_paths, renames)
    if path not in pr_paths:
        return Anchored(path=path, kind="outside", line=None)
    line = draft.line if draft.line is not None and draft.line >= 1 else None
    af = anchors.files.get(path)
    if af is None:  # in the PR but GitHub sent no patch (binary, too large)
        return Anchored(path=path, kind="file", line=line)
    kind, placed = _place(af, head_lines, line, draft.evidence)
    exact = kind in ("inline", "snapped") and _exact(
        line_text(head_lines, placed), evidence_variants(draft.evidence)
    )
    end = draft.end_line
    wants_range = end is not None and line is not None and end != line
    if (
        kind not in ("inline", "snapped")
        or placed is None
        or end is None
        or line is None
        or end == line
    ):
        return Anchored(path=path, kind=kind, line=placed, exact=exact, range_rejected=wants_range)
    end += placed - line  # a snapped start moves the whole range
    ok = (
        end > placed
        and end - placed <= MAX_RANGE_SPAN
        and is_anchorable(af, end)
        and same_hunk(af, placed, end)
    )
    return Anchored(
        path=path,
        kind=kind,
        line=placed,
        end_line=end if ok else None,
        exact=exact,
        range_rejected=not ok,
    )


def anchor(
    draft: DraftFinding,
    anchors: AnchorMap,
    head_lines: list[str] | None,
    pr_paths: set[str],
    renamed_from: dict[str, str],
) -> tuple[AnchorKind, int | None, int | None]:
    """WP5 contract form of ``anchor_finding``: ``(kind, line, start_line)``.

    ``line`` is the GitHub comment line (the end of a range) and ``start_line`` the
    start of a range, or ``None`` for a single line.
    """
    a = anchor_finding(draft, anchors, head_lines, pr_paths, renamed_from)
    return a.kind, a.github_line, a.start_line
