"""Verdict matrix (DESIGN 8.4, ADDENDUM D)."""

from __future__ import annotations

from typing import Any

import pytest

from morning_star_review.core.contract import FinalFinding, FindingState, ResolvedPrior
from morning_star_review.review.verdict import decide

GLM, LARGE = "zai-glm-5-3", "mistral-large-4"


def finding(state: FindingState = "open") -> FinalFinding:
    return FinalFinding(
        id="f111111111111",
        path="a.py",
        line=3,
        anchor="inline",
        severity="medium",
        category="correctness",
        title="t",
        evidence="e",
        explanation="x",
        state=state,
    )


def run(make, **kw: Any):
    base: dict[str, Any] = {
        "scope": "full",
        "expected_explorers": [GLM],
        "explores": [make.explore(GLM)],
        "drafts": [make.review_draft([])],
        "verify_needed": False,
        "verify": None,
        "partial_reason": None,
        "coverage": {"reviewable": 1, "covered": 1, "excluded": 0},
        "findings": [],
        "priors": [],
        "prior_state": None,
    }
    base.update(kw)
    return decide(**base)


def test_clean_needs_everything(make) -> None:
    assert run(make) == ("clean", [])


def test_open_findings(make) -> None:
    assert run(make, findings=[finding()]) == ("findings", [])
    assert run(make, findings=[finding("summary_only")]) == ("findings", [])


def test_resolved_without_a_fix_is_a_finding(make) -> None:
    p = ResolvedPrior(
        id="f1",
        title="t",
        path="a.py",
        action="keep_open",
        note="resolved without a fix by drive-by",
    )
    assert run(make, priors=[p]) == ("findings", [])


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"explores": []}, "the zai-glm-5-3 explorer produced no result"),
        ({"explores": "incomplete"}, "the zai-glm-5-3 explorer did not finish its review"),
        (
            {"explores": "failed"},
            "the zai-glm-5-3 explorer did not finish its review; state Failed",
        ),
        (
            {"explores": "exhausted"},
            "the zai-glm-5-3 explorer ran out of tool_calls and reported no finding",
        ),
        ({"drafts": "lossy"}, "the notes of the zai-glm-5-3 explorer could not be fully parsed"),
        (
            {"partial_reason": "more than 3000 files"},
            "only part of the change was reviewed: more than 3000 files",
        ),
        (
            {"coverage": {"reviewable": 3, "covered": 1}},
            "2 of 3 reviewable changed files were not opened",
        ),
        (
            {"coverage": {"reviewable": 3, "covered": 2}},
            "1 of 3 reviewable changed files was not opened",
        ),
        ({"verify_needed": True}, "the verifier did not run"),
        (
            {"verify_needed": True, "verify": "incomplete"},
            "the verifier did not finish (out of deadline)",
        ),
        (
            {"verify_needed": True, "verify": "lossy"},
            "the verifier's notes could not be fully parsed",
        ),
    ],
)
def test_incomplete_reasons(make, kw, reason) -> None:
    special = {
        "incomplete": [make.explore(GLM, complete=False)],
        "failed": [make.explore(GLM, complete=False, state="Failed")],
        "exhausted": [make.explore(GLM, budget_exhausted=True, exhausted_by=["tool_calls"])],
    }
    if isinstance(kw.get("explores"), str):
        kw["explores"] = special[kw["explores"]]
    if kw.get("drafts") == "lossy":
        kw["drafts"] = [make.review_draft([], source="parser_lossy")]
    if kw.get("verify") == "incomplete":
        kw["verify"] = make.verify(
            [], complete=False, budget_exhausted=True, exhausted_by=["deadline"]
        )
    elif kw.get("verify") == "lossy":
        kw["verify"] = make.verify([], source="parser_lossy")
    verdict, reasons = run(make, **kw)
    assert verdict == "incomplete"
    assert reasons == [reason]


def test_exhausted_budget_with_findings_is_not_incomplete(make) -> None:
    ex = [make.explore(GLM, budget_exhausted=True, exhausted_by=["deadline"])]
    assert run(make, explores=ex, findings=[finding()]) == ("findings", [])


def test_incomplete_wins_over_findings(make) -> None:
    verdict, _ = run(make, findings=[finding()], coverage={"reviewable": 2, "covered": 1})
    assert verdict == "incomplete"


def test_ensemble_needs_every_explorer_and_the_verifier(make) -> None:
    both = [make.explore(GLM), make.explore(LARGE)]
    drafts = [make.review_draft([], model=GLM), make.review_draft([], model=LARGE)]
    ok = run(
        make,
        expected_explorers=[GLM, LARGE],
        explores=both,
        drafts=drafts,
        verify_needed=True,
        verify=make.verify([]),
    )
    assert ok == ("clean", [])
    one = run(make, expected_explorers=[GLM, LARGE], explores=[make.explore(GLM)])
    assert one == ("incomplete", ["the mistral-large-4 explorer produced no result"])
    late = run(
        make,
        expected_explorers=[GLM, LARGE],
        explores=[both[0], make.explore(LARGE, complete=False)],
        drafts=drafts,
    )
    assert late == ("incomplete", ["the mistral-large-4 explorer did not finish its review"])


def test_explore_without_a_draft_is_never_clean(make) -> None:
    # The notes were never structured, so "no findings" proves nothing (lesson 1).
    missing = run(make, drafts=[])
    assert missing == ("incomplete", ["the notes of 1 explorer were not turned into findings"])
    both = [make.explore(GLM), make.explore(LARGE)]
    two = run(make, expected_explorers=[GLM, LARGE], explores=both, drafts=[], structured=0)
    assert two == ("incomplete", ["the notes of 2 explorers were not turned into findings"])
    # With an ensemble, the merge result tells how many drafts were structured.
    merged = run(make, expected_explorers=[GLM, LARGE], explores=both, structured=2)
    assert merged == ("clean", [])


def test_no_candidates_means_no_verifier_needed(make) -> None:
    assert run(make, verify_needed=False, verify=None) == ("clean", [])


@pytest.mark.parametrize(
    ("prior_kw", "findings", "expected"),
    [
        ({"complete": True, "verdict": "clean"}, [], "clean"),
        ({"complete": False, "verdict": "incomplete"}, [], "incomplete"),
        ({"complete": True, "verdict": "findings"}, ["open"], "findings"),
        ({"complete": False, "verdict": "incomplete"}, ["open"], "incomplete"),
    ],
)
def test_carry_over_inherits_completeness(make, prior_kw, findings, expected) -> None:
    prior = make.prior_state([], **prior_kw)
    verdict, _ = run(
        make,
        scope="carry_over",
        explores=[],
        drafts=[],
        prior_state=prior,
        findings=[finding(s) for s in findings],
        coverage={"reviewable": 1, "covered": 0},
    )
    assert verdict == expected
