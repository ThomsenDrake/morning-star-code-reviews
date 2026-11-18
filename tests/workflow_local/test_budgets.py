"""Tool-call budgets that reach the agent jobs in a full workflow run (docs/models.md).

The stub agent logs ``job.budgets.max_tool_calls`` of every job it runs, so these runs
check what the explore and verify activities really hand to the agents.
"""

from __future__ import annotations

import pytest
from harness import Harness
from wfkit import SQLI, notes_for

from morning_star_review.cli import build_policy
from morning_star_review.core.contract import ReviewRequest, RunOverrides

pytestmark = pytest.mark.timeout(120)

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
FLASH = "zai-glm-5-3-flash"
OPENED = ["app/db.py", "app/service.py"]


def _request(h: Harness, **overrides: object) -> ReviewRequest:
    ov = RunOverrides.model_validate(overrides)
    # The CLI's policy: an explicit budget above a default cap raises that cap.
    return h.request(overrides=ov, policy=build_policy(trust_repo_config=True, overrides=ov))


async def test_best_with_explicit_100_gives_every_explorer_100(harness: Harness) -> None:
    h = harness
    for model in (GLM, LARGE, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed"})
    out = await h.run(_request(h, preset="best", max_tool_calls=100))
    assert out.status == "posted", out.reason
    explores = h.calls("explore")
    assert sorted(c["model"] for c in explores) == sorted([GLM, LARGE, DEEPSEEK])
    assert {c["max_tool_calls"] for c in explores} == {100}
    (ver,) = h.calls("verify")
    assert ver["max_tool_calls"] == 30  # the verifier's budget was not set


@pytest.mark.parametrize(("budget", "expected"), [(60, 60), (None, 25)])
async def test_fast_flash_explorer_budget(
    harness: Harness, budget: int | None, expected: int
) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    out = await h.run(_request(h, preset="fast", max_tool_calls=budget))
    assert out.status == "posted", out.reason
    (call,) = h.calls("explore")
    assert call["model"] == FLASH
    assert call["max_tool_calls"] == expected


@pytest.mark.parametrize(("budget", "expected"), [(30, 30), (None, 25)])
async def test_flash_verifier_budget(harness: Harness, budget: int | None, expected: int) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed"})
    req = _request(h, preset="balanced", verifier_model=FLASH, verifier_max_tool_calls=budget)
    out = await h.run(req)
    assert out.status == "posted", out.reason
    (explore,) = h.calls("explore")
    assert explore["max_tool_calls"] == 60
    (ver,) = h.calls("verify")
    assert ver["model"] == FLASH
    assert ver["max_tool_calls"] == expected
