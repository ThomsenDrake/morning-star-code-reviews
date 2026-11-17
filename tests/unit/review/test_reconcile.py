"""Ids, de-duplication, caps, verification policy and prior reconciliation (8.2, 8.3, D)."""

from __future__ import annotations

import itertools

import pytest

from morning_star_review.core.contract import (
    Candidate,
    MergeResult,
    PriorStatus,
    VerifyEntry,
)
from morning_star_review.core.ids import finding_id
from morning_star_review.review.reconcile import (
    Source,
    apply_verification,
    build_findings,
    combine_prior_status,
    reconcile_priors,
    refound_priors,
)

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
PY = "ledger/service.py"
BOT = "morning-star-code-reviews[bot]"


def build(make, sources, cfg=None, **kw):
    return build_findings(
        sources,
        anchors=kw.pop("anchors", make.anchors()),
        head_text=kw.pop("head_text", make.head_text()),
        pr_paths=kw.pop("pr_paths", {PY}),
        cfg=cfg or make.config(),
        **kw,
    )


# --- ids ------------------------------------------------------------------------------


def test_ids_are_stable_across_titles_categories_and_snapping(make) -> None:
    a = make.draft(line=14, title="Off by one", category="correctness")
    b = make.draft(line=12, title="Pages overlap", category="reliability", severity="low")
    fa = build(make, [Source(a)]).findings[0]
    fb = build(make, [Source(b)]).findings[0]
    assert fa.id == fb.id == finding_id(PY, "        end = start + size + 1")
    assert fa.id.startswith("f") and len(fa.id) == 13


def test_ids_off_the_diff_use_evidence_then_title(make) -> None:
    out = make.draft(path="other.py", line=3, evidence="  x = 1 ", title="T")
    no_ev = make.draft(path="other.py", line=3, evidence="", title="Missing check")
    got = build(make, [Source(out), Source(no_ev)]).findings
    assert {f.id for f in got} == {
        finding_id("other.py", "x = 1"),
        finding_id("other.py", "Missing check"),
    }
    assert all(f.anchor == "outside" and f.line is None for f in got)


# --- dedupe and caps ------------------------------------------------------------------


def test_same_id_keeps_higher_severity_and_mentions_the_other(make) -> None:
    low = make.draft(severity="low", title="Pages overlap")
    high = make.draft(line=13 + 1, severity="critical", title="Off-by-one in page end index")
    got = build(make, [Source(low, (LARGE,)), Source(high, (GLM,))]).findings
    assert len(got) == 1
    f = got[0]
    assert f.severity == "critical" and f.title == "Off-by-one in page end index"
    assert f.explanation.endswith("Also: Pages overlap")
    assert f.votes == [GLM, LARGE]


def test_cap_of_50_and_inline_cap(make) -> None:
    head = [f"v{i} = {i}" for i in range(1, 81)]
    anchors = make.anchors({"m.py": [(1, 80)]})
    drafts = [
        make.draft(path="m.py", line=i, evidence=f"v{i} = {i}", title=f"Issue {i}")
        for i in range(1, 61)
    ]
    built = build(
        make,
        [Source(d) for d in drafts],
        anchors=anchors,
        head_text={"m.py": head}.get,
        pr_paths={"m.py"},
    )
    assert len(built.findings) == 50 and built.over_cap == 10
    states = [f.state for f in built.findings]
    assert states.count("open") == 15 and states.count("summary_only") == 35
    assert [f.line for f in built.findings[:15]] == list(range(1, 16))  # sorted by location


def test_below_threshold_and_unanchored_are_summary_only(make) -> None:
    low = make.draft(severity="low")
    off = make.draft(path="other.py", evidence="zzz", title="Elsewhere")
    file_level = make.draft(line=38, evidence="nothing", title="File level")
    got = {f.title: f for f in build(make, [Source(low), Source(off), Source(file_level)]).findings}
    assert got["Off-by-one in page end index"].state == "summary_only"
    assert got["Elsewhere"].state == "summary_only" and got["Elsewhere"].anchor == "outside"
    assert got["File level"].state == "summary_only" and got["File level"].anchor == "file"
    strict = make.config(inline_min_severity="low")
    assert build(make, [Source(low)], cfg=strict).findings[0].state == "open"


def test_suggestions_are_validated_and_drop_reasons_kept(make) -> None:
    good = make.draft(suggestion="        end = start + size")
    empty_like = make.draft(
        line=18,
        evidence="if user.id != entry.owner_id:",
        title="Authz",
        suggestion="        if user.id != entry.owner_id:",
    )
    got = {f.line: f for f in build(make, [Source(good), Source(empty_like)]).findings}
    assert got[14].suggestion is not None and got[14].notes == []
    assert got[18].suggestion is None
    assert got[18].notes == ["suggestion dropped: the replacement does not change the code"]
    off = make.config(suggestions=False)
    assert build(make, [Source(good)], cfg=off).findings[0].suggestion is None


# --- verification policy (ADDENDUM D) -------------------------------------------------


def _merge(make, *specs):
    cands = []
    for i, (draft, votes) in enumerate(specs):
        cands.append(Candidate.from_draft(draft, f"c{i + 1}", list(votes)))
    return MergeResult(
        candidates=cands, explorer_models=[GLM, LARGE, DEEPSEEK], input_findings=len(cands)
    )


def test_publishing_policy(make) -> None:
    confirmed = make.draft(line=14, severity="medium")
    lone = make.draft(line=18, evidence="if user.id != entry.owner_id:", title="Authz inverted")
    pair_high = make.draft(line=23, evidence="query = ", title="SQL injection", severity="high")
    pair_medium = make.draft(
        line=27,
        evidence="return prorate(days, amount, total_days)",
        title="Arguments swapped",
        severity="medium",
    )
    refuted = make.draft(line=30, evidence="return sum(", title="Wrong start value")
    merge = _merge(
        make,
        (confirmed, [GLM]),
        (lone, [GLM]),
        (pair_high, [GLM, LARGE]),
        (pair_medium, [GLM, LARGE]),
        (refuted, [DEEPSEEK]),
    )
    verify = make.verify(
        [
            VerifyEntry(candidate_id="c1", status="confirmed", reason="checked"),
            VerifyEntry(candidate_id="c2", status="uncertain"),
            VerifyEntry(candidate_id="c3", status="uncertain"),
            VerifyEntry(
                candidate_id="c5", status="refuted", reason="sum starts at Decimal(0) on purpose"
            ),
        ]
    )  # c4 never addressed -> uncertain
    sources, dropped = apply_verification(merge, verify, verified=True)
    assert [s.verify_status for s in sources] == [
        "confirmed",
        "uncertain",
        "uncertain",
        "uncertain",
    ]
    assert [(d.candidate_id, d.reason) for d in dropped] == [
        ("c5", "sum starts at Decimal(0) on purpose")
    ]
    got = {f.line: f for f in build(make, sources).findings}
    assert got[14].state == "open" and got[14].verify_status == "confirmed"
    assert got[18].state == "summary_only"  # one vote
    assert got[23].state == "open" and got[23].verify_status == "uncertain"  # 2 votes, high
    assert got[27].state == "summary_only"  # 2 votes but medium
    assert 30 not in got


def test_verifier_corrections_are_applied(make) -> None:
    d = make.draft(line=20, end_line=21, evidence="vague", severity="low")
    merge = _merge(make, (d, [GLM]))
    entry = VerifyEntry(
        candidate_id="c1",
        status="confirmed",
        line=14,
        evidence="end = start + size + 1",
        severity="critical",
    )
    sources, _ = apply_verification(merge, make.verify([entry]), verified=True)
    s = sources[0].draft
    assert (s.line, s.end_line, s.evidence, s.severity) == (
        14,
        15,
        "end = start + size + 1",
        "critical",
    )


def test_suggestion_does_not_follow_a_finding_the_verifier_moved(make) -> None:
    d = make.draft(line=14, suggestion="        end = start + size")
    merge = _merge(make, (d, [GLM]))
    moved = VerifyEntry(
        candidate_id="c1", status="confirmed", line=15, evidence="return items[start:end]"
    )
    sources, _ = apply_verification(merge, make.verify([moved]), verified=True)
    f = build(make, sources).findings[0]
    assert f.line == 15 and f.suggestion is None
    assert f.notes == ["suggestion dropped: the verifier moved the finding to other lines"]
    # Evidence restated for the same line keeps the suggestion.
    same = VerifyEntry(candidate_id="c1", status="confirmed", evidence="end = start + size + 1")
    sources, _ = apply_verification(merge, make.verify([same]), verified=True)
    f = build(make, sources).findings[0]
    assert f.line == 14 and f.suggestion is not None and f.notes == []


def test_missing_verifier_result_makes_everything_uncertain(make) -> None:
    merge = _merge(make, (make.draft(), [GLM]))
    sources, dropped = apply_verification(merge, None, verified=True)
    assert [s.verify_status for s in sources] == ["uncertain"] and dropped == []
    unverified, _ = apply_verification(merge, None, verified=False)
    assert unverified[0].verify_status is None


# --- prior reconciliation (DESIGN 8.3) ------------------------------------------------

PRESENT = "        end = start + size + 1"
_RESOLVERS = {
    "open": {},
    "human_with_write": {
        "thread_resolved": True,
        "resolved_by": "maintainer",
        "resolver_can_dismiss": True,
    },
    "human_without_write": {
        "thread_resolved": True,
        "resolved_by": "drive-by",
        "resolver_can_dismiss": False,
    },
    "bot": {"thread_resolved": True, "resolved_by": BOT, "resolver_can_dismiss": True},
}


def _expected(resolver: str, said: str | None, evidence: str) -> tuple[str, str]:
    if resolver == "human_with_write":
        return "dismissed", "dismissed by maintainer"
    if evidence == "deleted":
        return "file_deleted", "the file was deleted"
    if evidence == "absent":
        if said == "still_open":
            return "keep_open", "the flagged code changed, but the model reports it is still open"
        return "resolve", "the flagged code changed"
    if resolver == "human_without_write":
        return "keep_open", "resolved without a fix by drive-by"
    if said == "fixed":
        return "keep_open", "model reports fixed but the flagged code is unchanged"
    return "keep_open", "the flagged code is unchanged"


@pytest.mark.parametrize(
    ("resolver", "said", "evidence"),
    list(
        itertools.product(
            _RESOLVERS, ["fixed", "still_open", "unsure", None], ["present", "absent", "deleted"]
        )
    ),
)
def test_prior_matrix(make, resolver, said, evidence) -> None:
    p = make.prior_finding(evidence=PRESENT.strip(), **_RESOLVERS[resolver])
    head = {"present": make.heads()[PY], "absent": ["        end = start + size"], "deleted": None}[
        evidence
    ]
    statuses = [PriorStatus(id=p.id, status=said)] if said else []
    got = reconcile_priors(
        make.prior_state([p]), statuses, lambda path: head, make.config(), bot_login=BOT
    )
    assert [(r.action, r.note) for r in got] == [_expected(resolver, said, evidence)]


def test_prior_bookkeeping(make) -> None:
    fixed = make.prior_finding(id="f000000000002", state="fixed")
    dismissed = make.prior_finding(id="f000000000003", state="dismissed")
    renamed = make.prior_finding(id="f000000000004", path="ledger/old.py")
    state = make.prior_state([fixed, dismissed, renamed])
    got = reconcile_priors(state, [], make.head_text(), renamed_from={"ledger/old.py": PY})
    assert [(r.id, r.action) for r in got] == [
        ("f000000000003", "dismissed"),
        ("f000000000004", "keep_open"),
    ]
    assert reconcile_priors(None, [], make.head_text()) == []


def test_unreadable_file_is_not_treated_as_deleted(make) -> None:
    # head_text returns None for binary or oversized files as well as deleted ones.
    p = make.prior_finding()
    state = make.prior_state([p])
    kept = reconcile_priors(state, [], lambda path: None, existing_paths={PY})
    assert [(r.action, r.note) for r in kept] == [
        ("keep_open", "the file could not be read at head")
    ]
    gone = reconcile_priors(state, [], lambda path: None, existing_paths={"other.py"})
    assert [r.action for r in gone] == ["file_deleted"]


def test_long_stored_evidence_still_counts_as_present(make) -> None:
    line = "x = compute(" + ", ".join(f"arg{i}" for i in range(120)) + ")"
    p = make.prior_finding(evidence=line[:300])
    got = reconcile_priors(make.prior_state([p]), [], lambda path: [line])
    assert got[0].action == "keep_open"


def test_combine_prior_status_prefers_still_open(make) -> None:
    d1 = make.review_draft(
        [],
        prior_status=[PriorStatus(id="f1", status="fixed"), PriorStatus(id="f2", status="unsure")],
    )
    d2 = make.review_draft(
        [],
        model=LARGE,
        prior_status=[
            PriorStatus(id="f1", status="still_open"),
            PriorStatus(id="f2", status="fixed"),
        ],
    )
    got = {s.id: s.status for s in combine_prior_status([d1, None, d2])}
    assert got == {"f1": "still_open", "f2": "fixed"}


def test_new_findings_meet_priors(make) -> None:
    fid = finding_id(PY, PRESENT)
    authz_id = finding_id(PY, "        if user.id != entry.owner_id:")
    open_prior = make.prior_finding(id=fid)
    dismissed = make.prior_finding(
        id=authz_id,
        line=18,
        title="Authz",
        evidence="if user.id != entry.owner_id:",
        comment_id=102,
        thread_id="PRRT_2",
        thread_resolved=True,
        resolved_by="maintainer",
        resolver_can_dismiss=True,
    )
    untouched = make.prior_finding(
        id="f00000000000a",
        line=23,
        title="SQL injection",
        comment_id=103,
        thread_id="PRRT_3",
        evidence='query = "SELECT * FROM entries WHERE name',
    )
    summary_only = make.prior_finding(
        id="f00000000000b",
        line=None,
        title="Docs",
        comment_id=None,
        thread_id=None,
        state="summary_only",
        evidence="def describe(self):",
    )
    state = make.prior_state([open_prior, dismissed, untouched, summary_only])
    priors = reconcile_priors(state, [], make.head_text(), bot_login=BOT)
    sources = [
        Source(make.draft()),
        Source(make.draft(line=18, evidence="if user.id != entry.owner_id:", title="Authz")),
    ]
    built = build(make, sources, priors=priors, prior_state=state)
    by_id = {f.id: f for f in built.findings}
    assert built.dropped_dismissed == 1 and authz_id not in by_id
    assert by_id[fid].carried and by_id[fid].thread_id == "PRRT_1" and by_id[fid].comment_id == 101
    assert by_id[fid].state == "open" and by_id[fid].explanation  # re-reported: new text kept
    carried = by_id["f00000000000a"]
    assert carried.carried and carried.state == "open" and carried.thread_id == "PRRT_3"
    assert (
        by_id["f00000000000b"].state == "summary_only" and by_id["f00000000000b"].anchor == "file"
    )
    assert built.reported_ids == frozenset({fid})


def test_a_prior_reported_again_is_not_resolved(make) -> None:
    p = make.prior_finding(id="f00000000000c", evidence="gone from the file")
    priors = reconcile_priors(make.prior_state([p]), [], make.head_text())
    assert priors[0].action == "resolve"
    again = refound_priors(priors, frozenset({"f00000000000c"}))
    assert (again[0].action, again[0].note) == ("keep_open", "reported again in this review")
    assert refound_priors(priors, frozenset()) == priors
