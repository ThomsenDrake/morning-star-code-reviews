"""Unit tests of the budget meter (DESIGN 6.4)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from morning_star_review.agent import hooks
from morning_star_review.core.contract import RunLimits
from morning_star_review.core.runclock import RL_FALLBACK_MAX_WAIT_S

from .conftest import budgets


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _meter(
    path: Path | None = None, clock: Clock | None = None, **overrides: object
) -> hooks.Meter:
    return hooks.Meter.start(
        phase="explore",
        model="mistral-large-4",
        budgets=budgets(**overrides),
        price_in_per_m=1.0,
        price_out_per_m=2.0,
        path=path,
        clock=clock or Clock(),
    )


def test_tool_gate_counts_and_closes_at_the_limit() -> None:
    m = _meter(max_tool_calls=2, grace_llm_calls=2)
    assert m.llm_gate() is None
    assert m.tool_gate() is None
    assert m.tool_gate() is None
    assert m.tool_gate() == "tool_calls"
    assert m.state.tool_calls == 2
    assert m.state.tools_closed is True
    assert m.state.llm_call_limit == 1 + 2  # calls so far + grace
    assert m.tool_gate() == "closed"
    assert m.state.exhausted_by == ["tool_calls"]


def test_llm_gate_stops_after_grace_calls() -> None:
    m = _meter(grace_llm_calls=1)
    assert m.llm_gate() is None
    m.close_tools("context")
    assert m.llm_gate() is None  # the grace call
    assert m.llm_gate() == "budget: llm calls"
    assert m.state.llm_calls == 2


def test_deadline_closes_tools_then_ends_turn_after_grace() -> None:
    clock = Clock()
    m = _meter(clock=clock, explore_seconds=100, grace_seconds=50)
    clock.now += 100
    assert m.tool_gate() == "deadline"
    assert m.llm_gate() is None  # still inside the grace period
    clock.now += 50
    assert m.llm_gate() == "budget: time"
    assert m.state.exhausted_by == ["deadline"]


def test_rescue_has_its_own_deadline_and_call_allowance() -> None:
    clock = Clock()
    m = _meter(
        clock=clock, explore_seconds=10, grace_seconds=0, rescue_seconds=30, grace_llm_calls=1
    )
    clock.now += 20
    assert m.llm_gate() == "budget: time"
    assert m.can_afford_rescue()
    m.start_rescue()
    assert m.llm_gate() is None
    assert m.llm_gate() == "budget: llm calls"
    clock.now += 31
    m.state.llm_call_limit += 5
    assert m.llm_gate() == "budget: time"


def test_cost_reserve_closes_tools_before_the_cap() -> None:
    m = _meter(max_cost_usd=0.1, grace_llm_calls=2)
    # Projected next call: 8k output tokens at $2/M = $0.016; reserve = 3 calls = $0.048.
    m.state.cost_usd = 0.06
    assert m.tool_gate() == "cost"
    assert m.llm_gate() is None  # notes still fit under the cap
    m.state.cost_usd = 0.09
    assert m.llm_gate() == "budget: cost"
    assert "cost" in m.state.exhausted_by


def test_usage_is_booked_and_lost_calls_are_charged() -> None:
    m = _meter()
    m.reset_context(3_000)  # ~1000 tokens
    assert m.llm_gate() is None
    usage = SimpleNamespace(
        input_tokens=2_000, cached_input_tokens=500, output_tokens=300, reasoning_tokens=100
    )
    m.record_llm_reply(usage=usage, finish_reason="tool_call", text="", extra_chars=30)
    assert m.state.input_tokens == 2_000 and m.state.cached_input_tokens == 500
    assert abs(m.state.cost_usd - (2_000 * 1.0 + 300 * 2.0) / 1e6) < 1e-12
    assert m.context_estimate_tokens() == 2_000  # last input tokens beat the char estimate
    # Two calls without a reply in between: the SDK retried silently.
    assert m.llm_gate() is None
    assert m.llm_gate() is None
    assert m.state.estimated_calls == 1
    assert m.state.output_tokens == 300 + hooks.OUTPUT_ALLOWANCE_TOKENS
    m.record_llm_reply(usage=None, finish_reason="stop", text="notes")
    assert m.state.estimated_calls == 2
    u = m.usage()
    assert u.llm_calls == 3 and u.finish_reasons == ["tool_call", "stop"]


def test_meter_persists_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "meter.json"
    clock = Clock()
    m = _meter(path=path, clock=clock)
    m.llm_gate()
    m.tool_gate()
    deadline = m.state.deadline_epoch
    saved = json.loads(path.read_text())
    assert saved["llm_calls"] == 1 and saved["pending_call"] is True
    clock.now += 40
    again = _meter(path=path, clock=clock)
    assert again.state.runs == 2
    assert again.state.deadline_epoch == deadline
    assert again.state.llm_calls == 1 and again.state.tool_calls == 1
    assert again.state.pending_call is False and again.state.estimated_calls == 1


def test_meter_for_another_model_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "meter.json"
    m = _meter(path=path)
    m.llm_gate()
    other = hooks.Meter.start(
        phase="explore",
        model="zai-glm-5-3",
        budgets=budgets(),
        price_in_per_m=1.0,
        price_out_per_m=2.0,
        path=path,
    )
    assert other.state.llm_calls == 0
    path.write_text("{torn")
    assert _meter(path=path).state.llm_calls == 0


def test_partial_notes_sink() -> None:
    m = _meter()
    seen: list[str] = []
    m.on_text = seen.append
    m.record_llm_reply(usage=None, finish_reason="stop", text="  ")
    m.record_llm_reply(usage=None, finish_reason="stop", text="FINDING ...")
    assert seen == ["FINDING ..."]


def test_admitted_output_survives_a_parallel_tool_budget_close() -> None:
    # The SDK runs all pre_tool_call hooks of a parallel batch before any tool.
    m = _meter(max_tool_calls=1)
    assert m.tool_gate() is None  # call 1 of the batch: admitted
    assert m.tool_gate() == "tool_calls"  # call 2 closes tools
    assert m.admit_tool_output(100) is True  # call 1 still delivers its output
    assert m.state.tool_output_chars == 100


def test_context_close_withholds_the_rest_of_the_batch() -> None:
    m = _meter(max_context_tokens=1_000)
    assert m.admit_tool_output(6_000) is False  # ~2000 tokens > 1000
    assert m.state.tools_closed is True and m.state.exhausted_by == ["context"]
    assert m.admit_tool_output(10) is False


# ------------------------------------------------------------------ run limits, rate-limit waits


def _limited(clock: Clock, wind_down: float, stop: float, **overrides: object) -> hooks.Meter:
    return hooks.Meter.start(
        phase="explore",
        model="mistral-large-4",
        budgets=budgets(**overrides),
        price_in_per_m=1.0,
        price_out_per_m=2.0,
        path=None,
        clock=clock,
        limits=RunLimits(wind_down_epoch=clock.now + wind_down, stop_epoch=clock.now + stop),
    )


def test_old_meter_files_load_without_the_rate_limit_keys() -> None:
    old = {
        "phase": "explore",
        "model": "m",
        "started_epoch": 1.0,
        "deadline_epoch": 2.0,
        "llm_call_limit": 3,
    }
    state = hooks.MeterState.model_validate(old)
    assert state.rl_wait_seconds == 0.0 and state.rl_waits == 0
    assert state.rl_until_epoch is None and state.pending_since is None
    assert state.rl_streak == 0 and state.rl_last_code is None


def test_pause_moves_the_phase_clocks_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "meter.json"
    clock = Clock()
    m = _meter(path=path, clock=clock, explore_seconds=100, rescue_seconds=50)
    m.start_rescue()
    deadline, rescue = m.state.deadline_epoch, m.state.rescue_deadline_epoch
    assert rescue is not None
    m.pause(30.0)
    m.pause(-5.0)  # ignored
    m.pause(float("nan"))  # ignored
    assert m.state.deadline_epoch == deadline + 30
    assert m.state.rescue_deadline_epoch == rescue + 30
    saved = json.loads(path.read_text())
    assert saved["rl_wait_seconds"] == 30.0
    again = _meter(path=path, clock=clock, explore_seconds=100, rescue_seconds=50)
    assert again.state.deadline_epoch == deadline + 30
    assert again.state.rl_wait_seconds == 30.0


def test_a_wait_pauses_up_front_and_is_corrected_at_its_end(tmp_path: Path) -> None:
    path = tmp_path / "meter.json"
    clock = Clock()
    m = _meter(path=path, clock=clock, explore_seconds=100)
    deadline = m.state.deadline_epoch
    assert m.begin_wait(clock.now + 100, "completion_rate_limited") == 100
    # A reader of meter.json sees the whole wait as paused already.
    saved = json.loads(path.read_text())
    assert saved["rl_until_epoch"] == clock.now + 100
    assert saved["rl_waits"] == 1 and saved["rl_wait_seconds"] == 100
    assert saved["rl_last_code"] == "completion_rate_limited"
    # The planned seconds tell a reader what was waited before this wait (0 s here).
    assert saved["rl_wait_planned"] == 100
    assert m.call_end() == deadline + 100 + 60
    clock.now += 40  # the wait ended early (cancelled)
    assert m.end_wait() == 40
    assert m.state.deadline_epoch == deadline + 40
    assert m.state.rl_wait_seconds == 40 and m.state.rl_until_epoch is None
    assert m.state.rl_wait_planned == 0
    assert m.end_wait() == 0  # no wait in progress


def test_a_restart_drops_the_wait_of_a_dead_process(tmp_path: Path) -> None:
    path = tmp_path / "meter.json"
    clock = Clock()
    m = _meter(path=path, clock=clock)
    m.begin_wait(clock.now + 100)
    again = _meter(path=path, clock=clock)
    assert again.state.rl_until_epoch is None and again.state.rl_wait_planned == 0
    assert again.state.rl_waits == 1


def test_run_limits_close_tools_at_the_wind_down_and_calls_at_the_stop() -> None:
    clock = Clock()
    m = _limited(clock, wind_down=50, stop=80, explore_seconds=300, grace_seconds=60)
    assert m.tool_deadline() == clock.now + 50
    assert m.call_end() == clock.now + 80  # deadline + grace would be later
    assert m.tool_gate() is None
    clock.now += 50
    assert m.tool_gate() == "deadline"
    assert m.llm_gate() is None  # grace calls until the stop
    clock.now += 30
    assert m.llm_gate() == "budget: time"
    assert m.state.exhausted_by == ["deadline"]


def test_pauses_never_move_the_run_limits() -> None:
    clock = Clock()
    m = _limited(clock, wind_down=50, stop=80, explore_seconds=30, grace_seconds=10)
    assert m.tool_deadline() == clock.now + 30 and m.call_end() == clock.now + 40
    m.pause(100.0)
    assert m.tool_deadline() == clock.now + 50  # the wind-down binds now
    assert m.call_end() == clock.now + 60
    m.pause(100.0)
    assert m.call_end() == clock.now + 60


def test_waits_after_the_wind_down_extend_the_grace_window() -> None:
    clock = Clock()
    m = _limited(clock, wind_down=10, stop=300, explore_seconds=900, grace_seconds=60)
    start = clock.now
    clock.now += 10
    m.begin_wait(clock.now + 100)
    assert m.call_end() == start + 10 + 100 + 60
    clock.now += 100
    m.end_wait()
    assert m.state.rl_wait_after_wind_down == 100
    assert m.call_end() == start + 170
    assert m.tool_gate() == "deadline"  # tools stay closed at the wind-down
    clock.now += 5
    m.pause(20.0)  # refused-call time, all of it after the wind-down
    assert m.call_end() == start + 190
    clock.now += 1_000
    m.pause(1_000.0)
    assert m.call_end() == start + 300  # never after the stop


def test_rescue_needs_time_before_the_stop() -> None:
    clock = Clock()
    m = _limited(clock, wind_down=10, stop=40)
    assert m.can_afford_rescue()
    clock.now += 11
    assert not m.can_afford_rescue()


def test_refused_calls_are_forgotten_and_a_reply_resets_the_streak() -> None:
    clock = Clock()
    m = _meter(clock=clock)
    assert m.llm_gate() is None
    assert m.state.pending_since == clock.now
    clock.now += 12  # the SDK's own retries
    assert m.note_temporary_failure("completion_rate_limited") == 1
    assert m.forget_refused_call() == 12
    assert m.forget_refused_call() == 0  # nothing pending any more
    assert m.state.llm_calls == 0 and m.state.refused_calls == 1
    assert m.calls_attempted() == 1
    assert m.state.estimated_calls == 0 and m.state.pending_call is False
    assert m.note_temporary_failure("completion_failed") == 2
    assert m.state.transient_streak == 1
    assert m.llm_gate() is None
    m.record_llm_reply(usage=None, finish_reason="stop", text="")
    assert m.state.rl_streak == 0 and m.state.transient_streak == 0
    assert m.state.pending_since is None


def test_usage_reports_rate_limit_waits() -> None:
    clock = Clock()
    m = _meter(clock=clock)
    m.begin_wait(clock.now + 30)
    clock.now += 30
    m.end_wait()
    m.pause(4.5)
    u = m.usage()
    assert u.rate_limit_waits == 1
    assert u.rate_limit_wait_seconds == 34.5


def test_wait_limit_without_run_limits_is_the_fallback_budget() -> None:
    clock = Clock()
    m = _meter(clock=clock)
    assert m.wait_limit() == clock.now + RL_FALLBACK_MAX_WAIT_S
    m.state.rl_wait_seconds = RL_FALLBACK_MAX_WAIT_S + 5
    assert m.wait_limit() == clock.now
    limited = _limited(clock, wind_down=10, stop=40)
    assert limited.wait_limit() == clock.now + 40


# ------------------------------------------------------------------ shared cooldown (hook)


class Sleeper:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds


def _shared(path: Path, until: float, model: str = "mistral-large-4") -> Path:
    path.write_text(json.dumps({"model": model, "until_epoch": until, "streak": 1}))
    return path


async def test_cooldown_wait_pauses_the_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    sleeper = Sleeper(clock)
    monkeypatch.setattr(hooks, "_sleep", sleeper)
    m = _meter(clock=clock)
    deadline = m.state.deadline_epoch
    assert await hooks.wait_for_cooldown(m) == 0  # no cooldown file configured
    m.cooldown_path = _shared(tmp_path / "c.json", clock.now + 100)
    assert await hooks.wait_for_cooldown(m) == 100
    assert sleeper.sleeps == [100]
    assert m.state.deadline_epoch == deadline + 100
    assert m.state.rl_waits == 1 and m.state.rl_until_epoch is None
    assert await hooks.wait_for_cooldown(m) == 0  # the cooldown is over


async def test_cooldown_wait_is_bounded_and_skipped_when_useless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    sleeper = Sleeper(clock)
    monkeypatch.setattr(hooks, "_sleep", sleeper)
    m = _limited(clock, wind_down=50, stop=80)
    m.cooldown_path = _shared(tmp_path / "c.json", clock.now + 500)
    assert await hooks.wait_for_cooldown(m) == 80  # ends at the run's stop
    assert m.llm_gate() == "budget: time"
    # Another model's file, a torn file, a garbled far-future end:
    other = _meter(clock=clock)
    other.cooldown_path = _shared(tmp_path / "o.json", clock.now + 50, model="zai-glm-5-3")
    assert await hooks.wait_for_cooldown(other) == 0
    other.cooldown_path = tmp_path / "torn.json"
    other.cooldown_path.write_text('{"model": "mistral-large-4", "until')
    assert await hooks.wait_for_cooldown(other) == 0
    other.cooldown_path = _shared(tmp_path / "far.json", clock.now + 1e9)
    assert await hooks.wait_for_cooldown(other) == hooks.ratelimit.COOLDOWN_MAX_AHEAD_S
    # No wait when the gates would refuse the call anyway.
    done = _meter(clock=clock, explore_seconds=10, grace_seconds=0)
    clock.now += 11
    done.cooldown_path = _shared(tmp_path / "d.json", clock.now + 50)
    assert await hooks.wait_for_cooldown(done) == 0


async def test_pre_llm_call_waits_for_the_cooldown_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    sleeper = Sleeper(clock)
    monkeypatch.setattr(hooks, "_sleep", sleeper)
    m = _meter(clock=clock)
    m.cooldown_path = _shared(tmp_path / "c.json", clock.now + 20)
    hooks.install_meter(m)
    try:
        assert await hooks.BudgetHook().pre_llm_call(None, None) is None
    finally:
        hooks.install_meter(None)
    assert sleeper.sleeps == [20]
    assert m.state.llm_calls == 1 and m.state.pending_since == clock.now
