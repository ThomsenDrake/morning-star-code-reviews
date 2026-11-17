"""The driver's GitHub skip pre-check: a skipped run boots no worker (G5).

``early_skip`` makes the same decision as ``fetch_pr`` (superseded, closed, draft,
already reviewed) from a few reads; the driver runs it before choosing a deployment,
so a skip starts no worker and no execution.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "github"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from builders import make_req, make_state, no_sleep  # noqa: E402
from fakegh import BOT, HEAD, FakeGitHub  # noqa: E402
from test_driver_lifecycle import (  # noqa: E402
    FAST,
    KEY,
    PR,
    FakePlatform,
    Worker,
    _run_root,  # noqa: F401 - autouse: the fake publish step writes outcome.json there
    make_cfg,
)

from morning_star_review import driver  # noqa: E402
from morning_star_review.activities.fetch_pr import EarlySkip, early_skip  # noqa: E402
from morning_star_review.github import http as gh_http  # noqa: E402
from morning_star_review.github import state as st  # noqa: E402
from morning_star_review.github.http import GitHubHttp  # noqa: E402
from morning_star_review.github.publish import SKIP_REASONS  # noqa: E402

TOKEN = "ghs_precheck0123456789abcdef"  # synthetic


@pytest.fixture
def fake() -> Iterator[FakeGitHub]:
    f = FakeGitHub()
    gh_http.set_test_transport(f.transport)
    try:
        yield f
    finally:
        gh_http.set_test_transport(None)


def _summary(fake: FakeGitHub, **state_kw: Any) -> None:
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\nx\n{st.encode_state(make_state(**state_kw))}")


async def _early(fake: FakeGitHub, bot: str | None = BOT, **req_kw: Any) -> EarlySkip | None:
    async with GitHubHttp(TOKEN, transport=fake.transport, sleep=no_sleep) as gh:
        return await early_skip(make_req(**req_kw), gh, bot)


# ----------------------------------------------------------------------------- early_skip


@pytest.mark.parametrize(
    ("change", "req_kw", "expected"),
    [
        ({}, {}, None),
        ({"state": "closed"}, {}, "closed"),
        ({"merged": True, "state": "closed"}, {}, "closed"),
        ({"draft": True}, {}, "draft"),
        ({}, {"expected_head_sha": "d" * 40}, "superseded"),
        ({"state": "closed"}, {"force": True}, None),
        ({"draft": True}, {"force": True}, None),
        ({}, {"expected_head_sha": "d" * 40, "force": True}, "superseded"),
        ({}, {"expected_head_sha": HEAD}, None),
    ],
)
async def test_same_decisions_as_fetch_pr(
    fake: FakeGitHub, change: dict[str, Any], req_kw: dict[str, Any], expected: str | None
) -> None:
    fake.pr.update(change)
    got = await _early(fake, **req_kw)
    assert (got.skip if got else None) == expected
    if got is not None:
        assert got.head_sha == HEAD
    assert fake.writes == []


async def test_closed_and_superseded_need_only_the_pull_request(fake: FakeGitHub) -> None:
    fake.pr.update({"state": "closed"})
    assert await _early(fake) == EarlySkip("closed", HEAD)
    assert [p for _m, p in fake.requests] == ["/repos/octo-org/widgets/pulls/7"]


async def test_already_reviewed_needs_the_same_config(fake: FakeGitHub) -> None:
    _summary(fake, verdict="clean")
    assert await _early(fake) == EarlySkip("already_reviewed", HEAD, "clean")
    assert await _early(fake, force=True) is None
    # Without our bot login, earlier comments cannot be recognised: no skip.
    assert await _early(fake, bot=None) is None
    fake.issue_comments.clear()
    _summary(fake, config_hash="0" * 64)
    assert await _early(fake) is None  # the configuration changed
    fake.issue_comments.clear()
    _summary(fake, complete=False)
    assert await _early(fake) is None  # the earlier review was incomplete
    fake.issue_comments.clear()
    _summary(fake, head_sha="e" * 40)
    assert await _early(fake) is None  # a new head


async def test_a_forged_summary_does_not_skip(fake: FakeGitHub) -> None:
    fake.add_issue_comment(
        f"{st.SUMMARY_MARKER}\n{st.encode_state(make_state())}", login="someone-else"
    )
    assert await _early(fake) is None


async def test_drafts_reviewed_when_the_config_says_so(fake: FakeGitHub) -> None:
    fake.pr.update({"draft": True})
    fake.config_text = "review:\n  drafts: true\n"
    assert await _early(fake) is None


# ----------------------------------------------------------------------------- the driver


def _hooks(platform: FakePlatform, worker: Worker, **kw: Any) -> driver.Hooks:
    return driver.Hooks(platform=platform, worker=worker, timing=FAST, install_signals=False, **kw)


def _secrets(**kw: Any) -> driver.DriverSecrets:
    return driver.DriverSecrets(mistral_api_key=KEY, github_token=TOKEN, bot_login=BOT, **kw)


@pytest.mark.parametrize(
    ("change", "cfg_kw", "status"),
    [
        ({"state": "closed"}, {}, "skipped"),
        ({"draft": True}, {}, "skipped"),
        ({}, {"expected_head_sha": "d" * 40}, "superseded"),
    ],
)
async def test_skip_starts_no_worker_and_no_execution(
    fake: FakeGitHub,
    tmp_path: Path,
    change: dict[str, Any],
    cfg_kw: dict[str, Any],
    status: str,
) -> None:
    fake.pr.update(change)
    p = FakePlatform()
    w = Worker(p)
    outcome, code = await driver.run_review(make_cfg(tmp_path, **cfg_kw), _secrets(), _hooks(p, w))
    assert code == driver.EXIT_OK
    assert outcome.status == status
    assert outcome.reason in SKIP_REASONS.values()
    assert outcome.head_sha == HEAD and outcome.pr_url == PR
    assert w.starts == 0 and p.started == [] and p.terminated == []
    assert [c[0] for c in p.calls] == ["whoami"]  # no deployment read, no start
    assert fake.writes == []


async def test_already_reviewed_head_starts_no_worker(fake: FakeGitHub, tmp_path: Path) -> None:
    _summary(fake, verdict="findings")
    p = FakePlatform()
    w = Worker(p)
    outcome, code = await driver.run_review(make_cfg(tmp_path), _secrets(), _hooks(p, w))
    assert outcome.status == "skipped" and code == driver.EXIT_OK
    assert outcome.reason == SKIP_REASONS["already_reviewed"]
    assert outcome.verdict == "findings"
    assert outcome.usage == {} and outcome.cost_usd == 0
    assert w.starts == 0 and p.started == []


async def test_an_open_pull_request_is_reviewed(fake: FakeGitHub, tmp_path: Path) -> None:
    p = FakePlatform()
    w = Worker(p)
    outcome, _code = await driver.run_review(make_cfg(tmp_path), _secrets(), _hooks(p, w))
    assert outcome.status == "posted"
    assert w.starts == 1 and len(p.started) == 1


async def test_a_failing_precheck_lets_the_run_go_on(tmp_path: Path) -> None:
    async def broken(req: Any, secrets: Any) -> None:
        raise RuntimeError(f"boom with {TOKEN}")

    p = FakePlatform()
    w = Worker(p)
    outcome, _code = await driver.run_review(
        make_cfg(tmp_path), _secrets(), _hooks(p, w, precheck=broken)
    )
    assert outcome.status == "posted"
    assert len(p.started) == 1


async def test_a_slow_precheck_is_abandoned(tmp_path: Path) -> None:
    import asyncio

    async def slow(req: Any, secrets: Any) -> None:
        await asyncio.sleep(30)

    timing = driver.Timing(**{**FAST.__dict__, "precheck_timeout_s": 0.05})
    p = FakePlatform()
    w = Worker(p)
    hooks = driver.Hooks(platform=p, worker=w, timing=timing, install_signals=False, precheck=slow)
    outcome, _code = await driver.run_review(make_cfg(tmp_path), _secrets(), hooks)
    assert outcome.status == "posted"


async def test_local_mode_has_no_precheck(tmp_path: Path) -> None:
    calls: list[str] = []

    async def precheck(req: Any, secrets: Any) -> EarlySkip:
        calls.append("precheck")
        return EarlySkip("closed", HEAD)

    async def runner(req: Any) -> Any:
        from test_driver_lifecycle import outcome_dict

        return outcome_dict("x")

    hooks = driver.Hooks(timing=FAST, install_signals=False, local_runner=runner, precheck=precheck)
    cfg = make_cfg(tmp_path, mode="local")
    outcome, _code = await driver.run_review(cfg, _secrets(), hooks)
    assert calls == [] and outcome.status == "posted"
