"""Anchoring and snapping (DESIGN 8.1)."""

from __future__ import annotations

import pytest

from morning_star_review.review.anchoring import (
    anchor,
    anchor_finding,
    normalize_path,
    resolve_path,
)

PATH = "ledger/service.py"


@pytest.fixture
def ctx(make):
    anchors = make.anchors({PATH: [(10, 30)], "docs/notes.md": [(1, 3), (8, 9)]})
    return anchors, make.heads()[PATH], {PATH, "docs/notes.md", "bin/tool.bin"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ledger/service.py", "ledger/service.py"),
        ("./ledger/service.py", "ledger/service.py"),
        ("/ledger/service.py", "ledger/service.py"),
        ("`ledger/service.py`", "ledger/service.py"),
        ("'./ledger/service.py'", "ledger/service.py"),
        (" ledger\\service.py ", "ledger/service.py"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


def test_resolve_path_variants() -> None:
    prs = {"ledger/service.py", "ledger/money.py", "a/b.py"}
    renames = {"ledger/old_service.py": "ledger/service.py"}
    assert resolve_path("b/ledger/service.py", prs, renames) == "ledger/service.py"
    assert resolve_path("ledger/old_service.py", prs, renames) == "ledger/service.py"
    assert resolve_path("service.py", prs, renames) == "ledger/service.py"  # unique suffix
    assert resolve_path("a/b.py", prs, renames) == "a/b.py"  # exact wins over prefix strip
    assert resolve_path("nowhere.py", prs, renames) == "nowhere.py"


@pytest.mark.parametrize(
    ("line", "evidence", "expected"),
    [
        # Exact match on the given line.
        (14, "end = start + size + 1", ("inline", 14, None)),
        # Evidence is a large part of the line (ratio >= 0.6).
        (14, "end = start + size", ("inline", 14, None)),
        # Line off by two: snap to the matching line within +-5.
        (12, "end = start + size + 1", ("snapped", 14, None)),
        (19, "end = start + size + 1", ("snapped", 14, None)),
        # Far away but the evidence matches exactly one anchorable line.
        (29, "return prorate(days, amount, total_days)", ("snapped", 27, None)),
        # Paraphrased evidence on an anchorable line: keep the model's line.
        (18, "the owner check is inverted", ("inline", 18, None)),
        # Evidence missing: keep an anchorable line.
        (23, "", ("inline", 23, None)),
        # Line outside the hunk, evidence not found: file-level, line kept for the link.
        (38, "nothing like this", ("file", 38, None)),
        (None, "nothing like this", ("file", None, None)),
        # Evidence only outside the hunk (line 37): no snap there.
        (3, "return x * 2", ("file", 3, None)),
    ],
)
def test_anchor_table(ctx, make, line, evidence, expected) -> None:
    anchors, head, prs = ctx
    d = make.draft(line=line, evidence=evidence)
    assert anchor(d, anchors, head, prs, anchors.renamed_from) == expected


def test_nearest_snap_wins_and_earlier_on_ties(make) -> None:
    head = ["x = 1"] * 30
    anchors = make.anchors({"m.py": [(1, 30)]})
    d = make.draft(path="m.py", line=31 + 0, evidence="x = 1")  # line 31 is not anchorable
    kind, line, _ = anchor(d, anchors, head, {"m.py"}, {})
    assert (kind, line) == ("snapped", 30)
    head2 = ["a"] * 30
    head2[9] = head2[13] = "y = 2"  # lines 10 and 14, both 2 away from 12
    d2 = make.draft(path="m.py", line=12, evidence="y = 2")
    assert anchor(d2, anchors, head2, {"m.py"}, {})[:2] == ("snapped", 10)


def test_ambiguous_evidence_is_not_snapped_far_away(make) -> None:
    head = ["x = 1"] * 40
    anchors = make.anchors({"m.py": [(1, 10), (30, 40)]})
    d = make.draft(path="m.py", line=20, evidence="x = 1")
    assert anchor(d, anchors, head, {"m.py"}, {}) == ("file", 20, None)


def test_outside_and_unpatched_files(ctx, make) -> None:
    anchors, head, prs = ctx
    out = anchor(make.draft(path="other/file.py"), anchors, head, prs, {})
    assert out == ("outside", None, None)
    binary = anchor(make.draft(path="bin/tool.bin", line=4), anchors, None, prs, {})
    assert binary == ("file", 4, None)


def test_renamed_old_path_maps_to_new(make) -> None:
    anchors = make.anchors({PATH: [(10, 30)]}, renamed_from={"ledger/svc.py": PATH})
    d = make.draft(path="ledger/svc.py")
    a = anchor_finding(d, anchors, make.heads()[PATH], {PATH})
    assert (a.path, a.kind, a.line) == (PATH, "inline", 14)


@pytest.mark.parametrize(
    ("line", "end", "hunks", "expected", "rejected"),
    [
        (13, 15, [(10, 30)], ("inline", 15, 13), False),
        (14, 14, [(10, 30)], ("inline", 14, None), False),  # start == end: one line
        (13, 12, [(10, 30)], ("inline", 13, None), True),  # inverted range
        (10, 31, [(10, 40)], ("inline", 10, None), True),  # longer than 20
        (10, 30, [(10, 40)], ("inline", 30, 10), False),  # exactly 20 apart
        (13, 22, [(10, 15), (20, 30)], ("inline", 13, None), True),  # crosses hunks
        (13, 18, [(10, 15), (18, 30)], ("inline", 13, None), True),  # end in another hunk
    ],
)
def test_ranges(make, line, end, hunks, expected, rejected) -> None:
    anchors = make.anchors({PATH: hunks})
    d = make.draft(line=line, end_line=end, evidence="")
    a = anchor_finding(d, anchors, make.heads()[PATH], {PATH})
    assert (a.kind, a.github_line, a.start_line) == expected
    assert a.range_rejected is rejected


def test_snapped_range_moves_with_its_start(ctx, make) -> None:
    anchors, head, prs = ctx
    d = make.draft(line=12, end_line=13, evidence="end = start + size + 1")
    a = anchor_finding(d, anchors, head, prs)
    assert (a.kind, a.line, a.end_line, a.exact) == ("snapped", 14, 15, True)


def test_multiline_evidence_uses_its_first_line(ctx, make) -> None:
    anchors, head, prs = ctx
    ev = "if user.id != entry.owner_id:\n    return True"
    a = anchor_finding(make.draft(line=16, evidence=ev), anchors, head, prs)
    assert (a.kind, a.line) == ("snapped", 18)
