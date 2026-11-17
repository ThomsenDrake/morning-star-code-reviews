"""The eval helpers live under scripts/ (they are not part of the package)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from msreview_eval.build import head_tree  # noqa: E402
from msreview_eval.cases import CaseKey, load_cases  # noqa: E402


@pytest.fixture(scope="session")
def cases() -> dict[str, CaseKey]:
    return load_cases()


@pytest.fixture(scope="session")
def heads(cases: dict[str, CaseKey]) -> dict[str, dict[str, list[str]]]:
    """Head file lines of every case, built once per session."""
    return {
        cid: {path: text.splitlines() for path, text in head_tree(case).items()}
        for cid, case in cases.items()
    }
