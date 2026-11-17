"""Map of diff lines that GitHub accepts as review comment anchors.

Only files with a GitHub patch get anchors (a local diff can disagree with what
GitHub accepts). RIGHT-side lines are the added and context lines inside hunks.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping

from morning_star_review.core.contract import AnchorFile, AnchorMap, PrFile
from morning_star_review.diff.patch import FilePatch

__all__ = ["build_anchor_map", "hunk_of", "is_anchorable", "same_hunk"]


def _anchor_file(fp: FilePatch) -> AnchorFile | None:
    right = sorted({n for h in fp.hunks for _, n, _ in h.lines if n is not None})
    hunks = [
        (h.new_start, h.new_start + h.new_len - 1)
        for h in fp.hunks
        if h.new_len > 0 and any(n is not None for _, n, _ in h.lines)
    ]
    if not right:
        return None
    return AnchorFile(path=fp.path, right_lines=right, hunks=hunks)


def build_anchor_map(files: list[PrFile], github_patches: Mapping[str, FilePatch]) -> AnchorMap:
    """Anchors for every PR file that has a GitHub patch with at least one RIGHT line."""
    out: dict[str, AnchorFile] = {}
    renamed: dict[str, str] = {}
    for f in files:
        if f.previous_path and f.status == "renamed":
            renamed[f.previous_path] = f.path
        fp = github_patches.get(f.path)
        if not f.has_patch or fp is None:
            continue
        af = _anchor_file(fp)
        if af is not None:
            out[f.path] = af
    return AnchorMap(files=out, renamed_from=renamed)


def is_anchorable(am: AnchorMap, path: str, line: int) -> bool:
    """True when ``line`` is a RIGHT-side line of ``path`` in GitHub's patch."""
    af = am.files.get(path)
    if af is None:
        return False
    i = bisect_left(af.right_lines, line)
    return i < len(af.right_lines) and af.right_lines[i] == line


def hunk_of(am: AnchorMap, path: str, line: int) -> tuple[int, int] | None:
    """The inclusive RIGHT-side range of the hunk containing ``line``, if any."""
    af = am.files.get(path)
    if af is None:
        return None
    for start, end in af.hunks:
        if start <= line <= end:
            return (start, end)
    return None


def same_hunk(am: AnchorMap, path: str, a: int, b: int) -> bool:
    """True when lines ``a`` and ``b`` of ``path`` are inside the same hunk."""
    h = hunk_of(am, path, a)
    return h is not None and h[0] <= b <= h[1]
