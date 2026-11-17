"""Unit tests of the budget meter (DESIGN 6.4)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from morning_star_review.agent import hooks

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
