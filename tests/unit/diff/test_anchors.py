"""Anchor map: RIGHT-side lines GitHub accepts for review comments."""

from __future__ import annotations

from morning_star_review.core.contract import PrFile
from morning_star_review.diff.anchors import build_anchor_map, hunk_of, is_anchorable, same_hunk
from morning_star_review.diff.patch import parse_patch


def test_e1_anchor_map(h) -> None:
    files = h.e1_files()
    am = build_anchor_map(files, h.e1_patches())
    svc = am.files["ledger/service.py"]
    assert svc.hunks == [(7, 14), (16, 25)]
    assert 21 in svc.right_lines and 15 not in svc.right_lines
    assert is_anchorable(am, "ledger/service.py", 21)  # the swapped prorate call
    assert is_anchorable(am, "ledger/service.py", 7)  # context lines count
    assert not is_anchorable(am, "ledger/service.py", 15)  # between hunks
    assert not is_anchorable(am, "ledger/money.py", 13)  # file not in the PR
    # Binary files and deletion-only files have no RIGHT lines.
    assert "assets/logo.png" not in am.files
    assert "ledger/legacy.py" not in am.files
    assert am.renamed_from == {"ledger/old_util.py": "ledger/util.py"}


def test_same_hunk_and_hunk_of(h) -> None:
    am = build_anchor_map(h.e1_files(), h.e1_patches())
    assert same_hunk(am, "ledger/service.py", 19, 25)
    assert not same_hunk(am, "ledger/service.py", 10, 19)
    assert not same_hunk(am, "ledger/service.py", 15, 16)
    assert hunk_of(am, "ledger/service.py", 20) == (16, 25)
    assert hunk_of(am, "nope.py", 1) is None


def test_files_without_github_patch_get_no_anchors() -> None:
    f = PrFile(path="big.py", status="modified", additions=5, deletions=0, has_patch=False)
    local = {"big.py": parse_patch("big.py", "@@ -1,0 +1,1 @@\n+x", "modified", None)}
    am = build_anchor_map([f], local)
    assert am.files == {}
    assert not is_anchorable(am, "big.py", 1)


def test_removed_lines_are_not_anchors() -> None:
    patch = "@@ -1,3 +1,2 @@\n a\n-b\n c"
    f = PrFile(path="x.py", status="modified", additions=0, deletions=1, has_patch=True)
    am = build_anchor_map([f], {"x.py": parse_patch("x.py", patch, "modified", None)})
    assert am.files["x.py"].right_lines == [1, 2]
    assert am.files["x.py"].hunks == [(1, 2)]
