"""Validated suggestion blocks (ADDENDUM E, rules 1-5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from morning_star_review.core.contract import FinalFinding, Suggestion
from morning_star_review.review import suggest
from morning_star_review.review.suggest import check, fence_for, render_block, validate

PY = "ledger/service.py"


def run(
    make,
    raw,
    *,
    path=PY,
    line=14,
    start=None,
    anchor="inline",
    evidence=None,
    hunks=None,
    head=None,
    rejected=False,
):
    heads = make.heads()
    lines = head if head is not None else heads.get(path)
    af = make.anchor_file(path, hunks or [(1, 40)])
    ev = evidence if evidence is not None else (lines[(start or line) - 1] if lines else "")
    return check(
        raw,
        path=path,
        anchor=anchor,
        line=line,
        start_line=start,
        evidence=ev,
        head_lines=lines,
        anchor_file=af,
        range_rejected=rejected,
    )


# --- rule 4: the file still parses ----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line", "good", "bad"),
    [
        (PY, 14, "        end = start + size", "        end = start + (size"),
        ("conf/config.json", 3, '  "retries": 5,', '  "retries": 5'),
        ("conf/settings.yaml", 3, "  retries: 5", "  retries: [5"),
        ("pyproject.toml", 3, "retries = 5", "retries = "),
    ],
)
def test_structured_files_must_still_parse(make, path, line, good, bad) -> None:
    ok = run(make, good, path=path, line=line)
    assert ok.reason is None
    assert ok.suggestion == Suggestion(start_line=line, end_line=line, text=good)
    dropped = run(make, bad, path=path, line=line)
    assert dropped.suggestion is None
    assert (
        dropped.reason
        == "suggestion dropped: the file no longer parses with the replacement applied"
    )


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("\tx := compute(2, 1)", True),
        ("\tx := compute(2, 1", False),  # one "(" too many
        ("\tx := compute[2, 1)", False),  # same total, different bracket kinds
        ("\tx := compute(2, 1)}", False),
    ],
)
def test_other_languages_keep_the_bracket_balance(make, raw, kept) -> None:
    res = run(make, raw, path="cmd/main.go", line=4)
    assert (res.suggestion is not None) is kept
    if not kept:
        assert res.reason == "suggestion dropped: the replacement changes the bracket balance"


def test_unparsable_head_file_falls_back_to_brackets(make) -> None:
    head = ["def broken(:", "    x = f(1)", "    return x"]
    assert run(make, "    x = f(2)", path="b.py", line=2, head=head).suggestion is not None
    assert run(make, "    x = f(2", path="b.py", line=2, head=head).suggestion is None


# --- rule 2: non-empty, not a no-op ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "the replacement is empty"),
        ("   \n\t\n  ", "the replacement is empty"),
        ("```\n```", "the replacement is empty"),
        ("        end = start + size + 1", "the replacement does not change the code"),
        ("    end  =  start + size +  1  ", "the replacement does not change the code"),
    ],
)
def test_empty_whitespace_and_noop_are_rejected(make, raw, reason) -> None:
    res = run(make, raw)
    assert res.suggestion is None
    assert res.reason == f"suggestion dropped: {reason}"


def test_no_suggestion_means_no_reason(make) -> None:
    assert run(make, None) == suggest.SuggestionCheck()


def test_wrapping_fence_is_removed(make) -> None:
    res = run(make, "```python\n        end = start + size\n```\n")
    assert res.suggestion is not None
    assert res.suggestion.text == "        end = start + size"


# --- rule 3: indentation style --------------------------------------------------------


def test_tabs_instead_of_spaces_are_rejected(make) -> None:
    res = run(make, "\t\tend = start + size")
    assert res.reason == "suggestion dropped: the indentation style differs from the original line"


def test_mixed_indentation_is_rejected(make) -> None:
    res = run(make, "\t x := compute(2, 1)", path="cmd/main.go", line=4)
    assert res.reason == "suggestion dropped: the indentation mixes tabs and spaces"


def test_stripped_indentation_is_rejected(make) -> None:
    # Go has no parser here, so only rule 3 stops an unindented replacement line.
    res = run(make, "x := compute(2, 1)", path="cmd/main.go", line=4)
    assert res.reason == (
        "suggestion dropped: the replacement drops the indentation of the original line"
    )
    kept = run(make, "\tx := compute(2, 1)", path="cmd/main.go", line=4)
    assert kept.suggestion is not None and kept.suggestion.text == "\tx := compute(2, 1)"


# --- rule 5: no markers; code goes verbatim in a fence ---------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "        end = start + size  # <!-- morning-star-review:finding f1 -->",
        "        end = start + size  # morning-star-review: finding f1",
        "        end = start + size  # -->",
        "```\n        end = start + size  # <!-- x\n```",  # inside a wrapping fence too
    ],
)
def test_markers_are_rejected(make, raw) -> None:
    res = run(make, raw)
    assert res.suggestion is None
    assert res.reason == "suggestion dropped: contains a marker"


def test_decorator_suggestion_is_kept(make) -> None:
    text = "    @staticmethod\n    def page(items, page, size):"
    res = run(make, text, line=12)
    assert res.reason is None
    assert res.suggestion == Suggestion(start_line=12, end_line=12, text=text)
    assert render_block(res.suggestion) == f"```suggestion\n{text}\n```"


@pytest.mark.parametrize(
    "raw",
    [
        "        end = start + size  # thanks @octocat",
        "        end = start + size  # see #12, GH-3 and owner/repo#4",
        "        end = start + size  # see https://example.com/x",
        "        end = start + size  # www.example.com",
        "        end = start + size  # [x](https://evil.example) <b>",
    ],
)
def test_mentions_references_and_urls_in_code_are_kept(make, raw) -> None:
    res = run(make, raw)
    assert res.reason is None
    assert res.suggestion is not None and res.suggestion.text == raw


def test_long_backtick_run_is_kept_with_a_longer_fence(make) -> None:
    # A docstring line of six backticks would close a ``` fence; the block uses seven.
    text = '        return """\n``````\n"""'
    res = run(make, text, line=33)
    assert res.reason is None and res.suggestion is not None
    assert res.suggestion.text == text
    block = render_block(res.suggestion)
    assert block == f"```````suggestion\n{text}\n```````"
    inline = "        end = start + size  # `````` run"
    kept = run(make, inline)
    assert kept.suggestion is not None
    assert render_block(kept.suggestion).startswith("```````suggestion\n")


def test_a_line_that_closes_the_fence_is_rejected(make, monkeypatch) -> None:
    # Defence in depth: should the fence ever be too short, the block is not posted.
    monkeypatch.setattr(suggest, "fence_for", lambda text, char="`": "```")
    res = run(make, '        return """\n```\n"""', line=33)
    assert res.suggestion is None
    assert res.reason == "suggestion dropped: contains a line that would close the suggestion fence"


# --- rule 1: anchor and range ---------------------------------------------------------


def test_anchor_kinds(make) -> None:
    good = "        end = start + size"
    assert run(make, good, anchor="file").reason == (
        "suggestion dropped: the finding is not anchored to a changed line"
    )
    assert run(make, good, anchor="outside").suggestion is None
    assert run(make, good, anchor="snapped").suggestion is not None  # evidence is exact
    vague = run(make, good, anchor="snapped", evidence="end is computed wrongly")
    assert (
        vague.reason
        == "suggestion dropped: the finding was snapped without an exact evidence match"
    )


def test_ranges(make) -> None:
    two = "        start = page * size\n        end = start + size"
    ok = run(make, two, start=13, line=14)
    assert ok.suggestion == Suggestion(start_line=13, end_line=14, text=two)
    long_range = run(make, "x = 1\n" * 21, start=10, line=30, hunks=[(1, 40)])
    assert long_range.reason == "suggestion dropped: the range is longer than 20 lines"
    split = run(make, two, start=13, line=14, hunks=[(10, 13), (14, 30)])
    assert split.reason == "suggestion dropped: the range is not inside one diff hunk"
    rejected = run(make, two, line=13, rejected=True)
    assert rejected.reason == "suggestion dropped: the requested line range cannot be commented on"


def test_missing_head_or_line(make) -> None:
    assert (
        run(make, "x", line=None, evidence="").reason
        == "suggestion dropped: the finding has no line"
    )
    res = check(
        "x",
        path=PY,
        anchor="inline",
        line=99,
        start_line=None,
        evidence="",
        head_lines=make.heads()[PY],
    )
    assert res.reason == "suggestion dropped: the head file or the lines are not available"


def test_validate_entry_point(make) -> None:
    f = FinalFinding(
        id="f123456789012",
        path=PY,
        line=14,
        anchor="inline",
        severity="high",
        category="correctness",
        title="t",
        evidence="end = start + size + 1",
        explanation="e",
        state="open",
    )
    head = make.heads()[PY]
    s = validate(f, head, PY, raw="        end = start + size")
    assert s == Suggestion(start_line=14, end_line=14, text="        end = start + size")
    assert validate(f, head, PY, raw="") is None
    assert validate(f.model_copy(update={"suggestion": s}), head, PY) == s  # re-validation


# --- rendering ------------------------------------------------------------------------


def test_fence_is_longer_than_any_backtick_run() -> None:
    assert fence_for("plain") == "```"
    assert fence_for("a ``` b") == "````"
    assert fence_for("`````") == "``````"


def test_render_block_uses_a_safe_fence() -> None:
    s = Suggestion(start_line=1, end_line=1, text='doc = """```py\nx\n```"""')
    block = render_block(s)
    assert block.startswith("````suggestion\n") and block.endswith("\n````")
    assert render_block(Suggestion(start_line=1, end_line=1, text="x")) == "```suggestion\nx\n```"


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_suggestion_cannot_exist(text: str) -> None:
    with pytest.raises(ValidationError):
        Suggestion(start_line=1, end_line=1, text=text)
