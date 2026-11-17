"""Broken and no-op suggestion shapes seen in the 2026-11-17 eval (ADDENDUM E).

The replacements credited to a model below are the exact texts it proposed on the
eval's ``ledger`` fixture, checked against that fixture's head lines at their real line
numbers. All of them passed the parse check, so before these rules they were posted as
committable suggestion blocks. The other shapes are small made-up variants.
"""

from __future__ import annotations

import ast

import pytest

from morning_star_review.core.contract import Suggestion
from morning_star_review.review.suggest import MAX_LINE_DELTA, SuggestionCheck, check


def _head(first: int, lines: list[str], *, header: str, filler: str) -> list[str]:
    """A parseable Python head file whose ``lines`` start at line ``first``."""
    return [header, *[filler] * (first - 2), *lines]


# ledger/db.py at the e1 head, lines 144-156.
E1_DB = _head(
    144,
    [
        "    def search_transactions(self, account_id: int, term: str) -> list[Transaction]:",
        '        """Transactions of one account whose memo contains ``term``, oldest first."""',
        "        rows = self._conn.execute(",
        '            f"SELECT * FROM transactions WHERE account_id = ? '
        "AND memo LIKE '%{term}%' ORDER BY id\",",
        "            (account_id,),",
        "        ).fetchall()",
        "        return [_row_to_transaction(row) for row in rows]",
        "",
        "    def count_transactions(self, account_id: int) -> int:",
        "        row = self._conn.execute(",
        '            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)',
        "        ).fetchone()",
        '        return int(row["n"])',
    ],
    header="class Database:",
    filler="    pass",
)
E1_EVIDENCE = (
    "f\"SELECT * FROM transactions WHERE account_id = ? AND memo LIKE '%{term}%' ORDER BY id\","
)

# ledger/db.py at the e5 head, lines 150-164 (the lines above 150 are filler here).
E5_DB = _head(
    150,
    [
        "    def list_transactions_sorted(",
        "        self, account_id: int, sort: str, limit: int, offset: int",
        "    ) -> list[Transaction]:",
        '        """Like ``list_transactions`` but ordered by a caller-chosen column."""',
        "        rows = self._conn.execute(",
        '            f"SELECT * FROM transactions WHERE account_id = ? '
        'ORDER BY {sort} LIMIT ? OFFSET ?",',
        "            (account_id, limit, offset),",
        "        ).fetchall()",
        "        return [_row_to_transaction(row) for row in rows]",
        "",
        "    def count_transactions(self, account_id: int) -> int:",
        "        row = self._conn.execute(",
        '            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)',
        "        ).fetchone()",
        '        return int(row["n"])',
    ],
    header="class Database:",
    filler="    pass",
)
E5_EVIDENCE = 'f"SELECT * FROM transactions WHERE account_id = ? ORDER BY {sort} LIMIT ? OFFSET ?",'

# ledger/handlers.py at the e1 head, lines 90-95.
E1_HANDLERS = _head(
    90,
    [
        "def change_plan(service: LedgerService, request: Request) -> Response:",
        "    account = service.get_account(_account_id(request))",
        "    if auth.can_modify_account(request.user, account):",
        '        raise Forbidden("you may not change this account")',
        "    try:",
        '        day = int(request.body.get("day_of_period", "0"))',
        "    except ValueError as exc:",
        '        raise InvalidRequest("day_of_period must be a whole number") from exc',
    ],
    header="import auth",
    filler="x = 0",
)

# A rewrite of the whole execute(...) call, as three models proposed it for e1.
E1_CALL_REWRITES = {
    "mistral-large-4": [
        "        rows = self._conn.execute(",
        '            "SELECT * FROM transactions WHERE account_id = ? '
        'AND instr(memo, ?) > 0 ORDER BY id",',
        "            (account_id, term),",
        "        ).fetchall()",
    ],
    "zai-glm-5-3": [
        "        rows = self._conn.execute(",
        '            "SELECT * FROM transactions WHERE account_id = ? '
        'AND memo LIKE ? ORDER BY id",',
        '            (account_id, f"%{term}%"),',
        "        ).fetchall()",
    ],
    "best": [
        "        rows = self._conn.execute(",
        '            "SELECT * FROM transactions WHERE account_id = ? '
        "AND memo LIKE ? ESCAPE '\\\\' ORDER BY id\",",
        '            (account_id, "%" + term.replace("\\\\", "\\\\\\\\")'
        '.replace("%", "\\\\%").replace("_", "\\\\_") + "%"),',
        "        ).fetchall()",
    ],
}


def run(raw: str, head: list[str], line: int, *, start: int | None, evidence: str, path: str):
    return check(
        raw,
        path=path,
        anchor="inline",
        line=line,
        start_line=start,
        evidence=evidence,
        head_lines=head,
    )


def applied(head: list[str], start: int, end: int, raw: str) -> list[str]:
    return [*head[: start - 1], *raw.split("\n"), *head[end:]]


def dropped(reason: str) -> SuggestionCheck:
    return SuggestionCheck(reason=f"suggestion dropped: {reason}")


def test_fixture_lines_are_at_their_eval_line_numbers() -> None:
    assert E1_DB[146 - 1] == "        rows = self._conn.execute("
    assert E1_DB[147 - 1].strip() == E1_EVIDENCE
    assert E5_DB[155 - 1].strip() == E5_EVIDENCE
    assert E1_HANDLERS[92 - 1].strip() == "if auth.can_modify_account(request.user, account):"
    for head in (E1_DB, E5_DB, E1_HANDLERS):
        ast.parse("\n".join(head))


# --- rule 4: the replacement repeats the lines around the range -----------------------


@pytest.mark.parametrize("model", sorted(E1_CALL_REWRITES))
@pytest.mark.parametrize(("start", "line"), [(147, 148), (None, 147)])
def test_e1_call_rewrite_anchored_on_its_inner_lines_is_dropped(model, start, line) -> None:
    # The eval's db.py 147-148 case (and 147 alone): the whole call replaces only its
    # arguments, so "rows = self._conn.execute(" lands inside the call it repeats.
    raw = "\n".join(E1_CALL_REWRITES[model])
    patched = applied(E1_DB, start or line, line, raw)
    ast.parse("\n".join(patched))  # why the parse check alone let it through
    assert patched.count("        rows = self._conn.execute(") == 2
    res = run(raw, E1_DB, line, start=start, evidence=E1_EVIDENCE, path="ledger/db.py")
    assert res == dropped("the replacement repeats the line before the range")


@pytest.mark.parametrize("model", sorted(E1_CALL_REWRITES))
def test_e1_call_rewrite_on_the_whole_call_is_kept(model) -> None:
    raw = "\n".join(E1_CALL_REWRITES[model])
    res = run(raw, E1_DB, 149, start=146, evidence=E1_EVIDENCE, path="ledger/db.py")
    assert res.reason is None
    assert res.suggestion == Suggestion(start_line=146, end_line=149, text=raw)


def test_e1_argument_rewrite_on_the_argument_lines_is_kept() -> None:
    # zai-glm-5-3 e1 run-2: the same fix, sized to the range it was anchored on.
    raw = (
        '            "SELECT * FROM transactions WHERE account_id = ? '
        "AND memo LIKE '%' || ? || '%' ORDER BY id\",\n"
        "            (account_id, term),"
    )
    evidence = E1_EVIDENCE + "\n            (account_id,),"
    res = run(raw, E1_DB, 148, start=147, evidence=evidence, path="ledger/db.py")
    assert res.reason is None and res.suggestion is not None


def test_e5_call_rewrite_that_keeps_the_injection_is_dropped() -> None:
    # mistral-large-4 e5 run-3 on db.py 155-156: the f-string is unchanged and the
    # call is repeated around it.
    raw = "\n".join(
        [
            "        rows = self._conn.execute(",
            '            f"SELECT * FROM transactions WHERE account_id = ? '
            'ORDER BY {sort} LIMIT ? OFFSET ?",',
            "            (account_id, limit, offset),",
            "        ).fetchall()",
        ]
    )
    ast.parse("\n".join(applied(E5_DB, 155, 156, raw)))
    res = run(raw, E5_DB, 156, start=155, evidence=E5_EVIDENCE, path="ledger/db.py")
    assert res == dropped("the replacement repeats the line before the range")


def test_a_repeat_of_the_line_after_the_range_is_dropped() -> None:
    # zai-glm-5-3-flash e1 run-3 on handlers.py 92: the raise on line 93 is repeated.
    raw = (
        "    if not auth.can_modify_account(request.user, account):\n"
        '        raise Forbidden("you may not change this account")'
    )
    evidence = "if auth.can_modify_account(request.user, account):"
    res = run(raw, E1_HANDLERS, 92, start=None, evidence=evidence, path="ledger/handlers.py")
    assert res == dropped("the replacement repeats the line after the range")
    # deepseek e1 run-1: the one-line fix for the same range is kept.
    one = "    if not auth.can_modify_account(request.user, account):"
    kept = run(one, E1_HANDLERS, 92, start=None, evidence=evidence, path="ledger/handlers.py")
    assert kept.suggestion == Suggestion(start_line=92, end_line=92, text=one)
    # And the two-line fix is kept when it is anchored on both lines.
    both = run(raw, E1_HANDLERS, 93, start=92, evidence=evidence, path="ledger/handlers.py")
    assert both.reason is None


def test_only_the_trailing_call_line_repeated_is_dropped() -> None:
    raw = "\n".join([*E5_DB[155 - 1 : 156], "        ).fetchall()"]).replace("{sort}", "id")
    res = run(raw, E5_DB, 156, start=155, evidence=E5_EVIDENCE, path="ledger/db.py")
    assert res == dropped("the replacement repeats the line after the range")


def test_a_repeat_across_blank_lines_is_dropped() -> None:
    head = ["import os", "", "", "cwd = os.getcwd()", ""]
    raw = 'import os\ncwd = os.getcwd() or "/"'
    res = run(raw, head, 4, start=None, evidence="cwd = os.getcwd()", path="a.py")
    assert res == dropped("the replacement repeats the line before the range")


@pytest.mark.parametrize(
    ("head", "line", "raw", "path"),
    [
        # The head file already repeats the line: the replacement adds no copy.
        (
            [
                "def rows(reader):",
                "    next(reader)",
                "    next(reader)",
                "    return list(reader)",
            ],
            4,
            "    next(reader)\n    return [r for r in reader if r]",
            "csvload.py",
        ),
        # One-word and brackets-only closers legitimately end a nested block.
        (
            ["def f(x)", "  if x", "    y", "  end", "end"],
            3,
            "    if z\n      y\n    end",
            "lib/f.rb",
        ),
        (
            ["func f() {", "\tif x {", "\t\ty()", "\t}", "}"],
            3,
            "\t\tif z {\n\t\t\ty()\n\t\t}",
            "f.go",
        ),
    ],
)
def test_not_a_repeat(head, line, raw, path) -> None:
    start = line - 1 if raw.count("\n") and head[line - 2] == raw.split("\n")[0] else None
    res = run(raw, head, line, start=start, evidence=head[line - 1], path=path)
    assert res.reason is None and res.suggestion is not None


# --- rule 4: scope creep --------------------------------------------------------------

# best e5 run-2 on db.py 150-158: a nine-line range rewritten as 22 lines, including
# a class constant the method then reads as a bare (undefined) name.
E5_REWRITE = [
    "    # Allowlist of sortable columns; raw input is never interpolated into SQL.",
    '    SORTABLE_COLUMNS = {"id", "amount_cents", "memo", "created_at"}',
    "",
    "    def list_transactions_sorted(",
    "        self, account_id: int, sort: str, limit: int, offset: int",
    "    ) -> list[Transaction]:",
    '        """Like ``list_transactions`` but ordered by a caller-chosen column.',
    "",
    '        ``sort`` must be ``"<column>"`` or ``"<column> ASC|DESC"`` with the',
    "        column in ``SORTABLE_COLUMNS``; anything else raises ``InvalidRequest``.",
    '        """',
    '        column, _, direction = sort.partition(" ")',
    "        column = column.strip().lower()",
    "        direction = direction.strip().upper()",
    '        if column not in SORTABLE_COLUMNS or direction not in ("", "ASC", "DESC"):',
    '            raise InvalidRequest(f"invalid sort: {sort!r}")',
    '        order = f"{column} {direction}".strip()',
    "        rows = self._conn.execute(",
    '            f"SELECT * FROM transactions WHERE account_id = ? '
    'ORDER BY {order} LIMIT ? OFFSET ?",',
    "            (account_id, limit, offset),",
    "        ).fetchall()",
    "        return [_row_to_transaction(row) for row in rows]",
]


def test_e5_scope_creep_is_dropped() -> None:
    raw = "\n".join(E5_REWRITE)
    ast.parse("\n".join(applied(E5_DB, 150, 158, raw)))
    res = run(raw, E5_DB, 158, start=150, evidence="def list_transactions_sorted(", path="db.py")
    assert res == dropped(f"the replacement changes the line count by more than {MAX_LINE_DELTA}")


@pytest.mark.parametrize(("extra", "kept"), [(MAX_LINE_DELTA, True), (MAX_LINE_DELTA + 1, False)])
def test_line_count_limit(extra, kept) -> None:
    head = [f"step {n}" for n in range(1, 41)]
    too_many = f"the replacement changes the line count by more than {MAX_LINE_DELTA}"
    reason = None if kept else dropped(too_many).reason
    grow = "\n".join(["step 20 fixed", *[f"new step {n}" for n in range(extra)]])
    assert run(grow, head, 20, start=None, evidence="step 20", path="notes.txt").reason == reason
    shrink = run("step 20 fixed", head, 20, start=20 - extra, evidence="step 20", path="notes.txt")
    assert shrink.reason == reason


# --- rule 2: the flagged code is left as it is ----------------------------------------

E5_INJECTION_LINE = E5_DB[155 - 1]
E5_ARGS_LINE = E5_DB[156 - 1]


@pytest.mark.parametrize(
    "raw",
    [
        # A comment above the unchanged f-string.
        f"            # sort is checked by the handler\n{E5_INJECTION_LINE}\n{E5_ARGS_LINE}",
        # A linter suppression on the flagged line itself.
        f"{E5_INJECTION_LINE}  # noqa: S608\n{E5_ARGS_LINE}",
        # Only the spacing of the other line changes.
        f"{E5_INJECTION_LINE}\n            (account_id,limit,offset),",
        # Both, with the comment moved below.
        f"{E5_INJECTION_LINE}\n            (account_id,  limit, offset),  # validated upstream",
    ],
)
def test_e5_noop_for_the_flagged_line_is_dropped(raw) -> None:
    res = run(raw, E5_DB, 156, start=155, evidence=E5_EVIDENCE, path="ledger/db.py")
    assert res == dropped("the replacement leaves the flagged code unchanged")


def test_e5_real_fix_next_to_the_flagged_line_is_kept() -> None:
    # The evidence line is unchanged, but the code around it is not.
    raw = "\n".join(
        [
            '            f"SELECT * FROM transactions WHERE account_id = ? ORDER BY {_SORTS[sort]} '
            'LIMIT ? OFFSET ?",',
            E5_ARGS_LINE,
        ]
    )
    res = run(raw, E5_DB, 156, start=155, evidence=E5_EVIDENCE, path="ledger/db.py")
    assert res.reason is None
    add_check = f'            _check_sort(sort) or "",\n{E5_INJECTION_LINE}'
    res = run(add_check, E5_DB, 155, start=None, evidence=E5_EVIDENCE, path="ledger/db.py")
    assert res.reason is None


@pytest.mark.parametrize(
    ("head", "evidence", "raw", "path"),
    [
        # The finding is about the comment: it is the evidence, and it changes.
        (
            ["def timeout():", "    # Timeout in seconds.", "    return 5000"],
            "# Timeout in seconds.",
            "    # Timeout in milliseconds.\n    return 5000",
            "t.py",
        ),
        # The evidence quotes the code with its comment, so the comment is flagged too.
        (
            ["TIMEOUT = 5000  # seconds", "RETRIES = 3"],
            "TIMEOUT = 5000  # seconds",
            "TIMEOUT = 5000  # milliseconds\nRETRIES = 3",
            "t.py",
        ),
        (
            ["const timeout = 5000; // seconds", "const retries = 3;"],
            "const timeout = 5000; // seconds",
            "const timeout = 5000; // milliseconds\nconst retries = 3;",
            "t.ts",
        ),
    ],
)
def test_comment_fixes_are_kept(head, evidence, raw, path) -> None:
    res = run(raw, head, len(head), start=len(head) - 1, evidence=evidence, path=path)
    assert res.reason is None and res.suggestion is not None


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ("const total = a + b;  // TODO: overflow\nconsole.log(total);", "sum.ts"),
        ("const total = a + b;\n/* overflow is handled later */\nconsole.log(total);", "sum.ts"),
        ("const total = a + b;\n * overflow\nconsole.log(total);", "sum.ts"),
        ("const total = a + b;\nconsole.log( total );", "sum.ts"),
        ("const total = a + b;\nconsole.log(total) ;", "sum.ts"),
        ("local total = a + b\nprint(total) -- overflow?", "sum.lua"),
        ("total := a + b\nfmt.Println(total)  // overflow", "sum.go"),
    ],
)
def test_comment_or_layout_only_changes_in_other_languages_are_dropped(raw, path) -> None:
    head = {
        "sum.ts": ["const total = a + b;", "console.log(total);"],
        "sum.lua": ["local total = a + b", "print(total)"],
        "sum.go": ["total := a + b", "fmt.Println(total)"],
    }[path]
    res = run(raw, head, 2, start=1, evidence=head[0], path=path)
    assert res == dropped("the replacement leaves the flagged code unchanged")


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ('url = "http://example.com/a"  # see docs\nfetch(url)', False),
        ('url = "http://example.com/b"\nfetch(url)', True),  # inside the string
        ('url = "http://example.com/a#frag"\nfetch(url)', True),
        ("url = 'http://example.com/a'\nfetch(url)", True),  # other quotes: code changed
    ],
)
def test_strings_are_not_comments(raw, kept) -> None:
    head = ['url = "http://example.com/a"', "fetch(url)"]
    res = run(raw, head, 2, start=1, evidence=head[0], path="f.py")
    assert (res.suggestion is not None) is kept, res.reason


def test_python_indentation_is_code() -> None:
    # The evidence line stays, a comment is added, and another line moves out of
    # the loop: in Python that changes the code, so the suggestion is kept.
    head = ["def total(xs):", "    t = 0", "    for x in xs:", "        t += x", "        return t"]
    raw = "    for x in xs:  # sum all\n        t += x\n    return t"
    res = run(raw, head, 5, start=3, evidence="for x in xs:", path="t.py")
    assert res.reason is None and res.suggestion is not None


def test_no_evidence_line_in_the_range_means_rule_2_does_not_apply() -> None:
    head = ["const total = a + b;", "console.log(total);"]
    raw = "const total = a + b;  // checked\nconsole.log(total);"
    res = run(raw, head, 2, start=1, evidence="overflow is not handled", path="sum.ts")
    assert res.reason is None
