"""Scripted-model tests of the verify and format phases (ADDENDUM D, DESIGN 6.7)."""

from __future__ import annotations

import json

from mistralai.agents import testing

from morning_star_review.agent import runner
from morning_star_review.core.contract import FormatterDraft, VerifyDraft
from morning_star_review.core.notes import parse_verdicts

from .conftest import JobKit, ReadJson, candidate

V1 = """VERDICT c1
status: confirmed
reason: prorate(amount, days_used, days_total) is called as prorate(amount, total, used).
END VERDICT
"""
V2 = """VERDICT c2
status: refuted
reason: Line 6 only subtracts the share; nothing is wrong there.
END VERDICT
"""


async def test_verify_happy_path(kit: JobKit) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1"), candidate("c2", line=6)])
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/service.py"),
            testing.call("read_repo_file", path="shop/money.py"),
            testing.reply(V1 + V2 + "VERIFY COMPLETE"),
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model)
    model.assert_consumed()
    assert result.phase == "verify"
    assert result.state == "Completed"
    assert result.complete is True
    assert result.rescued is False
    parsed = parse_verdicts(result.notes or "", ["c1", "c2"])
    assert {e.candidate_id: e.status for e in parsed.entries} == {
        "c1": "confirmed",
        "c2": "refuted",
    }
    assert result.opened_paths == ["shop/service.py", "shop/money.py"]


async def test_verify_asks_again_for_unaddressed_candidates(kit: JobKit) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1"), candidate("c2", line=6)])
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/service.py"),
            testing.reply(V1 + "VERIFY COMPLETE"),  # c2 forgotten
            testing.reply(V2 + "VERIFY COMPLETE"),  # follow-up turn
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert result.budget_exhausted is False  # a follow-up for missing ids is not a budget stop
    assert result.complete is True
    parsed = parse_verdicts(result.notes or "", ["c1", "c2"])
    assert parsed.missing(["c1", "c2"]) == []


async def test_verify_still_unaddressed_is_incomplete(kit: JobKit) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1"), candidate("c2", line=6)])
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/service.py"),
            testing.reply(V1 + "VERIFY COMPLETE"),
            testing.reply("I could not decide.\nVERIFY COMPLETE"),
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model)
    assert result.complete is False
    parsed = parse_verdicts(result.notes or "", ["c1", "c2"])
    assert parsed.missing(["c1", "c2"]) == ["c2"]  # downstream: uncertain


async def test_verify_budget_exhaustion_and_rescue(kit: JobKit) -> None:
    job_dir = kit.make("verify", candidates=[candidate("c1")], max_tool_calls=1, max_llm_calls=2)
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/service.py"),
            testing.call("read_repo_file", path="shop/money.py"),  # skipped: tool budget
            # call 3 refused by pre_llm_call (grace calls used) -> rescue turn
            testing.reply(V1 + "VERIFY COMPLETE"),
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is True
    assert result.budget_exhausted is True
    assert "tool_calls" in result.exhausted_by
    assert result.usage.tool_calls == 1
    assert result.complete is True


async def test_verify_without_candidates_makes_no_model_call(kit: JobKit) -> None:
    job_dir = kit.make("verify", candidates=[])
    result = await runner.run_job("verify", job_dir, model=testing.ScriptedModel([]))
    assert result.state == "Completed"
    assert result.complete is True
    assert result.usage.llm_calls == 0


async def test_verify_ids_from_prompt_when_candidates_file_is_missing(kit: JobKit) -> None:
    from morning_star_review.agent import prompts

    text = prompts.render_verify_prompt(
        [candidate("c1"), candidate("c2", line=6)], boundary="ab12cd34", max_tool_calls=5
    )
    job_dir = kit.make("verify", text=text)
    model = testing.ScriptedModel(
        [
            testing.call("read_repo_file", path="shop/service.py"),
            testing.reply(V1 + V2 + "VERIFY COMPLETE"),
        ]
    )
    result = await runner.run_job("verify", job_dir, model=model)
    assert result.complete is True


async def test_formatter_review_valid_json(kit: JobKit, read_json: ReadJson) -> None:
    draft = FormatterDraft(
        findings=[candidate("c1").to_draft()], prior_status=[], summary="One problem."
    )
    job_dir = kit.make("format", text="1. service.py line 5: swapped args (high)")
    model = testing.ScriptedModel([testing.reply(draft.model_dump_json())])
    result = await runner.run_job("format", job_dir, model=model)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.complete is True
    assert FormatterDraft.model_validate(result.draft) == draft
    assert read_json(job_dir, "result.json")["draft"]["summary"] == "One problem."


async def test_formatter_invalid_then_valid(kit: JobKit) -> None:
    draft = FormatterDraft(findings=[], prior_status=[], summary="Nothing found.")
    job_dir = kit.make("format", text="notes")
    model = testing.ScriptedModel(
        [testing.reply('{"findings": "oops"}'), testing.reply(draft.model_dump_json())]
    )
    result = await runner.run_job("format", job_dir, model=model)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.usage.llm_calls == 2


async def test_formatter_invalid_twice_fails(kit: JobKit) -> None:
    job_dir = kit.make("format", text="notes")
    model = testing.ScriptedModel([testing.reply("not json"), testing.reply('{"x": 1}')])
    result = await runner.run_job("format", job_dir, model=model)
    assert result.state in ("Failed", "InvalidOutput")
    assert result.draft is None
    assert result.complete is False  # the structure activity keeps the parsed findings (lossy)


async def test_formatter_fenced_json_is_salvaged(kit: JobKit) -> None:
    draft = FormatterDraft(findings=[], prior_status=[], summary="Fenced.")
    fenced = "```json\n" + draft.model_dump_json() + "\n```"
    job_dir = kit.make("format", text="notes")
    model = testing.ScriptedModel([testing.reply("Here you go:\n" + fenced)])
    result = await runner.run_job("format", job_dir, model=model)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.usage.llm_calls == 1  # unwrapped without a correction call
    assert FormatterDraft.model_validate(result.draft).summary == "Fenced."


async def test_formatter_verify_target(kit: JobKit) -> None:
    out = {
        "entries": [
            {"candidate_id": "c1", "status": "confirmed", "reason": "swapped"},
            {"candidate_id": "c2", "status": "uncertain", "line": None, "reason": ""},
        ]
    }
    job_dir = kit.make(
        "format",
        text="c1 is real (swapped). c2: could not tell",
        format_target="verify",
        candidates=[candidate("c1"), candidate("c2")],
    )
    model = testing.ScriptedModel([testing.reply(json.dumps(out))])
    result = await runner.run_job("format", job_dir, model=model)
    assert result.state == "Completed"
    parsed = VerifyDraft.model_validate(result.draft)
    assert [e.status for e in parsed.entries] == ["confirmed", "uncertain"]


def test_salvage_json_variants() -> None:
    good = FormatterDraft(findings=[], prior_status=[], summary="s")
    text = good.model_dump_json()
    assert runner.salvage_json(text, FormatterDraft) == good
    assert runner.salvage_json("Here it is:\n" + text + "\nThanks", FormatterDraft) == good
    assert runner.salvage_json("```\n" + text + "\n```", FormatterDraft) == good
    assert runner.salvage_json(json.loads(text), FormatterDraft) == good
    assert runner.salvage_json("Fill {path} and {line}:\n" + text, FormatterDraft) == good
    assert runner.salvage_json("no json at all", FormatterDraft) is None
    assert runner.salvage_json('{"summary": 1}', FormatterDraft) is None


def test_verify_complete_rules() -> None:
    open_blocks = (
        "VERDICT c1\nstatus: confirmed\nreason: r\n\nVERDICT c2\nstatus: refuted\nreason: r\n"
    )
    ids = ["c1", "c2"]
    assert runner.verify_complete(
        ended_normally=True, notes=open_blocks + "VERIFY COMPLETE", ids=ids, tool_calls=1
    )
    assert not runner.verify_complete(
        ended_normally=True, notes=open_blocks, ids=ids, tool_calls=1
    )  # no END VERDICT and no sentinel: maybe cut off
    assert not runner.verify_complete(
        ended_normally=True, notes=open_blocks + "VERIFY COMPLETE", ids=ids, tool_calls=0
    )
    assert not runner.verify_complete(
        ended_normally=True, notes=V1 + "VERIFY COMPLETE", ids=ids, tool_calls=1
    )  # c2 unaddressed
    assert not runner.verify_complete(
        ended_normally=False, notes=V1 + V2 + "VERIFY COMPLETE", ids=ids, tool_calls=1
    )


async def test_lossy_explore_notes_go_to_the_formatter(kit: JobKit) -> None:
    from morning_star_review.core.notes import parse_notes

    messy = (
        "1. **shop/service.py line 5** (HIGH): prorate gets (total, used), wants (used, total).\n"
        "FINDING 2:\npath: shop/service.py\nline: 6\nseverity: low\n"
        "SUMMARY: one or two problems.\n"
    )
    explore_dir = kit.make("explore", name="explore")
    model = testing.ScriptedModel(
        [testing.call("read_repo_file", path="shop/money.py"), testing.reply(messy)]
    )
    explored = await runner.run_job("explore", explore_dir, model=model)
    notes = explored.notes or ""
    parsed = parse_notes(notes)
    assert not parsed.lossless  # the structure activity would call the formatter
    draft = FormatterDraft(
        findings=[candidate("c1").to_draft(), candidate("c2", line=6, severity="low").to_draft()],
        prior_status=[],
        summary="one or two problems.",
    )
    fmt_dir = kit.make("format", name="format", text=notes)
    fmt_model = testing.ScriptedModel([testing.reply(draft.model_dump_json())])
    formatted = await runner.run_job("format", fmt_dir, model=fmt_model)
    assert formatted.state == "Completed"
    assert len(FormatterDraft.model_validate(formatted.draft).findings) == 2


TWO_BLOCKS = (
    "FINDING\nfile: shop/service.py\nline: 5\nseverity: high\ncategory: correctness\n"
    "title: swapped\nevidence:     share = prorate(amount, total, used)\nwhy: w\nEND FINDING\n"
    "FINDING 2: shop/service.py around line 6 looks off too (low)\n"
    "SUMMARY: two problems.\n"
)


async def test_formatter_that_drops_a_finding_is_asked_once_more(kit: JobKit) -> None:
    one = FormatterDraft(findings=[candidate("c1").to_draft()], prior_status=[], summary="s")
    two = FormatterDraft(
        findings=[candidate("c1").to_draft(), candidate("c2", line=6, severity="low").to_draft()],
        prior_status=[],
        summary="s",
    )
    job_dir = kit.make("format", text=TWO_BLOCKS)
    model = testing.ScriptedModel(
        [testing.reply(one.model_dump_json()), testing.reply(two.model_dump_json())]
    )
    result = await runner.run_job("format", job_dir, model=model)
    model.assert_consumed()
    assert result.state == "Completed"
    assert result.rescued is True
    assert result.usage.llm_calls == 2
    assert len(FormatterDraft.model_validate(result.draft).findings) == 2


async def test_formatter_retry_that_is_no_better_keeps_the_first_draft(kit: JobKit) -> None:
    one = FormatterDraft(findings=[candidate("c1").to_draft()], prior_status=[], summary="s")
    job_dir = kit.make("format", text=TWO_BLOCKS)
    model = testing.ScriptedModel(
        [testing.reply(one.model_dump_json()), testing.reply("not json"), testing.reply("{}")]
    )
    result = await runner.run_job("format", job_dir, model=model)
    assert result.state == "Completed"
    assert FormatterDraft.model_validate(result.draft) == one


async def test_formatter_keeping_every_finding_needs_one_call(kit: JobKit) -> None:
    two = FormatterDraft(
        findings=[candidate("c1").to_draft(), candidate("c2", line=6, severity="low").to_draft()],
        prior_status=[],
        summary="s",
    )
    job_dir = kit.make("format", text=TWO_BLOCKS)
    model = testing.ScriptedModel([testing.reply(two.model_dump_json())])
    result = await runner.run_job("format", job_dir, model=model)
    model.assert_consumed()
    assert result.rescued is False
    assert result.usage.llm_calls == 1
