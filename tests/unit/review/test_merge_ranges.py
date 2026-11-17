"""Merge of findings whose line ranges overlap (one explorer flags a whole function)."""

from __future__ import annotations

from morning_star_review.review.merge import TITLE_OVERLAP, merge_drafts, title_similarity

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
DB = "ledger/db.py"

# The head of ledger/db.py in the eval's e5 case: the PR adds lines 144..159 inside
# one hunk (141..162). Lines 144..149 are comments; their text does not matter here.
_HUNK = [
    "        ).fetchall()",  # 141
    "        return [_row_to_transaction(row) for row in rows]",
    "",
    *["    # Sorted variant of list_transactions."] * 6,  # 144..149
    "    def list_transactions_sorted(",  # 150
    "        self, account_id: int, sort: str, limit: int, offset: int",
    "    ) -> list[Transaction]:",
    '        """Like ``list_transactions`` but ordered by a caller-chosen column."""',
    "        rows = self._conn.execute(",  # 154
    '            f"SELECT * FROM transactions WHERE account_id = ? ORDER BY {sort} LIMIT ?'
    ' OFFSET ?",',  # 155
    "            (account_id, limit, offset),",  # 156
    "        ).fetchall()",
    "        return [_row_to_transaction(row) for row in rows]",  # 158
    "",
    "    def count_transactions(self, account_id: int) -> int:",
    "        row = self._conn.execute(",
    '            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)',
]
DB_PY = [f"# line {n}" for n in range(1, 141)] + _HUNK
assert DB_PY[154].startswith('            f"SELECT')  # line 155

FSTRING = DB_PY[154].strip()
# Mistral Large 4 wrote the whole function on one line with literal "\n" escapes, so
# its evidence matches no head line: the anchor stays on its own line 150, not exact.
ESCAPED = "\\n".join(DB_PY[149:157]).strip()

TITLE_NARROW = "SQL injection through the caller-controlled `sort` parameter"
TITLE_WIDE = (
    "SQL injection via unvalidated `sort` query parameter interpolated into ORDER BY clause"
)
LONG_WHY = (
    "The sort value comes from the query string and is placed into the statement with "
    "an f-string. Nothing between the request and this line checks it against a list "
    "of columns, so any user who can view one account can run SQL expressions."
)


def _anchors(make, paths=(DB,)):
    return make.anchors({p: [(141, 162)] for p in paths})


def _heads(paths=(DB,)):
    return dict.fromkeys(paths, DB_PY).get


def _narrow(make, **kw):
    base = {
        "path": DB,
        "line": 155,
        "end_line": 156,
        "severity": "critical",
        "category": "security",
        "title": TITLE_NARROW,
        "evidence": FSTRING,
        "explanation": "The sort parameter is interpolated into ORDER BY.",
        "fix": "Check sort against an allowlist of columns.",
    }
    base.update(kw)
    return make.draft(**base)


def _wide(make, **kw):
    base = {
        "path": DB,
        "line": 150,
        "end_line": 158,
        "severity": "critical",
        "category": "security",
        "title": TITLE_WIDE,
        "evidence": ESCAPED,
        "explanation": LONG_WHY,
        "fix": "Validate sort before it reaches the database.",
    }
    base.update(kw)
    return make.draft(**base)


def _merge(make, *per_model, paths=(DB,)):
    drafts = [make.review_draft(fs, model=m) for m, fs in per_model]
    return merge_drafts(drafts, _anchors(make, paths), _heads(paths))


def test_eval_duplicate_wide_range_and_lines_inside_are_one_candidate(make) -> None:
    # preset_best e5 run-2: GLM and DeepSeek flagged 155-156, Mistral 150-158. Both
    # were published as separate critical inline findings for the same bug.
    assert TITLE_OVERLAP <= title_similarity(TITLE_NARROW, TITLE_WIDE) < 0.5
    res = _merge(
        make,
        (GLM, [_narrow(make)]),
        (LARGE, [_wide(make, suggestion="        pass")]),
        (DEEPSEEK, [_narrow(make, explanation="Untrusted sort reaches SQL.")]),
    )
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.votes == [GLM, LARGE, DEEPSEEK]
    assert (cand.path, cand.line, cand.end_line) == (DB, 155, 156)  # the exact anchor
    assert cand.evidence == FSTRING
    assert cand.explanation == LONG_WHY  # the longest explanation
    assert cand.severity == "critical" and cand.title == TITLE_NARROW
    assert cand.suggestion is None  # written for 150-158, not for the anchor's lines


def test_single_line_inside_a_more_severe_wide_range_takes_the_exact_line(make) -> None:
    res = _merge(
        make,
        (LARGE, [_wide(make)]),
        (GLM, [_narrow(make, end_line=None, severity="high", suggestion="x")]),
    )
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.votes == [LARGE, GLM]  # explorer order
    assert cand.severity == "critical" and cand.title == TITLE_WIDE  # the strongest claim
    assert (cand.line, cand.end_line, cand.evidence) == (155, None, FSTRING)
    assert cand.suggestion == "x"  # written for exactly the anchored line


def test_same_category_inside_a_range_merges_without_similar_words(make) -> None:
    wide = _wide(make, title="Unsafe ORDER BY")
    inner = _narrow(make, title="Untrusted input reaches the database", end_line=None)
    assert title_similarity(wide.title, inner.title) == 0.0
    res = _merge(make, (LARGE, [wide]), (GLM, [inner]))
    assert len(res.candidates) == 1 and res.candidates[0].votes == [LARGE, GLM]


def test_other_category_inside_a_range_stays_apart(make) -> None:
    wide = _wide(make, title="SQL injection via sort")
    errors = make.draft(
        path=DB,
        line=157,
        category="reliability",
        title="Database errors escape the handler",
        evidence=").fetchall()",
    )
    res = _merge(make, (LARGE, [wide]), (GLM, [errors]))
    assert sorted(c.line or 0 for c in res.candidates) == [150, 157]


def test_same_category_with_small_overlap_needs_similar_words(make) -> None:
    # 150-153 and 153-156 share one line: 25% of the smaller range.
    a = _wide(make, end_line=153, title="Unsafe ORDER BY")
    b = _narrow(make, line=153, end_line=156, evidence=DB_PY[152].strip(), title="Docs drift")
    assert len(_merge(make, (LARGE, [a]), (GLM, [b])).candidates) == 2


def test_ranges_within_three_lines_are_near(make) -> None:
    head = _wide(make, end_line=152, evidence=DB_PY[149].strip(), title="Sort parameter in SQL")
    gap3 = _narrow(make, end_line=None, title="SQL built from sort parameter")
    gap4 = _narrow(make, line=156, end_line=None, evidence=DB_PY[155].strip(), title=gap3.title)
    assert len(_merge(make, (LARGE, [head]), (GLM, [gap3])).candidates) == 1
    assert len(_merge(make, (LARGE, [head]), (GLM, [gap4])).candidates) == 2


def test_identical_findings_on_different_paths_never_merge(make) -> None:
    other = "ledger/db_copy.py"
    res = _merge(
        make,
        (GLM, [_narrow(make)]),
        (LARGE, [_narrow(make, path=other)]),
        paths=(DB, other),
    )
    assert sorted(c.path for c in res.candidates) == [DB, other]


def test_off_diff_findings_merge_on_the_requested_range(make) -> None:
    # ledger/service.py changes lines 10..30 only, so both findings are file-level.
    whole = make.draft(
        line=33, end_line=38, evidence="def helper(x):", title="Helper doubles instead of halving"
    )
    inner = make.draft(line=37, evidence="return x * 2", title="Wrong factor in helper")
    res = merge_drafts(
        [make.review_draft([whole], model=GLM), make.review_draft([inner], model=LARGE)],
        make.anchors(),
        make.head_text(),
    )
    assert len(res.candidates) == 1 and res.candidates[0].votes == [GLM, LARGE]
    # A range longer than MAX_RANGE_SPAN counts as its first line only.
    huge = make.draft(line=5, end_line=40, evidence="def helper(x):", title=whole.title)
    res2 = merge_drafts(
        [make.review_draft([huge], model=GLM), make.review_draft([inner], model=LARGE)],
        make.anchors(),
        make.head_text(),
    )
    assert len(res2.candidates) == 2
