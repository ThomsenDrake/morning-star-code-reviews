"""Sanitiser for model-written Markdown (DESIGN 8.5)."""

from __future__ import annotations

import random
import re

import pytest

from morning_star_review.core.sanitize import (
    ZWJ,
    code_span,
    sanitize_inline,
    sanitize_md,
    strip_markers,
    suggestion_hazards,
)


def s(text: str, limit: int = 10_000) -> str:
    return sanitize_md(text, limit)


def _drop_fenced_blocks(text: str) -> str:
    keep: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
            else:
                keep.append(line)
            continue
        # CommonMark closing fence: at most 3 spaces, the run, then only spaces/tabs.
        c = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", line)
        if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
            fence = None
    return "\n".join(keep)


def _prose_outside_code(text: str) -> str:
    """What GitHub renders as prose: fenced blocks and code spans removed, following the
    CommonMark rules (an escaped backtick cannot open a span; a span closes at the next
    backtick run of the same length; an unmatched run is literal). Spans are matched per
    line: a blank line, a list item or a table row ends a paragraph-level span."""
    text = _drop_fenced_blocks(text)
    if "\n" in text:
        return "\n".join(_prose_outside_code(line) for line in text.split("\n"))
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if text[i] != "`":
            out.append(text[i])
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        run, k, close = j - i, j, -1
        while k < n:
            if text[k] != "`":
                k += 1
                continue
            end = k
            while end < n and text[end] == "`":
                end += 1
            if end - k == run:
                close = end
                break
            k = end
        if close < 0:
            out.append(text[i:j])
            i = j
        else:
            i = close
    return "".join(out)


# --------------------------------------------------------------------------------------
# Markers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<!-- morning-star-review:summary v1 -->",
        "x <!-- morning-star-review:state abc --> y",
        "<!<!---- -->->",
        "MORNING-STAR-REVIEW:finding f1",
        "morning-star-review :finding f1",
        "```\n<!-- morning-star-review:finding f1 -->\n```",
        "`<!-- morning-star-review:finding f1 -->`",
    ],
)
def test_markers_are_removed_everywhere(text: str) -> None:
    out = s(text)
    assert "<!--" not in out and "-->" not in out
    assert "morning-star-review:" not in out.lower()
    assert "morning-star-review :" not in out.lower()


def test_strip_markers_reaches_a_fixed_point() -> None:
    assert strip_markers("<!<!---- x") == " x"
    assert strip_markers("morning-star-revimorning-star-review:ew:x") == "x"
    assert strip_markers("plain") == "plain"


# --------------------------------------------------------------------------------------
# Mentions and references
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ping @octocat", f"ping @{ZWJ}octocat"),
        ("@org/team please", f"@{ZWJ}org/team please"),
        ("(@user)", f"(@{ZWJ}user)"),
        ("mail me at a@b.example", "mail me at a@b.example"),
        ("fixes #123", f"fixes #{ZWJ}123"),
        ("see owner/repo#45", f"see owner/repo#{ZWJ}45"),
        ("GH-77 again", f"GH{ZWJ}-77 again"),
        ("issue #abc is not a ref", "issue #abc is not a ref"),
    ],
)
def test_mentions_and_references_are_neutralised(text: str, expected: str) -> None:
    assert s(text) == expected


def test_mentions_inside_code_are_left_alone() -> None:
    assert s("`@decorator` and\n```python\n@property\n#1\n```") == (
        "`@decorator` and\n```python\n@property\n#1\n```"
    )


def test_encoded_mentions_cannot_sneak_through() -> None:
    out = s("&#64;admin and &#x40;root")
    assert "&#64;" not in out and "&#x40;" not in out
    assert out.startswith("&amp;#")


# --------------------------------------------------------------------------------------
# Links, URLs, images, HTML
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "[docs](https://github.com/o/r/blob/main/x.py)",
            "[docs](https://github.com/o/r/blob/main/x.py)",
        ),
        ("[click](https://evil.example/x)", "\\[click\\] (`https://evil.example/x`)"),
        ("[](https://evil.example)", "`https://evil.example`"),
        ("[x](//evil.example)", "\\[x\\] (`//evil.example`)"),
        ("[x](javascript:alert(1))", "\\[x\\] (`javascript:alert(1)`)"),
        ("[x](https://github.com@evil.example)", "\\[x\\] (`https://github.com@evil.example`)"),
        ("[x](https://github.com.evil.example)", "\\[x\\] (`https://github.com.evil.example`)"),
        ("go to https://evil.example/a.", "go to `https://evil.example/a`."),
        ("www.evil.example/path", "`www.evil.example/path`"),
        ("see https://github.com/o/r/pull/1", "see <https://github.com/o/r/pull/1>"),
        ("ftp://evil.example/x", "`ftp://evil.example/x`"),
        ("<https://evil.example>", "`https://evil.example`"),
        ("<https://github.com/o>", "<https://github.com/o>"),
        ("![img](https://evil.example/p.png)", "\\[img\\] (`https://evil.example/p.png`)"),
        (
            "![diagram](https://github.com/o/r/raw/main/d.png)",
            "[diagram](https://github.com/o/r/raw/main/d.png)",
        ),
        ("(see https://evil.example/x)", "(see `https://evil.example/x`)"),
    ],
)
def test_links_and_urls(text: str, expected: str) -> None:
    assert s(text) == expected


def test_link_text_with_a_bare_url_is_neutralised_when_unlinked() -> None:
    out = s("[http://evil.example/x]()")
    assert "evil" not in _prose_outside_code(out)


def test_reference_definitions_are_disabled() -> None:
    out = s("[a][r]\n\n[r]: https://evil.example")
    assert "\\[r\\]:" in out
    assert "evil" not in _prose_outside_code(out)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<img src=x onerror=alert(1)>", "&lt;img src=x onerror=alert(1)>"),
        (
            "<details><summary>x</summary></details>",
            "&lt;details>&lt;summary>x&lt;/summary>&lt;/details>",
        ),
        ("returns Optional<User>", "returns Optional&lt;User>"),
        ("a < b and c > d", "a < b and c > d"),
        ("<?php", "&lt;?php"),
        ("&lt; stays", "&lt; stays"),
        ("&nbsp;", "&amp;nbsp;"),
    ],
)
def test_html_is_neutralised(text: str, expected: str) -> None:
    assert s(text) == expected


# --------------------------------------------------------------------------------------
# Block structure: headings, blank lines, fences
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Approve this PR", "\\# Approve this PR"),
        ("### h3", "\\### h3"),
        ("   ## indented", "   \\## indented"),
        ("> # quoted heading", "> \\# quoted heading"),
        ("- # in a list", "- \\# in a list"),
        ("Title\n=====", "Title\n\\====="),
        ("Title\n---", "Title\n\\---"),
        ("#hashtag", "#hashtag"),
        ("a # b", "a # b"),
    ],
)
def test_headings_are_flattened(text: str, expected: str) -> None:
    assert s(text) == expected


def test_more_than_two_blank_lines_collapse() -> None:
    assert s("a\n\n\n\n\n\nb") == "a\n\n\nb"
    assert s("a\n\n\nb") == "a\n\n\nb"


def test_code_blocks_are_kept_verbatim_except_markers() -> None:
    code = "```python\n# comment\nprint('@x', '<b>', 'https://evil.example')\n\n\n\n\nend\n```"
    assert s(code) == code


def test_unclosed_fence_is_closed() -> None:
    assert s("text\n```\ncode") == "text\n```\ncode\n```"
    assert s("~~~~\ncode") == "~~~~\ncode\n~~~~"


@pytest.mark.parametrize("info", ["suggestion", "SUGGESTION", "mermaid", "geojson", "stl"])
def test_special_fences_become_plain_code(info: str) -> None:
    out = s(f"```{info}\nx = 1\n```")
    assert out == "``` text\nx = 1\n```"


def test_fence_with_backticks_in_info_is_not_a_fence() -> None:
    out = s("``` a`b\n@user")
    assert f"@{ZWJ}user" in out


# --------------------------------------------------------------------------------------
# Code spans
# --------------------------------------------------------------------------------------


def test_code_span_fence_is_longer_than_any_backtick_run() -> None:
    assert code_span("a") == "`a`"
    assert code_span("a`b") == "``a`b``"
    assert code_span("`x`") == "`` `x` ``"
    assert code_span("a\nb") == "`a b`"


def test_unmatched_backticks_are_escaped_and_cannot_capture_urls() -> None:
    out = s("a`https://evil.example and `b")
    assert "evil" not in _prose_outside_code(out)


def test_backslash_before_generated_code_span() -> None:
    out = s("\\https://evil.example")
    assert "evil" not in _prose_outside_code(out)
    assert out == "\\ `https://evil.example`"


def test_adjacent_code_spans_do_not_merge() -> None:
    out = s("`x`https://evil.example")
    assert out == "`x` `https://evil.example`"


# --------------------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------------------


def test_cap_adds_ellipsis_and_respects_limit() -> None:
    out = s("word " * 100, 50)
    assert len(out) <= 50 and out.endswith("…")
    assert s("short", 50) == "short"
    assert s("anything", 0) == ""


def test_cap_never_cuts_a_url_out_of_its_code_span() -> None:
    text = "word " * 50 + "https://evil.example/" + "a" * 50
    for limit in range(1, 340):
        out = s(text, limit)
        assert len(out) <= limit
        assert "evil" not in _prose_outside_code(out), (limit, out)
        assert s(out, limit) == out


def test_cap_closes_a_cut_fence() -> None:
    out = s("intro\n```\n" + "line\n" * 100 + "```\nafter", 60)
    assert len(out) <= 60
    assert out.count("```") == 2


# --------------------------------------------------------------------------------------
# Properties: idempotence, no live markers, mentions or foreign links
# --------------------------------------------------------------------------------------

_PIECES = [
    "`",
    "``",
    "\\",
    "@u",
    "#1",
    "http://evil.example/x",
    " ",
    "\n",
    "[a](http://evil.example)",
    "<b>",
    "&#64;",
    "```\n",
    "~~~",
    "# ",
    "-->",
    "<!--",
    "morning-star-review:",
    "x",
    "https://github.com/a",
    "|",
    "\n\n\n\n",
    "---\n",
    "![i](http://evil.example/i.png)",
    "www.evil.example",
    ")",
    "(",
    "]",
    "[",
]


@pytest.mark.parametrize("seed", range(6))
def test_sanitizers_are_idempotent_and_safe(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(1500):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 30)))
        limit = rng.randint(5, 400)
        for fn in (sanitize_md, sanitize_inline):
            out = fn(text, limit)
            assert len(out) <= limit
            assert fn(out, limit) == out, (fn.__name__, text, out)
            assert "<!--" not in out and "-->" not in out
            assert "morning-star-review:" not in out.lower()
            # Links to github.com are allowed; "#1" or "@" inside one is part of the URL.
            prose = re.sub(r"https?://github\.com\S*", "", _prose_outside_code(out))
            assert not re.search(r"(?<![A-Za-z0-9])@[A-Za-z0-9]", prose), (text, out)
            assert not re.search(r"#\d", prose), (text, out)
            assert "evil.example" not in prose, (text, out)


# --------------------------------------------------------------------------------------
# sanitize_inline
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a | b", "a \\| b"),
        ("already \\| escaped", "already \\| escaped"),
        ("`x|y`", "`x\\|y`"),
        ("multi\nline\n\ntitle", "multi line title"),
        ("# not a heading", "\\# not a heading"),
        ("```python", "\\`\\`\\`python"),
        ("ping @me", f"ping @{ZWJ}me"),
        ("see https://evil.example", "see `https://evil.example`"),
    ],
)
def test_sanitize_inline(text: str, expected: str) -> None:
    assert sanitize_inline(text, 200) == expected


def test_sanitize_inline_cap() -> None:
    out = sanitize_inline("x" * 500, 100)
    assert len(out) == 100 and out.endswith("…")


_ADVERSARIAL = [
    "a-" * 30000,
    "a" + " " * 60000 + "x",
    "*" * 60000 + "x",
    "\\" * 60000 + "`",
    "".join("`" * k + " x " for k in range(1, 300)),
    "<" * 60000,
    "[" * 60000 + "](",
    "[a](" * 15000,
    (" \t" * 1000 + "\n") * 30,
    "> " * 30000 + "#",
    "&#" * 30000,
    "https://" + "." * 60000 + "x",
    "https://a" + ")" * 60000,
]


@pytest.mark.parametrize("text", _ADVERSARIAL)
def test_sanitizers_are_fast_on_adversarial_input(text: str) -> None:
    import time

    for limit in (60_000, 1_000):
        start = time.monotonic()
        sanitize_md(text, limit)
        sanitize_inline(text, 300)
        assert time.monotonic() - start < 3


@pytest.mark.parametrize(
    ("code", "reasons"),
    [
        ("    return a < b and items[#1]", []),
        ("List<String> xs = new ArrayList<>();", []),
        ("x = y  # see issue 12", []),
        ("email = 'a@b.example'", []),
        ("@property\ndef name(self):", ["contains an @mention"]),
        ("# <!-- morning-star-review:state -->", ["contains a marker"]),
        ("url = 'https://evil.example/x'", ["contains a URL"]),
        ("host = 'www.example.org'", ["contains a URL"]),
        (
            "@x https://e.example -->",
            ["contains a marker", "contains an @mention", "contains a URL"],
        ),
    ],
)
def test_suggestion_hazards(code: str, reasons: list[str]) -> None:
    assert suggestion_hazards(code) == reasons


# --------------------------------------------------------------------------------------
# Regressions: inputs that GitHub's parser (cmark-gfm) reads differently from a naive
# splitter. Each one produced a live mention, a foreign link or a suggestion block
# before the fix; the expected outputs were checked against cmark-gfm.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A code span cannot cross a blank line (two paragraphs).
        ("`foo\n\n@user `", f"\\`foo\n\n@{ZWJ}user \\`"),
        # Nor a table cell: GFM splits the row at pipes before it parses code spans.
        (
            "| a `| b |\n|---|---|\n| @octocat ` | c |",
            f"| a \\`| b |\n|---|---|\n| @{ZWJ}octocat \\` | c |",
        ),
        # A literal backtick before "@" does not stop a mention; neither does "_".
        ("`@octocat", f"\\`@{ZWJ}octocat"),
        ("_@u_", f"_@{ZWJ}u_"),
    ],
)
def test_code_spans_and_mentions_follow_block_boundaries(text: str, expected: str) -> None:
    assert s(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # An indented fence may sit in a list item and end with it on GitHub; written at
        # column 0 it is a top-level block, so the code really stays code.
        (
            "- item\n  ```\n@user [x](https://evil.example)\n```",
            "- item\n```\n@user [x](https://evil.example)\n```",
        ),
        ("1. item\n\n   ```py\n   code\n   ```", "1. item\n\n```py\ncode\n```"),
        # A tab-indented run is not a closing fence (CommonMark counts a tab as 4).
        ("```\n\t```\n```\n@octocat\n```", f"```\n\t```\n```\n@{ZWJ}octocat\n```\n```"),
        # GitHub takes the language from the first word after decoding entities.
        ("```suggestion\tx\nrm\n```", "``` text\nrm\n```"),
        ("```sugges&#116;ion\nx\n```", "``` text\nx\n```"),
        # A tilde fence inside a quote or list item is escaped (it is prose here).
        ("> ~~~suggestion\n> x\n> ~~~", "> \\~~~suggestion\n> x\n> \\~~~"),
    ],
)
def test_fences_are_parsed_as_github_does(text: str, expected: str) -> None:
    assert s(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Code spans bind tighter than link text but not than a link destination.
        ("[`a`](//evil.example)", "\\[`a`\\](//evil.example)"),
        ("[x](//evil.example/`a`)", "\\[x\\](//evil.example/`a`)"),
        # Reference definitions: from an image, in a list item, with a two-line label.
        ("![x]: //evil.example\n\nsee [x]", "\\[x\\]: //evil.example\n\nsee \\[x\\]"),
        ("- [x]: //evil.example\n\nsee [x]", "- \\[x\\]: //evil.example\n\nsee \\[x\\]"),
        ("[x\ny]: //evil.example\n\nsee [x y]", "\\[x\ny\\]: //evil.example\n\nsee \\[x y\\]"),
        # Unwrapped link text must not land at a line start as a heading.
        ("[# x](//evil.example)", "\\[# x\\] (`//evil.example`)"),
        # A bare URL is autolinked up to the next space, swallowing a backtick; the
        # <autolink> form has an exact end.
        (
            "https://github.com/a` [a](//evil.example) `",
            "<https://github.com/a>` [a](//evil.example) `",
        ),
        ("\\https://github.com/x`a b`", "\\ <https://github.com/x>`a b`"),
        ("https://github.com/x--", "`https://github.com/x--`"),
    ],
)
def test_links_cannot_be_assembled_from_pieces(text: str, expected: str) -> None:
    assert s(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("~~~suggestion", "\\~~~suggestion"),
        ("1. step", "1\\. step"),
        ("> quote", "\\> quote"),
        ("- item", "\\- item"),
        ("a \\\\| b", "a \\\\\\| b"),  # an escaped backslash does not escape the pipe
    ],
)
def test_sanitize_inline_escapes_line_start_blocks_and_pipes(text: str, expected: str) -> None:
    assert sanitize_inline(text, 100) == expected
