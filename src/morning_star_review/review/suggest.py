"""Validation and rendering of model-proposed suggestion blocks (ADDENDUM E).

A GitHub suggestion replaces the commented lines with the block's content, and an
empty block means "delete these lines". A suggestion is therefore posted only when
every rule below holds; otherwise it is dropped, the finding is kept, and the reason
is recorded in the finding's internal ``notes``:

1. the anchor is ``inline`` (or ``snapped`` with an exact evidence match), and the
   range is anchorable, inside one hunk and at most 20 lines long;
2. the replacement is not empty or blank, differs from the current lines after
   whitespace normalisation, and is not a no-op for the flagged code: when every
   evidence line of the range is still there and the rest only changes whitespace or
   comments, nothing would be fixed;
3. its first non-blank line keeps the indentation style of the original line;
4. the head file still parses with the replacement applied (Python, JSON, YAML,
   TOML), or for other files the bracket balance does not change. A parse is not
   enough: a replacement whose first lines repeat the head lines just before the
   range, or whose last lines repeat the lines just after it, is dropped too (models
   rewrite a whole call but anchor only its inner lines, and the duplicated
   ``rows = conn.execute(`` still parses as a keyword argument); so is a replacement
   that changes the line count by more than ``MAX_LINE_DELTA`` (scope creep);
5. it contains no marker (``<!--`` or ``morning-star-review:``) and no line that
   would close the fence it is posted in. ``-->`` alone is ordinary code (``i-->0``,
   arrows in comments) and forges nothing without the other two. ``@decorators``,
   ``#`` comments and URLs are allowed: GitHub links no mention, reference or URL
   inside a fenced block, and ``render_block`` picks a fence longer than any
   backtick run in the text.

The explanation and the prose fix are sanitised as usual; only the code goes verbatim.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from pathlib import PurePosixPath

import yaml

from morning_star_review.core.contract import AnchorFile, AnchorKind, FinalFinding, Suggestion
from morning_star_review.core.ids import evidence_match, norm
from morning_star_review.core.sanitize import suggestion_hazards
from morning_star_review.review.anchoring import (
    evidence_variants,
    is_anchorable,
    line_text,
    same_hunk,
)

__all__ = [
    "MAX_LINE_DELTA",
    "MAX_SUGGESTION_CHARS",
    "MAX_SUGGESTION_LINES",
    "SuggestionCheck",
    "check",
    "fence_for",
    "render_block",
    "validate",
]

MAX_SUGGESTION_LINES = 20
MAX_SUGGESTION_CHARS = 2000
MAX_LINE_DELTA = 10  # replacement lines minus range lines, either way
_WRAPPING_FENCE = re.compile(r"^\s*(`{3,}|~{3,})[\w+.#-]*\s*$")
_BRACKETS = ("()", "[]", "{}")


@dataclass(frozen=True, slots=True)
class SuggestionCheck:
    """Outcome of ``check``: a suggestion to post, or the reason one was dropped.

    Both are ``None`` when the model proposed no suggestion.
    """

    suggestion: Suggestion | None = None
    reason: str | None = None


def _drop(reason: str) -> SuggestionCheck:
    return SuggestionCheck(suggestion=None, reason=f"suggestion dropped: {reason}")


def _clean_replacement(raw: str) -> list[str]:
    """Replacement lines: CRs removed, outer blank lines and one wrapping fence dropped."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 2:
        opener = _WRAPPING_FENCE.match(lines[0])
        closer = lines[-1].strip()
        if opener and closer and set(closer) == {opener.group(1)[0]}:
            return _clean_replacement("\n".join(lines[1:-1])) if len(lines) > 2 else []
    return lines


def _flat(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _first_nonblank(lines: list[str]) -> str | None:
    return next((ln for ln in lines if ln.strip()), None)


def _indent_problem(replacement: list[str], original: list[str]) -> str | None:
    new_line, old_line = _first_nonblank(replacement), _first_nonblank(original)
    if new_line is None:
        return None
    new = set(_indent(new_line))
    old = set(_indent(old_line)) if old_line is not None else set()
    if len(new) > 1 and len(old) <= 1:
        return "the indentation mixes tabs and spaces"
    if len(old) == 1 and new and new != old:
        return "the indentation style differs from the original line"
    if old and not new:
        # Models often strip leading whitespace; GitHub would apply the line unindented.
        return "the replacement drops the indentation of the original line"
    return None


def _parse_python(text: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # e.g. SyntaxWarning for invalid escapes
        ast.parse(text)


def _parse_yaml(text: str) -> None:
    for _ in yaml.safe_load_all(text):
        pass


_PARSERS: dict[str, Callable[[str], object]] = {
    ".py": _parse_python,
    ".pyi": _parse_python,
    ".json": json.loads,
    ".yaml": _parse_yaml,
    ".yml": _parse_yaml,
    ".toml": tomllib.loads,
}


def _parses(parser: Callable[[str], object], text: str) -> bool:
    try:
        parser(text)
    except Exception:  # any parser failure means "does not parse"
        return False
    return True


def _bracket_delta(lines: list[str]) -> tuple[int, ...]:
    text = "\n".join(lines)
    return tuple(text.count(pair[0]) - text.count(pair[1]) for pair in _BRACKETS)


def _syntax_problem(
    path: str, head_lines: list[str], start: int, end: int, replacement: list[str]
) -> str | None:
    original = head_lines[start - 1 : end]
    parser = _PARSERS.get(PurePosixPath(path).suffix.lower())
    if parser is not None and _parses(parser, "\n".join(head_lines)):
        patched = [*head_lines[: start - 1], *replacement, *head_lines[end:]]
        if not _parses(parser, "\n".join(patched)):
            return "the file no longer parses with the replacement applied"
        return None
    # Unknown language, or the head file did not parse to begin with.
    if _bracket_delta(replacement) != _bracket_delta(original):
        return "the replacement changes the bracket balance"
    return None


def _range_problem(
    anchor: AnchorKind,
    exact: bool,
    start: int,
    end: int,
    anchor_file: AnchorFile | None,
    range_rejected: bool,
) -> str | None:
    if anchor == "snapped" and not exact:
        return "the finding was snapped without an exact evidence match"
    if anchor not in ("inline", "snapped"):
        return "the finding is not anchored to a changed line"
    if range_rejected:
        return "the requested line range cannot be commented on"
    if end - start + 1 > MAX_SUGGESTION_LINES:
        return f"the range is longer than {MAX_SUGGESTION_LINES} lines"
    if anchor_file is not None and not (
        is_anchorable(anchor_file, start)
        and is_anchorable(anchor_file, end)
        and same_hunk(anchor_file, start, end)
    ):
        return "the range is not inside one diff hunk"
    return None


# --- rule 2: no-op for the flagged code ----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Syntax:
    """Just enough of a language to tell code from comments and layout."""

    comments: tuple[str, ...] = ()  # line-comment starts
    blocks: bool = False  # /* ... */ comments
    indent_matters: bool = True


def _for(suffixes: str, syn: _Syntax) -> dict[str, _Syntax]:
    return {f".{suffix}": syn for suffix in suffixes.split()}


_HASH = _Syntax(comments=("#",))
_C_LIKE = _Syntax(comments=("//",), blocks=True, indent_matters=False)
_C_AND_HASH = _Syntax(comments=("//", "#"), blocks=True, indent_matters=False)
_SYNTAXES: dict[str, _Syntax] = {
    **_for("py pyi rb sh bash zsh yaml yml toml cfg ini r pl pm ex exs nix ps1 jl tcl mk", _HASH),
    **_for("js mjs cjs ts mts cts jsx tsx go rs java kt kts c h cc cpp cxx hpp hh", _C_LIKE),
    **_for("cs swift scala dart m mm groovy gradle proto zig sol jsonc css scss less", _C_LIKE),
    **_for("php tf hcl", _C_AND_HASH),
    **_for("sql lua hs elm", _Syntax(comments=("--",), indent_matters=False)),
}
_QUOTES = "\"'`"


def _syntax_of(path: str) -> _Syntax:
    pure = PurePosixPath(path)
    if pure.name in ("Dockerfile", "Makefile"):
        return _HASH
    return _SYNTAXES.get(pure.suffix.lower(), _Syntax())  # unknown: whitespace only


def _strip_comment(line: str, syn: _Syntax) -> str:
    """``line`` without its comment; quote-aware, one line at a time.

    A line comment must start the line or follow whitespace (``http://``, ``$#``).
    Misreads only make two lines look different, which keeps the suggestion.
    """
    if syn.blocks and re.match(r"\s*(\*/|\*\s|\*$)", line):
        return ""  # inside a /** ... */ block
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            out.append(line[i : i + 2] if ch == "\\" else ch)
            i += 2 if ch == "\\" else 1
            quote = None if ch == quote else quote
            continue
        if ch in _QUOTES:
            quote = ch
        elif syn.blocks and line.startswith("/*", i):
            close = line.find("*/", i + 2)
            if close < 0:
                break
            out.append(" ")
            i = close + 2
            continue
        elif (i == 0 or line[i - 1].isspace()) and line.startswith(syn.comments, i):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _code_key(lines: list[str], syn: _Syntax) -> str:
    """The code of ``lines`` without comments, blank lines and insignificant spaces."""
    keyed: list[str] = []
    for line in lines:
        code = _strip_comment(line, syn)
        if code.strip():
            body = "".join(code.split())
            keyed.append(_indent(code) + body if syn.indent_matters else body)
    return "\n".join(keyed)


def _evidence_lines(original: list[str], evidence: str, syn: _Syntax) -> list[tuple[str, bool]]:
    """Lines of the range that the evidence quotes (whole, by line, or ``\\n``-joined).

    The flag says whether the quote is code only; then a comment the replacement adds
    to that line (``# noqa``, ``// TODO``) does not count as changing it.
    """
    pieces = evidence_variants(evidence)
    for text in (evidence, evidence.replace("\\n", "\n")):
        pieces += [ln for ln in text.splitlines() if norm(ln)]
    found: list[tuple[str, bool]] = []
    for line in original:
        quotes = [p for p in pieces if evidence_match(line, p)]
        if quotes:
            found.append((line, all(norm(_strip_comment(p, syn)) == norm(p) for p in quotes)))
    return found


def _noop_problem(
    path: str, original: list[str], replacement: list[str], evidence: str
) -> str | None:
    syn = _syntax_of(path)
    flagged = _evidence_lines(original, evidence, syn)
    if not flagged:
        return None
    full = {norm(ln) for ln in replacement}
    code = {norm(_strip_comment(ln, syn)) for ln in replacement}
    for line, code_only in flagged:
        if (norm(_strip_comment(line, syn)) not in code) if code_only else (norm(line) not in full):
            return None  # an evidence line changed or went: a real fix, even of a comment
    if _code_key(original, syn) != _code_key(replacement, syn):
        return None
    return "the replacement leaves the flagged code unchanged"


# --- rule 4: boundary repeats and scope ------------------------------------------------

_BARE_WORD = re.compile(r"\w+[:;,]?")  # "end", "else:", "pass", "break;"


def _substantive(text: str) -> bool:
    """A normalised line worth treating as a repeat: not punctuation or one bare word.

    Brackets-only lines (``)``, ``});``) are left to the parse and bracket checks, and
    one-word closers (``end``, ``fi``) legitimately end a nested replacement block.
    """
    return any(ch.isalnum() for ch in text) and not _BARE_WORD.fullmatch(text)


def _repeats(replacement: list[str], context: list[str], original: list[str]) -> bool:
    """Some leading lines of ``replacement`` equal the last lines of ``context``.

    Compared normalised and without blank lines. At least one repeated line must be
    substantive, and the range must not already start with the same lines (then the
    head file repeats them itself). Callers reverse all three lists to compare the
    trailing side.
    """
    rep, ctx, old = ([n for n in map(norm, part) if n] for part in (replacement, context, original))
    for k in range(1, min(len(rep), len(ctx)) + 1):
        lead = rep[:k]
        if lead == ctx[-k:] and lead != old[:k] and any(_substantive(n) for n in lead):
            return True
    return False


def _boundary_problem(
    head_lines: list[str], start: int, end: int, replacement: list[str]
) -> str | None:
    window = len(replacement)  # non-blank context lines that could be repeated
    original = head_lines[start - 1 : end]
    before = [*islice((ln for ln in reversed(head_lines[: start - 1]) if ln.strip()), window)]
    after = [*islice((ln for ln in head_lines[end:] if ln.strip()), window)]
    if _repeats(replacement, before[::-1], original):
        return "the replacement repeats the line before the range"
    if _repeats(replacement[::-1], after[::-1], original[::-1]):
        return "the replacement repeats the line after the range"
    return None


def _scope_problem(replacement: list[str], original: list[str]) -> str | None:
    if abs(len(replacement) - len(original)) > MAX_LINE_DELTA:
        return f"the replacement changes the line count by more than {MAX_LINE_DELTA}"
    return None


def _exact_in_range(head_lines: list[str], start: int, end: int, evidence: str) -> bool:
    """The evidence equals one line of the range, or the whole range (normalised)."""
    variants = [norm(v) for v in evidence_variants(evidence)]
    if not variants:
        return False
    texts = [line_text(head_lines, n) or "" for n in range(start, end + 1)]
    if norm("\n".join(texts)) in variants:
        return True
    return any(norm(t) in variants for t in texts)


def _hazards(text: str) -> list[str]:
    """Rule 5 on the text that is posted, against the fence ``render_block`` will use.

    ``suggestion_hazards`` also counts a bare ``-->`` as a marker; in code it is an
    arrow or ``i-->0``, so it is spaced out first. That cannot form ``<!--`` or
    ``morning-star-review:`` (a line holding ``<!-->`` still holds ``<!--``), and no
    line with ``-->`` in it could close a backtick fence.
    """
    return suggestion_hazards(text.replace("-->", "-- >"), fence=fence_for(text))


def check(
    raw: str | None,
    *,
    path: str,
    anchor: AnchorKind,
    line: int | None,
    start_line: int | None,
    evidence: str,
    head_lines: list[str] | None,
    anchor_file: AnchorFile | None = None,
    range_rejected: bool = False,
) -> SuggestionCheck:
    """Apply rules 1-5 to the raw replacement for head lines ``start_line..line``.

    ``line``/``start_line`` follow the GitHub convention (``line`` is the end of a
    range). ``anchor_file`` lets rule 1 re-check the range against the patch.
    """
    if raw is None:
        return SuggestionCheck()
    if line is None:
        return _drop("the finding has no line")
    start, end = (start_line, line) if start_line is not None else (line, line)
    if head_lines is None or not 1 <= start <= end <= len(head_lines):
        return _drop("the head file or the lines are not available")
    lines = [ln.rstrip("\r\n") for ln in head_lines]
    exact = _exact_in_range(lines, start, end, evidence)
    problem = _range_problem(anchor, exact, start, end, anchor_file, range_rejected)
    if problem:
        return _drop(problem)
    replacement = _clean_replacement(raw)
    text = "\n".join(replacement)
    original = lines[start - 1 : end]
    if not text.strip():
        return _drop("the replacement is empty")
    if len(text) > MAX_SUGGESTION_CHARS:
        return _drop("the replacement is too long")
    if _flat(replacement) == _flat(original):
        return _drop("the replacement does not change the code")
    problem = (
        _noop_problem(path, original, replacement, evidence)
        or _scope_problem(replacement, original)
        or _indent_problem(replacement, original)
        or _boundary_problem(lines, start, end, replacement)
        or _syntax_problem(path, lines, start, end, replacement)
    )
    if problem:
        return _drop(problem)
    hazards = _hazards(text)
    if hazards:
        return _drop(", ".join(hazards))
    return SuggestionCheck(suggestion=Suggestion(start_line=start, end_line=end, text=text))


def validate(
    f: FinalFinding,
    head_lines: list[str] | None,
    path: str,
    *,
    raw: str | None = None,
    anchor_file: AnchorFile | None = None,
    range_rejected: bool = False,
) -> Suggestion | None:
    """ADDENDUM E entry point: the validated suggestion for ``f``, or ``None``.

    ``raw`` is the model's replacement text; when omitted, the text of
    ``f.suggestion`` is validated again.
    """
    text = raw if raw is not None else (f.suggestion.text if f.suggestion else None)
    return check(
        text,
        path=path,
        anchor=f.anchor,
        line=f.line,
        start_line=f.start_line,
        evidence=f.evidence,
        head_lines=head_lines,
        anchor_file=anchor_file,
        range_rejected=range_rejected,
    ).suggestion


def fence_for(text: str, char: str = "`") -> str:
    """A fence of ``char`` longer than any run of it in ``text`` (at least three)."""
    longest = max((len(m) for m in re.findall(f"{re.escape(char)}+", text)), default=0)
    return char * max(3, longest + 1)


def render_block(s: Suggestion) -> str:
    """The suggestion as a fenced block GitHub turns into an "apply" button.

    Never renders an empty block: ``Suggestion`` rejects blank text.
    """
    fence = fence_for(s.text)
    return f"{fence}suggestion\n{s.text}\n{fence}"
