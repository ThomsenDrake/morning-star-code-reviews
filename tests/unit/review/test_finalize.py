"""``review.finalize`` end to end, with golden Markdown snapshots.

Regenerate the snapshots with ``MSREVIEW_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from morning_star_review.core.contract import (
    ExcludedFile,
    FinalReview,
    PriorStatus,
    VerifyEntry,
)
from morning_star_review.core.ids import finding_id
from morning_star_review.review import finalize, render
from morning_star_review.review.final import models_of
from morning_star_review.review.merge import merge_drafts

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "review" / "golden"
GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
PY = "ledger/service.py"
BOT = "morning-star-code-reviews[bot]"


def check_golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("MSREVIEW_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")  # files end with one newline
    assert path.exists(), f"missing golden file {name}; run with MSREVIEW_UPDATE_GOLDEN=1"
    golden = path.read_text(encoding="utf-8").removesuffix("\n")
    assert text == golden, f"{name} differs from the golden file"


# --------------------------------------------------------------------------------------
# Scenario: three explorers, a verifier, and a previous review
# --------------------------------------------------------------------------------------


def pipeline_inputs(make):
    roles = make.roles([GLM, LARGE, DEEPSEEK], verifier=GLM)
    cfg = make.config(roles, preset="best")
    sqli_id = finding_id(
        PY, '        query = "SELECT * FROM entries WHERE name = \'" + name + "\'"'
    )
    total_id = finding_id(PY, "        return sum((e.amount for e in entries), Decimal(0))")
    prior = make.prior_state(
        [
            make.prior_finding(
                id=sqli_id,
                line=23,
                title="SQL built by string concatenation",
                severity="critical",
                evidence='query = "SELECT * FROM entries WHERE name = \'" + name + "\'"',
                comment_id=103,
                thread_id="PRRT_3",
            ),
            make.prior_finding(
                id="f0000000000aa",
                line=20,
                title="Old null check",
                evidence="if entry is None: return",
                comment_id=104,
                thread_id="PRRT_4",
            ),
            make.prior_finding(
                id=total_id,
                line=30,
                title="Total ignores refunds",
                severity="medium",
                evidence="return sum((e.amount for e in entries), Decimal(0))",
                comment_id=105,
                thread_id="PRRT_5",
                thread_resolved=True,
                resolved_by="drive-by",
                resolver_can_dismiss=False,
            ),
        ]
    )
    snap = make.snapshot(paths=[PY, "uv.lock"], config=cfg, prior=prior)
    off_glm = make.draft(
        line=14,
        title="Off-by-one in page end index",
        category="correctness",
        explanation="`end` adds one, so each page repeats the first item of the next page.",
        fix="Use `start + size` as the end.",
        suggestion="        end = start + size",
    )
    authz = make.draft(
        line=18,
        evidence="if user.id != entry.owner_id:",
        title="Ownership check is inverted",
        severity="critical",
        category="security",
        explanation="Anyone except the owner may edit the entry; the owner may not.",
        fix="Compare with `==`.",
        suggestion="        if user.id == entry.owner_id:",
    )
    sqli = make.draft(
        line=23,
        evidence='query = "SELECT * FROM entries WHERE name = \'" + name + "\'"',
        title="SQL built by string concatenation",
        severity="critical",
        category="security",
        explanation="`name` is pasted into the SQL text; a crafted name can read any row.",
        fix="Pass `name` as a query parameter.",
    )
    off_large = make.draft(
        line=15,
        severity="medium",
        evidence="start + size + 1",
        title="Pagination returns one extra item",
        explanation="Each page has size + 1 items, and consecutive pages overlap by one item.",
    )
    prorate = make.draft(
        line=27,
        evidence="return prorate(days, amount, total_days)",
        title="prorate arguments are swapped",
        severity="high",
        explanation=(
            "`prorate(amount, days, total)` is defined in ledger/money.py; "
            "days and amount are passed in the wrong order."
        ),
        fix="Call `prorate(amount, days, total_days)`.",
    )
    nit = make.draft(
        line=33,
        evidence="return 'ledger'",
        title="Description is hard-coded",
        severity="low",
        category="other",
        explanation="Minor: consider a constant.",
        fix=None,
    )
    off_ds = make.draft(
        line=12,
        severity="critical",
        evidence="end = start + size + 1  # page end",
        title="Page slice off by one",
        explanation="Off by one.",
    )
    wrong = make.draft(
        line=30,
        evidence="return sum((e.amount for e in entries), Decimal(0))",
        title="Total starts from the wrong value",
        severity="medium",
        explanation="Should start at 0.",
    )
    money = make.draft(
        path="ledger/money.py",
        line=41,
        evidence="def prorate(amount, days, total):",
        title="prorate has no guard for zero total days",
        severity="medium",
        category="reliability",
        explanation="Division by zero when total is 0.",
    )
    drafts = [
        make.review_draft(
            [off_glm, authz, sqli],
            model=GLM,
            prior_status=[PriorStatus(id="f0000000000aa", status="fixed")],
        ),
        make.review_draft(
            [off_large, prorate, nit], model=LARGE, formatter_usage=make.usage(LARGE, cost_usd=0.01)
        ),
        make.review_draft([off_ds, wrong, money], model=DEEPSEEK),
    ]
    anchors = make.anchors()
    heads = make.heads()
    merge = merge_drafts(drafts, anchors, heads.get, pr_paths={PY, "uv.lock"})
    by_title = {c.title: c.candidate_id for c in merge.candidates}
    verify = make.verify(
        [
            VerifyEntry(
                candidate_id=by_title["Page slice off by one"],
                status="confirmed",
                reason="Slice end is one too far.",
            ),
            VerifyEntry(candidate_id=by_title["Ownership check is inverted"], status="confirmed"),
            VerifyEntry(
                candidate_id=by_title["SQL built by string concatenation"], status="uncertain"
            ),
            VerifyEntry(
                candidate_id=by_title["prorate arguments are swapped"],
                status="confirmed",
                severity="critical",
            ),
            VerifyEntry(
                candidate_id=by_title["Total starts from the wrong value"],
                status="refuted",
                reason="Decimal(0) is the intended start value.",
            ),
            VerifyEntry(
                candidate_id=by_title["prorate has no guard for zero total days"],
                status="uncertain",
            ),
        ],
        usage=make.usage(GLM, llm_calls=4, tool_calls=9, cost_usd=0.03, seconds=80.0),
    )
    explores = [
        make.explore(m, usage=make.usage(m, cost_usd=c))
        for m, c in ((GLM, 0.06), (LARGE, 0.05), (DEEPSEEK, 0.04))
    ]
    prep = make.prepared(
        paths=[PY],
        roles=roles,
        excluded=[ExcludedFile(path="uv.lock", reason="lock file", changed_lines=40)],
    )
    return snap, prep, explores, drafts, anchors, heads, merge, verify


def run_pipeline(make) -> FinalReview:
    snap, prep, explores, drafts, anchors, heads, merge, verify = pipeline_inputs(make)
    return finalize(
        snap,
        prep,
        explores[0],
        drafts[0],
        anchors,
        heads.get,
        make.info(),
        explores=explores,
        drafts=drafts,
        merge=merge,
        verify=verify,
    )


def test_pipeline_outcome(make) -> None:
    final = run_pipeline(make)
    assert final.verdict == "findings" and final.verdict_reasons == []
    by_title = {f.title: f for f in final.findings}
    off = by_title["Page slice off by one"]
    assert (off.severity, off.state, off.verify_status, off.line) == (
        "critical",
        "open",
        "confirmed",
        14,
    )
    assert off.votes == [GLM, LARGE, DEEPSEEK]
    assert off.suggestion is not None and off.suggestion.text == "        end = start + size"
    authz = by_title["Ownership check is inverted"]
    assert authz.state == "open" and authz.suggestion is not None
    prorate = by_title["prorate arguments are swapped"]
    assert (prorate.severity, prorate.verify_status, prorate.state) == (
        "critical",
        "confirmed",
        "open",
    )
    sqli = by_title["SQL built by string concatenation"]
    assert sqli.carried and sqli.state == "open" and sqli.thread_id == "PRRT_3"
    money = by_title["prorate has no guard for zero total days"]
    assert (money.anchor, money.state, money.verify_status) == (
        "outside",
        "summary_only",
        "uncertain",
    )
    nit = by_title["Description is hard-coded"]
    assert (nit.anchor, nit.state, nit.line) == ("file", "summary_only", 33)
    total = by_title["Total ignores refunds"]  # carried prior, refuted candidate did not remove it
    assert total.carried and total.state == "open"
    assert [r.title for r in final.refuted] == ["Total starts from the wrong value"]
    assert {(p.id, p.action) for p in final.priors} == {
        (sqli.id, "keep_open"),
        ("f0000000000aa", "resolve"),
        (total.id, "keep_open"),
    }
    assert set(final.usage) == {
        f"explorer:{GLM}",
        f"explorer:{LARGE}",
        f"explorer:{DEEPSEEK}",
        f"formatter:{LARGE}",
        f"verifier:{GLM}",
    }
    assert final.cost_usd == pytest.approx(0.06 + 0.05 + 0.04 + 0.01 + 0.03)
    assert final.coverage == {"reviewable": 1, "covered": 1, "excluded": 1}
    # State: every listed finding, with the head code as evidence for new anchored ones.
    state_ids = {p.id: p for p in final.state.findings}
    assert set(state_ids) == {f.id for f in final.findings}
    assert state_ids[off.id].evidence == "        end = start + size + 1"
    assert state_ids[sqli.id].comment_id == 103 and final.state.summary_comment_id == 55
    assert final.state.complete and final.state.verdict == "findings"
    assert final.state.reviewed_at == "2026-11-17T12:00:00Z"
    FinalReview.model_validate(final.model_dump(mode="json"))  # round trip


def test_pipeline_golden(make) -> None:
    final = run_pipeline(make)
    check_golden("pipeline_summary.md", final.summary_md)
    check_golden("pipeline_report.md", final.report_md)
    inline = [
        render.inline_body(f, ", ".join(f.votes), make.head_sha[:7])
        for f in final.findings
        if f.state == "open" and not f.carried
    ]
    check_golden("pipeline_inline.md", "\n\n---\n\n".join(inline))
    for text in (final.summary_md, final.report_md, *inline):
        make.assert_safe(text)


def test_findings_json(make) -> None:
    final = run_pipeline(make)
    snap, prep, *_ = pipeline_inputs(make)
    data = render.findings_json(
        findings=final.findings,
        verdict=final.verdict,
        verdict_reasons=final.verdict_reasons,
        pr_url=snap.html_url,
        head_sha=snap.head_sha,
        execution_id=make.info().execution_id,
        models=models_of(prep.roles),
        refuted=final.refuted,
        priors=final.priors,
        coverage=final.coverage,
        usage=final.usage,
        cost_usd=final.cost_usd,
    )
    text = json.dumps(data, sort_keys=True)
    back = json.loads(text)
    assert back["verdict"] == "findings" and len(back["findings"]) == len(final.findings)
    assert back["refuted"][0]["reason"] == "Decimal(0) is the intended start value."
    assert back["models"]["verifier"] == {"model": GLM, "effort": "high"}
    assert [e["model"] for e in back["models"]["explorers"]] == [GLM, LARGE, DEEPSEEK]


# --------------------------------------------------------------------------------------
# Single explorer: clean and incomplete
# --------------------------------------------------------------------------------------


def single(make, draft_findings=(), **explore_kw):
    snap = make.snapshot(paths=[PY, "ledger/money.py"])
    prep = make.prepared(
        paths=[PY], on_demand_paths=["ledger/money.py"], reviewable_paths=[PY, "ledger/money.py"]
    )
    explore = make.explore(GLM, **{"opened_paths": [PY, "ledger/money.py"], **explore_kw})
    draft = make.review_draft(list(draft_findings))
    anchors = make.anchors({PY: [(10, 30)], "ledger/money.py": [(1, 5)]})
    heads = make.heads(
        {"ledger/money.py": ["def prorate(amount, days, total):", "    return amount"]}
    )
    return finalize(
        snap, prep, explore, draft, anchors, heads.get, make.info(duration_seconds=None)
    )


def test_single_clean_golden(make) -> None:
    final = single(make)
    assert final.verdict == "clean" and final.findings == [] and final.refuted == []
    assert set(final.usage) == {f"explorer:{GLM}"}
    check_golden("single_clean_summary.md", final.summary_md)
    make.assert_safe(final.summary_md)


def test_single_incomplete_golden(make) -> None:
    final = single(
        make,
        [make.draft()],
        complete=False,
        opened_paths=[PY],
        budget_exhausted=True,
        exhausted_by=["deadline"],
    )
    assert final.verdict == "incomplete"
    assert final.verdict_reasons == [
        "the zai-glm-5-3 explorer did not finish its review (out of deadline)",
        "1 of 2 reviewable changed files was not opened",
    ]
    f = final.findings[0]
    assert f.votes == [GLM] and f.verify_status is None and f.state == "open"
    assert not final.state.complete
    check_golden("single_incomplete_summary.md", final.summary_md)
    check_golden("single_incomplete_report.md", final.report_md)


def test_carry_over_runs_no_model_and_keeps_threads(make) -> None:
    fid = finding_id(PY, "        end = start + size + 1")
    prior = make.prior_state([make.prior_finding(id=fid)], complete=True, verdict="findings")
    snap = make.snapshot(prior=prior)
    prep = make.prepared(scope="carry_over", since_sha=prior.head_sha)
    final = finalize(snap, prep, None, None, make.anchors(), make.heads().get, make.info())
    assert final.verdict == "findings" and final.usage == {} and final.cost_usd == 0
    assert [(f.id, f.carried, f.state) for f in final.findings] == [(fid, True, "open")]
    assert "no reviewable changes since `ddddddd`" in final.summary_md


def test_usage_is_summed_per_role_and_model(make) -> None:
    from morning_star_review.review.final import collect_usage

    drafts = [
        make.review_draft(
            [], model=GLM, formatter_usage=make.usage(LARGE, llm_calls=1, cost_usd=0.01)
        ),
        make.review_draft(
            [], model=DEEPSEEK, formatter_usage=make.usage(LARGE, llm_calls=2, cost_usd=0.02)
        ),
    ]
    verify = make.verify([], formatter_usage=make.usage(LARGE, llm_calls=3, cost_usd=0.03))
    usage = collect_usage([make.explore(GLM), make.explore(DEEPSEEK)], drafts, verify)
    assert sorted(usage) == [
        f"explorer:{DEEPSEEK}",
        f"explorer:{GLM}",
        f"formatter:{LARGE}",
        f"verifier:{GLM}",
    ]
    assert usage[f"formatter:{LARGE}"].llm_calls == 6
    assert usage[f"formatter:{LARGE}"].cost_usd == pytest.approx(0.06)


def test_show_usage_off_hides_run_details(make) -> None:
    snap = make.snapshot(config=make.config(show_usage=False))
    final = finalize(
        snap,
        make.prepared(),
        make.explore(),
        make.review_draft([]),
        make.anchors(),
        make.heads().get,
        make.info(),
    )
    assert "Run details" not in final.summary_md and final.verdict == "clean"


def test_verifier_failure_keeps_candidates_but_blocks_clean(make) -> None:
    roles = make.roles([GLM], verifier=LARGE)
    snap = make.snapshot(config=make.config(roles))
    prep = make.prepared(roles=roles)
    draft = make.review_draft([make.draft(severity="critical")])
    merge = merge_drafts([draft], make.anchors(), make.heads().get)
    final = finalize(
        snap,
        prep,
        make.explore(),
        draft,
        make.anchors(),
        make.heads().get,
        make.info(),
        explores=[make.explore()],
        drafts=[draft],
        merge=merge,
        verify=None,
    )
    assert final.verdict == "incomplete" and final.verdict_reasons == ["the verifier did not run"]
    f = final.findings[0]
    assert f.verify_status == "uncertain" and f.state == "summary_only"  # one vote: summary only
    assert "#### Possible issues (not confirmed)" in final.summary_md


def test_explore_without_draft_is_incomplete_not_clean(make) -> None:
    # Structure never ran for the explorer's notes: zero findings proves nothing.
    final = finalize(
        make.snapshot(),
        make.prepared(),
        make.explore(),
        None,
        make.anchors(),
        make.heads().get,
        make.info(),
    )
    assert final.verdict == "incomplete"
    assert final.verdict_reasons == ["the notes of 1 explorer were not turned into findings"]


def test_verifier_configured_but_no_merge_publishes_nothing_as_confirmed(make) -> None:
    roles = make.roles([GLM], verifier=LARGE)
    draft = make.review_draft([make.draft(severity="critical")])
    final = finalize(
        make.snapshot(config=make.config(roles)),
        make.prepared(roles=roles),
        make.explore(),
        draft,
        make.anchors(),
        make.heads().get,
        make.info(),
    )
    assert final.verdict == "incomplete" and final.verdict_reasons == ["the verifier did not run"]
    assert [(f.verify_status, f.state) for f in final.findings] == [("uncertain", "summary_only")]


def test_prior_on_an_unreadable_pr_file_stays_open(make) -> None:
    fid = finding_id(PY, "        end = start + size + 1")
    prior = make.prior_state([make.prior_finding(id=fid)])
    final = finalize(
        make.snapshot(prior=prior),
        make.prepared(),
        make.explore(),
        make.review_draft([]),
        make.anchors(),
        lambda path: None,  # e.g. the file grew past the size cap
        make.info(),
    )
    assert [(p.action, p.note) for p in final.priors] == [
        ("keep_open", "the file could not be read at head")
    ]
    assert [(f.id, f.carried, f.state) for f in final.findings] == [(fid, True, "open")]


def test_head_text_is_never_asked_for_paths_outside_the_checkout(make) -> None:
    # An earlier "outside" finding can store any model-written path in the state.
    prior = make.prior_state(
        [make.prior_finding(path="../../etc/passwd", comment_id=None, thread_id=None)]
    )
    asked: list[str] = []
    heads = make.heads()

    def head_text(path: str) -> list[str] | None:
        asked.append(path)
        return heads.get(path)

    final = finalize(
        make.snapshot(prior=prior),
        make.prepared(),
        make.explore(),
        make.review_draft([]),
        make.anchors(),
        head_text,
        make.info(),
    )
    assert "../../etc/passwd" not in asked
    assert [p.action for p in final.priors] == ["file_deleted"]
