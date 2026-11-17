"""Verdict matrix (DESIGN 8.4, ADDENDUM D; open findings take precedence)."""

from __future__ import annotations

from typing import Any

import pytest

from morning_star_review.core.contract import FinalFinding, FindingState, ResolvedPrior
from morning_star_review.review.verdict import (
    MAX_REASONS,
    Assessment,
    assess,
    decide,
    incomplete_reasons,
)

GLM, LARGE, DEEP = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"


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


def kwargs(make, **kw: Any) -> dict[str, Any]:
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
    return base


def run(make, **kw: Any):
    return decide(**kwargs(make, **kw))


def check(make, **kw: Any) -> Assessment:
    a = assess(**kwargs(make, **kw))
    assert decide(**kwargs(make, **kw)) == (a.verdict, a.reasons)
    return a


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
            "the zai-glm-5-3 explorer ran out of tool_calls and stopped early",
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
    a = check(make, **kw)
    assert (a.verdict, a.reasons, a.complete) == ("incomplete", [reason], False)
    # The same gap does not hide an open finding: the verdict is findings, the reason stays.
    a = check(make, findings=[finding()], **kw)
    assert (a.verdict, a.reasons, a.complete) == ("findings", [reason], False)
    blockers = incomplete_reasons(
        **{k: v for k, v in kwargs(make, **kw).items() if k not in ("findings", "priors")}
    )
    assert blockers == [reason]


def test_exhausted_primary_with_findings_is_findings_but_not_complete(make) -> None:
    ex = [make.explore(GLM, budget_exhausted=True, exhausted_by=["deadline"])]
    a = check(make, explores=ex, findings=[finding()])
    assert a.verdict == "findings" and not a.complete
    assert a.reasons == ["the zai-glm-5-3 explorer ran out of deadline and stopped early"]
    # Without a finding, a primary explorer that ran out of budget never proves clean.
    assert run(make, explores=ex)[0] == "incomplete"


def test_findings_win_over_incomplete(make) -> None:
    # Live E5/E6: the planted bug was found, but files stayed unopened.
    a = check(make, findings=[finding()], coverage={"reviewable": 78, "covered": 40})
    assert a.verdict == "findings" and not a.complete
    assert a.reasons == ["38 of 78 reviewable changed files were not opened"]
    a = check(
        make,
        findings=[finding("summary_only")],
        explores=[make.explore(GLM, complete=False)],
        partial_reason="more than 3000 files",
    )
    assert a.verdict == "findings"
    assert a.reasons == [
        "the zai-glm-5-3 explorer did not finish its review",
        "only part of the change was reviewed: more than 3000 files",
    ]


def test_resolved_without_a_fix_wins_over_incomplete(make) -> None:
    p = ResolvedPrior(
        id="f1", title="t", path="a.py", action="keep_open", note="resolved without a fix by x"
    )
    a = check(make, priors=[p], coverage={"reviewable": 2, "covered": 1})
    assert (a.verdict, a.complete) == ("findings", False)


@pytest.mark.parametrize("state", ["fixed", "dismissed"])
def test_fixed_or_dismissed_findings_do_not_count(make, state: FindingState) -> None:
    assert run(make, findings=[finding(state)])[0] == "clean"
    gap = {"reviewable": 2, "covered": 1}
    assert run(make, findings=[finding(state)], coverage=gap)[0] == "incomplete"


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
        ({"complete": False, "verdict": "incomplete"}, ["open"], "findings"),
        ({"complete": False, "verdict": "findings"}, ["open"], "findings"),
        ({"complete": False, "verdict": "findings"}, [], "incomplete"),
    ],
)
def test_carry_over_inherits_completeness(make, prior_kw, findings, expected) -> None:
    prior = make.prior_state([], **prior_kw)
    a = check(
        make,
        scope="carry_over",
        explores=[],
        drafts=[],
        prior_state=prior,
        findings=[finding(s) for s in findings],
        coverage={"reviewable": 1, "covered": 0},
    )
    assert a.verdict == expected
    assert a.complete is prior_kw["complete"]


# --- Ensemble: the primary explorer must finish; a secondary may run out of budget. ---

TRIO = [GLM, LARGE, DEEP]
SECONDARY_NOTE = (
    "the deepseek-v4-1-flash-1017 explorer ran out of tool_calls; its findings were kept, "
    "and as a secondary explorer it does not make the review incomplete"
)


def ensemble(make, deep: dict[str, Any] | None = None, **kw: Any) -> Assessment:
    """The best preset with a verifier that finished; ``deep`` tweaks the third explorer."""
    explores = [make.explore(GLM), make.explore(LARGE), make.explore(DEEP, **(deep or {}))]
    drafts = [make.review_draft([], model=m) for m in TRIO]
    base: dict[str, Any] = {
        "expected_explorers": TRIO,
        "explores": explores,
        "drafts": drafts,
        "verify_needed": True,
        "verify": make.verify([]),
        "structured": 3,
    }
    base.update(kw)
    return check(make, **base)


def test_ensemble_complete_is_clean(make) -> None:
    a = ensemble(make)
    assert (a.verdict, a.reasons, a.complete) == ("clean", [], True)


@pytest.mark.parametrize("complete", [True, False])
def test_secondary_out_of_budget_with_a_full_draft_does_not_block_clean(
    make, complete: bool
) -> None:
    # complete=True: the rescue turn finished; False: the notes stop but still parse.
    deep = {"complete": complete, "budget_exhausted": True, "exhausted_by": ["tool_calls"]}
    a = ensemble(make, deep)
    assert (a.verdict, a.complete) == ("clean", True)
    assert a.reasons == [SECONDARY_NOTE] and a.notes == (SECONDARY_NOTE,) and not a.blockers
    # With a finding it is a findings verdict; the note stays.
    b = ensemble(make, deep, findings=[finding()])
    assert (b.verdict, b.reasons, b.complete) == ("findings", [SECONDARY_NOTE], True)


def test_secondary_with_formatter_draft_is_also_full(make) -> None:
    drafts = [make.review_draft([], model=m) for m in (GLM, LARGE)]
    drafts.append(make.review_draft([], model=DEEP, source="parser+formatter"))
    deep = {"budget_exhausted": True, "exhausted_by": ["tool_calls"]}
    assert ensemble(make, deep, drafts=drafts).verdict == "clean"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "parser_lossy",
            "the notes of the deepseek-v4-1-flash-1017 explorer could not be fully parsed",
        ),
        ("empty", "the deepseek-v4-1-flash-1017 explorer ran out of tool_calls and stopped early"),
    ],
)
def test_secondary_out_of_budget_without_a_full_draft_blocks_clean(
    make, source: str, reason: str
) -> None:
    drafts = [make.review_draft([], model=m) for m in (GLM, LARGE)]
    drafts.append(make.review_draft([], model=DEEP, source=source))
    deep = {"budget_exhausted": True, "exhausted_by": ["tool_calls"]}
    a = ensemble(make, deep, drafts=drafts)
    assert a.verdict == "incomplete" and not a.complete
    assert reason in a.blockers and SECONDARY_NOTE not in a.reasons


def test_secondary_out_of_budget_without_any_draft_blocks_clean(make) -> None:
    drafts = [make.review_draft([], model=m) for m in (GLM, LARGE)]
    deep = {"complete": False, "budget_exhausted": True, "exhausted_by": ["tool_calls"]}
    a = ensemble(make, deep, drafts=drafts, structured=2)
    assert a.verdict == "incomplete"
    assert a.reasons == [
        "the deepseek-v4-1-flash-1017 explorer did not finish its review (out of tool_calls)",
        "the notes of 1 explorer were not turned into findings",
    ]


def test_secondary_that_failed_without_running_out_blocks_clean(make) -> None:
    # Only running out of budget is tolerated; a crash or a missing ending is not.
    a = ensemble(make, {"complete": False, "state": "Failed"})
    assert a.verdict == "incomplete"
    assert a.reasons == [
        "the deepseek-v4-1-flash-1017 explorer did not finish its review; state Failed"
    ]
    missing = ensemble(make, explores=[make.explore(GLM), make.explore(LARGE)])
    assert missing.verdict == "incomplete"
    assert "the deepseek-v4-1-flash-1017 explorer produced no result" in missing.blockers


@pytest.mark.parametrize("complete", [True, False])
def test_primary_out_of_budget_blocks_clean_even_in_an_ensemble(make, complete: bool) -> None:
    explores = [
        make.explore(GLM, complete=complete, budget_exhausted=True, exhausted_by=["deadline"]),
        make.explore(LARGE),
        make.explore(DEEP),
    ]
    a = ensemble(make, explores=explores)
    assert (a.verdict, a.complete) == ("incomplete", False)
    assert len(a.blockers) == 1 and a.blockers[0].startswith("the zai-glm-5-3 explorer")
    b = ensemble(make, explores=explores, findings=[finding()])
    assert (b.verdict, b.reasons) == ("findings", list(a.blockers))


def test_verifier_gaps_block_clean_but_not_findings(make) -> None:
    unfinished = make.verify([], complete=False, budget_exhausted=True, exhausted_by=["deadline"])
    a = ensemble(make, verify=unfinished)
    assert (a.verdict, a.reasons) == (
        "incomplete",
        ["the verifier did not finish (out of deadline)"],
    )
    b = ensemble(make, verify=None, findings=[finding()])
    assert (b.verdict, b.reasons, b.complete) == ("findings", ["the verifier did not run"], False)


def test_blockers_come_before_notes_and_reasons_are_bounded(make) -> None:
    deep = {"budget_exhausted": True, "exhausted_by": ["tool_calls"]}
    a = ensemble(make, deep, coverage={"reviewable": 3, "covered": 2})
    assert a.reasons == ["1 of 3 reviewable changed files was not opened", SECONDARY_NOTE]
    many = [make.explore(f"m{i}", complete=False) for i in range(30)]
    b = check(make, expected_explorers=[f"m{i}" for i in range(30)], explores=many)
    assert len(b.reasons) == MAX_REASONS and len(b.blockers) > MAX_REASONS
    long = check(make, partial_reason="x" * 400)
    assert len(long.reasons[0]) == 300 and long.reasons[0].endswith("…")


# --- nothing to review (every changed file excluded, no model ran) -----------------------

NOTHING = "all 2 changed files are lock files"


def _idle(make, **kw: Any) -> Assessment:
    base: dict[str, Any] = {
        "explores": [],
        "drafts": [],
        "coverage": {"reviewable": 0, "covered": 0, "excluded": 2},
        "nothing_to_review": NOTHING,
    }
    base.update(kw)
    return check(make, **base)


def test_nothing_to_review_is_clean_with_a_note(make) -> None:
    a = _idle(make)
    assert (a.verdict, a.complete, a.reasons) == ("clean", True, [f"nothing to review: {NOTHING}"])
    assert _idle(make, expected_explorers=[GLM, LARGE, DEEP]).verdict == "clean"


def test_nothing_to_review_needs_the_reason_and_an_empty_full_review(make) -> None:
    # Without the reason, a missing explorer result still blocks clean.
    a = _idle(make, nothing_to_review=None)
    assert a.verdict == "incomplete"
    assert a.blockers == ("the zai-glm-5-3 explorer produced no result",)
    # Something reviewable, an explorer that ran, a cut plan or an incremental scope:
    # the usual rules apply.
    reviewable = {"reviewable": 1, "covered": 0, "excluded": 2}
    assert _idle(make, coverage=reviewable).verdict == "incomplete"
    assert not _idle(make, explores=[make.explore(GLM, complete=False)]).complete
    assert _idle(make, partial_reason="too large").verdict == "incomplete"
    assert _idle(make, scope="incremental").verdict == "incomplete"


def test_nothing_reviewed_with_unreviewed_code_is_incomplete(make) -> None:
    # Security review EXCL-1: lock files, generated code or binaries were excluded and
    # nothing else changed, so nothing was reviewed: not "clean".
    a = _idle(make, unreviewed_code="2 lock files")
    assert a.verdict == "incomplete" and not a.complete
    assert a.blockers == (
        f"nothing was reviewed: {NOTHING}, and the excluded files (2 lock files) can "
        "change what runs",
    )
