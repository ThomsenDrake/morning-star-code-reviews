"""Deterministic merge of explorer drafts (ADDENDUM D)."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from morning_star_review.review.merge import (
    MAX_CANDIDATES,
    draft_models,
    merge_drafts,
    title_similarity,
)

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
PY = "ledger/service.py"


def merged(make, *per_model):
    drafts = [make.review_draft(fs, model=m) for m, fs in per_model]
    return merge_drafts(drafts, make.anchors(), make.head_text())


def test_same_bug_from_three_explorers_is_one_candidate_with_three_votes(make) -> None:
    a = make.draft(
        line=14,
        evidence="end = start + size + 1",
        title="Off-by-one in page end index",
        explanation="short",
    )
    b = make.draft(
        line=15,
        severity="medium",
        evidence="start + size + 1",
        title="Pagination returns one extra item per page",
        explanation="The end index adds one, so each page repeats the next page's first item.",
    )
    c = make.draft(
        path="./ledger/service.py",
        line=12,
        severity="critical",
        evidence="end = start + size + 1  # page end",
        title="Page slice off by one",
        explanation="medium length explanation",
    )
    res = merged(make, (GLM, [a]), (LARGE, [b]), (DEEPSEEK, [c]))
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.candidate_id == "c1"
    assert cand.votes == [GLM, LARGE, DEEPSEEK]
    assert cand.severity == "critical"  # highest severity wins
    assert cand.explanation == b.explanation  # longest explanation wins
    assert cand.path == PY and cand.line == 14  # the representative, snapped to its evidence
    assert res.explorer_models == [GLM, LARGE, DEEPSEEK]
    assert res.input_findings == 3 and res.dropped_over_cap == 0


def test_different_bugs_on_nearby_lines_stay_apart(make) -> None:
    off = make.draft(line=14)
    authz = make.draft(
        line=18, evidence="if user.id != entry.owner_id:", title="Inverted ownership check"
    )
    res = merged(make, (GLM, [off, authz]), (LARGE, [authz]))
    # Equal severity: more votes first.
    assert [(c.line, c.votes) for c in res.candidates] == [(18, [GLM, LARGE]), (14, [GLM])]


def test_title_overlap_merges_paraphrased_evidence(make) -> None:
    a = make.draft(
        line=23,
        evidence="query built by concatenation",
        title="SQL injection in find query",
        category="security",
    )
    b = make.draft(
        line=24,
        evidence="name is not escaped",
        title="SQL injection through find name",
        category="security",
    )
    res = merged(make, (GLM, [a]), (LARGE, [b]))
    assert len(res.candidates) == 1 and res.candidates[0].votes == [GLM, LARGE]


def test_unrelated_titles_and_evidence_do_not_merge(make) -> None:
    a = make.draft(line=23, evidence="query built by concatenation", title="SQL injection in find")
    b = make.draft(
        line=24, evidence="no error handling", title="Missing error handling for execute"
    )
    assert len(merged(make, (GLM, [a]), (LARGE, [b])).candidates) == 2


def test_same_evidence_far_apart_or_other_hunk_is_not_merged(make) -> None:
    head = {"m.py": ["x = compute(a, b)"] * 40}
    anchors = make.anchors({"m.py": [(1, 10), (12, 30)]})
    d1 = make.draft(path="m.py", line=5, evidence="x = compute(a, b)", title="Arguments swapped")
    far = make.draft(
        path="m.py", line=9 + 0, evidence="x = compute(a, b)", title="Arguments swapped"
    )
    other_hunk = make.draft(
        path="m.py", line=12, evidence="x = compute(a, b)", title="Arguments swapped"
    )
    drafts = [make.review_draft([d1], model=GLM), make.review_draft([far, other_hunk], model=LARGE)]
    res = merge_drafts(drafts, anchors, head.get)
    assert sorted(c.line or 0 for c in res.candidates) == [5, 9, 12]


def test_findings_off_the_diff_merge_on_model_lines(make) -> None:
    a = make.draft(
        path="ledger/money.py",
        line=41,
        evidence="def prorate(amount, days, total):",
        title="Callers pass prorate arguments in the wrong order",
    )
    b = make.draft(
        path="ledger/money.py",
        line=42,
        evidence="def prorate(amount, days, total)",
        title="prorate argument order",
    )
    res = merged(make, (GLM, [a]), (DEEPSEEK, [b]))
    assert len(res.candidates) == 1 and res.candidates[0].votes == [GLM, DEEPSEEK]


def test_duplicates_from_one_model_count_one_vote(make) -> None:
    res = merged(make, (GLM, [make.draft(line=14), make.draft(line=15)]))
    assert len(res.candidates) == 1 and res.candidates[0].votes == [GLM]


def test_suggestion_is_taken_only_for_the_same_lines(make) -> None:
    rep = make.draft(line=14, severity="critical", suggestion=None)
    same = make.draft(line=14, suggestion="        end = start + size")
    shifted = make.draft(
        line=15,
        evidence="return items[start:end]",
        title="Off-by-one in page end index",
        suggestion="        return items[start:end - 1]",
    )
    res = merged(make, (GLM, [rep]), (LARGE, [same]))
    assert res.candidates[0].suggestion == "        end = start + size"
    res2 = merged(make, (GLM, [rep]), (LARGE, [shifted]))
    assert len(res2.candidates) == 1 and res2.candidates[0].suggestion is None


def test_suggestion_of_an_inexactly_snapped_finding_is_not_carried(make) -> None:
    # Line 12 is off; the evidence only roughly matches line 14, where it snaps. The
    # candidate then sits on line 14 as if the model had said so, so merge must apply
    # rule 1 (snapped needs an exact evidence match) itself.
    rough = make.draft(
        line=12,
        evidence="end = start + size + 1  # page end",
        suggestion="        end = start + size",
    )
    res = merged(make, (GLM, [rough]))
    assert res.candidates[0].line == 14 and res.candidates[0].suggestion is None
    exact = make.draft(line=13, suggestion="        end = start + size")
    res2 = merged(make, (GLM, [exact]))
    assert res2.candidates[0].line == 14
    assert res2.candidates[0].suggestion == "        end = start + size"


def test_cap_and_ordering(make) -> None:
    many = [
        make.draft(
            path=f"pkg/m{i}.py",
            line=1,
            evidence=f"value_{i} = {i}",
            title=f"Issue number {i}",
            severity="low" if i % 2 else "high",
        )
        for i in range(70)
    ]
    res = merge_drafts([make.review_draft(many)], make.anchors({}), make.head_text({}))
    assert len(res.candidates) == MAX_CANDIDATES and res.dropped_over_cap == 10
    assert [c.candidate_id for c in res.candidates] == [f"c{i}" for i in range(1, 61)]
    sevs = [c.severity for c in res.candidates]
    assert sevs == sorted(sevs, key=["critical", "high", "medium", "low"].index)
    assert sevs.count("high") == 35


def test_is_deterministic(make) -> None:
    a = [
        make.draft(line=14),
        make.draft(line=18, evidence="if user.id != entry.owner_id:", title="Authz"),
    ]
    b = [make.draft(line=23, evidence="query = ", title="SQL injection")]
    one = merged(make, (GLM, a), (LARGE, b))
    two = merged(make, (GLM, a), (LARGE, b))
    assert one == two


def test_title_similarity_ignores_stopwords() -> None:
    assert title_similarity("Off-by-one in pagination", "Pagination is off by one") >= 0.5
    assert title_similarity("SQL injection in query", "Missing error handling in query") < 0.5
    assert title_similarity("", "anything") == 0.0


def test_draft_models_are_unique(make) -> None:
    drafts = [
        make.review_draft([], model=GLM),
        make.review_draft([], model=None),
        make.review_draft([], model=GLM),
    ]
    assert draft_models(drafts) == [GLM, "explorer-2", f"{GLM}#3"]


# --- properties -----------------------------------------------------------------------

_EVIDENCE = [
    "end = start + size + 1",
    "if user.id != entry.owner_id:",
    "return prorate(days, amount, total_days)",
    "query = ",
    "",
    "unrelated text",
]
_TITLES = ["Off by one", "Inverted check", "SQL injection", "Swapped arguments", "Style"]


@st.composite
def _drafts(draw):
    out = []
    for model in draw(
        st.lists(st.sampled_from([GLM, LARGE, DEEPSEEK]), min_size=1, max_size=3, unique=True)
    ):
        findings = [
            {
                "line": draw(st.none() | st.integers(1, 45)),
                "end_line": draw(st.none() | st.integers(1, 45)),
                "evidence": draw(st.sampled_from(_EVIDENCE)),
                "title": draw(st.sampled_from(_TITLES)),
                "severity": draw(st.sampled_from(["critical", "high", "medium", "low"])),
                "path": draw(st.sampled_from([PY, "./" + PY, "other.py"])),
            }
            for _ in range(draw(st.integers(0, 6)))
        ]
        out.append((model, findings))
    return out


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(spec=_drafts())
def test_merge_properties(make, spec) -> None:
    drafts = [make.review_draft([make.draft(**f) for f in fs], model=m) for m, fs in spec]
    res = merge_drafts(drafts, make.anchors(), make.head_text())
    total = sum(len(fs) for _, fs in spec)
    assert res.input_findings == total and len(res.candidates) <= total
    models = [m for m, _ in spec]
    reporting = {m for m, fs in spec if fs}
    assert {v for c in res.candidates for v in c.votes} == reporting
    for c in res.candidates:
        assert c.votes == [m for m in models if m in c.votes]  # unique, in explorer order
    assert res == merge_drafts(drafts, make.anchors(), make.head_text())
