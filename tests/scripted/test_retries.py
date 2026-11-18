"""Scripted-model tests: turns that fail with a temporary model error are retried.

The runner retries a turn that ended ``Failed`` with a retryable code (HTTP 429 and the
like) in the same session, after a backoff that pauses the phase clocks, while the
run's limits, the cost cap and the model-call limit allow. Time is simulated: the test
clock drives the meter and the patched sleeps advance it. The long rate-limit
scenarios are in ``test_ratelimit.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from mistralai import agents
from mistralai.agents import testing

from morning_star_review.agent import hooks, ratelimit, runner
from morning_star_review.core.contract import FormatterDraft, RunLimits
from morning_star_review.core.runclock import RUN_DEADLINE_STOP

from .conftest import NOTES_ONE, FakeTime, JobKit, budgets, candidate, flaky_model

VERDICTS = """VERDICT c1
status: confirmed
reason: prorate(amount, total, used) passes the totals in the wrong order.
END VERDICT
VERIFY COMPLETE
"""


def _jitter_range(base: float) -> tuple[float, float]:
    return base, base * (1 + ratelimit.JITTER)


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
    low, high = _jitter_range(15.0)
    assert low <= fake_time.sleeps[0] <= high
    # The retry continues the same session: the tool result is still in context.
    assert model.requests[-1] == runner.RETRY_MESSAGE
    assert result.opened_paths == ["shop/money.py"]
    # The refused call was neither billed nor counted as a model call.
    meter = hooks.MeterState.model_validate_json((job_dir / "meter.json").read_bytes())
    assert meter.estimated_calls == 0
    assert meter.refused_calls == 1
    assert result.usage.llm_calls == 2
    assert result.usage.rate_limit_waits == 1
    assert result.usage.rate_limit_wait_seconds == pytest.approx(fake_time.sleeps[0], abs=1e-3)


async def test_waits_do_not_use_up_the_phase_time(kit: JobKit, fake_time: FakeTime) -> None:
    # Deadline + grace = 70 s, but three waits (15 + 30 + 60 s and more) pause the clock.
    job_dir = kit.make("explore", explore_seconds=40, grace_seconds=30)
    model = flaky_model(["429", "429", "429", testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert len(fake_time.sleeps) == 3
    meter = hooks.MeterState.model_validate_json((job_dir / "meter.json").read_bytes())
    assert meter.deadline_epoch == pytest.approx(1_000_000.0 + 40 + sum(fake_time.sleeps))


async def test_retries_have_no_count_limit(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", explore_seconds=900)
    model = flaky_model(["429"] * 5 + [testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert len(fake_time.sleeps) == 5
    for slept, base in zip(fake_time.sleeps, (15.0, 30.0, 60.0, 120.0, 180.0), strict=True):
        low, high = _jitter_range(base)
        assert low <= slept <= high


async def test_no_time_for_a_first_retry(kit: JobKit, fake_time: FakeTime) -> None:
    # The phase's own window (deadline + grace = 25 s) is shorter than a retried turn needs.
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


async def test_unavailable_and_lost_streams_are_retried(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore")
    model = flaky_model(["503", "capacity", "lost", testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert len(fake_time.sleeps) == 3
    meter = hooks.MeterState.model_validate_json((job_dir / "meter.json").read_bytes())
    assert meter.rl_last_code == "completion_failed"
    assert meter.rl_streak == 0  # the reply reset it
    # Two refusals were forgotten; the lost stream counts as a call.
    assert meter.refused_calls == 2
    assert result.usage.llm_calls == 2


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


async def test_verifier_stopped_by_the_run_limit_says_so(kit: JobKit, fake_time: FakeTime) -> None:
    now = fake_time.now
    limits = RunLimits(wind_down_epoch=now + 40, stop_epoch=now + 40)
    job_dir = kit.make("verify", candidates=[candidate("c1")], run_limits=limits)
    model = flaky_model(["429", "429"])
    result = await runner.run_job("verify", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    # Only a 5 s wait fits before the stop (with 30 s for the turn and the last wait's
    # 5 s margin): one last wait.
    assert fake_time.sleeps == [pytest.approx(40.0 - 30.0 - runner.FINAL_WAIT_MARGIN_S)]
    assert result.state == "Failed" and result.complete is False
    assert result.stop_reason == RUN_DEADLINE_STOP
    assert "gave up after 1 retry: the run's time limit is reached" in (result.error or "")


class _SlowSession:
    """A session whose turn runs until it is interrupted (the clock moves per poll)."""

    def __init__(self, fake_time: FakeTime) -> None:
        self.fake_time = fake_time
        self.interrupted = False

    def _running(self) -> Any:
        return agents.Running(session_id="s", turn_id="t", cursor="c", text=None)

    async def run(self, content: str, timeout: float) -> Any:
        self.fake_time.now += timeout
        return self._running()

    def turn(self, turn_id: str) -> _SlowSession:
        return self

    async def result(self, timeout: float, after: Any = None) -> Any:
        if self.interrupted:
            return agents.Interrupted(
                session_id="s", turn_id="t", cursor="c", text=None, cause="user"
            )
        self.fake_time.now += timeout
        return self._running()

    async def interrupt(self) -> None:
        self.interrupted = True


@pytest.mark.parametrize(("stop_after", "reason"), [(100.0, RUN_DEADLINE_STOP), (None, "timeout")])
async def test_a_turn_interrupted_at_the_run_stop_says_so(
    fake_time: FakeTime, stop_after: float | None, reason: str
) -> None:
    now = fake_time.now
    limits = None
    if stop_after is not None:
        limits = RunLimits(wind_down_epoch=now + stop_after, stop_epoch=now + stop_after)
    m = _meter(limits, started_epoch=now, deadline_epoch=now + 100.0)
    m.clock = fake_time.clock
    turn = await runner._run_turn_retrying(_SlowSession(fake_time), "go", m, m.call_end)
    assert turn.kind == "Interrupted"
    assert turn.stop_reason == reason
    assert runner._stop_reason_extras(turn) == ["deadline"]


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


async def test_formatter_window_grows_with_the_waits(
    kit: JobKit, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "FORMATTER_TIMEOUT", 60.0)  # 15 + 30 + 60 s of waits
    draft = FormatterDraft(findings=[], prior_status=[], summary="Nothing found.")
    job_dir = kit.make("format", text="notes")
    model = flaky_model(["429", "429", "429", testing.reply(draft.model_dump_json())])
    result = await runner.run_job("format", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert len(fake_time.sleeps) == 3
    assert result.state == "Completed"


async def test_formatter_gives_up_at_the_run_stop(kit: JobKit, fake_time: FakeTime) -> None:
    now = fake_time.now
    limits = RunLimits(wind_down_epoch=now + 100, stop_epoch=now + 100)
    job_dir = kit.make("format", text="notes", run_limits=limits)
    model = flaky_model(["429", "429", "429", "429"])
    result = await runner.run_job("format", job_dir, model=model, clock=fake_time.clock)
    # Waits of about 15 and 30 s fit before the stop (with 30 s for the turn); 60 do not,
    # so one last, shorter wait ends 30 s (plus its 5 s margin) before the stop. Its
    # retry fails too.
    assert model.used == 4
    assert len(fake_time.sleeps) == 3
    gap = runner.RETRY_MIN_TURN_SECONDS + runner.FINAL_WAIT_MARGIN_S
    assert fake_time.now == pytest.approx(now + 100 - gap)
    assert result.state == "Failed"
    assert result.stop_reason == RUN_DEADLINE_STOP
    assert result.draft is None
    assert "gave up after 3 retries: the run's time limit is reached" in (result.error or "")


def _meter(limits: RunLimits | None = None, **state: Any) -> hooks.Meter:
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
        limits=limits,
    )


RL = "completion_rate_limited"
LOST = "completion_failed"


def test_retry_blockers() -> None:
    assert runner._retry_blocker(_meter(), RL, 15.0) is None
    # Waits pause the phase clock: a long wait alone does not block.
    assert runner._retry_blocker(_meter(), RL, 900.0) is None
    late = runner._retry_blocker(_meter(deadline_epoch=-50.0), RL, 15.0)  # ends at 10
    assert late == "the phase deadline leaves no time for another attempt"
    rescue = runner._retry_blocker(_meter(rescue_deadline_epoch=20.0), RL, 15.0)
    assert rescue is not None and "deadline" in rescue
    stop = RunLimits(wind_down_epoch=50.0, stop_epoch=100.0)
    assert runner._retry_blocker(_meter(stop), RL, 15.0) is None
    assert runner._retry_blocker(_meter(stop), RL, 80.0) == "the run's time limit is reached"
    waited = runner._retry_blocker(_meter(rl_wait_seconds=3590.0), RL, 15.0)
    assert waited == "the rate-limit waits reached their 60-minute limit"
    assert runner._retry_blocker(_meter(stop, rl_wait_seconds=3590.0), RL, 15.0) is None
    assert runner._retry_blocker(_meter(transient_streak=6), LOST, 15.0) == "6 failures in a row"
    assert runner._retry_blocker(_meter(transient_streak=5), LOST, 15.0) is None
    assert runner._retry_blocker(_meter(transient_streak=6), RL, 15.0) is None
    calls = runner._retry_blocker(_meter(llm_calls=10), RL, 15.0)
    assert calls == "the model-call limit is reached"
    cost = runner._retry_blocker(_meter(cost_usd=0.99), RL, 15.0)
    assert cost is not None and "cost cap" in cost
    # The decision uses the clock reading it is given, not a later one.
    assert runner._retry_blocker(_meter(stop), RL, 40.0, now=0.0) is None
    assert runner._retry_blocker(_meter(stop), RL, 40.0, now=31.0) == RUN_LIMIT
    # The last, shorter wait was sized against the stop: only the phase's end and the
    # other limits still count for it.
    assert runner._retry_blocker(_meter(stop), RL, 80.0, now=0.0, final=True) is None
    late = runner._retry_blocker(_meter(stop, deadline_epoch=-50.0), RL, 5.0, final=True)
    assert late == "the phase deadline leaves no time for another attempt"
    calls = runner._retry_blocker(_meter(stop, llm_calls=10), RL, 5.0, final=True)
    assert calls == "the model-call limit is reached"


RUN_LIMIT = "the run's time limit is reached"


def test_only_temporary_codes_are_retried() -> None:
    def failed(code: str, retryable: bool = True) -> runner._Turn:
        return runner._Turn("Failed", "failed", "", "x", retryable=retryable, code=code)

    assert runner._should_retry(failed("completion_rate_limited"))
    assert runner._should_retry(failed("completion_unavailable"))
    assert runner._should_retry(failed("completion_timed_out"))
    assert runner._should_retry(failed("completion_failed"))
    assert not runner._should_retry(failed("completion_unavailable", retryable=False))
    assert not runner._should_retry(failed("completion_failed_permanently"))
    assert not runner._should_retry(failed("completion_context_too_large"))
    assert not runner._should_retry(runner._Turn("Completed", None, "notes"))
