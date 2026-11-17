"""Finding ids, normalisation and evidence matching."""

from __future__ import annotations

import hashlib
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from morning_star_review.core import ids


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  x  =   1 ", "x = 1"),
        ("\tif a:\n", "if a:"),
        ("`total += price`", "total += price"),
        ("```py```", "py"),
        ('"quoted"', "quoted"),
        ("'a'", "a"),
        (" ` ' mixed ' ` ", "mixed"),
        ('x = "abc"', 'x = "abc'),  # both sides are normalised the same way
        ("", ""),
        ("   ", ""),
        ("a\u00a0b", "a b"),  # non-breaking space is whitespace
    ],
)
def test_norm(raw: str, expected: str) -> None:
    assert ids.norm(raw) == expected


def test_norm_nfc() -> None:
    assert ids.norm("cafe\u0301") == ids.norm("caf\u00e9")


@given(st.text())
def test_norm_is_idempotent(text: str) -> None:
    once = ids.norm(text)
    assert ids.norm(once) == once


def test_finding_id_format_and_formula() -> None:
    fid = ids.finding_id("billing/invoice.py", "    amount = split(days, total)")
    assert re.fullmatch(r"f[0-9a-f]{12}", fid)
    payload = b"billing/invoice.py\namount = split(days, total)"
    expected = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
    assert fid == "f" + expected[:12]


def test_finding_id_ignores_whitespace_and_wrapping() -> None:
    a = ids.finding_id("a.py", "x  =  1")
    assert a == ids.finding_id("a.py", "`x = 1`")
    assert a == ids.finding_id("a.py", "\tx = 1\n")
    assert a != ids.finding_id("b.py", "x = 1")
    assert a != ids.finding_id("a.py", "x = 2")


@pytest.mark.parametrize(
    ("a", "b", "match"),
    [
        ("x = 1", "x = 1", True),
        ("`x  = 1`", "x = 1", True),
        ("total = a + b", "total = a + b  # sum", True),  # 13/19 >= 0.6
        ("a + b", "total = a + b  # running sum", False),  # too short a part
        ("x = 1", "y = 2", False),
        ("", "x", False),
        ("   ", "   ", False),
    ],
)
def test_evidence_match(a: str, b: str, match: bool) -> None:
    assert ids.evidence_match(a, b) is match
    assert ids.evidence_match(b, a) is match


def test_evidence_match_ratio_parameter() -> None:
    assert ids.evidence_match("a + b", "total = a + b", min_ratio=0.3)
    assert not ids.evidence_match("a + b", "total = a + b", min_ratio=0.6)


def test_candidate_id() -> None:
    assert [ids.candidate_id(i) for i in range(3)] == ["c1", "c2", "c3"]
    with pytest.raises(ValueError):
        ids.candidate_id(-1)
