"""Fixtures for the GitHub client tests; builders live in ``builders.py``."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakegh import FakeGitHub

from morning_star_review.github import http as gh_http


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture(autouse=True)
def _reset_test_transport() -> Iterator[None]:
    yield
    gh_http.set_test_transport(None)
