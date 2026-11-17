"""Fixtures for the opt-in platform smoke test (DESIGN 15.5; MSREVIEW_PLATFORM_TESTS=1).

The worker runs in this process, so the activities use the fake GitHub transport, a
local file:// repository and the stub agent from tests/workflow_local.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent.parent
for _p in (_TESTS / "workflow_local", _TESTS / "github"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fakegh import FakeGitHub  # noqa: E402
from harness import Harness  # noqa: E402
from wfkit import Repo, point_fake_at  # noqa: E402

from morning_star_review.activities import explore as explore_mod  # noqa: E402
from morning_star_review.activities import prepare as prepare_mod  # noqa: E402
from morning_star_review.core.runctx import clear_run_context  # noqa: E402
from morning_star_review.github import http as gh_http  # noqa: E402


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    repo = Repo.create(tmp_path)
    fake = FakeGitHub()
    point_fake_at(fake, repo)
    gh_http.set_test_transport(fake.transport)
    prepare_mod.set_test_clone_url(lambda snap: repo.path.as_uri())
    monkeypatch.setattr(explore_mod, "STAGGER_SECONDS", 0.0)
    try:
        yield Harness.create(tmp_path, repo, fake)
    finally:
        gh_http.set_test_transport(None)
        prepare_mod.set_test_clone_url(None)
        clear_run_context()
