"""Scripted-model tests of the explore phase (DESIGN 15.3)."""

from __future__ import annotations

import json
from pathlib import Path

from mistralai.agents import testing

from morning_star_review.agent import runner
from morning_star_review.core.notes import parse_notes

from .conftest import NOTES_ONE, JobKit, ReadJson, usage_model


async def test_happy_path_records_coverage_and_completes(kit: JobKit, read_json: ReadJson) -> None:
    job_dir = kit.make("explore")
    model = testing.ScriptedModel(
        [
            testing.call("show_file_diff", path="shop/service.py"),
            testing.call("read_repo_file", path="shop/money.py"),
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.complete is True
    assert result.rescued is False
    assert result.budget_exhausted is False
    assert result.stop_reason is None
    assert result.usage.tool_calls == 2
    assert result.usage.llm_calls == 3
    assert result.opened_paths == ["shop/service.py", "shop/money.py"]
    assert read_json(job_dir, "coverage.json") == ["shop/service.py", "shop/money.py"]
    on_disk = read_json(job_dir, "result.json")
    assert on_disk["complete"] is True
    parsed = parse_notes(result.notes or "")
    assert len(parsed.findings) == 1
    assert parsed.findings[0].suggestion == "    share = prorate(amount, used, total)"
    meter = read_json(job_dir, "meter.json")
    assert meter["llm_calls"] == 3 and meter["tool_calls"] == 2


async def test_usage_tokens_and_cost_are_booked(kit: JobKit) -> None:
    job_dir = kit.make("explore")
    model = usage_model(
        [testing.call("read_repo_file", path="shop/money.py"), testing.reply(NOTES_ONE)],
        tokens_in=10_000,
        tokens_out=1_000,
    )
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.usage.input_tokens == 20_000
    assert result.usage.output_tokens == 2_000
    # 20k * 0.70 + 2k * 2.10 per million
    assert abs(result.usage.cost_usd - (20_000 * 0.70 + 2_000 * 2.10) / 1e6) < 1e-9
    assert result.usage.finish_reasons == ["tool_call", "stop"]


async def test_tool_budget_skips_extra_calls_and_notes_follow(kit: JobKit) -> None:
    job_dir = kit.make("explore", max_tool_calls=2)
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            testing.call("read_repo_file", path="shop/service.py"),
            testing.call("search_repo", text="prorate"),  # third: skipped by the hook
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.usage.tool_calls == 2
    assert result.budget_exhausted is True
    assert result.exhausted_by == ["tool_calls"]
    assert result.complete is True  # notes written within the grace calls
    assert result.rescued is False
    assert parse_notes(result.notes or "").marker_count == 1


async def test_hard_stop_then_rescue_turn_writes_notes(kit: JobKit) -> None:
    job_dir = kit.make("explore", max_llm_calls=2)
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            testing.call("read_repo_file", path="shop/service.py"),
            # call 3 is refused by pre_llm_call (skip) -> rescue turn:
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert result.budget_exhausted is True
    assert "iterations" in result.exhausted_by
    assert result.complete is True
    assert "budget:" not in (result.notes or "")  # the skip reason is not notes
    assert parse_notes(result.notes or "").marker_count == 1


async def test_skip_text_is_never_notes_when_rescue_is_unaffordable(kit: JobKit) -> None:
    job_dir = kit.make("explore", max_llm_calls=1, rescue_seconds=0)
    model = testing.ScriptedModel([testing.call("read_repo_file", path="shop/money.py")])
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.rescued is False
    assert result.notes is None
    assert result.stop_reason == "skipped"
    assert result.complete is False
    assert result.budget_exhausted is True


async def test_cost_cap_preloaded_meter_stops_before_first_call(
    kit: JobKit, read_json: ReadJson
) -> None:
    import time

    now = time.time()
    meter = {
        "phase": "explore",
        "model": "mistral-large-4",
        "started_epoch": now,
        "deadline_epoch": now + 300,
        "llm_call_limit": 12,
        "cost_usd": 4.999,
    }
    job_dir = kit.make("explore", meter=meter)
    model = testing.ScriptedModel([testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.state == "NotRun"
    assert result.budget_exhausted is True
    assert "cost" in result.exhausted_by
    assert result.usage.llm_calls == 0
    assert read_json(job_dir, "result.json")["state"] == "NotRun"


async def test_retry_resumes_counts_from_meter(kit: JobKit) -> None:
    import time

    now = time.time()
    meter = {
        "phase": "explore",
        "model": "mistral-large-4",
        "started_epoch": now - 100,
        "deadline_epoch": now + 200,
        "llm_call_limit": 12,
        "llm_calls": 4,
        "tool_calls": 3,
        "cost_usd": 0.5,
        "input_tokens": 1234,
        "pending_call": True,  # the first process died during a model call
        "runs": 1,
    }
    job_dir = kit.make("explore", meter=meter)
    model = testing.ScriptedModel(
        [testing.call("read_repo_file", path="shop/money.py"), testing.reply(NOTES_ONE)]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.usage.llm_calls == 6
    assert result.usage.tool_calls == 4
    assert result.usage.cost_usd > 0.5  # the lost call was charged at the projected cost
    saved = json.loads((job_dir / "meter.json").read_text())
    assert saved["runs"] == 2
    assert saved["deadline_epoch"] == now + 200


async def test_iteration_limit_leads_to_rescue(kit: JobKit) -> None:
    import time

    now = time.time()
    # A meter that allows more calls than the SDK backstop (max_llm_calls + 4 = 5).
    meter = {
        "phase": "explore",
        "model": "mistral-large-4",
        "started_epoch": now,
        "deadline_epoch": now + 300,
        "llm_call_limit": 50,
    }
    job_dir = kit.make("explore", max_llm_calls=1, meter=meter)
    steps = [testing.call("read_repo_file", path="shop/money.py") for _ in range(5)]
    model = testing.ScriptedModel([*steps, testing.reply(NOTES_ONE)])
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert "iterations" in result.exhausted_by
    assert parse_notes(result.notes or "").marker_count == 1


async def test_output_limit_is_rescued_with_a_short_answer(kit: JobKit) -> None:
    """The SDK retries cut-off replies itself; post_llm_call sees only the last one."""
    job_dir = kit.make("explore")
    cut = ("length", "FINDING\nfile: shop/service.py\nline: 5\nwhy: very long")
    model = usage_model(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            *([cut] * 7),
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert "length" in result.exhausted_by
    assert result.complete is True
    # 1 tool call reply + 7 cut-off replies + 1 rescue reply; all counted as model calls
    assert result.usage.llm_calls == 9


async def test_empty_answer_without_tools_is_incomplete(kit: JobKit) -> None:
    job_dir = kit.make("explore", rescue_seconds=0)
    model = testing.ScriptedModel([testing.reply("NO FINDINGS\nSUMMARY: LGTM\nREVIEW COMPLETE")])
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.state == "Completed"
    assert result.complete is False  # placeholder summary and no tool call


async def test_no_findings_with_evidence_of_checking_is_complete(kit: JobKit) -> None:
    job_dir = kit.make("explore")
    notes = (
        "NO FINDINGS\nSUMMARY: Compared refund() with prorate() in shop/money.py; the "
        "arguments match the definition.\nREVIEW COMPLETE"
    )
    model = testing.ScriptedModel(
        [testing.call("read_repo_file", path="shop/money.py"), testing.reply(notes)]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    assert result.complete is True


def test_explore_complete_rules() -> None:
    ok = "NO FINDINGS\nSUMMARY: Checked the refund path end to end.\nREVIEW COMPLETE"
    assert runner.explore_complete(ended_normally=True, notes=ok, tool_calls=1, small_diff=False)
    assert not runner.explore_complete(
        ended_normally=False, notes=ok, tool_calls=1, small_diff=False
    )
    assert not runner.explore_complete(
        ended_normally=True, notes=ok, tool_calls=0, small_diff=False
    )
    assert runner.explore_complete(ended_normally=True, notes=ok, tool_calls=0, small_diff=True)
    unclosed = "FINDING\nfile: a.py\nline: 1\nseverity: high\ntitle: x\nevidence: y\nwhy: z\n"
    assert not runner.explore_complete(
        ended_normally=True, notes=unclosed, tool_calls=3, small_diff=False
    )
    assert not runner.explore_complete(ended_normally=True, notes="", tool_calls=3, small_diff=True)


def test_build_explorer_shape(kit: JobKit, tmp_path: Path) -> None:
    from morning_star_review.agent import jobio

    job, _files = jobio.load_job(kit.make("explore", effort="high"), "explore")
    agent = runner.build_explorer(job)
    assert agent.name == runner.EXPLORER_NAME
    assert agent.reasoning_effort == "high"
    assert agent.auto_compact_threshold == 200_000
    assert agent.max_iterations == job.budgets.max_llm_calls + 4
    assert agent.tool_errors == "hide"
    default = runner.build_explorer(job.model_copy(update={"effort": "default"}))
    assert default.reasoning_effort is None


async def test_answer_without_notes_gets_a_format_rescue(kit: JobKit) -> None:
    job_dir = kit.make("explore")
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/money.py"),
            testing.reply("I will now look at the callers."),  # ends the turn, no notes
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert result.budget_exhausted is False  # not a budget stop
    assert result.complete is True


async def test_parallel_batch_crossing_the_tool_budget_keeps_admitted_output(
    kit: JobKit, read_json: ReadJson
) -> None:
    """All pre_tool_call hooks of a batch run first; the admitted call still delivers."""
    job_dir = kit.make("explore", max_tool_calls=1)
    model = usage_model(
        [
            [
                testing.call("read_repo_file", path="shop/money.py"),
                testing.call("read_repo_file", path="shop/service.py"),  # over budget: skipped
            ],
            testing.reply(NOTES_ONE),
        ]
    )
    result = await runner.run_job("explore", job_dir, model=model)
    model.assert_consumed()
    assert result.usage.tool_calls == 1
    assert result.exhausted_by == ["tool_calls"]
    # Coverage is recorded only when the output reached the model.
    assert read_json(job_dir, "coverage.json") == ["shop/money.py"]
    assert result.complete is True
