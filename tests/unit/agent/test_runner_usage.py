"""Finish reasons are normalised before they reach the ``Usage`` contract."""

from __future__ import annotations

import enum

import pytest

from morning_star_review.agent import hooks, runner
from morning_star_review.core.contract import AgentRunResult

from .conftest import budgets


class _Reason(enum.Enum):
    STOP = "stop"
    TOOL_CALL = "tool_call"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("tool_call", "tool_call"),
        ("length", "length"),
        ("content_filter", "content_filter"),
        ("other", "other"),
        (_Reason.STOP, "stop"),
        ("FinishReason.TOOL_CALL", "tool_call"),
        ("  Length ", "length"),
        ("content filter", "content_filter"),
        (None, "unknown"),
        ("None", "none"),
        ("", "unknown"),
        ("model_context_window_exceeded", "model_context_window"),
        ("x" * 19 + "-yz", "x" * 19),
        ("été", "t"),
    ],
)
def test_normalize_finish_reason(raw: object, expected: str) -> None:
    out = runner.normalize_finish_reason(raw)
    assert out == expected
    assert 0 < len(out) <= runner.FINISH_REASON_MAX_CHARS


def _meter() -> hooks.Meter:
    return hooks.Meter.start(
        phase="explore",
        model="mistral-large-4",
        budgets=budgets(),
        price_in_per_m=1.0,
        price_out_per_m=2.0,
        path=None,
        clock=lambda: 1_000_000.0,
    )


def test_unknown_long_reasons_cannot_fail_the_result() -> None:
    m = _meter()
    for reason in ("FinishReason.TOOL_CALL", "provider_specific_reason_" * 4, "stop"):
        m.record_llm_reply(usage=None, finish_reason=reason, text="")
    usage = runner._usage(m)
    assert usage.finish_reasons == ["tool_call", "provider_specific_re", "stop"]
    result = AgentRunResult(phase="explore", state="Completed", stop_reason=None, usage=usage)
    assert AgentRunResult.model_validate_json(result.model_dump_json()) == result
    assert runner._last_finish(m) == "stop"


def test_enum_style_stop_counts_as_a_normal_end() -> None:
    m = _meter()
    m.record_llm_reply(usage=None, finish_reason="FinishReason.STOP", text="")
    assert runner._last_finish(m) == "stop"


def test_usage_keeps_the_last_200_reasons() -> None:
    m = _meter()
    m.state.finish_reasons = ["tool_call"] * 250 + ["stop"]
    usage = runner._usage(m)
    assert len(usage.finish_reasons) == runner.MAX_FINISH_REASONS
    assert usage.finish_reasons[-1] == "stop"
    assert runner._last_finish(m) == "stop"
    assert _meter().usage().finish_reasons == []


def test_usage_carries_the_rate_limit_waits() -> None:
    m = _meter()
    m.state.rl_waits = 3
    m.state.rl_wait_seconds = 125.12345
    usage = runner._usage(m)
    assert usage.rate_limit_waits == 3
    assert usage.rate_limit_wait_seconds == 125.123
    result = AgentRunResult(phase="explore", state="Completed", stop_reason=None, usage=usage)
    assert AgentRunResult.model_validate_json(result.model_dump_json()) == result
    assert _meter().usage().rate_limit_waits == 0
