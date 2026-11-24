"""Rendering (DESIGN 8.5): inline bodies, summary sections, caps and safety properties."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from morning_star_review.core.contract import (
    ExcludedFile,
    FinalFinding,
    ResolvedPrior,
    Suggestion,
)
from morning_star_review.core.sanitize import ZWJ, suggestion_hazards
from morning_star_review.review import render

SHA = "a" * 40


def ff(**kw: Any) -> FinalFinding:
    base: dict[str, Any] = {
        "id": "f0123456789ab",
        "path": "ledger/service.py",
        "line": 14,
        "anchor": "inline",
        "severity": "high",
        "category": "correctness",
        "title": "Off by one",
        "evidence": "end = start + size + 1",
        "explanation": "Pages overlap.",
        "state": "open",
    }
    base.update(kw)
    return FinalFinding(**base)


def view(make, findings=(), **kw: Any) -> render.SummaryInput:
    base: dict[str, Any] = {
        "owner": "octo",
        "repo": "ledger",
        "number": 7,
        "head_sha": SHA,
        "verdict": "findings",
        "verdict_reasons": [],
        "scope": "full",
        "since_sha": None,
        "findings": list(findings),
        "priors": [],
        "inline_min_severity": "medium",
        "coverage": render.Coverage(reviewable=1, covered=1),
        "details": None,
    }
    base.update(kw)
    return render.SummaryInput(**base)


# --- inline bodies --------------------------------------------------------------------


def test_inline_body_basic_layout() -> None:
    body = render.inline_body(ff(fix="Drop the + 1."), "zai-glm-5-3", SHA)
    assert body.startswith(
        "**High · correctness: Off by one**\n\nPages overlap.\n\n**Suggested fix:** Drop the + 1."
    )
    assert body.endswith(
        "<sub>Morning Star Code Review · zai-glm-5-3 · `aaaaaaa` · id `f0123456789ab`</sub>\n"
        "<!-- morning-star-review:finding f0123456789ab -->"
    )


def test_multiline_fix_gets_its_own_paragraph() -> None:
    body = render.inline_body(ff(fix="```python\nx = 1\n```"), "m", SHA)
    assert "**Suggested fix:**\n\n```python\nx = 1\n```" in body


def test_suggestion_block_under_suggested_fix() -> None:
    s = Suggestion(start_line=14, end_line=14, text="        end = start + size  # ``` ok")
    with_fix = render.inline_body(ff(fix="Drop it.", suggestion=s), "m", SHA)
    assert (
        "**Suggested fix:** Drop it.\n\n````suggestion\n        end = start + size  # ``` ok\n````"
        in with_fix
    )
    no_fix = render.inline_body(ff(suggestion=s), "m", SHA)
    assert "**Suggested fix:**\n\n````suggestion\n" in no_fix


def test_no_suggestion_block_without_a_validated_suggestion() -> None:
    body = render.inline_body(ff(fix="Prose only."), "m", SHA)
    assert "suggestion" not in body.replace("Suggested", "")


def test_unconfirmed_tag_and_verifier_note() -> None:
    body = render.inline_body(ff(verify_status="uncertain", votes=["a", "b"]), "a, b", SHA)
    assert body.startswith("**High · correctness: Off by one (unconfirmed)**")
    assert "· not confirmed by the verifier · id " in body
    assert "· confirmed by the verifier · id " in render.inline_body(
        ff(verify_status="confirmed"), "m", SHA
    )


def test_model_text_is_sanitised_in_inline_bodies() -> None:
    f = ff(
        title="ping @admin <!-- x -->",
        explanation="see [here](https://evil.example) #12 @team",
        fix="<img src=x onerror=alert(1)>",
    )
    body = render.inline_body(f, "m", SHA)
    assert f"@{ZWJ}admin" in body and f"@{ZWJ}team" in body and f"#{ZWJ}12" in body
    assert "](https://evil.example)" not in body
    assert body.count("<!--") == 1  # only our marker


def test_finding_marker_only_keeps_safe_id_characters() -> None:
    assert render.finding_marker("f12 --> <b>") == "<!-- morning-star-review:finding f12b -->"


def test_markers_match_what_publish_reads_back() -> None:
    from morning_star_review.github import state

    fid = "f0123456789ab"
    assert render.finding_marker(fid) == state.finding_marker(fid)
    assert state.finding_ids_in(render.inline_body(ff(id=fid), "m", SHA)) == [fid]
    assert render.SUMMARY_MARKER == state.SUMMARY_MARKER


def test_run_facts_cannot_fake_a_marker(make) -> None:
    details = render.RunDetails(
        execution_id="msreview-x",
        run_label="<!-- morning-star-review:state AAAA -->",
        roles=None,
        usage={},
        cost_usd=0.0,
    )
    out = render.summary(view(make, [], details=details))
    assert out.count("<!--") == 1 and "morning-star-review:state" not in out


# --- summary --------------------------------------------------------------------------


def test_verdict_headlines(make) -> None:
    assert (
        render.verdict_headline(view(make, verdict="clean"))
        == "**Verdict: clean** · `aaaaaaa` · full review"
    )
    inc = view(
        make, scope="incremental", since_sha="b" * 40, findings=[ff(), ff(id="f2", severity="low")]
    )
    assert render.verdict_headline(inc) == (
        "**Verdict: 2 open findings (1 high, 1 low)** · `aaaaaaa` · incremental since `bbbbbbb`"
    )
    maybe = ff(id="f9", verify_status="uncertain", state="summary_only")
    # Possible issues alone never make "findings": the verdict follows completeness.
    assert render.verdict_headline(view(make, verdict="clean", findings=[maybe])) == (
        "**Verdict: clean** · No confirmed findings · 1 possible issue (not confirmed)"
        " · `aaaaaaa` · full review"
    )
    assert render.verdict_headline(view(make, verdict="incomplete", findings=[maybe, maybe])) == (
        "**Verdict: incomplete** · 2 possible issues (not confirmed) · `aaaaaaa` · full review"
    )
    both = view(make, findings=[ff(), maybe])
    assert render.verdict_headline(both) == (
        "**Verdict: 1 open finding (1 high)** · 1 possible issue (not confirmed)"
        " · `aaaaaaa` · full review"
    )
    # Unconfirmed but posted inline (2+ votes, high) and carried priors are open findings.
    inline = ff(verify_status="uncertain", votes=["a", "b"])
    carried = ff(id="f8", verify_status="uncertain", state="summary_only", carried=True)
    assert render.verdict_headline(view(make, findings=[inline, carried])).startswith(
        "**Verdict: 2 open findings (2 high)** · `aaaaaaa`"
    )


def test_sections_and_reasons(make) -> None:
    findings = [
        ff(id="f1"),
        ff(id="f2", anchor="outside", line=None, path="x.py", state="summary_only"),
        ff(id="f3", severity="low", state="summary_only"),
        ff(id="f4", state="summary_only"),  # anchored, severe enough: inline cap
        ff(id="f5", state="summary_only", carried=True, explanation=""),
    ]
    closed = [
        make.prior_finding(
            id="p1", title="Fixed bug", path="a.py", state="fixed", fixed_in="e" * 40
        ),
        make.prior_finding(
            id="p3",
            title="Dismissed",
            path="c.py",
            line=None,
            state="dismissed",
            dismissed_by="alice",
            dismiss_reason="intended: | the API allows it",
        ),
        make.prior_finding(id="p4", title="Old", path="d.py", state="dismissed", resolved_by="bob"),
    ]
    text = render.summary(
        view(
            make,
            findings,
            closed=closed,
            verdict="incomplete",
            verdict_reasons=["the verifier did not run"],
            command_notes=["a dismiss command by eve was ignored: only people with write access"],
        )
    )
    assert (
        "The review is incomplete, so it cannot be called clean:\n- the verifier did not run"
        in text
    )
    assert "Commands that did nothing:\n- a dismiss command by eve was ignored" in text
    # One table of every open finding, inline or not, with its id and why it has no thread.
    table = text.split(render.OPEN_HEADING, 1)[1].split("\n\n", 2)[1]
    rows = table.split("\n")
    assert rows[0] == "| ID | Severity | Finding | Location | Status |"
    status = {r.split(" | ", 1)[0][2:]: r.rsplit(" | ", 1)[1][:-2] for r in rows[2:]}
    assert status == {
        "`f1`": "new",
        "`f2`": "new · no inline comment: outside this PR's changed files",
        "`f3`": "new · no inline comment: on a changed line, below the inline threshold (medium)",
        "`f4`": "new · no inline comment: on a changed line, inline comment limit reached",
        "`f5`": "still open · no inline comment",
    }
    # The record of fixed and dismissed findings, collapsed.
    record = text.split(f"<details><summary>{render.CLOSED_SUMMARY} (3)</summary>", 1)[1]
    record = record.split("</details>", 1)[0]
    assert "| `p1` | High | Fixed bug | `a.py:14` | fixed in `eeeeeee` |" in record
    assert (
        "| `p3` | High | Dismissed | `c.py` | dismissed by alice: intended: \\| the API allows it |"
        in record
    )
    assert "| `p4` | High | Old | `d.py:14` | dismissed by bob |" in record
    assert "Other findings" not in text and "Resolved since" not in text


def test_explanations_of_open_findings(make) -> None:
    fixed = ff(id="f1", fix="Drop the + 1.")
    bare = ff(id="f2", explanation="", carried=True, state="summary_only")
    text = render.summary(view(make, [fixed, bare]))
    block = text.split("<details><summary>Explanations and suggested fixes</summary>", 1)[1]
    block = block.split("</details>", 1)[0]
    assert "##### `f1` · High · correctness: Off by one\n\nPages overlap." in block
    assert "**Suggested fix:** Drop the + 1." in block
    assert "`f2`" not in block  # nothing recorded to explain
    # The block stops at its size limit and says how many it left out.
    many = [ff(id=f"f{i}", explanation="x" * 1500) for i in range(20)]
    text = render.summary(view(make, many))
    assert "more explanations not shown here: see the inline comments" in text
    assert len(text) <= render.SUMMARY_LIMIT
    # The report explains every finding in detail instead.
    report = render.report(view(make, [fixed]), lambda _p: None)
    assert "Explanations and suggested fixes" not in report
    assert "##### 1. `f1` · High · correctness: Off by one" in report


def test_headline_of_a_refresh(make) -> None:
    refresh = view(make, [], verdict="clean", scope="carry_over", since_sha=SHA)
    assert render.verdict_headline(refresh) == (
        "**Verdict: clean** · `aaaaaaa` · updated for `/msreview` commands"
    )
    older = view(make, [], verdict="clean", scope="carry_over", since_sha="b" * 40)
    assert render.verdict_headline(older).endswith("no reviewable changes since `bbbbbbb`")


def test_readiness_line(make) -> None:
    low = ff(id="f1", severity="low")
    ready = view(make, [low], ready=True, blocking=0)
    assert render.readiness_line(ready) == (
        "**Ready to merge:** no open findings at medium severity or above "
        "(1 less severe finding still open)."
    )
    assert render.readiness_line(view(make, [], ready=True, verdict="clean")) == (
        "**Ready to merge:** no open findings at medium severity or above."
    )
    blocked = view(make, [ff(), ff(id="f2")], ready=False, blocking=2)
    assert render.readiness_line(blocked) == (
        "**Not ready to merge:** 2 open findings at medium severity or above."
    )
    one = view(make, [ff()], ready=False, blocking=1, blocking_severity="critical")
    assert render.readiness_line(one) == "**Not ready to merge:** 1 open critical finding."
    every = view(make, [], ready=True, blocking_severity="low", verdict="clean")
    assert render.readiness_line(every) == "**Ready to merge:** no open findings."
    incomplete = view(make, [], ready=False, verdict="incomplete")
    assert render.readiness_line(incomplete) == (
        "**Not ready to merge:** the review is incomplete (see below)."
    )
    assert render.readiness_line(view(make, [])) == ""  # unknown: no line
    text = render.summary(blocked)
    assert text.split("\n")[2:4] == [
        "**Verdict: 2 open findings (2 high)** · `aaaaaaa` · full review",
        "**Not ready to merge:** 2 open findings at medium severity or above.",
    ]


def test_footer_says_how_to_dismiss(make) -> None:
    text = render.summary(view(make, [ff()]))
    assert text.endswith(
        "To dismiss a finding, resolve its thread or comment "
        "`/msreview dismiss <id> <reason>` (anyone with write access).</sub>"
    )
    strict = render.summary(view(make, [ff()], author_may_dismiss=False))
    assert strict.endswith("(anyone with write access except the PR author).</sub>")


def test_reasons_for_findings_and_clean_verdicts(make) -> None:
    # An incomplete review with open findings says what it did not cover.
    gap = "1 of 2 reviewable changed files was not opened"
    text = render.summary(
        view(make, [ff(id="f1")], verdict="findings", verdict_reasons=[gap], complete=False)
    )
    assert f"The review did not cover everything, so there may be more findings:\n- {gap}" in text
    assert "cannot be called clean" not in text
    # Notes on a complete review (clean or findings) are listed as notes.
    note = "nothing to review: all 2 changed files are lock files"
    text = render.summary(view(make, verdict="clean", verdict_reasons=[note]))
    assert f"Notes on this review:\n- {note}" in text
    assert "did not cover" not in text and "incomplete" not in text
    assert "Notes on" not in render.summary(view(make, verdict="clean"))


def test_report_open_findings_match_their_detail_lines(make) -> None:
    findings = [
        ff(id="f1"),
        ff(id="f2", anchor="outside", line=None, path="x.py", state="summary_only"),
        ff(id="f3", anchor="file", line=3, path="y.py", state="summary_only"),
        ff(id="f4", severity="low", state="summary_only", line=20),
        ff(id="f5", anchor="snapped", severity="low", state="summary_only", line=30),
    ]
    details = render.RunDetails(
        execution_id="msreview-x", run_label="cli:1", roles=None, usage={}, cost_usd=0.25
    )
    text = render.report(view(make, findings, details=details), lambda _path: None)
    table = text.split(render.OPEN_HEADING, 1)[1].split("\n\n", 2)[1]
    rows = table.split("\n")[2:]
    assert [r.rsplit(" | ", 1)[1] for r in rows] == [
        "new |",
        "new · no inline comment: outside this PR's changed files |",
        "new · no inline comment: in a changed file, not on a changed line |",
        "new · no inline comment: on a changed line, below the inline threshold (medium) |",
        "new · no inline comment: on a changed line, below the inline threshold (medium) |",
    ]
    # Each row's location words open the finding's detail line too.
    assert "`x.py` · outside this PR's changed files" in text
    assert "`y.py:3` · in a changed file, not on a changed line" in text
    assert "`ledger/service.py:20` · on a changed line\n" in text
    assert "`ledger/service.py:30` · on a changed line (moved to" in text
    # The run details appear once, as a collapsible block with its own title.
    assert text.count("Run details") == 1
    assert "<details><summary>Run details</summary>" in text and "#### Run details" not in text


def test_coverage_lists_are_capped(make) -> None:
    excluded = [
        ExcludedFile(path=f"gen/f{i}.js", reason="generated", changed_lines=1) for i in range(30)
    ]
    cov = render.Coverage(
        reviewable=40, covered=10, excluded=excluded, not_opened=[f"s{i}.py" for i in range(30)]
    )
    text = render.summary(view(make, coverage=cov))
    assert "10 of 40 reviewable changed files reviewed." in text
    assert text.count("(generated)") == render.MAX_LISTED_PATHS and ", and 10 more." in text


def test_summary_and_report_fit_their_limits(make) -> None:
    long = "word " * 400
    findings = [
        ff(
            id=f"f{i:012d}",
            title=long[:160],
            explanation=(long * 2)[:2000],
            fix=long[:1000],
            path=f"{'deep/' * 90}f{i}.py",
        )
        for i in range(50)
    ]
    priors = [
        ResolvedPrior(id=f"p{i}", title=long[:160], path="p" * 500, action="resolve", note="n")
        for i in range(60)
    ]
    v = view(make, findings, priors=priors, over_cap=20)
    text = render.summary(v)
    assert len(text) <= render.SUMMARY_LIMIT and text.endswith("</sub>")
    assert "Shortened to fit" in text and text.count("<details>") == text.count("</details>")
    heads = {f.path: ["x" * 5000] * 20 for f in findings}
    rep = render.report(v, heads.get)
    assert len(rep) <= render.REPORT_LIMIT


# --- safety properties ----------------------------------------------------------------

PIECES = [
    "`",
    "``",
    "```",
    "~~~",
    "\\",
    "@u",
    "@org/team",
    "#1",
    "GH-2",
    "x/y#3",
    " ",
    "\n",
    "|",
    "<b>",
    "&#64;",
    "<!--",
    "-->",
    "morning-star-review:",
    "MORNING-STAR-REVIEW :",
    "[a](http://evil.example)",
    "![i](https://evil.example/i.png)",
    "http://evil.example/x",
    "www.evil.example",
    "<https://evil.example>",
    "[r]: https://evil.example",
    "https://github.com/a/b",
    "# h",
    "---\n",
    "x",
    "(",
    ")",
    "[",
    "]",
    "*",
    "_",
]
adversarial = st.lists(st.sampled_from(PIECES), max_size=25).map("".join) | st.text(max_size=60)
nonblank = adversarial.filter(lambda s: bool(s.strip()))


@st.composite
def findings_st(draw: Any) -> FinalFinding:
    path = draw(nonblank)[:500]
    text = draw(adversarial)
    suggestion = None
    if text.strip() and not suggestion_hazards(text) and len(text) <= 2000:
        suggestion = Suggestion(start_line=1, end_line=1, text=text)
    return FinalFinding(
        id="f" + f"{draw(st.integers(0, 2**48 - 1)):012x}",
        path=path,
        line=draw(st.none() | st.integers(1, 50)),
        anchor=draw(st.sampled_from(["inline", "snapped", "file", "outside"])),
        severity=draw(st.sampled_from(["critical", "high", "medium", "low"])),
        category="security",
        title=draw(adversarial)[:160],
        evidence=draw(adversarial)[:500],
        explanation=draw(adversarial)[:2000],
        fix=draw(st.none() | adversarial.map(lambda s: s[:1000])),
        state=draw(st.sampled_from(["open", "summary_only"])),
        carried=draw(st.booleans()),
        comment_id=draw(st.none() | st.integers(1, 10**9)),
        votes=draw(st.lists(adversarial.map(lambda s: s[:100] or "m").filter(bool), max_size=3)),
        verify_status=draw(st.sampled_from([None, "confirmed", "uncertain"])),
        suggestion=suggestion,
    )


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    findings=st.lists(findings_st(), max_size=4),
    prior_title=adversarial,
    login=adversarial,
    excluded=nonblank,
    reason=adversarial,
    head_line=adversarial,
)
def test_outputs_are_safe_for_any_model_text(
    make, findings, prior_title, login, excluded, reason, head_line
) -> None:
    priors = [
        ResolvedPrior(
            id="p1", title=prior_title[:160], path=excluded[:500], action="resolve", note="x"
        ),
        ResolvedPrior(
            id="p2",
            title=prior_title[:160],
            path="a.py",
            action="dismissed",
            note=f"dismissed by {login}"[:300],
        ),
        ResolvedPrior(
            id="p3",
            title="t",
            path="a.py",
            action="keep_open",
            note=f"resolved without a fix by {login}"[:300],
        ),
    ]
    cov = render.Coverage(
        reviewable=2,
        covered=1,
        excluded=[ExcludedFile(path=excluded, reason=reason[:200], changed_lines=1)],
        not_opened=[excluded],
    )
    v = view(
        make,
        findings,
        priors=priors,
        coverage=cov,
        verdict="incomplete",
        verdict_reasons=[reason[:300]],
    )
    heads = {f.path: [head_line] * 60 for f in findings}
    outputs = [render.summary(v), render.report(v, heads.get)]
    outputs += [render.inline_body(f, ", ".join(f.votes), SHA) for f in findings]
    for out in outputs:
        make.assert_safe(out)


def test_reports_carry_no_terminal_control_sequences(make) -> None:
    # Security review TERM-1: paths and code excerpts come from the PR and the CLI
    # prints the report to a terminal.
    osc = "\x1b]52;c;ZWNobyBwd25lZA==\x07"
    path = f"deps/x{osc}.lock"
    excluded = [ExcludedFile(path=path, reason="lock_file", changed_lines=1)]
    cov = render.Coverage(reviewable=1, covered=1, excluded=excluded)
    finding = ff(path=f"src/a{osc}.py", line=2, evidence="x = 1")
    lines = ["import os", f"x = 1  # {osc}\x9b2J\r", "y = 2"]
    v = view(make, [finding], coverage=cov)
    for text in (render.summary(v), render.report(v, lambda _p: lines)):
        assert not any(ch in text for ch in "\x1b\x07\x9b\r"), repr(text)
        assert "�]52;c;" in text


# --- rate-limit waits and the skipped verifier in the run details ----------------------


def details_with(make, usage: dict[str, Any], **kw: Any) -> str:
    details = render.RunDetails(
        execution_id="msreview-x",
        run_label="cli:1",
        roles=make.roles(("zai-glm-5-3", "mistral-large-4"), verifier="zai-glm-5-3"),
        usage=usage,
        cost_usd=0.4,
        duration_seconds=1800.0,
        **kw,
    )
    text = render.summary(view(make, [], details=details))
    return text.split("<details><summary>Run details</summary>", 1)[1]


def table_rows(block: str) -> list[list[str]]:
    lines = [ln for ln in block.split("\n") if ln.startswith("| ") and "---" not in ln]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]


def test_rate_limit_column_is_absent_without_waits(make) -> None:
    usage = {
        "explorer:zai-glm-5-3": make.usage("zai-glm-5-3"),
        "explorer:mistral-large-4": make.usage("mistral-large-4"),
    }
    block = details_with(make, usage)
    assert "Rate-limit wait" not in block and "rate-limit waits" not in block
    header, *rows = table_rows(block)
    assert header[-1] == "Time" and all(len(r) == len(header) for r in rows)
    assert "Estimated cost: $0.40 (list prices) · duration 1800 s\n" in block


def test_rate_limit_column_and_total_when_a_model_waited(make) -> None:
    usage = {
        "explorer:zai-glm-5-3": make.usage(
            "zai-glm-5-3", rate_limit_waits=1, rate_limit_wait_seconds=45.0
        ),
        "explorer:mistral-large-4": make.usage(
            "mistral-large-4", rate_limit_waits=5, rate_limit_wait_seconds=430.4
        ),
        "verifier:zai-glm-5-3": make.usage("zai-glm-5-3"),
    }
    block = details_with(make, usage)
    header, *rows = table_rows(block)
    assert header[-2:] == ["Time", "Rate-limit wait"]
    waits = {(r[0], r[1]): r[-1] for r in rows}
    assert waits == {
        ("explorer", "zai-glm-5-3"): "45s (1x)",
        ("explorer", "mistral-large-4"): "7m 10s (5x)",
        ("verifier", "zai-glm-5-3"): "-",
    }
    assert "· duration 1800 s · rate-limit waits 7m 55s\n" in block


def test_skipped_verifier_is_not_shown_as_nothing_to_verify(make) -> None:
    usage = {"explorer:zai-glm-5-3": make.usage("zai-glm-5-3")}
    ran = details_with(make, usage, verify_counts={"confirmed": 1, "uncertain": 2, "refuted": 1})
    assert "Verifier: 1 confirmed, 2 not confirmed, 1 refuted and dropped" in ran
    skipped = details_with(
        make, usage, verify_counts={"confirmed": 0, "uncertain": 3, "refuted": 0, "not_run": 1}
    )
    assert "Verifier: did not run; 3 not confirmed\n" in skipped
    assert "0 confirmed" not in skipped
