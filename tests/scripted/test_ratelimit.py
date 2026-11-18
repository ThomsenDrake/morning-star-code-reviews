"""Scripted-model tests: rate limits make a phase slower, they never end it.

Temporary model errors are retried without a count limit (backoff 15, 30, 60, 120, 180,
180, ... s), the waits pause the phase clocks, refused calls do not use up model calls,
and the agent processes of one run share a cooldown per model. Only the run's limits
(``RunLimits``), the cost cap, or six lost streams in a row end the retries; before the
run's stop one last, shorter wait is made. Time is simulated (``FakeTime``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
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

T0 = 1_000_000.0  # FakeTime's start
# The last shorter wait ends this long before the run's stop.
FINAL_GAP = runner.RETRY_MIN_TURN_SECONDS + runner.FINAL_WAIT_MARGIN_S


def _meter(job_dir: Path) -> hooks.MeterState:
    return hooks.MeterState.model_validate_json((job_dir / "meter.json").read_bytes())


def _assert_backoff(sleeps: list[float], bases: list[float]) -> None:
    assert len(sleeps) == len(bases), sleeps
    for slept, base in zip(sleeps, bases, strict=True):
        assert base <= slept <= base * (1 + ratelimit.JITTER), (slept, base)


def _curve(n: int) -> list[float]:
    return [min(180.0, 15.0 * 2**i) for i in range(n)]


async def test_twelve_give_ups_then_success_complete(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", explore_seconds=300, grace_seconds=60)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            *["429"] * 12,
            testing.call("read_repo_file", path="shop/service.py"),
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.complete is True
    _assert_backoff(fake_time.sleeps, _curve(12))  # 15, 30, 60, 120, 180, 180, ...
    waited = sum(fake_time.sleeps)
    assert waited > 300 + 60  # more than the whole phase time, still not a failure
    meter = _meter(job_dir)
    assert meter.deadline_epoch == pytest.approx(T0 + 300 + waited)
    assert meter.rl_wait_seconds == pytest.approx(waited)
    assert meter.rl_until_epoch is None
    # The tool call after the waits was still admitted.
    assert result.opened_paths == ["shop/money.py", "shop/service.py"]
    assert result.usage.tool_calls == 2
    assert result.usage.llm_calls == 3  # refused calls do not count
    assert result.usage.rate_limit_waits == 12
    assert result.usage.rate_limit_wait_seconds == pytest.approx(waited, abs=1e-3)
    assert result.exhausted_by == []


async def test_every_burst_starts_again_at_15_seconds(kit: JobKit, fake_time: FakeTime) -> None:
    steps: list[Any] = []
    for _ in range(4):
        steps += [testing.call("read_repo_file", path="shop/money.py"), "429", "429"]
    model = flaky_model([*steps, testing.reply(NOTES_ONE)])
    job_dir = kit.make("explore", explore_seconds=900)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    _assert_backoff(fake_time.sleeps, [15.0, 30.0] * 4)


async def test_i2_bursts_separated_by_progress_complete(kit: JobKit, fake_time: FakeTime) -> None:
    # Ported from the I2 probe: four 429 episodes used to exhaust three retries per turn.
    steps: list[Any] = []
    for _ in range(4):
        steps += [testing.call("read_repo_file", path="shop/money.py"), "429"]
    model = flaky_model([*steps, testing.reply(NOTES_ONE)])
    job_dir = kit.make("explore", explore_seconds=900)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.error is None
    _assert_backoff(fake_time.sleeps, [15.0] * 4)


async def test_rescue_turn_survives_three_give_ups(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", max_llm_calls=1, grace_llm_calls=2)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),  # the backstop ends turn 1
            "429",
            "429",
            "429",  # the rescue allows 2 calls: refusals must not use them up
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.rescued is True
    assert result.state == "Completed"
    assert "FINDING" in (result.notes or "")
    assert "model-call limit" not in (result.error or "")
    _assert_backoff(fake_time.sleeps, [15.0, 30.0, 60.0])
    meter = _meter(job_dir)
    # The rescue deadline moved with the waits.
    assert meter.rescue_deadline_epoch is not None
    assert meter.rescue_deadline_epoch >= T0 + 60 + sum(fake_time.sleeps) - 1e-6


async def test_i2_rescue_after_closed_tools_completes(kit: JobKit, fake_time: FakeTime) -> None:
    # Ported from the I2 probe: tools close at the tool budget, then 429s in the grace calls.
    job_dir = kit.make("explore", explore_seconds=900, max_tool_calls=1)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            testing.call("read_repo_file", path="shop/money.py"),  # skipped: tools closed
            "429",
            "429",
            "429",
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert "FINDING" in (result.notes or "")
    assert result.exhausted_by == ["tool_calls"]


@pytest.mark.parametrize(("retry_after", "expected"), [(200, 200.0), (900, 300.0)])
async def test_retry_after_is_honoured_and_capped(
    kit: JobKit, fake_time: FakeTime, retry_after: int, expected: float
) -> None:
    job_dir = kit.make("explore")
    model = flaky_model([("429", {"retry_after_seconds": retry_after}), testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert fake_time.sleeps == [pytest.approx(expected)]


async def test_endless_429_gives_up_at_the_run_stop(kit: JobKit, fake_time: FakeTime) -> None:
    limits = RunLimits(wind_down_epoch=T0 + 300, stop_epoch=T0 + 600)
    job_dir = kit.make("explore", explore_seconds=900, run_limits=limits)
    (job_dir / "notes.partial.txt").write_text(NOTES_ONE)  # an earlier call's notes
    model = flaky_model(["429"] * 30)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    # 15 + 30 + 60 + 120 + 180 s fit before the stop (with 30 s for a turn); 180 more do
    # not, so one last, shorter wait ends 30 s (plus its 5 s margin) before the stop. The
    # retry after it fails too, and no wait fits any more.
    assert model.used == 7
    _assert_backoff(fake_time.sleeps[:5], _curve(5))
    assert len(fake_time.sleeps) == 6
    assert fake_time.sleeps[5] < 180.0
    assert fake_time.now == pytest.approx(limits.stop_epoch - FINAL_GAP)
    assert result.state == "Failed"
    assert result.stop_reason == RUN_DEADLINE_STOP  # the review says the time limit ended it
    assert "gave up after 6 retries: the run's time limit is reached" in (result.error or "")
    assert result.exhausted_by == ["deadline"] and result.budget_exhausted is True
    assert result.notes == NOTES_ONE  # from the partial notes
    saved = json.loads((job_dir / "result.json").read_text())
    assert saved["state"] == "Failed" and saved["stop_reason"] == RUN_DEADLINE_STOP
    assert saved["usage"]["rate_limit_waits"] == 6
    assert _meter(job_dir).rl_final_wait is True


async def test_the_last_shorter_wait_gives_the_turn_another_chance(
    kit: JobKit, fake_time: FakeTime
) -> None:
    limits = RunLimits(wind_down_epoch=T0 + 100, stop_epoch=T0 + 100)
    job_dir = kit.make("explore", explore_seconds=900, run_limits=limits)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            "429",
            "429",
            "429",
            testing.reply(NOTES_ONE),
        ],
        clock=fake_time.clock,
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    # About 15 and 30 s fit before the stop (with 30 s for the turn); about 60 do not,
    # so the third wait is cut to end 35 s before the stop, and the retry succeeds.
    _assert_backoff(fake_time.sleeps[:2], _curve(2))
    assert len(fake_time.sleeps) == 3
    assert fake_time.sleeps[2] < 60.0
    assert model.times[-1] == pytest.approx(limits.stop_epoch - FINAL_GAP)
    assert result.state == "Completed" and result.complete is True
    assert result.stop_reason is None
    assert "FINDING" in (result.notes or "")


async def test_no_room_before_the_stop_gives_up_with_the_run_limit(
    fake_time: FakeTime,
) -> None:
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 20)  # less than a turn left
    m = _wait_meter(fake_time, limits)
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    gave_up = await runner._wait_to_retry(m, turn, 2)
    assert gave_up is not None and gave_up.run_limit is True
    assert gave_up.error == "x (gave up after 2 retries: the run's time limit is reached)"
    assert fake_time.sleeps == []
    assert m.state.rl_final_wait is False


async def test_only_one_last_shorter_wait_is_made(fake_time: FakeTime) -> None:
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 100)
    m = _wait_meter(fake_time, limits)
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    m.state.rl_streak = 3  # the next backoff is about 120 s: it does not fit
    assert await runner._wait_to_retry(m, turn, 3) is None
    assert fake_time.sleeps == [pytest.approx(100.0 - FINAL_GAP)]
    assert m.state.rl_final_wait is True
    fake_time.now = T0  # even with room again, the last wait was made
    gave_up = await runner._wait_to_retry(m, turn, 4)
    assert gave_up is not None and gave_up.run_limit is True
    assert len(fake_time.sleeps) == 1


async def test_other_blockers_are_not_the_run_limit(fake_time: FakeTime) -> None:
    m = _wait_meter(fake_time, None, transient_streak=5)
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code="completion_failed")
    gave_up = await runner._wait_to_retry(m, turn, 5)
    assert gave_up is not None and gave_up.run_limit is False
    assert "6 failures in a row" in gave_up.error


class SteppingTime(FakeTime):
    """Fake time that moves on by ``step`` seconds at every read, like ``time.time()``."""

    def __init__(self, step: float) -> None:
        super().__init__()
        self.step = step
        self.reads = 0

    def clock(self) -> float:
        self.reads += 1
        self.now += self.step
        return self.now


@pytest.fixture
def stepping_time(monkeypatch: pytest.MonkeyPatch) -> SteppingTime:
    st = SteppingTime(step=0.01)
    monkeypatch.setattr(runner, "_sleep", st.sleep)
    monkeypatch.setattr(hooks, "_sleep", st.sleep)
    return st


@pytest.mark.parametrize("step", [0.0, 0.001, 0.05, 1.0])
async def test_the_last_wait_is_made_when_the_clock_moves_on_every_read(
    monkeypatch: pytest.MonkeyPatch, step: float
) -> None:
    """Live 0.1.1 bug: the last wait was re-checked a moment later and always refused."""
    st = SteppingTime(step)
    ends: list[float] = []

    async def sleep(seconds: float) -> None:
        await st.sleep(seconds)
        ends.append(st.now)

    monkeypatch.setattr(runner, "_sleep", sleep)
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 100)
    m = _wait_meter(st, limits, rl_streak=3)  # the next backoff (~120 s) does not fit
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    assert await runner._wait_to_retry(m, turn, 3) is None
    assert st.reads > 2  # the decision and the wait read the clock more than once
    # The wait ends where it was decided, 35 s before the stop, however late it starts.
    assert ends == [pytest.approx(limits.stop_epoch - FINAL_GAP)]
    (slept,) = st.sleeps
    assert 50.0 < slept <= 100.0 - FINAL_GAP
    assert m.state.rl_final_wait is True and m.state.rl_waits == 1


async def test_endless_429_makes_the_last_wait_with_a_moving_clock(
    kit: JobKit, stepping_time: SteppingTime
) -> None:
    st = stepping_time
    limits = RunLimits(wind_down_epoch=T0 + 100, stop_epoch=T0 + 100)
    job_dir = kit.make("explore", explore_seconds=900, run_limits=limits)
    model = flaky_model(
        [testing.call("read_repo_file", path="shop/money.py"), "429", "429", "429", "429"],
        clock=st.clock,
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=st.clock)
    model.assert_consumed()
    # About 15 and 30 s fit; the third wait is the last, shorter one; its retry is
    # refused too and nothing fits any more.
    assert len(st.sleeps) == 3 and st.sleeps[2] < 60.0
    assert model.times[-1] < limits.stop_epoch - runner.RETRY_MIN_TURN_SECONDS
    assert _meter(job_dir).rl_final_wait is True
    assert result.stop_reason == RUN_DEADLINE_STOP
    assert "gave up after 3 retries: the run's time limit is reached" in (result.error or "")


async def test_a_refused_wait_leaves_the_shared_cooldown_alone(
    fake_time: FakeTime, tmp_path: Path
) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 20)  # less than a turn left
    m = _wait_meter(fake_time, limits)
    m.cooldown_path = shared
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    gave_up = await runner._wait_to_retry(m, turn, 2)
    assert gave_up is not None and gave_up.run_limit is True
    assert not shared.exists()  # no other agent waits for a retry that never comes
    _cooldown(shared, T0 + 10)
    before = shared.read_bytes()
    assert await runner._wait_to_retry(m, turn, 3) is not None
    assert shared.read_bytes() == before
    assert fake_time.sleeps == []


async def test_the_last_wait_moves_the_shared_cooldown_to_its_end(
    stepping_time: SteppingTime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = stepping_time
    shared = tmp_path / "cooldown-mistral-large-4.json"
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 100)
    m = _wait_meter(st, limits, rl_streak=3)
    m.cooldown_path = shared
    seen: list[float | None] = []

    async def sleep(seconds: float) -> None:
        seen.append(ratelimit.read_cooldown(shared, "mistral-large-4"))  # written before
        await st.sleep(seconds)

    monkeypatch.setattr(runner, "_sleep", sleep)
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    assert await runner._wait_to_retry(m, turn, 3) is None
    (until,) = seen
    assert until is not None
    assert until == pytest.approx(limits.stop_epoch - FINAL_GAP, abs=0.5)
    assert until <= limits.stop_epoch - FINAL_GAP
    assert m.state.rl_final_wait is True


async def test_no_last_wait_while_the_shared_cooldown_lasts_longer(
    fake_time: FakeTime, tmp_path: Path
) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    _cooldown(shared, T0 + 90)  # another agent: the model is refused until then
    before = shared.read_bytes()
    limits = RunLimits(wind_down_epoch=T0, stop_epoch=T0 + 100)
    m = _wait_meter(fake_time, limits)
    m.cooldown_path = shared
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code=RL)
    gave_up = await runner._wait_to_retry(m, turn, 1)
    assert gave_up is not None and gave_up.run_limit is True
    assert fake_time.sleeps == [] and m.state.rl_final_wait is False
    assert shared.read_bytes() == before


async def test_wind_down_closes_tools_and_notes_are_written(
    kit: JobKit, fake_time: FakeTime
) -> None:
    limits = RunLimits(wind_down_epoch=T0 + 10, stop_epoch=T0 + 310)
    job_dir = kit.make("explore", explore_seconds=900, run_limits=limits)
    model = flaky_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            "429",  # the wait passes the wind-down; run limits never pause
            testing.call("read_repo_file", path="shop/service.py"),  # skipped
            testing.reply(NOTES_ONE),  # a grace call writes the notes
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert fake_time.now > limits.wind_down_epoch
    assert result.opened_paths == ["shop/money.py"]
    assert result.exhausted_by == ["deadline"]
    assert result.budget_exhausted is True
    assert result.state == "Completed"
    assert "FINDING" in (result.notes or "")


async def test_lost_streams_give_up_after_six_in_a_row(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("explore", explore_seconds=900)
    model = flaky_model(["lost"] * 7)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    assert model.used == 6
    _assert_backoff(fake_time.sleeps, _curve(5))
    assert result.state == "Failed"
    assert (result.error or "").startswith("completion_failed")
    assert "gave up after 5 retries: 6 failures in a row" in (result.error or "")


RL = "completion_rate_limited"


def _wait_meter(fake_time: FakeTime, limits: RunLimits | None, **state: Any) -> hooks.Meter:
    values: dict[str, Any] = {
        "phase": "explore",
        "model": "mistral-large-4",
        "started_epoch": T0,
        "deadline_epoch": T0 + 900,
        "llm_call_limit": 10,
    }
    values.update(state)
    return hooks.Meter(
        hooks.MeterState(**values),
        budgets(),
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        clock=fake_time.clock,
        limits=limits,
    )


def _cooldown(path: Path, until: float, model: str = "mistral-large-4") -> None:
    data = {"model": model, "until_epoch": until, "streak": 2, "updated_epoch": T0}
    path.write_text(json.dumps(data))


async def test_shared_cooldown_pauses_before_the_call(
    kit: JobKit, fake_time: FakeTime, tmp_path: Path
) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    _cooldown(shared, T0 + 100)
    job_dir = kit.make("explore", explore_seconds=300, cooldown_path=shared)
    model = flaky_model([testing.reply(NOTES_ONE)], clock=fake_time.clock)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert fake_time.sleeps == [pytest.approx(100.0)]
    assert model.times == [pytest.approx(T0 + 100)]  # the call waited for the cooldown
    meter = _meter(job_dir)
    assert meter.deadline_epoch == pytest.approx(T0 + 300 + 100)
    assert meter.rl_waits == 1 and meter.rl_wait_seconds == pytest.approx(100.0)
    assert result.usage.rate_limit_waits == 1


async def test_a_429_extends_the_shared_cooldown_and_waits_for_it(
    kit: JobKit, fake_time: FakeTime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    job_dir = kit.make("explore", cooldown_path=shared)
    model = flaky_model(["429", testing.reply(NOTES_ONE)], clock=fake_time.clock)

    async def another_process_hits_the_limit_too(seconds: float) -> None:
        # While this agent waits, another agent of the run is refused and extends the
        # shared end further; the file keeps the later of both ends.
        ours = ratelimit.read_cooldown(shared)
        assert ours == pytest.approx(T0 + seconds)
        assert ratelimit.extend_cooldown(shared, "mistral-large-4", T0 + 500, 1) == T0 + 500
        assert ratelimit.extend_cooldown(shared, "mistral-large-4", T0 + 20, 1) == T0 + 500
        await fake_time.sleep(seconds)

    monkeypatch.setattr(runner, "_sleep", another_process_hits_the_limit_too)
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    # Our backoff (about 15 s), then the hook waited for the rest of the shared cooldown.
    assert len(fake_time.sleeps) == 2
    assert sum(fake_time.sleeps) == pytest.approx(500.0)
    assert model.times[1] == pytest.approx(T0 + 500)
    assert json.loads(shared.read_text())["until_epoch"] == pytest.approx(T0 + 500)
    assert result.usage.rate_limit_waits == 2
    assert result.usage.rate_limit_wait_seconds == pytest.approx(500.0, abs=1e-3)


async def test_the_runner_waits_for_a_later_shared_end(fake_time: FakeTime, tmp_path: Path) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    _cooldown(shared, T0 + 400)
    state = hooks.MeterState(
        phase="explore",
        model="mistral-large-4",
        started_epoch=T0,
        deadline_epoch=T0 + 300,
        llm_call_limit=10,
        llm_calls=1,
        pending_call=True,
        pending_since=T0,
    )
    m = hooks.Meter(
        state,
        budgets(),
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        clock=fake_time.clock,
        cooldown_path=shared,
    )
    turn = runner._Turn("Failed", "failed", "", "x", retryable=True, code="completion_rate_limited")
    assert await runner._wait_to_retry(m, turn, 0) is None
    assert fake_time.sleeps == [pytest.approx(400.0)]
    assert m.state.deadline_epoch == pytest.approx(T0 + 700)
    assert m.state.llm_calls == 0 and m.state.refused_calls == 1
    assert m.state.rl_streak == 1 and m.state.rl_until_epoch is None


async def test_a_429_writes_the_shared_cooldown(
    kit: JobKit, fake_time: FakeTime, tmp_path: Path
) -> None:
    shared = tmp_path / "cooldown-mistral-large-4.json"
    job_dir = kit.make("explore", cooldown_path=shared)
    model = flaky_model(["429", testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    assert result.state == "Completed"
    data = json.loads(shared.read_text())
    assert data["until_epoch"] == pytest.approx(T0 + fake_time.sleeps[0])
    assert data["streak"] == 1
    # A later process of the same run (another formatter) waits for what is left.
    fake_time.now = T0 + 5
    second = kit.make("format", name="job-format-2", text="notes", cooldown_path=shared)
    draft = FormatterDraft(findings=[], prior_status=[], summary="Nothing found.")
    formatter = flaky_model([testing.reply(draft.model_dump_json())])
    out = await runner.run_job("format", second, model=formatter, clock=fake_time.clock)
    assert out.state == "Completed"
    assert fake_time.sleeps[1] == pytest.approx(fake_time.sleeps[0] - 5)


async def test_formatter_survives_long_rate_limits(kit: JobKit, fake_time: FakeTime) -> None:
    draft = FormatterDraft(findings=[], prior_status=[], summary="Nothing found.")
    job_dir = kit.make("format", text="notes")
    model = flaky_model(["429"] * 10 + [testing.reply(draft.model_dump_json())])
    result = await runner.run_job("format", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert sum(fake_time.sleeps) > runner.FORMATTER_TIMEOUT
    assert result.state == "Completed"
    assert FormatterDraft.model_validate(result.draft) == draft
    assert result.usage.rate_limit_waits == 10


async def test_verifier_survives_eight_give_ups(kit: JobKit, fake_time: FakeTime) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1")])
    model = flaky_model(
        [
            *["429"] * 8,
            testing.call("read_repo_file", path="shop/money.py"),
            testing.reply(VERDICTS),
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model, clock=fake_time.clock)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.complete is True
    assert len(fake_time.sleeps) == 8


async def test_the_sdk_awaits_a_sleeping_pre_llm_call(
    kit: JobKit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook really sleeps (0.5 s of real time standing in for a 30 s cooldown); the
    # SDK must wait for it and then make the call: one pre/post pair, no lost call.
    ft = FakeTime(real=0.5)
    monkeypatch.setattr(runner, "_sleep", ft.sleep)
    monkeypatch.setattr(hooks, "_sleep", ft.sleep)
    shared = tmp_path / "cooldown-mistral-large-4.json"
    _cooldown(shared, T0 + 30)
    job_dir = kit.make("explore", cooldown_path=shared)
    model = flaky_model([testing.reply(NOTES_ONE)], clock=ft.clock)
    result = await runner.run_job("explore", job_dir, model=model, clock=ft.clock)
    model.assert_consumed()
    assert result.state == "Completed" and result.complete is True
    assert ft.sleeps == [pytest.approx(30.0)]
    assert model.times == [pytest.approx(T0 + 30)]
    meter = _meter(job_dir)
    assert meter.llm_calls == 1 and meter.estimated_calls == 0
    assert meter.finish_reasons == ["stop"]
    assert meter.pending_call is False and meter.rl_until_epoch is None


# ------------------------------------------------------------------ policy helpers


def test_backoff_curve_jitter_and_retry_after() -> None:
    low = [ratelimit.backoff(n, rand=lambda a, b: a) for n in range(1, 9)]
    assert low == [15, 30, 60, 120, 180, 180, 180, 180]
    high = ratelimit.backoff(3, rand=lambda a, b: b)
    assert high == 60 * (1 + ratelimit.JITTER)
    assert ratelimit.backoff(0, rand=lambda a, b: a) == 15  # defensive: streak >= 1
    assert ratelimit.backoff(10_000, rand=lambda a, b: a) == 180
    assert ratelimit.backoff(1, 200, rand=lambda a, b: a) == 200
    assert ratelimit.backoff(1, 900, rand=lambda a, b: a) == ratelimit.RETRY_AFTER_MAX_S
    assert ratelimit.backoff(5, 2, rand=lambda a, b: a) == 180  # a short Retry-After
    assert ratelimit.backoff(1, float("inf"), rand=lambda a, b: a) == 15


def test_retry_after_of_details() -> None:
    assert ratelimit.retry_after_of({"retry_after_seconds": 12}) == 12.0
    assert ratelimit.retry_after_of({"retry_after_seconds": "7.5"}) == 7.5
    assert ratelimit.retry_after_of({"retry_after_seconds": "soon"}) is None
    assert ratelimit.retry_after_of({"retry_after_seconds": -1}) is None
    assert ratelimit.retry_after_of({"retry_after_seconds": True}) is None
    assert ratelimit.retry_after_of({"retry_after_seconds": [1]}) is None
    assert ratelimit.retry_after_of({"reason": "rate_limited"}) is None
    assert ratelimit.retry_after_of(None) is None


async def test_failed_turn_carries_retry_after(
    kit: JobKit, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = kit.make("explore", explore_seconds=20, grace_seconds=5)  # no time to retry
    model = flaky_model([("429", {"retry_after_seconds": 42})])
    seen: list[runner._Turn] = []
    reduce = runner._reduce

    def spy(state: Any) -> runner._Turn:
        turn = reduce(state)
        seen.append(turn)
        return turn

    monkeypatch.setattr(runner, "_reduce", spy)
    await runner.run_job("explore", job_dir, model=model, clock=fake_time.clock)
    assert seen and seen[0].retry_after == 42.0
    assert seen[0].code == "completion_rate_limited"


def test_cooldown_file_round_trip_and_damage(tmp_path: Path) -> None:
    path = tmp_path / "cooldown-m.json"
    assert ratelimit.read_cooldown(None) is None
    assert ratelimit.read_cooldown(path) is None  # missing
    assert ratelimit.extend_cooldown(path, "m", 100.0, 1, now=50.0) == 100.0
    assert ratelimit.extend_cooldown(path, "m", 80.0, 2, now=60.0) == 100.0  # max of both
    data = json.loads(path.read_text())
    assert data == {"model": "m", "until_epoch": 100.0, "streak": 2, "updated_epoch": 60.0}
    assert ratelimit.read_cooldown(path, "m") == 100.0
    assert ratelimit.read_cooldown(path, "other") is None
    assert ratelimit.read_cooldown(path, "m", now=0.0) == 100.0
    assert ratelimit.read_cooldown(path, "m", now=-1000.0) == -1000.0 + 600.0  # capped
    for damaged in ('{"model": "m", "until', "[1, 2]", '{"until_epoch": "x"}', "x" * 5000):
        path.write_text(damaged)
        assert ratelimit.read_cooldown(path) is None
    path.write_text('{"model": "m", "until_epoch": NaN}')
    assert ratelimit.read_cooldown(path) is None
    # A torn file is replaced by the next writer.
    path.write_text('{"model": "m"')
    assert ratelimit.extend_cooldown(path, "m", 30.0, 1, now=1.0) == 30.0
    assert ratelimit.read_cooldown(path, "m") == 30.0
    # A write that fails only loses the sharing.
    blocked = tmp_path / "missing-dir" / "cooldown.json"
    assert ratelimit.extend_cooldown(blocked, "m", 30.0, 1) == 30.0
    assert ratelimit.extend_cooldown(None, "m", 30.0, 1) == 30.0


def test_sdk_completion_config_caps_the_sdk_retry_wait() -> None:
    config = ratelimit.sdk_completion("mistral-large-4")
    assert config is not None
    assert config.model == "mistral-large-4"
    assert config.retry.max_attempts == ratelimit.SDK_MAX_ATTEMPTS == 5
    assert config.retry.retry_after_max_wait_seconds == ratelimit.SDK_RETRY_AFTER_CAP_S == 60
    job = _job_for_build("mistral-large-4")
    for build in (runner.build_explorer, runner.build_verifier, runner.build_formatter):
        assert build(job).completion is not None
        assert build(job, sdk_retry=False).completion is None


def test_sdk_completion_falls_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_config(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("mistralai.vibe.harness.runtime.completion"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_config)
    monkeypatch.setattr(ratelimit, "_SDK_CONFIG_WARNED", False)
    assert ratelimit.sdk_completion("mistral-large-4") is None
    assert ratelimit.sdk_completion("mistral-large-4") is None
    err = capsys.readouterr().err
    assert err.count("sdk retry config unavailable; using SDK defaults") == 1
    agent = runner.build_explorer(_job_for_build("mistral-large-4"))
    assert agent.completion is None and agent.model == "mistral-large-4"


def _job_for_build(model: str) -> Any:
    from morning_star_review.core.contract import AgentJob

    return AgentJob(
        phase="explore",
        execution_id="msreview-test-1",
        repo_dir="/nonexistent",
        head_sha="0" * 40,
        merge_base_sha="0" * 40,
        input_path="prompt.txt",
        input_sha256="0" * 64,
        variant="full",
        model=model,
        effort="high",
        budgets=budgets(),
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        changed_files=[],
        meter_path="meter.json",
        coverage_path="coverage.json",
        result_path="result.json",
        partial_notes_path="notes.partial.txt",
    )
