"""Notes grammar parser: canonical notes, live-style deviations, suggestions, priors."""

from __future__ import annotations

import pytest

from morning_star_review.core.contract import DraftFinding, ReviewDraft, VerifyDraft, VerifyEntry
from morning_star_review.core.notes import (
    GRAMMAR_HELP,
    VERDICT_GRAMMAR_HELP,
    is_placeholder_summary,
    looks_like_unformatted_findings,
    parse_notes,
    parse_verdicts,
)

CANONICAL = """\
FINDING
file: ledger/db.py
line: 136
severity: high
category: security
title: SQL injection in find_account
evidence: cur.execute(f"SELECT * FROM accounts WHERE name = '{name}'")
why: The account name comes from the request and is formatted into the SQL text.
A name containing a quote changes the query.
fix: Pass the name as a bound parameter.
END FINDING
FINDING
file: ledger/pagination.py
line: 80
end_line: 82
severity: medium
category: correctness
title: Last page is never returned
evidence: for page in range(1, total_pages):
why: range() stops before total_pages.
fix: Iterate to total_pages + 1.
END FINDING
PRIOR f0123456789ab: fixed - the query now uses parameters
PRIOR f0123456789cd: still_open - the check is still inverted
SUMMARY: Reviewed the four changed files. Two problems found.
REVIEW COMPLETE
"""


def test_canonical_notes_parse_losslessly() -> None:
    p = parse_notes(CANONICAL)
    assert p.marker_count == 2
    assert p.lossless
    assert p.unparsed == []
    assert p.closed_blocks
    assert p.sentinel
    assert p.ends_with_summary
    assert p.looks_complete
    assert not p.no_findings_declared
    first, second = p.findings
    assert first == DraftFinding(
        path="ledger/db.py",
        line=136,
        end_line=None,
        severity="high",
        category="security",
        title="SQL injection in find_account",
        evidence="cur.execute(f\"SELECT * FROM accounts WHERE name = '{name}'\")",
        explanation=(
            "The account name comes from the request and is formatted into the SQL text.\n"
            "A name containing a quote changes the query."
        ),
        fix="Pass the name as a bound parameter.",
        suggestion=None,
    )
    assert (second.line, second.end_line) == (80, 82)
    assert second.category == "correctness"
    assert [(x.id, x.status) for x in p.priors] == [
        ("f0123456789ab", "fixed"),
        ("f0123456789cd", "still_open"),
    ]
    assert p.priors[0].reason == "the query now uses parameters"
    assert p.summary == "Reviewed the four changed files. Two problems found."


def test_parsed_findings_fit_a_review_draft() -> None:
    p = parse_notes(CANONICAL)
    draft = ReviewDraft(
        findings=p.findings,
        prior_status=p.priors,
        summary=p.summary,
        source="parser",
        marker_count=p.marker_count,
    )
    assert len(draft.findings) == 2


def test_grammar_help_documents_every_marker_and_field() -> None:
    for token in (
        "FINDING",
        "END FINDING",
        "file:",
        "line:",
        "end_line:",
        "severity:",
        "category:",
        "title:",
        "evidence:",
        "why:",
        "fix:",
        "suggestion:",
        "<<<",
        ">>>",
        "PRIOR <id>:",
        "NO FINDINGS",
        "SUMMARY:",
        "REVIEW COMPLETE",
    ):
        assert token in GRAMMAR_HELP


def test_grammar_help_example_round_trips_through_the_parser() -> None:
    example = GRAMMAR_HELP.split("For each problem, write one block:")[1].split("Rules:")[0]
    filled = (
        example.replace("<path of the file in the repository>", "a.py")
        .replace("<line number in the new version of the file>", "3")
        .replace("<last line, only when the problem spans several lines>", "4")
        .replace("critical | high | medium | low", "low")
        .replace("security | correctness | reliability | performance | other", "other")
    )
    p = parse_notes(filled)
    assert p.marker_count == 1
    assert len(p.findings) == 1
    assert p.findings[0].path == "a.py"
    assert (p.findings[0].line, p.findings[0].end_line) == (3, 4)


# --------------------------------------------------------------------------------------
# Tolerance: decoration, numbering, aliases, ranges, case
# --------------------------------------------------------------------------------------

BOLD_LIST = """\
Here is what I found.

1. **FINDING 1**
   - **file:** `src/app/handlers.py`
   - **line:** `45`
   - **severity:** **High**
   - **category:** Security
   - **title:** Authorization check is inverted
   - **evidence:** `if user.is_admin:`
   - **why:** Admins are rejected and everyone else is let through.
   - **fix:** Negate the condition.
   **END FINDING**

**SUMMARY:** One issue in the handlers.

**REVIEW COMPLETE**
"""


def test_bold_markers_and_list_bullets() -> None:
    p = parse_notes(BOLD_LIST)
    assert p.lossless and p.marker_count == 1
    f = p.findings[0]
    assert f.path == "src/app/handlers.py"
    assert f.line == 45
    assert f.severity == "high"
    assert f.category == "security"
    assert f.evidence == "if user.is_admin:"
    assert f.explanation == "Admins are rejected and everyone else is let through."
    assert p.sentinel and p.closed_blocks
    assert p.summary == "One issue in the handlers."


@pytest.mark.parametrize(
    "marker",
    [
        "FINDING",
        "FINDING 2",
        "FINDING 2:",
        "FINDING #3",
        "### FINDING 4",
        "## Finding 5:",
        "- FINDING",
        "**Finding 1**",
        "`FINDING`",
        "> FINDING",
        "finding:",
    ],
)
def test_finding_marker_variants(marker: str) -> None:
    p = parse_notes(f"{marker}\nfile: a.py\nline: 1\nseverity: low\ntitle: t\nEND FINDING\n")
    assert p.marker_count == 1
    assert len(p.findings) == 1
    assert p.findings[0].title == "t"


@pytest.mark.parametrize(
    "line",
    [
        "Finding the root cause took a while.",
        "Findings are listed below.",
        "FINDINGS",
        "The FINDING marker is described above.",
    ],
)
def test_prose_that_mentions_findings_is_not_a_marker(line: str) -> None:
    assert parse_notes(line).marker_count == 0


def _one(block_body: str) -> DraftFinding:
    p = parse_notes(f"FINDING\n{block_body}\nEND FINDING\nREVIEW COMPLETE\n")
    assert p.marker_count == 1
    assert len(p.findings) == 1, p.unparsed
    return p.findings[0]


@pytest.mark.parametrize(
    ("line_value", "expected"),
    [
        ("80", (80, None)),
        ("80-82", (80, 82)),
        ("80 - 82", (80, 82)),
        ("80\u201382", (80, 82)),  # en dash
        ("80\u201482", (80, 82)),  # em dash
        ("80..82", (80, 82)),
        ("80 to 82", (80, 82)),
        ("L80", (80, None)),
        ("L80-L82", (80, 82)),
        ("82-80", (80, 82)),
        ("80-80", (80, None)),
        ("`80`", (80, None)),
        ("~80 (approximately)", (80, None)),
        ("n/a", (None, None)),
        ("0", (None, None)),
        ("", (None, None)),
    ],
)
def test_line_values(line_value: str, expected: tuple[int | None, int | None]) -> None:
    f = _one(f"file: a.py\nline: {line_value}\nseverity: low\ntitle: t")
    assert (f.line, f.end_line) == expected


def test_end_line_field_and_aliases() -> None:
    f = _one("file: a.py\nline: 10\nend line: 12\nseverity: low\ntitle: t")
    assert (f.line, f.end_line) == (10, 12)
    f = _one("file: a.py\nline: 10\nend_line: 10\nseverity: low\ntitle: t")
    assert (f.line, f.end_line) == (10, None)
    f = _one("file: a.py\nline: 10\nend_line: 4\nseverity: low\ntitle: t")
    assert (f.line, f.end_line) == (10, None)


@pytest.mark.parametrize(
    ("file_value", "expected"),
    [
        ("src/a.py", ("src/a.py", None, None)),
        ("`src/a.py`", ("src/a.py", None, None)),
        ("./src/a.py", ("src/a.py", None, None)),
        ("/src/a.py", ("src/a.py", None, None)),
        ("src/a.py:12", ("src/a.py", 12, None)),
        ("src/a.py:12-14", ("src/a.py", 12, 14)),
        ("`src/a.py:12`", ("src/a.py", 12, None)),
        ("src/a.py#L12", ("src/a.py", 12, None)),
        ("src/a.py#L12-L14", ("src/a.py", 12, 14)),
        ("src/a.py (line 12)", ("src/a.py", 12, None)),
        ("src/a.py, line 12", ("src/a.py", 12, None)),
        ("src/my file.py", ("src/my file.py", None, None)),
    ],
)
def test_file_values_and_embedded_lines(
    file_value: str, expected: tuple[str, int | None, int | None]
) -> None:
    f = _one(f"file: {file_value}\nseverity: low\ntitle: t")
    assert (f.path, f.line, f.end_line) == expected


def test_line_s_key() -> None:
    f = _one("File(s): a.py\nLine(s): 80-82\nseverity: low\ntitle: t")
    assert (f.path, f.line, f.end_line) == ("a.py", 80, 82)


def test_explicit_line_wins_over_line_in_path() -> None:
    f = _one("file: src/a.py:12\nline: 40\nseverity: low\ntitle: t")
    assert (f.path, f.line) == ("src/a.py", 40)


@pytest.mark.parametrize("key", ["file", "path", "Path", "location", "File Path", "filename"])
def test_path_aliases(key: str) -> None:
    assert _one(f"{key}: x/y.go\nseverity: low\ntitle: t").path == "x/y.go"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("critical", "critical"),
        ("CRITICAL", "critical"),
        ("High", "high"),
        ("**high**", "high"),
        ("medium (data loss is unlikely)", "medium"),
        ("Low", "low"),
        ("major", "high"),
        ("minor", "low"),
        ("moderate", "medium"),
        ("blocker", "critical"),
        ("nit", "low"),
    ],
)
def test_severity_values(value: str, expected: str) -> None:
    assert _one(f"file: a.py\nseverity: {value}\ntitle: t").severity == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("security", "security"),
        ("Correctness", "correctness"),
        ("bug", "correctness"),
        ("logic error", "correctness"),
        ("perf", "performance"),
        ("concurrency", "reliability"),
        ("style", "other"),
        ("something new", "other"),
    ],
)
def test_category_values(value: str, expected: str) -> None:
    f = _one(f"file: a.py\nseverity: low\ncategory: {value}\ntitle: t")
    assert f.category == expected


def test_missing_category_defaults_to_other() -> None:
    assert _one("file: a.py\nseverity: low\ntitle: t").category == "other"


def test_severity_from_marker_title_when_field_is_missing() -> None:
    p = parse_notes("FINDING 1 (HIGH): Token is logged\nfile: a.py\nline: 3\nEND FINDING\n")
    assert p.findings[0].severity == "high"
    assert p.findings[0].title == "Token is logged"
    p = parse_notes("FINDING\nfile: a.py\ntitle: [critical] Remote code execution\nEND FINDING")
    assert p.findings[0].severity == "critical"
    assert p.findings[0].title == "Remote code execution"


def test_title_from_marker_line() -> None:
    p = parse_notes("FINDING 2: Off-by-one in pagination\npath: p.py:80\nseverity: medium\n")
    assert p.findings[0].title == "Off-by-one in pagination"


def test_title_falls_back_to_first_sentence_of_why() -> None:
    f = _one("file: a.py\nseverity: low\nwhy: The cache never expires. It grows forever.")
    assert f.title == "The cache never expires."
    assert f.explanation == "The cache never expires. It grows forever."


def test_explanation_falls_back_to_title() -> None:
    assert _one("file: a.py\nseverity: low\ntitle: Leaks a file handle").explanation == (
        "Leaks a file handle"
    )


@pytest.mark.parametrize(
    "key", ["why", "explanation", "Description", "problem", "impact", "details", "reason"]
)
def test_why_aliases(key: str) -> None:
    f = _one(f"file: a.py\nseverity: low\ntitle: t\n{key}: it breaks")
    assert f.explanation == "it breaks"


@pytest.mark.parametrize("key", ["fix", "recommendation", "Suggested fix", "remediation"])
def test_fix_aliases(key: str) -> None:
    assert _one(f"file: a.py\nseverity: low\ntitle: t\n{key}: do this").fix == "do this"


def test_multiline_why_and_fix_are_dedented_but_code_fences_kept() -> None:
    f = _one(
        "file: a.py\nseverity: low\ntitle: t\n"
        "why: first line\n    second line\n  third line\n"
        "fix: replace it:\n```python\n    return x + 1\n```\nthen test."
    )
    assert f.explanation == "first line\nsecond line\nthird line"
    assert f.fix == "replace it:\n```python\n    return x + 1\n```\nthen test."


def test_unknown_keys_inside_why_are_prose() -> None:
    f = _one("file: a.py\nseverity: low\ntitle: t\nwhy: it fails.\nNote: only on Windows.")
    assert f.explanation == "it fails.\nNote: only on Windows."


def test_second_evidence_like_key_inside_why_stays_prose() -> None:
    f = _one("file: a.py\nseverity: low\ntitle: t\nevidence: x = 1\nwhy: bad.\nCode: also y.")
    assert f.evidence == "x = 1"
    assert f.explanation == "bad.\nCode: also y."


# --------------------------------------------------------------------------------------
# Block boundaries, sentinel, completeness
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminator",
    [
        "FINDING\nfile: b.py\nseverity: low\ntitle: second\nEND FINDING",
        "SUMMARY: done",
        "PRIOR f00000000001: fixed - gone",
        "REVIEW COMPLETE",
        "NO FINDINGS",
    ],
)
def test_missing_end_finding_closes_at_the_next_structure(terminator: str) -> None:
    p = parse_notes(f"FINDING\nfile: a.py\nseverity: high\ntitle: first\nwhy: w\n{terminator}\n")
    assert p.findings[0].title == "first"
    assert p.findings[0].explanation == "w"
    assert not p.closed_blocks
    assert not p.looks_complete


def test_missing_end_finding_at_end_of_text() -> None:
    p = parse_notes("FINDING\nfile: a.py\nseverity: high\ntitle: t\nwhy: w")
    assert len(p.findings) == 1 and p.lossless
    assert not p.closed_blocks and not p.sentinel


def test_end_finding_variants() -> None:
    for end in ("END FINDING", "**END FINDING**", "End finding.", "END_FINDING", "END OF FINDING"):
        p = parse_notes(f"FINDING\nfile: a.py\nseverity: low\ntitle: t\n{end}\nREVIEW COMPLETE")
        assert p.closed_blocks, end


def test_stray_end_finding_is_ignored() -> None:
    p = parse_notes("END FINDING\nNO FINDINGS\nSUMMARY: nothing\nREVIEW COMPLETE")
    assert p.marker_count == 0 and p.closed_blocks and p.no_findings_declared


def test_sentinel_missing_but_summary_paragraph_after_last_block() -> None:
    # Seen live: a complete review that omitted REVIEW COMPLETE.
    p = parse_notes(CANONICAL.replace("REVIEW COMPLETE\n", ""))
    assert not p.sentinel
    assert p.ends_with_summary
    assert p.looks_complete


def test_trailing_prose_after_last_block_counts_as_summary() -> None:
    p = parse_notes("FINDING\nfile: a.py\nseverity: low\ntitle: t\nEND FINDING\n\nAll good.")
    assert p.ends_with_summary and p.summary == "All good."
    assert p.looks_complete


def test_notes_that_stop_inside_a_block_are_not_complete() -> None:
    p = parse_notes("SUMMARY: so far ok\nFINDING\nfile: a.py\nseverity: low\ntitle: t\n")
    assert not p.ends_with_summary
    assert not p.looks_complete


def test_finding_after_sentinel_clears_it() -> None:
    p = parse_notes("REVIEW COMPLETE\nFINDING\nfile: a.py\nseverity: low\ntitle: t\nEND FINDING")
    assert not p.sentinel
    assert len(p.findings) == 1


def test_no_findings_declared() -> None:
    p = parse_notes("NO FINDINGS\nSUMMARY: Checked the two files; nothing wrong.\nREVIEW COMPLETE")
    assert p.no_findings_declared
    assert p.findings == [] and p.marker_count == 0 and p.lossless
    assert p.looks_complete
    for variant in ("No findings.", "**NO FINDINGS**", "no new findings", "No issues found."):
        assert parse_notes(variant).no_findings_declared, variant


def test_empty_notes() -> None:
    p = parse_notes("")
    assert p.findings == [] and p.summary == "" and not p.looks_complete
    assert p.lossless  # nothing to lose; the caller still checks no_findings_declared


def test_crlf_line_endings() -> None:
    p = parse_notes(CANONICAL.replace("\n", "\r\n"))
    assert len(p.findings) == 2 and p.sentinel and p.lossless


def test_summary_spans_lines_and_preamble_is_fallback() -> None:
    p = parse_notes("SUMMARY:\nLine one.\nLine two.\nREVIEW COMPLETE\nignored chatter")
    assert p.summary == "Line one.\nLine two."
    assert parse_notes("I checked everything carefully.").summary == (
        "I checked everything carefully."
    )


# --------------------------------------------------------------------------------------
# Unparseable blocks
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "severity: high\ntitle: no file",
        "file: a.py\ntitle: no severity",
        "file: a.py\nseverity: extreme\ntitle: unknown severity",
        "file: a.py\nseverity: low",
        "This block is free text about a.py line 12 being wrong.",
    ],
)
def test_blocks_missing_required_fields_are_unparsed(body: str) -> None:
    p = parse_notes(f"FINDING\n{body}\nEND FINDING\nREVIEW COMPLETE")
    assert p.marker_count == 1
    assert p.findings == []
    assert len(p.unparsed) == 1
    assert p.unparsed[0].startswith("FINDING")
    assert not p.lossless


def test_unparsed_blocks_are_capped() -> None:
    p = parse_notes("FINDING\n" + "x" * 5000 + "\nEND FINDING")
    assert len(p.unparsed[0]) <= 2000


def test_long_fields_are_capped_to_contract_limits() -> None:
    f = _one(
        "file: a.py\nseverity: low\ntitle: "
        + "t" * 400
        + "\nevidence: "
        + "e" * 900
        + "\nwhy: "
        + "w" * 9000
        + "\nfix: "
        + "f" * 3000
    )
    assert len(f.title) <= 160 and len(f.evidence) <= 500
    assert len(f.explanation) <= 4000 and f.fix is not None and len(f.fix) <= 2000


def test_overlong_path_is_unparsed() -> None:
    p = parse_notes("FINDING\nfile: " + "a/" * 300 + "x.py\nseverity: low\ntitle: t\nEND FINDING")
    assert p.findings == [] and len(p.unparsed) == 1


def test_parser_never_raises_on_noise() -> None:
    import random

    rng = random.Random(7)
    pieces = [
        "FINDING",
        "END FINDING",
        "file: a.py",
        "line: 9-",
        "severity: high",
        "suggestion:",
        "<<<",
        ">>>",
        "```",
        "PRIOR f1: ???",
        "SUMMARY:",
        "NO FINDINGS",
        "REVIEW COMPLETE",
        "**",
        "",
        "why: x",
        "  indented",
    ]
    for _ in range(500):
        text = "\n".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
        p = parse_notes(text)
        assert p.marker_count >= len(p.findings)
        assert p.marker_count == len(p.findings) + sum(
            1 for u in p.unparsed if not u.startswith("PRIOR")
        )


# --------------------------------------------------------------------------------------
# Suggestion blocks (ADDENDUM E)
# --------------------------------------------------------------------------------------

_HEAD = "file: a.py\nline: 10\nend_line: 11\nseverity: medium\ntitle: t\n"


def test_suggestion_keeps_exact_indentation_and_inner_blank_lines() -> None:
    p = parse_notes(
        "FINDING\n" + _HEAD + "suggestion:\n<<<\n    if x:\n\n        return y\n>>>\n"
        "END FINDING\nREVIEW COMPLETE"
    )
    assert p.findings[0].suggestion == "    if x:\n\n        return y"
    assert p.dropped_suggestions == 0
    assert p.lossless and p.closed_blocks


def test_suggestion_lines_are_not_parsed_as_fields_or_markers() -> None:
    body = "suggestion:\n<<<\nfile: other.py\n# FINDING\nseverity = 'high'\n>>>\nwhy: w"
    f = _one(_HEAD + body)
    assert f.path == "a.py" and f.severity == "medium"
    assert f.suggestion == "file: other.py\n# FINDING\nseverity = 'high'"
    assert f.explanation == "w"


@pytest.mark.parametrize(
    "body",
    [
        "suggestion:\n<<<\n>>>",
        "suggestion:\n<<<\n   \n\n>>>",
        "suggestion: <<< >>>",
    ],
)
def test_empty_suggestion_is_dropped(body: str) -> None:
    p = parse_notes("FINDING\n" + _HEAD + body + "\nEND FINDING")
    assert p.findings[0].suggestion is None
    assert p.dropped_suggestions == 1


def test_suggestion_none_is_not_a_suggestion() -> None:
    p = parse_notes("FINDING\n" + _HEAD + "suggestion: none\nEND FINDING")
    assert p.findings[0].suggestion is None and p.dropped_suggestions == 0


def test_unterminated_suggestion_is_dropped_but_finding_kept() -> None:
    p = parse_notes("FINDING\n" + _HEAD + "suggestion:\n<<<\n    x = 1\nEND FINDING\nNO FINDINGS")
    assert len(p.findings) == 1
    assert p.findings[0].suggestion is None
    assert p.dropped_suggestions == 1
    assert p.closed_blocks


def test_suggestion_on_the_same_line() -> None:
    assert _one(_HEAD + "suggestion: <<< return x >>>").suggestion == " return x"
    assert _one(_HEAD + "suggestion: <<<\n  return x\n>>>").suggestion == "  return x"


def test_suggestion_in_a_code_fence_is_accepted() -> None:
    f = _one(_HEAD + "suggestion:\n```python\n    return x\n```\nfix: do it")
    assert f.suggestion == "    return x"
    assert f.fix == "do it"


def test_prose_suggestion_becomes_the_fix() -> None:
    assert _one(_HEAD + "suggestion: Use a parameterized query.").fix == (
        "Use a parameterized query."
    )
    f = _one(_HEAD + "suggestion:\nUse a lock around the counter.")
    assert f.suggestion is None and f.fix == "Use a lock around the counter."


def test_only_the_first_suggestion_counts() -> None:
    p = parse_notes(
        "FINDING\n" + _HEAD + "suggestion:\n<<<\none\n>>>\nsuggestion:\n<<<\ntwo\n>>>\nEND FINDING"
    )
    assert p.findings[0].suggestion == "one"
    assert p.dropped_suggestions == 1


def test_overlong_suggestion_is_dropped() -> None:
    p = parse_notes("FINDING\n" + _HEAD + "suggestion:\n<<<\n" + "x" * 2500 + "\n>>>\nEND FINDING")
    assert p.findings[0].suggestion is None and p.dropped_suggestions == 1


# --------------------------------------------------------------------------------------
# PRIOR lines
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PRIOR f0123456789a: fixed - removed", ("f0123456789a", "fixed", "removed")),
        ("PRIOR f0123456789a: still_open - same code", ("f0123456789a", "still_open", "same code")),
        ("PRIOR f0123456789a: still open \u2013 same", ("f0123456789a", "still_open", "same")),
        ("PRIOR f0123456789a: Not fixed", ("f0123456789a", "still_open", "")),
        ("PRIOR f0123456789a: unsure - cannot tell", ("f0123456789a", "unsure", "cannot tell")),
        ("PRIOR #f0123456789a \u2014 Resolved (moved)", ("f0123456789a", "fixed", "moved")),
        ("- **PRIOR f0123456789a**: fixed", ("f0123456789a", "fixed", "")),
        ("PRIOR `f0123456789a`: fixed", ("f0123456789a", "fixed", "")),
        ("prior f0123456789a: status: unclear", ("f0123456789a", "unsure", "")),
    ],
)
def test_prior_lines(line: str, expected: tuple[str, str, str]) -> None:
    p = parse_notes(line)
    assert [(x.id, x.status, x.reason) for x in p.priors] == [expected]
    assert p.unparsed == []


def test_prior_last_line_for_an_id_wins() -> None:
    p = parse_notes("PRIOR f1: fixed\nPRIOR f2: unsure\nPRIOR f1: still_open - re-checked")
    assert [(x.id, x.status) for x in p.priors] == [("f2", "unsure"), ("f1", "still_open")]


def test_unreadable_prior_line_is_unparsed() -> None:
    p = parse_notes("PRIOR f0123456789a: maybe later\nNO FINDINGS")
    assert p.priors == [] and p.unparsed == ["PRIOR f0123456789a: maybe later"]
    assert not p.lossless


def test_prose_starting_with_prior_is_not_a_prior_line() -> None:
    p = parse_notes("Prior to this change the input was trusted.\nNO FINDINGS")
    assert p.priors == [] and p.unparsed == []


# --------------------------------------------------------------------------------------
# Live-style notes (written for this suite; shapes seen from the four models)
# --------------------------------------------------------------------------------------

MESSY = """\
I reviewed `ledger/service.py`, `ledger/money.py` and `ledger/db.py`.

## Findings

### FINDING 1: Arguments to prorate() are swapped
- path: ledger/service.py
- line: 103
- severity: HIGH
- category: correctness
- evidence: `amount = prorate(days_used, total, days_in_period)`
- why: `prorate(total, days_used, days_in_period)` is the signature in ledger/money.py:41,
  so the first two arguments are swapped and the customer is charged the wrong amount.
- fix: Call `prorate(total, days_used, days_in_period)`.

### FINDING 2: Off-by-one when computing the last page
- path: ledger/pagination.py
- lines: 80-82
- severity: Medium
- category: Correctness
- evidence: `last = total // size`
- why: When total is not a multiple of size the final partial page is dropped.

PRIOR f3a9c0d1e2b4: still open - the handler still checks `not is_admin`

Summary: Two problems in the billing path; the authz check from the last review is
still inverted.
"""


def test_messy_live_style_notes() -> None:
    p = parse_notes(MESSY)
    assert p.marker_count == 2 and p.lossless
    first, second = p.findings
    assert (first.path, first.line, first.severity) == ("ledger/service.py", 103, "high")
    assert first.title == "Arguments to prorate() are swapped"
    assert first.evidence == "amount = prorate(days_used, total, days_in_period)"
    assert "ledger/money.py:41" in first.explanation
    assert first.explanation.count("\n") == 1  # continuation line joined, indentation gone
    assert (second.line, second.end_line) == (80, 82)
    assert p.priors[0].status == "still_open"
    assert p.summary.startswith("Two problems in the billing path")
    assert not p.sentinel and not p.closed_blocks  # no END FINDING lines
    assert p.ends_with_summary


def test_evidence_in_a_fenced_block() -> None:
    f = _one("file: a.py\nline: 2\nseverity: low\ntitle: t\nevidence:\n```python\n  x = y\n```")
    assert f.evidence == "x = y"  # surrounding whitespace is not significant (ids.norm)


# --------------------------------------------------------------------------------------
# looks_like_unformatted_findings and placeholder summaries
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notes",
    [
        "The query in ledger/db.py:136 is injectable.",
        "See src/app.ts#L40 for the race.",
        "handlers.py (line 45) inverts the check",
        "Severity: high. The check is inverted.",
        "**High**: the check is inverted",
        "[critical] - remote code execution",
    ],
)
def test_unformatted_findings_are_detected(notes: str) -> None:
    assert looks_like_unformatted_findings(notes)
    p = parse_notes(notes)
    assert p.findings == [] and not p.no_findings_declared


@pytest.mark.parametrize(
    "notes",
    [
        "",
        "NO FINDINGS\nSUMMARY: I read the diff and the callers; nothing is wrong.",
        "The change renames a helper and updates its docstring.",
        CANONICAL,  # locations inside FINDING blocks do not count
    ],
)
def test_clean_notes_do_not_look_unformatted(notes: str) -> None:
    assert not looks_like_unformatted_findings(notes)


@pytest.mark.parametrize(
    "summary",
    [
        "",
        "  ",
        "LGTM",
        "lgtm!",
        "LGTM, ship it",
        "Placeholder",
        "placeholder summary",
        "N/A",
        "TBD",
    ],
)
def test_placeholder_summaries(summary: str) -> None:
    assert is_placeholder_summary(summary)


@pytest.mark.parametrize(
    "summary",
    [
        "Reviewed db.py and handlers.py; the SQL is parameterized and the check is correct.",
        "LGTM after checking all callers of prorate(): argument order matches the signature in "
        "money.py and the tests cover the partial page case.",
    ],
)
def test_real_summaries_are_not_placeholders(summary: str) -> None:
    assert not is_placeholder_summary(summary)


# --------------------------------------------------------------------------------------
# VERDICT grammar (verify phase, ADDENDUM D)
# --------------------------------------------------------------------------------------

IDS = ["c1", "c2", "c3", "c4"]

VERDICTS = """\
VERDICT c1
status: confirmed
reason: db.py:136 formats the name into the SQL string; I read the caller.
END VERDICT
VERDICT c2
status: refuted
reason: The loop runs to total_pages + 1, so the last page is returned.
END VERDICT
VERDICT c3
status: uncertain
line: 47
evidence: if not user.is_admin:
severity: medium
reason: The check looks inverted but a decorator may guard the route.
END VERDICT
VERIFY COMPLETE
"""


def test_canonical_verdicts() -> None:
    v = parse_verdicts(VERDICTS, IDS)
    assert v.marker_count == 3
    assert not v.lossy and v.sentinel and v.closed_blocks
    assert [e.status for e in v.entries] == ["confirmed", "refuted", "uncertain"]
    third = v.entries[2]
    assert third == VerifyEntry(
        candidate_id="c3",
        status="uncertain",
        line=47,
        evidence="if not user.is_admin:",
        severity="medium",
        reason="The check looks inverted but a decorator may guard the route.",
    )
    assert v.entries[0].line is None and v.entries[0].evidence is None
    assert v.missing(IDS) == ["c4"]
    VerifyDraft(entries=v.entries)  # fits the formatter schema


def test_verdict_grammar_help_round_trips() -> None:
    assert "VERDICT <candidate id>" in VERDICT_GRAMMAR_HELP
    assert "VERIFY COMPLETE" in VERDICT_GRAMMAR_HELP
    for token in ("status:", "line:", "evidence:", "severity:", "reason:", "END VERDICT"):
        assert token in VERDICT_GRAMMAR_HELP


@pytest.mark.parametrize(
    "marker",
    [
        "VERDICT c2",
        "**VERDICT c2**",
        "### VERDICT c2",
        "VERDICT: c2",
        "Verdict for c2",
        "VERDICT C2",
    ],
)
def test_verdict_marker_variants(marker: str) -> None:
    v = parse_verdicts(f"{marker}\nstatus: refuted\nreason: r\nEND VERDICT", IDS)
    assert [(e.candidate_id, e.status) for e in v.entries] == [("c2", "refuted")]


def test_status_on_the_marker_line_and_numeric_ids() -> None:
    v = parse_verdicts("VERDICT c1: confirmed\nreason: yes\nVERDICT 2 - refuted\n", IDS)
    assert [(e.candidate_id, e.status) for e in v.entries] == [
        ("c1", "confirmed"),
        ("c2", "refuted"),
    ]
    assert not v.closed_blocks and not v.sentinel


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Confirmed", "confirmed"),
        ("**CONFIRMED**", "confirmed"),
        ("valid", "confirmed"),
        ("true positive", "confirmed"),
        ("refuted", "refuted"),
        ("false positive", "refuted"),
        ("not a bug", "refuted"),
        ("invalid - the guard exists", "refuted"),
        ("uncertain", "uncertain"),
        ("unsure", "uncertain"),
        ("inconclusive", "uncertain"),
        ("can't confirm", "uncertain"),
    ],
)
def test_verdict_status_aliases(value: str, expected: str) -> None:
    v = parse_verdicts(f"VERDICT c1\nstatus: {value}\nEND VERDICT", IDS)
    assert v.entries[0].status == expected


def test_verdict_field_named_verdict_is_the_status_not_a_marker() -> None:
    v = parse_verdicts("VERDICT c1\nverdict: confirmed\nreason: r\nEND VERDICT", IDS)
    assert v.marker_count == 1 and v.entries[0].status == "confirmed"


@pytest.mark.parametrize("value", ["unchanged", "same", "n/a", "none", "-", ""])
def test_correction_fields_saying_unchanged_are_none(value: str) -> None:
    v = parse_verdicts(
        f"VERDICT c1\nstatus: confirmed\nline: {value}\nevidence: {value}\nseverity: {value}\n",
        IDS,
    )
    e = v.entries[0]
    assert (e.line, e.evidence, e.severity) == (None, None, None)


def test_multiline_reason_and_reason_without_key() -> None:
    v = parse_verdicts(
        "VERDICT c1\nstatus: refuted\nreason: first.\n  second.\nVERDICT c2\nstatus: confirmed\n"
        "The caller passes user input directly.\n",
        IDS,
    )
    assert v.entries[0].reason == "first.\nsecond."
    assert v.entries[1].reason == "The caller passes user input directly."


def test_unparseable_verdict_is_lossy() -> None:
    v = parse_verdicts("VERDICT c1\nstatus: probably\nEND VERDICT\nVERIFY COMPLETE", IDS)
    assert v.entries == [] and len(v.unparsed) == 1 and v.lossy
    v = parse_verdicts("VERDICT c1\nreason: no status\nEND VERDICT", IDS)
    assert v.lossy


def test_unknown_ids_are_reported_and_dropped() -> None:
    v = parse_verdicts("VERDICT c9\nstatus: confirmed\nEND VERDICT", IDS)
    assert v.entries == [] and v.unknown_ids == ["c9"] and not v.lossy
    loose = parse_verdicts("VERDICT c9\nstatus: confirmed\nEND VERDICT")
    assert [e.candidate_id for e in loose.entries] == ["c9"]  # no id list: keep everything


def test_duplicate_verdicts_last_wins() -> None:
    v = parse_verdicts(
        "VERDICT c1\nstatus: uncertain\nEND VERDICT\nVERDICT c1\nstatus: confirmed\nEND VERDICT",
        IDS,
    )
    assert [(e.candidate_id, e.status) for e in v.entries] == [("c1", "confirmed")]
    assert v.duplicate_ids == ["c1"]


def test_prose_verdicts_without_blocks_are_unformatted() -> None:
    v = parse_verdicts("c1 is confirmed. c2 looks like a false positive.", IDS)
    assert v.marker_count == 0 and v.unformatted and v.lossy
    assert not parse_verdicts("", IDS).lossy


def test_verdict_after_sentinel_clears_it() -> None:
    v = parse_verdicts("VERIFY COMPLETE\nVERDICT c1\nstatus: confirmed\n", IDS)
    assert not v.sentinel and len(v.entries) == 1


def test_verdict_parser_never_raises_on_noise() -> None:
    import random

    rng = random.Random(11)
    pieces = [
        "VERDICT c1",
        "VERDICT 2",
        "END VERDICT",
        "status: confirmed",
        "status:",
        "line: x",
        "severity: ultra",
        "reason:",
        "VERIFY COMPLETE",
        "**",
        "",
        "verdict: refuted",
    ]
    for _ in range(500):
        text = "\n".join(rng.choice(pieces) for _ in range(rng.randint(0, 30)))
        v = parse_verdicts(text, IDS)
        assert len({e.candidate_id for e in v.entries}) == len(v.entries)
        VerifyDraft(entries=v.entries[:60])


_ADVERSARIAL = [
    "FINDING" + " " * 60000 + "x",
    "FINDING\nfile" + " " * 60000 + ": a.py\nseverity: low",
    "FINDING\nfile: a" + " " * 60000 + "b\nseverity: low\ntitle: t",
    "*" * 60000 + "x",
    "a." * 30000,
    "VERDICT" + " " * 60000 + "c1",
    "FINDING\nline: " + "1-" * 30000,
    ("FINDING\nfile: a.py\nseverity: low\ntitle: t\nEND FINDING\n") * 2000,
]


@pytest.mark.parametrize("text", _ADVERSARIAL)
def test_parsers_are_fast_on_adversarial_input(text: str) -> None:
    import time

    start = time.monotonic()
    parse_notes(text)
    parse_verdicts(text, IDS)
    looks_like_unformatted_findings(text)
    assert time.monotonic() - start < 3


def test_code_lines_named_like_markers_stay_inside_a_suggestion_or_fence() -> None:
    # Indented or lower-case "finding:" lines are code (a dataclass field), not markers.
    code = "@dataclass\nclass A:\n    finding: Finding\nfinding: Finding\nEND_X = 1"
    notes = (
        "FINDING\nfile: src/a.py\nline: 10\nseverity: high\ntitle: Wrong field\n"
        f"why: Breaks.\n```python\n    finding: Finding\n```\nsuggestion:\n<<<\n{code}\n>>>\n"
        "END FINDING\nSUMMARY: Checked a.py.\nREVIEW COMPLETE\n"
    )
    p = parse_notes(notes)
    assert p.marker_count == 1 and p.lossless and p.looks_complete
    assert p.findings[0].suggestion == code
    assert "    finding: Finding" in p.findings[0].explanation


def test_an_unindented_end_finding_still_ends_an_unterminated_suggestion() -> None:
    notes = (
        "FINDING\nfile: a.py\nline: 1\nseverity: low\ntitle: T\nwhy: W\n"
        "suggestion:\n<<<\nx = 1\nEND FINDING\nSUMMARY: s\nREVIEW COMPLETE\n"
    )
    p = parse_notes(notes)
    assert len(p.findings) == 1 and p.findings[0].suggestion is None
    assert p.dropped_suggestions == 1 and p.looks_complete
