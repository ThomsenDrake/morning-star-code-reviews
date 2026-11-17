"""Validation and rendering of model-proposed suggestion blocks (ADDENDUM E).

A GitHub suggestion replaces the commented lines with the block's content, and an
empty block means "delete these lines". A suggestion is therefore posted only when
every rule below holds; otherwise it is dropped, the finding is kept, and the reason
is recorded in the finding's internal ``notes``:

1. the anchor is ``inline`` (or ``snapped`` with an exact evidence match), and the
   range is anchorable, inside one hunk and at most 20 lines long;
2. the replacement is not empty or blank and differs from the current lines after
   whitespace normalisation;
3. its first non-blank line keeps the indentation style of the original line;
4. the head file still parses with the replacement applied (Python, JSON, YAML,
   TOML), or for other files the bracket balance does not change;
5. it contains no marker, ``@mention`` or URL.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from morning_star_review.core.contract import AnchorFile, AnchorKind, FinalFinding, Suggestion
from morning_star_review.core.ids import norm
from morning_star_review.core.sanitize import suggestion_hazards
from morning_star_review.review.anchoring import (
    evidence_variants,
    is_anchorable,
    line_text,
    same_hunk,
)

__all__ = [
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


def _exact_in_range(head_lines: list[str], start: int, end: int, evidence: str) -> bool:
    """The evidence equals one line of the range, or the whole range (normalised)."""
    variants = [norm(v) for v in evidence_variants(evidence)]
    if not variants:
        return False
    texts = [line_text(head_lines, n) or "" for n in range(start, end + 1)]
    if norm("\n".join(texts)) in variants:
        return True
    return any(norm(t) in variants for t in texts)


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
    problem = _indent_problem(replacement, original) or _syntax_problem(
        path, lines, start, end, replacement
    )
    if problem:
        return _drop(problem)
    hazards = suggestion_hazards(raw)
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
