"""``review.finalize`` end to end, with golden Markdown snapshots.

Regenerate the snapshots with ``MSREVIEW_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from morning_star_review.core.contract import (
    ExcludedFile,
    FinalizeInput,
    FinalReview,
    PriorStatus,
    ReviewRequest,
    Usage,
    VerifyEntry,
    VerifyResult,
)
from morning_star_review.core.ids import finding_id
from morning_star_review.core.runclock import RUN_DEADLINE_STOP
from morning_star_review.core.runctx import RunContext
from morning_star_review.review import finalize, render
from morning_star_review.review.final import models_of
from morning_star_review.review.merge import merge_drafts
from morning_star_review.review.verdict import is_possible

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
    # State: every listed finding but the possible issues, with the head code as
    # evidence for new anchored ones, then the prior fixed in this run.
    state_ids = {p.id: p for p in final.state.findings}
    possible = {f.id for f in final.findings if is_possible(f)}
    assert money.id in possible and len(possible) == 2
    assert set(state_ids) == ({f.id for f in final.findings} - possible) | {"f0000000000aa"}
    fixed = state_ids["f0000000000aa"]
    assert (fixed.state, fixed.fixed_in, fixed.explanation) == ("fixed", make.head_sha, "")
    assert final.state.findings[-1].id == "f0000000000aa"
    assert state_ids[off.id].evidence == "        end = start + size + 1"
    # Open findings keep their explanation for later summaries.
    assert state_ids[off.id].explanation.startswith("Each page has size + 1 items")
    assert state_ids[off.id].fix == "Drop the + 1." and state_ids[off.id].category == "correctness"
    assert (final.ready, final.blocking) == (False, 5)
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
    # The open finding gives "findings"; the review is still incomplete: the summary
    # lists the gaps and the stored state is not complete, so the next run is full.
    assert final.verdict == "findings"
    assert final.verdict_reasons == [
        "the zai-glm-5-3 explorer did not finish its review (out of deadline)",
        "1 of 2 reviewable changed files was not opened",
    ]
    f = final.findings[0]
    assert f.votes == [GLM] and f.verify_status is None and f.state == "open"
    assert not final.state.complete and final.state.verdict == "findings"
    assert "The review did not cover everything" in final.summary_md
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
    # The unconfirmed finding is a possible issue (summary only), which does not count
    # as a finding; without the verifier the review is not complete.
    assert final.verdict == "incomplete" and final.verdict_reasons == ["the verifier did not run"]
    assert not final.state.complete and final.state.verdict == "incomplete"
    f = final.findings[0]
    assert f.verify_status == "uncertain" and f.state == "summary_only"  # one vote: summary only
    assert "#### Possible issues (not confirmed)" in final.summary_md
    assert "**Verdict: incomplete** · 1 possible issue (not confirmed) · " in final.summary_md


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
    assert not final.state.complete
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


# --------------------------------------------------------------------------------------
# Possible issues only: they do not make the verdict "findings"
# --------------------------------------------------------------------------------------


def possible_only(make, *, verifier_complete: bool = True, votes: int = 1, severity="medium"):
    """One explorer per vote reports the same finding; the verifier leaves it uncertain."""
    models = [GLM, LARGE, DEEPSEEK][:votes]
    roles = make.roles(models, verifier=GLM)
    snap = make.snapshot(config=make.config(roles))
    prep = make.prepared(roles=roles)
    drafts = [
        make.review_draft([make.draft(severity=severity, title="Maybe off by one")], model=m)
        for m in models
    ]
    merge = merge_drafts(drafts, make.anchors(), make.heads().get)
    verify = make.verify(
        [VerifyEntry(candidate_id=c.candidate_id, status="uncertain") for c in merge.candidates],
        complete=verifier_complete,
    )
    explores = [make.explore(m) for m in models]
    return finalize(
        snap,
        prep,
        explores[0],
        drafts[0],
        make.anchors(),
        make.heads().get,
        make.info(),
        explores=explores,
        drafts=drafts,
        merge=merge,
        verify=verify,
    )


def test_possible_issue_alone_is_clean_when_complete(make) -> None:
    final = possible_only(make)
    assert final.verdict == "clean" and final.verdict_reasons == []
    assert final.state.complete and final.state.verdict == "clean"
    [f] = final.findings
    assert (f.verify_status, f.state, f.carried) == ("uncertain", "summary_only", False)
    head = "**Verdict: clean** · No confirmed findings · 1 possible issue (not confirmed) · "
    assert head in final.summary_md and head in final.report_md
    assert "#### Possible issues (not confirmed)" in final.summary_md
    check_golden("possible_only_summary.md", final.summary_md)
    make.assert_safe(final.summary_md)


def test_possible_issue_alone_follows_completeness(make) -> None:
    final = possible_only(make, verifier_complete=False)
    assert final.verdict == "incomplete"
    assert final.verdict_reasons == ["the verifier did not finish"]
    assert "**Verdict: incomplete** · 1 possible issue (not confirmed) · " in final.summary_md


def test_unconfirmed_finding_posted_inline_is_a_finding(make) -> None:
    # ADDENDUM D: 2+ votes and high severity put an uncertain finding inline, tagged
    # "(unconfirmed)"; it counts as an open finding.
    final = possible_only(make, votes=2, severity="high")
    [f] = final.findings
    assert (f.verify_status, f.state) == ("uncertain", "open")
    assert final.verdict == "findings" and final.state.verdict == "findings"
    assert "**Verdict: 1 open finding (1 high)** · " in final.summary_md
    assert "possible issue" not in final.summary_md


@pytest.mark.parametrize("scope", ["incremental", "carry_over"])
def test_a_later_review_does_not_carry_a_possible_issue_as_a_finding(make, scope) -> None:
    # The state has no verification status: a recorded possible issue would come back
    # as a carried, open finding and turn the next review into "findings".
    first = possible_only(make)
    assert first.verdict == "clean" and first.state.findings == []
    snap = make.snapshot(prior=first.state)
    prep = make.prepared(scope=scope, since_sha=first.state.head_sha)
    if scope == "carry_over":
        final = finalize(snap, prep, None, None, make.anchors(), make.heads().get, make.info())
    else:
        final = finalize(
            snap,
            prep,
            make.explore(),
            make.review_draft([]),
            make.anchors(),
            make.heads().get,
            make.info(),
        )
    assert final.verdict == "clean" and final.findings == []


# --- run time limit and rate-limit waits (run clock in the RunContext) ------------------

T0 = 1_800_000_000.0


def run_ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "execution_id": "msreview-cli-octo-ledger-pr7-aaaaaaa-1234abcd",
        "request_sha256": "0" * 64,
        "seal_key": b"k" * 32,
        "run_root": "/tmp/runs",
        "mode": "cli",
        "on_worker": False,
        "github_token": None,
        "read_token": None,
        "bot_login": BOT,
        "mistral_api_key": "test-key",
        "agent_python": "/usr/bin/python3",
        "run_started_epoch": T0,
        "wind_down_epoch": T0 + 75 * 60,
        "hard_epoch": T0 + 90 * 60,
    }
    base.update(kw)
    return RunContext(**base)


def finalize_input(make, run_dir: Path, verify: VerifyResult | None) -> FinalizeInput:
    req = ReviewRequest(
        pr_url="https://github.com/octo/ledger/pull/7",
        repo="octo/ledger",
        mode="cli",
        policy=make.policy(),
        run_label="cli:abc",
    )
    return FinalizeInput(
        req=req,
        snap=make.snapshot(run_dir=str(run_dir)),
        prep=make.prepared(job_root=str(run_dir)),
        verify=verify,
    )


def test_run_info_reads_the_run_clock_and_sums_rate_limit_waits(make, tmp_path) -> None:
    from morning_star_review.activities.common import STARTED_FILE
    from morning_star_review.activities.finalize import run_info

    (tmp_path / STARTED_FILE).write_text(f"{T0 + 5:.3f}\n", encoding="utf-8")
    verify = make.verify(
        [],
        usage=make.usage(GLM, rate_limit_waits=2, rate_limit_wait_seconds=45.0),
        formatter_usage=make.usage(LARGE, rate_limit_waits=1, rate_limit_wait_seconds=15.0),
    )
    inp = finalize_input(make, tmp_path, verify)
    explores = [
        make.explore(GLM),
        make.explore(
            LARGE, usage=make.usage(LARGE, rate_limit_waits=7, rate_limit_wait_seconds=780.4)
        ),
    ]
    drafts = [
        make.review_draft(
            [], model=LARGE, formatter_usage=make.usage(LARGE, rate_limit_wait_seconds=60.0)
        )
    ]
    ctx = run_ctx()

    def info(now: float, **kw: Any):
        return run_info(inp, "exec-1", now, explores=explores, drafts=drafts, **kw)

    before = info(T0 + 60 * 60, ctx=ctx)
    assert (before.run_limit_reached, before.deadline_minutes) == (False, 90)
    assert before.rate_limit_wait_seconds == pytest.approx(900.4)
    assert before.duration_seconds == pytest.approx(60 * 60 - 5)
    assert info(T0 + 75 * 60, ctx=ctx).run_limit_reached  # at the wind-down
    assert info(T0 + 89 * 60, ctx=ctx).run_limit_reached
    # A context without a run clock (older driver, tests): no limit, waits still summed.
    for no_clock in (None, run_ctx(run_started_epoch=None, wind_down_epoch=None, hard_epoch=None)):
        plain = info(T0 + 89 * 60, ctx=no_clock)
        assert (plain.run_limit_reached, plain.deadline_minutes) == (False, None)
        assert plain.rate_limit_wait_seconds == pytest.approx(900.4)
    assert run_info(inp, "exec-1", T0, ctx=ctx).rate_limit_wait_seconds == pytest.approx(60.0)


def test_with_a_verifier_the_run_winds_down_with_the_explorers(make, tmp_path) -> None:
    """A verifying run stops its explorers at 64.5 of 90 min: finalize after that was
    finished early, even though the run's own wind-down (75) has not come yet."""
    from morning_star_review.activities.finalize import run_info

    req = finalize_input(make, tmp_path, None).req
    snap = make.snapshot(run_dir=str(tmp_path))
    ctx = run_ctx()
    for verifier, expected in ((GLM, True), (None, False)):
        roles = make.roles(explorers=(GLM, LARGE), verifier=verifier)
        prep = make.prepared(job_root=str(tmp_path), roles=roles)
        inp = FinalizeInput(req=req, snap=snap, prep=prep)
        assert not run_info(inp, "exec-1", T0 + 64 * 60, ctx=ctx).run_limit_reached
        assert run_info(inp, "exec-1", T0 + 65 * 60, ctx=ctx).run_limit_reached is expected
        assert run_info(inp, "exec-1", T0 + 75 * 60, ctx=ctx).run_limit_reached


def test_finalize_after_the_wind_down_is_never_clean(make) -> None:
    info = make.info(run_limit_reached=True, deadline_minutes=25, rate_limit_wait_seconds=600.0)
    snap = make.snapshot()
    final = finalize(
        snap,
        make.prepared(),
        make.explore(),
        make.review_draft([]),
        make.anchors(),
        make.heads().get,
        info,
    )
    assert final.verdict == "incomplete" and not final.state.complete
    assert final.verdict_reasons == [
        "the review was finished early to stay within its 25-minute time limit; "
        "models were rate-limited for 10 min in total"
    ]


def test_skipped_verifier_leaves_every_candidate_unconfirmed(make) -> None:
    roles = make.roles([GLM], verifier=GLM)
    snap = make.snapshot(config=make.config(roles))
    prep = make.prepared(roles=roles)
    draft = make.review_draft([make.draft(severity="critical")])
    merge = merge_drafts([draft], make.anchors(), make.heads().get)
    skipped = make.verify(
        [],
        source="empty",
        state="NotRun",
        stop_reason=RUN_DEADLINE_STOP,
        complete=False,
        budget_exhausted=True,
        exhausted_by=["deadline"],
        opened_paths=[],
        unaddressed=[c.candidate_id for c in merge.candidates],
        usage=Usage(model=GLM),
    )
    final = finalize(
        snap,
        prep,
        make.explore(),
        draft,
        make.anchors(),
        make.heads().get,
        make.info(run_limit_reached=True, deadline_minutes=90),
        explores=[make.explore()],
        drafts=[draft],
        merge=merge,
        verify=skipped,
    )
    assert final.verdict == "incomplete" and not final.state.complete
    assert final.verdict_reasons == [
        "the review was finished early to stay within its 90-minute time limit",
        "the verifier did not run: the run reached its time limit",
    ]
    assert [f.verify_status for f in final.findings] == ["uncertain"]
    assert "Verifier: did not run; 1 not confirmed\n" in final.summary_md
    assert "Rate-limit wait" not in final.summary_md


# --------------------------------------------------------------------------------------
# The record of findings, `/msreview dismiss` commands and readiness
# --------------------------------------------------------------------------------------


def test_a_refresh_applies_commands_and_keeps_the_reviewed_state(make) -> None:
    from morning_star_review.core.contract import DismissCommand

    off = finding_id(PY, "        end = start + size + 1")
    authz = finding_id(PY, "        if user.id != entry.owner_id:")
    prior = make.prior_state(
        [
            make.prior_finding(id=off, explanation="Pages overlap.", category="correctness"),
            make.prior_finding(
                id=authz,
                line=None,
                severity="low",
                title="Wording",
                evidence="if user.id != entry.owner_id:",
                comment_id=None,
                thread_id=None,
                state="summary_only",
            ),
            make.prior_finding(
                id="f0000000000f1", title="Fixed long ago", state="fixed", fixed_in="e" * 40
            ),
        ],
        tool_version="0.3.0",
        config_hash="9" * 64,
        head_sha=make.head_sha,
    )
    snap = make.snapshot(
        prior=prior,
        dismiss_commands=[
            DismissCommand(id=off, by="alice", reason="the API allows it", comment_id=9),
            DismissCommand(id="f0000000000ff", by="alice", comment_id=9),
        ],
        ignored_commands=["a dismiss command by eve was ignored: only people with write access"],
    )
    prep = make.prepared(scope="carry_over", since_sha=prior.head_sha)
    final = finalize(snap, prep, None, None, make.anchors(), make.heads().get, make.info())
    # The dismissed finding is gone from the open ones, and recorded with who and why.
    assert [f.id for f in final.findings] == [authz]
    by_id = {f.id: f for f in final.state.findings}
    assert (by_id[off].state, by_id[off].dismissed_by, by_id[off].dismiss_reason) == (
        "dismissed",
        "alice",
        "the API allows it",
    )
    assert by_id[off].explanation == ""  # closed records keep no text
    assert by_id["f0000000000f1"].state == "fixed"  # history is kept
    assert [f.id for f in final.state.findings] == [authz, off, "f0000000000f1"]
    # Only low findings are left: ready to merge.
    assert final.verdict == "findings" and (final.ready, final.blocking) == (True, 0)
    assert "**Ready to merge:** no open findings at medium severity or above" in final.summary_md
    assert "dismissed by alice: the API allows it" in final.summary_md
    assert "dismiss command for f0000000000ff by alice: this pull request has no such finding" in (
        final.summary_md
    )
    assert "a dismiss command by eve was ignored" in final.summary_md
    # Nothing was reviewed again: the state still names the review it carries over.
    assert final.state.config_hash == "9" * 64 and final.state.tool_version == "0.3.0"


def test_a_new_finding_that_repeats_a_dismissed_one_is_recorded_as_dismissed(make) -> None:
    gone = make.prior_finding(
        id="f00000000000d",
        title="Page slice is off by one and pages overlap",
        state="dismissed",
        dismissed_by="alice",
        dismiss_reason="intended",
        evidence="something else",
    )
    prior = make.prior_state([gone])
    draft = make.draft(title="Off by one: page slice overlaps the next pages")
    final = finalize(
        make.snapshot(prior=prior),
        make.prepared(),
        make.explore(),
        make.review_draft([draft]),
        make.anchors(),
        make.heads().get,
        make.info(),
    )
    assert final.findings == [] and final.verdict == "clean" and final.ready
    [repeat, earlier] = final.state.findings
    assert earlier.id == "f00000000000d" and earlier.state == "dismissed"
    assert repeat.state == "dismissed" and repeat.dismissed_by == "alice"
    assert repeat.dismiss_reason == "repeats f00000000000d"
    assert "dismissed by alice: repeats f00000000000d" in final.summary_md


def test_a_fix_is_recorded_even_when_the_thread_was_resolved(make) -> None:
    # The author resolved the thread after fixing the code: fixed, not dismissed.
    p = make.prior_finding(
        evidence="gone from the file",
        thread_resolved=True,
        resolved_by="dev",
        resolver_can_dismiss=True,
    )
    prior = make.prior_state([p])
    final = finalize(
        make.snapshot(prior=prior),
        make.prepared(),
        make.explore(),
        make.review_draft([]),
        make.anchors(),
        make.heads().get,
        make.info(),
    )
    [record] = final.state.findings
    assert (record.state, record.fixed_in) == ("fixed", make.head_sha)
    assert "fixed in `aaaaaaa`" in final.summary_md


def test_incomplete_review_is_not_ready(make) -> None:
    final = single(make, complete=False, budget_exhausted=True, exhausted_by=["deadline"])
    assert final.verdict == "incomplete" and not final.ready and final.blocking == 0
    assert "**Not ready to merge:** the review is incomplete (see below)." in final.summary_md


def test_a_carried_over_incomplete_review_keeps_its_reasons_and_coverage(make) -> None:
    first = single(
        make,
        [make.draft()],
        opened_paths=[PY],
        complete=False,
        budget_exhausted=True,
        exhausted_by=["tool_calls"],
    )
    assert not first.state.complete and first.state.not_opened == ["ledger/money.py"]
    reasons = list(first.verdict_reasons)
    assert first.state.blockers == reasons and len(reasons) == 2
    # A refresh (or a push of a lock file) carries that review over: same reasons, same
    # coverage, and the finding keeps its range and explanation.
    snap = make.snapshot(paths=[PY, "ledger/money.py"], prior=first.state)
    prep = make.prepared(
        paths=[PY],
        on_demand_paths=["ledger/money.py"],
        reviewable_paths=[PY, "ledger/money.py"],
        scope="carry_over",
        since_sha=make.head_sha,
    )
    again = finalize(snap, prep, None, None, make.anchors(), make.heads().get, make.info())
    assert again.verdict_reasons == reasons and not again.state.complete
    assert again.state.blockers == reasons and again.state.not_opened == ["ledger/money.py"]
    assert "1 of 2 reviewable changed files reviewed." in again.summary_md
    assert "Not opened: `ledger/money.py`." in again.summary_md
    [carried] = again.findings
    assert carried.carried and carried.explanation == first.findings[0].explanation
    assert carried.start_line == first.findings[0].start_line


def test_a_carried_over_state_from_before_the_record(make) -> None:
    # States before 0.3.0 have no reasons or coverage: the generic reason, nothing covered.
    prior = make.prior_state([], complete=False, verdict="incomplete")
    snap = make.snapshot(prior=prior)
    prep = make.prepared(scope="carry_over", since_sha=prior.head_sha)
    final = finalize(snap, prep, None, None, make.anchors(), make.heads().get, make.info())
    assert final.verdict_reasons == [
        "the previous review was incomplete and no reviewable file changed since"
    ]
    assert final.coverage["covered"] == 0
