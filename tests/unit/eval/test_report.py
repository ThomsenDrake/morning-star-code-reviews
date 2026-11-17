"""Table rendering, and compatibility of the scorer with the real outcome contract."""

from __future__ import annotations

from msreview_eval.cases import CaseKey
from msreview_eval.report import aggregate, render_markdown
from msreview_eval.scoring import Finding, RunScore, score_run

from morning_star_review.core.contract import FinalFinding, ReviewOutcome, Suggestion, Usage


def _real_outcome() -> dict:
    found = FinalFinding(
        id="f0123456789ab",
        path="ledger/db.py",
        line=147,
        start_line=146,
        anchor="inline",
        severity="critical",
        category="security",
        title="SQL injection in the memo search",
        evidence="LIKE '%{term}%'",
        explanation="The term is interpolated into the SQL text.",
        fix="Bind the pattern as a parameter.",
        state="open",
        votes=["zai-glm-5-3", "mistral-large-4"],
        verify_status="confirmed",
        suggestion=Suggestion(start_line=147, end_line=147, text="    pattern = ..."),
    )
    out = ReviewOutcome(
        status="report",
        reason="post=never",
        verdict="findings",
        pr_url="https://github.com/acme/playground/pull/3",
        head_sha="b" * 40,
        execution_id="msreview-cli-abc",
        findings=[found],
        usage={"explorer:zai-glm-5-3": Usage(model="zai-glm-5-3", input_tokens=5, llm_calls=2)},
        cost_usd=0.01,
    )
    return out.model_dump(mode="json")


def test_scorer_reads_the_real_contract(cases: dict[str, CaseKey]) -> None:
    data = _real_outcome()
    f = Finding.from_json(data["findings"][0])
    assert f.span() == (146, 147) and f.is_inline and f.suggestion == "    pattern = ..."
    s = score_run(cases["e1"], data, config="preset:best", run=1)
    assert s.detected["B4"] and s.inline_detected["B4"]
    assert s.usage.llm_calls == 2 and s.cost_usd == 0.01


def _scores(cases: dict[str, CaseKey]) -> list[RunScore]:
    data = _real_outcome()
    return [
        score_run(cases["e1"], data, config="preset:best", run=1, meta={"seconds": 100}),
        score_run(cases["e1"], data, config="preset:best", run=2, meta={"seconds": 200}),
        score_run(cases["e1"], None, config="model:x|y", run=1, meta={"error": "boom"}),
        score_run(
            cases["e4"], dict(data, findings=[], verdict="clean"), config="preset:best", run=1
        ),
    ]


def test_aggregate_counts(cases: dict[str, CaseKey]) -> None:
    agg = aggregate(s for s in _scores(cases) if s.config == "preset:best")
    assert agg.runs == 3 and agg.bug_runs == 8 and agg.detected == 2
    assert agg.detected_by_bug["B4"] == 2 and agg.min_detected == 1
    assert agg.seconds == [100.0, 200.0]
    assert agg.verdicts["findings"] == 2 and agg.verdicts["clean"] == 1


def test_render_markdown_tables(cases: dict[str, CaseKey]) -> None:
    md = render_markdown(_scores(cases), cases, header=["date: 2026-11-17"])
    assert md.startswith("# Evaluation results\n\n- date: 2026-11-17\n")
    assert "## Summary by configuration" in md
    assert "| preset:best | 3 | 2/8 (25%) | 2/8 |" in md
    assert "## E1: Planted bugs" in md and "## E4: Clean refactor" in md
    assert "| model:x\\|y | e1 | 1 | - | - |" in md  # pipes in labels are escaped
    assert "error: boom" in md
    assert "B4" in md and "2/2 (2 inline)" in md
    # Every table row has the same number of cells as its header.
    for block in md.split("\n\n"):
        rows = [r for r in block.splitlines() if r.startswith("|")]
        if rows:
            widths = {r.replace("\\|", "").count("|") for r in rows}
            assert len(widths) == 1, block
