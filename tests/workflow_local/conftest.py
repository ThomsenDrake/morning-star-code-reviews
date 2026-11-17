"""Fixtures for in-process workflow runs (DESIGN 15.4): no server, no key, no network."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "github"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fakegh import FakeGitHub  # noqa: E402
from harness import Harness  # noqa: E402
from mistralai.workflows.core.config.config import config as wf_config  # noqa: E402
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
    # Local runs retry failed activities client-side; keep the waits short in tests.
    monkeypatch.setattr(wf_config.worker, "local_retry_backoff_coefficient", 0.01)
    try:
        yield Harness.create(tmp_path, repo, fake)
    finally:
        gh_http.set_test_transport(None)
        prepare_mod.set_test_clone_url(None)
        clear_run_context()
