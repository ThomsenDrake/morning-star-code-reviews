"""Scripted-model tests: turns that fail with a temporary model error are retried.

The runner retries a turn that ended ``Failed`` with a retryable code (HTTP 429 and the
like) in the same session, after a backoff, while the phase deadline, the cost cap and
the model-call limit allow. Time is simulated: the test clock drives the meter and the
patched sleep advances it.
"""

from __future__ import annotations

from typing import Any

import pytest
from mistralai.agents import testing

from morning_star_review.agent import hooks, runner
from morning_star_review.core.contract import FormatterDraft

from .conftest import NOTES_ONE, JobKit, budgets, candidate

VERDICTS = """VERDICT c1
status: confirmed
reason: prorate(amount, total, used) passes the totals in the wrong order.
END VERDICT
VERIFY COMPLETE
"""


def _failure(code: str) -> Any:
    from mistralai.vibe.harness.runtime.completion.models import (
        CompletionError,
        CompletionFailure,
    )

    if code == "429":
        return CompletionError(
            CompletionFailure(
                kind="provider",
                retryable=True,
                status_code=429,
                message="Rate limit exceeded",
                details={"reason": "rate_limited", "status_code": 429},
            )
        )
    if code == "400":
        return CompletionError(
            CompletionFailure(
                kind="provider",
                retryable=False,
                status_code=400,
                message="Invalid model",
                details={"reason": "invalid_request_error", "status_code": 400},
            )
        )
    raise AssertionError(code)


def flaky_model(steps: list[Any]) -> Any:
    """A ScriptedModel whose steps may also be ``"429"`` or ``"400"`` (a failed call).

    ``requests`` keeps the last user message of every model call.
    """

    class FlakyModel(testing.ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])
            self.steps = list(steps)
            self.used = 0
            self.requests: list[str] = []

        def __deepcopy__(self, memo: dict[int, object]) -> FlakyModel:
            memo[id(self)] = self
            return self

        async def complete(self, request: Any) -> Any:
            assert self.used < len(self.steps), f"model call {self.used + 1} has no step"
            step = self.steps[self.used]
            self.used += 1
            users = [m for m in request.messages if getattr(m, "role", "") == "user"]
            self.requests.append(str(users[-1].content) if users else "")
            if isinstance(step, str):
                raise _failure(step)
            inner = testing.ScriptedModel([step])
            async for item in inner.complete(request):
                yield item

        def assert_consumed(self) -> None:
            assert self.used == len(self.steps), f"used {self.used} of {len(self.steps)} steps"

    return FlakyModel()


class FakeTime:
    """A clock for the meter and a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    ft = FakeTime()
    monkeypatch.setattr(runner, "_sleep", ft.sleep)
    return ft


async def test_one_429_then_success_completes(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore")
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            "429",
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.complete is True
    assert result.error is None
    assert len(fake_time.sleeps) == 1
    assert 15.0 <= fake_time.sleeps[0] <= 15.0 * (1 + runner.RETRY_JITTER)
    # The retry continues the same session: the tool result is still in context.
    assert model.requests[-1] == runner.RETRY_MESSAGE
    assert result.opened_paths == ["shop/money.py"]
    # The refused call was not billed: no estimate is charged for it.
    meter = hooks.MeterState.model_validate_json((job_dir / "meter.json").read_bytes())
    assert meter.estimated_calls == 0
    assert result.usage.llm_calls == 3


async def test_repeated_429_until_the_deadline_fails_with_a_reason(
    kit: JobKit, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "RETRY_JITTER", 0.0)
    # Deadline + grace = 70 s; a retried turn needs 30 s of it. The first retry (after
    # 15 s) fits, the second (at 15 + 30 s) does not.
    job_dir = kit.make("explore", explore_seconds=40, grace_seconds=30)
    model = flaky_model(["429", "429"])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert fake_time.sleeps == [15.0]
    assert result.state == "Failed"
    assert result.complete is False
    assert result.error is not None
    assert result.error.startswith("completion_rate_limited: Rate limit exceeded")
    assert "gave up after 1 retry" in result.error
    assert "phase deadline" in result.error


async def test_retries_stop_after_three(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", explore_seconds=900)
    model = flaky_model(["429"] * 4)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert len(fake_time.sleeps) == 3
    for slept, base in zip(fake_time.sleeps, runner.RETRY_DELAYS, strict=True):
        assert base <= slept <= base * (1 + runner.RETRY_JITTER)
    assert result.state == "Failed"
    assert "gave up after 3 retries: the retry limit is reached" in (result.error or "")


async def test_no_time_for_a_first_retry(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", explore_seconds=20, grace_seconds=5)
    model = flaky_model(["429"])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    assert fake_time.sleeps == []
    assert result.state == "Failed"
    assert "(not retried: the phase deadline" in (result.error or "")


async def test_non_retryable_failure_is_not_retried(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore")
    model = flaky_model(["400"])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert fake_time.sleeps == []
    assert result.state == "Failed"
    assert result.error == "completion_rejected: Invalid model"


async def test_verifier_429_then_success(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1")])
    model = flaky_model(
        ["429", testing.call("read_repo_file", path="shop/money.py"), testing.reply(VERDICTS)]
    )
    result = await runner.run_job("verify", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert len(fake_time.sleeps) == 1
    assert result.state == "Completed"
    assert result.complete is True


async def test_rescue_turn_is_retried_too(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", max_llm_calls=1)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),  # the backstop ends turn 1
            "429",  # the rescue's first call
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert len(fake_time.sleeps) == 1
    assert result.rescued is True
    assert "FINDING" in (result.notes or "")


async def test_formatter_429_then_success(kit: JobKit, fake_time: FakeTime) -> None:
    draft = FormatterDraft(findings=[], prior_status=[], summary="Nothing found.")
    job_dir = kit.make("format", text="notes")
    model = flaky_model(["429", testing.reply(draft.model_dump_json())])
    result = await runner.run_job("format", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert len(fake_time.sleeps) == 1
    assert result.state == "Completed"
    assert FormatterDraft.model_validate(result.draft) == draft


async def test_formatter_gives_up_within_its_own_time_limit(
    kit: JobKit, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "RETRY_JITTER", 0.0)
    monkeypatch.setattr(runner, "FORMATTER_TIMEOUT", 60.0)  # 15 + 30 fits, 30 more does not
    job_dir = kit.make("format", text="notes")
    model = flaky_model(["429", "429"])
    result = await runner.run_job("format", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert fake_time.sleeps == [15.0]
    assert result.state == "Failed"
    assert result.draft is None
    assert "gave up after 1 retry: the phase deadline" in (result.error or "")


def _meter(**state: Any) -> hooks.Meter:
    values: dict[str, Any] = {
        "phase": "explore",
        "model": "mistral-large-4",
        "started_epoch": 0.0,
        "deadline_epoch": 300.0,
        "llm_call_limit": 10,
    }
    values.update(state)
    return hooks.Meter(
        hooks.MeterState(**values),
        budgets(max_cost_usd=1.0, grace_seconds=60),
        price_in_per_m=0.70,
        price_out_per_m=2.10,
        clock=lambda: 0.0,
    )


def test_retry_blockers() -> None:
    assert runner._retry_blocker(_meter(), 0, 15.0) is None
    assert runner._retry_blocker(_meter(), 3, 0.0) == "the retry limit is reached"
    late = runner._retry_blocker(_meter(), 0, 340.0)
    assert late is not None and "deadline" in late
    rescue = runner._retry_blocker(_meter(rescue_deadline_epoch=40.0), 0, 15.0)
    assert rescue is not None and "deadline" in rescue
    calls = runner._retry_blocker(_meter(llm_calls=10), 0, 15.0)
    assert calls == "the model-call limit is reached"
    cost = runner._retry_blocker(_meter(cost_usd=0.99), 0, 15.0)
    assert cost is not None and "cost cap" in cost


def test_only_temporary_codes_are_retried() -> None:
    def failed(code: str, retryable: bool = True) -> runner._Turn:
        return runner._Turn("Failed", "failed", "", "x", retryable=retryable, code=code)

    assert runner._should_retry(failed("completion_rate_limited"))
    assert runner._should_retry(failed("completion_unavailable"))
    assert not runner._should_retry(failed("completion_unavailable", retryable=False))
    assert not runner._should_retry(failed("completion_context_too_large"))
    assert not runner._should_retry(runner._Turn("Completed", None, "notes"))
