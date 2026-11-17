"""Stable finding ids and text normalisation."""

from __future__ import annotations

import hashlib
import unicodedata

__all__ = ["candidate_id", "evidence_match", "finding_id", "norm"]

# Characters models wrap code in; stripped from both ends (with whitespace).
_WRAP_CHARS = " `'\""


def norm(text: str) -> str:
    """Normalise a code line or evidence string for comparison.

    NFC-normalise, collapse every whitespace run to one space, then strip
    whitespace, backticks and quotes from both ends. Idempotent.
    """
    collapsed = " ".join(unicodedata.normalize("NFC", text).split())
    return collapsed.strip(_WRAP_CHARS)


def finding_id(path: str, anchor_text: str) -> str:
    """``"f"`` + 12 hex chars of sha1(path + "\\n" + norm(anchor_text)).

    ``anchor_text`` is the head text of the anchored line (inline/snapped), else the
    evidence, else the title. Titles and categories are deliberately not part of the
    id, so the same flagged code keeps its id across runs.
    """
    digest = hashlib.sha1(
        f"{path}\n{norm(anchor_text)}".encode(),
        usedforsecurity=False,
    ).hexdigest()
    return "f" + digest[:12]


def evidence_match(a: str, b: str, min_ratio: float = 0.6) -> bool:
    """True when the normalised texts are equal, or one contains the other and the
    shorter is at least ``min_ratio`` of the longer's length. Empty never matches."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return short in long_ and len(short) / len(long_) >= min_ratio


def candidate_id(index: int) -> str:
    """Short id for the ``index``-th merged candidate (0-based): ``c1``, ``c2``, ..."""
    if index < 0:
        raise ValueError("index must not be negative")
    return f"c{index + 1}"
