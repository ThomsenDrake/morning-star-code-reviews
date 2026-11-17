"""Plain-text grammars for explorer notes and verifier verdicts, and tolerant parsers.

Explorer notes (DESIGN 6.7, plus the ADDENDUM E ``suggestion`` field) and verifier
output (ADDENDUM D) are plain text, because models follow a simple line grammar far
more reliably than a JSON schema while they are still using tools. The parsers accept
the usual deviations (list markers, bold, backticks, numbering, field aliases, missing
END lines, line ranges, mixed case, multi-line code written on one line with literal
``\\n`` escapes). Whatever they cannot read is reported as ``unparsed`` so the caller
can fall back to the formatter.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from morning_star_review.core.contract import (
    Category,
    DraftFinding,
    PriorStatus,
    PriorStatusValue,
    Severity,
    VerifyEntry,
    VerifyStatus,
)

__all__ = [
    "GRAMMAR_HELP",
    "VERDICT_GRAMMAR_HELP",
    "ParsedNotes",
    "ParsedVerdicts",
    "is_placeholder_summary",
    "looks_like_unformatted_findings",
    "parse_notes",
    "parse_verdicts",
]

GRAMMAR_HELP = """\
Write your review notes in this plain-text format. Do not use JSON.

For each problem, write one block:

FINDING
file: <path of the file in the repository>
line: <line number in the new version of the file>
end_line: <last line, only when the problem spans several lines>
severity: critical | high | medium | low
category: security | correctness | reliability | performance | other
title: <one short line>
evidence: <the code on that line, copied character for character>
why: <what goes wrong, for which input or state; may take several lines>
fix: <how to correct it; may take several lines>
suggestion:
<<<
<optional: the exact replacement for lines line..end_line, with the file's indentation>
>>>
END FINDING

Rules:
- Line numbers refer to the new (head) version of the file.
- Leave out "suggestion" unless you are sure of the exact replacement code. Never
  write an empty suggestion.
- For each earlier finding you were asked to re-check, write one line:
  PRIOR <id>: fixed | still_open | unsure - <short reason>
- If you found no problems, write the line: NO FINDINGS
- Then write: SUMMARY: <two to five sentences on what you checked and what you found>
- End with the line: REVIEW COMPLETE
"""

VERDICT_GRAMMAR_HELP = """\
For every candidate, write exactly one block in this plain-text format:

VERDICT <candidate id>
status: confirmed | refuted | uncertain
line: <corrected line number in the new version of the file; omit if correct>
evidence: <corrected code on that line, copied exactly; omit if correct>
severity: <corrected severity: critical | high | medium | low; omit if correct>
reason: <one or two sentences that cite the code you read>
END VERDICT

Use "confirmed" only when the code you read shows the problem is real, "refuted"
only when the code shows it is not, and "uncertain" otherwise. Do not add new
candidates. After the last block, write the line: VERIFY COMPLETE
"""

_TITLE_MAX = 160
_EVIDENCE_MAX = 500
_EXPLANATION_MAX = 4000
_FIX_MAX = 2000
_SUGGESTION_MAX = 2000
_SUMMARY_MAX = 4000
_PRIOR_REASON_MAX = 500
_VERIFY_REASON_MAX = 1000
_BLOCK_MAX = 2000
_ID_MAX = 40


@dataclass
class ParsedNotes:
    findings: list[DraftFinding] = field(default_factory=list)
    priors: list[PriorStatus] = field(default_factory=list)
    summary: str = ""
    marker_count: int = 0  # FINDING marker lines seen
    unparsed: list[str] = field(default_factory=list)  # raw blocks that could not be read
    no_findings_declared: bool = False
    sentinel: bool = False  # REVIEW COMPLETE, with no FINDING after it
    # Every FINDING block was closed: by END FINDING, or implicitly by the next FINDING,
    # PRIOR, NO FINDINGS, SUMMARY: or REVIEW COMPLETE (models often omit END FINDING).
    # False only when the notes stop inside a block, which is what a cut-off looks like.
    closed_blocks: bool = True
    ends_with_summary: bool = False  # summary text after the last block / NO FINDINGS
    dropped_suggestions: int = 0  # suggestion fields that were empty or unterminated

    @property
    def lossless(self) -> bool:
        """Every FINDING marker became a finding and nothing was left unread."""
        return not self.unparsed and self.marker_count == len(self.findings)

    @property
    def looks_complete(self) -> bool:
        """The notes part of the explore-completeness rule (DESIGN 6.6).

        A block without END FINDING counts as closed when a later marker ends it (see
        ``closed_blocks``); notes that stop inside a block are never complete.
        """
        has_content = bool(self.findings or self.priors or self.summary)
        has_content = has_content or self.no_findings_declared
        return has_content and self.closed_blocks and (self.sentinel or self.ends_with_summary)


@dataclass
class ParsedVerdicts:
    entries: list[VerifyEntry] = field(default_factory=list)
    marker_count: int = 0  # VERDICT marker lines seen
    unparsed: list[str] = field(default_factory=list)
    sentinel: bool = False  # VERIFY COMPLETE
    closed_blocks: bool = True  # every VERDICT block had its END VERDICT
    unknown_ids: list[str] = field(default_factory=list)  # ids that match no candidate
    duplicate_ids: list[str] = field(default_factory=list)  # later blocks win
    unformatted: bool = False  # no blocks, but the text talks about verdicts

    @property
    def lossy(self) -> bool:
        """The formatter should run: blocks that did not parse, or prose verdicts."""
        return bool(self.unparsed) or self.unformatted

    def addressed(self) -> set[str]:
        return {e.candidate_id for e in self.entries}

    def missing(self, candidate_ids: Collection[str]) -> list[str]:
        """Candidates with no verdict (the caller treats them as ``uncertain``)."""
        done = self.addressed()
        return [c for c in candidate_ids if c not in done]


# --------------------------------------------------------------------------------------
# Line normalisation and markers
# --------------------------------------------------------------------------------------

# Leading decoration a model may put before a marker or field: blockquote, heading
# hashes, list bullets or numbers.
_LEAD = re.compile(r"^\s*(?:>\s*)*(?:#{1,6}\s+)?(?:(?:[-*+•]|\d{1,3}[.)])\s+)?")
_WRAP_LEFT = "*_`["  # bold/italic/code markup and brackets around a whole marker line
_WRAP_RIGHT = "*_`]"
_SCAN = 300  # field and marker syntax lives at the start of a line
# Hyphen, en dash, em dash: models use all three as separators. The hyphen is escaped
# because this is spliced into regex character classes.
_DASHES = "\\-\u2013\u2014"

_END_FINDING = re.compile(r"^END(?:\s+OF)?[\s_-]*FINDING(?:\s*#?\s*\d+)?\s*[.:]?$", re.I)
_FINDING = re.compile(
    rf"^FINDING\b(?P<num>\s*#?\s*\d+)?(?P<sep>\s*[:.)\]{_DASHES}])?\s*(?P<rest>.*)$", re.I
)
_PRIOR = re.compile(
    r"^PRIOR\b\s*(?:finding\s*)?[:#]?\s*[*_`]*(?P<id>[A-Za-z0-9_-]{1,64})[*_`]*"
    rf"\s*[:{_DASHES}]?\s*(?P<rest>.*)$",
    re.I,
)
_NO_FINDINGS = re.compile(r"^NO\s+(?:NEW\s+)?(?:FINDINGS?|ISSUES)(?:\s+FOUND)?\s*[.!]?$", re.I)
_SUMMARY = re.compile(rf"^SUMMARY(?:\s*[:{_DASHES}]\s*(?P<rest>.*))?$", re.I)
_REVIEW_COMPLETE = re.compile(r"^REVIEW[\s_-]+COMPLETE\s*[.!]?$", re.I)

_END_VERDICT = re.compile(r"^END(?:\s+OF)?[\s_-]*VERDICT\s*[.:]?$", re.I)
_VERDICT = re.compile(
    r"^VERDICT\b\s*(?:for\s+)?(?:candidate\s+)?[:#]?\s*(?P<id>[A-Za-z0-9_-]{1,64})\b"
    rf"\s*[:{_DASHES}]?\s*(?P<rest>.*)$",
    re.I,
)
_VERIFY_COMPLETE = re.compile(r"^VERIF(?:Y|ICATION)[\s_-]+COMPLETE\s*[.!]?$", re.I)

_FIELD = re.compile(
    r"^\s*(?:>\s*)*(?:(?:[-*+•]|\d{1,3}[.)])\s+)?(?:\*\*|__|\*|_|`)?"
    r"(?P<key>[A-Za-z][A-Za-z _-]{0,24}?(?:\(s\))?)\s*(?:\*\*|__|\*|_|`)?\s*:"
    r"(?:\s*(?:\*\*|__))?\s?(?P<val>.*)$"
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _bare(line: str) -> str:
    """The line without decoration and with whitespace runs collapsed, for markers.

    Collapsing keeps the marker patterns linear on adversarial whitespace.
    """
    text = _LEAD.sub("", " ".join(line.split()))
    previous = None
    while previous != text:
        previous = text
        text = text.lstrip(_WRAP_LEFT).rstrip(_WRAP_RIGHT).strip()
    return text


def _key(raw: str) -> str:
    """Normalised field name: "End Line" -> "end_line", "Line(s)" -> "lines"."""
    return re.sub(r"[\s-]+", "_", raw.strip().lower().replace("(s)", "s"))


_FINDING_KEYS: dict[str, str] = {}
for _canonical, _aliases in {
    "file": ("file", "files", "path", "filename", "file_name", "file_path", "filepath", "location"),
    "line": ("line", "lines", "line_number", "line_no", "lineno", "start_line", "line_range"),
    "end_line": ("end_line", "endline", "end", "last_line", "line_end", "to_line"),
    "severity": ("severity", "sev", "priority", "level"),
    "category": ("category", "type", "kind", "class"),
    "title": ("title", "headline", "issue", "name"),
    "evidence": ("evidence", "code", "snippet", "quote", "offending_code"),
    "why": (
        "why",
        "explanation",
        "description",
        "problem",
        "details",
        "impact",
        "rationale",
        "failure",
        "reason",
    ),
    "fix": (
        "fix",
        "suggested_fix",
        "recommendation",
        "remediation",
        "solution",
        "how_to_fix",
        "proposed_fix",
    ),
    "suggestion": ("suggestion", "suggested_change", "replacement", "patch"),
}.items():
    for _alias in _aliases:
        _FINDING_KEYS[_alias] = _canonical

_VERDICT_KEYS: dict[str, str] = {}
for _canonical, _aliases in {
    "status": ("status", "verdict", "result", "decision"),
    "line": ("line", "corrected_line", "line_number", "head_line"),
    "evidence": ("evidence", "corrected_evidence", "code"),
    "severity": ("severity", "corrected_severity", "sev"),
    "reason": ("reason", "why", "explanation", "rationale", "justification", "notes"),
}.items():
    for _alias in _aliases:
        _VERDICT_KEYS[_alias] = _canonical

_SINGLE_VALUE = frozenset({"file", "line", "end_line", "severity", "category", "title", "status"})


# --------------------------------------------------------------------------------------
# Value parsers
# --------------------------------------------------------------------------------------

_SEVERITY_ALIASES: dict[str, Severity] = {
    "critical": "critical",
    "crit": "critical",
    "blocker": "critical",
    "severe": "critical",
    "high": "high",
    "major": "high",
    "important": "high",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "normal": "medium",
    "low": "low",
    "minor": "low",
    "trivial": "low",
    "nit": "low",
    "info": "low",
}
_CATEGORY_ALIASES: dict[str, Category] = {
    "security": "security",
    "sec": "security",
    "vulnerability": "security",
    "correctness": "correctness",
    "bug": "correctness",
    "logic": "correctness",
    "functional": "correctness",
    "reliability": "reliability",
    "robustness": "reliability",
    "concurrency": "reliability",
    "error": "reliability",
    "resilience": "reliability",
    "performance": "performance",
    "perf": "performance",
    "efficiency": "performance",
    "other": "other",
    "style": "other",
    "maintainability": "other",
    "readability": "other",
}
_WORD = re.compile(r"[A-Za-z]+")


def _severity(text: str) -> Severity | None:
    for word in _WORD.findall(text.lower())[:4]:
        if word in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[word]
    return None


def _category(text: str) -> Category:
    for word in _WORD.findall(text.lower())[:4]:
        if word in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[word]
    return "other"


_RANGE = re.compile(
    rf"(?<![\w.])L?(?P<a>\d{{1,7}})(?:\s*(?:[{_DASHES}]|\.\.|to|through|:)\s*L?(?P<b>\d{{1,7}}))?"
)


def _line_range(text: str) -> tuple[int | None, int | None]:
    """First line number (and range end) in ``text``; ``(None, None)`` if there is none."""
    m = _RANGE.search(text[:200])
    if m is None:
        return None, None
    start = int(m.group("a"))
    end = int(m.group("b")) if m.group("b") else None
    if start <= 0:
        return None, None
    if end is not None and end < start:
        start, end = end, start
        if start <= 0:
            return None, None
    return start, (end if end != start else None)


_PATH_LINE = re.compile(
    r"^(?P<path>.+?)(?:#L(?P<g1>\d+)(?:-L?(?P<g2>\d+))?"
    r"|:(?P<c1>\d+)(?:[-:](?P<c2>\d+))?"
    rf"|[\s,]+\(?(?:lines?|L)\s*(?P<p1>\d+)(?:\s*[{_DASHES}]\s*(?P<p2>\d+))?\)?)$",
    re.I,
)


def _path_and_line(value: str) -> tuple[str, int | None, int | None]:
    """Split ``src/a.py:12-14``, ``src/a.py#L12`` or ``src/a.py (line 12)``."""
    text = _unwrap(value.strip()[:600])  # longer than any valid path; bounds the regex
    start: int | None = None
    end: int | None = None
    m = _PATH_LINE.match(text)
    if m:
        text = m.group("path")
        nums = [m.group(k) for k in ("g1", "g2", "c1", "c2", "p1", "p2")]
        found = [int(n) for n in nums if n]
        if found and found[0] > 0:
            start = found[0]
            end = found[1] if len(found) > 1 and found[1] > start else None
    path = _unwrap(text.strip()).strip().rstrip(",;:")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/"), start, end


def _unwrap(text: str) -> str:
    """Remove one layer of wrapping: a fenced block, backticks, quotes or bold."""
    lines = text.strip("\n").split("\n")
    if len(lines) >= 2 and _FENCE.match(lines[0]) and _FENCE.match(lines[-1]):
        return "\n".join(lines[1:-1])
    if len(lines) >= 2 and _FENCE.match(lines[0]):  # unterminated fence
        return "\n".join(lines[1:])
    s = text.strip()
    for left, right in (("```", "```"), ("``", "``"), ("`", "`"), ("**", "**"), ('"', '"')):
        if len(s) > len(left) + len(right) and s.startswith(left) and s.endswith(right):
            return s[len(left) : -len(right)].strip() if left != '"' else s[1:-1]
    return s


_ESCAPES = {"n": "\n", "t": "\t"}
_STRING_PREFIXES = frozenset({"b", "f", "r", "u", "br", "rb", "fr", "rf"})  # f'..', rb'..'
_WORD_END = re.compile(r"[A-Za-z]+$")


def _is_apostrophe(text: str, i: int) -> bool:
    """``'`` at ``text[i]`` ends a word ("don't", "users'"), so it opens no string.

    A string prefix such as ``f`` in ``f'{x}'`` is the exception: that quote opens one.
    """
    m = _WORD_END.search(text, max(0, i - 40), i)
    if m is None:
        return False
    before = text[m.start() - 1] if m.start() else " "
    standalone = not (before.isalnum() or before == "_")
    return not (standalone and m.group().lower() in _STRING_PREFIXES)


def _unescape(text: str, *, code_spans: bool) -> str:
    """Undo literal ``\\n`` and ``\\t`` escapes in one-line text.

    Some models (Mistral Large 4) write several lines of code on one line, joined by
    the two characters backslash and ``n``. Text with a real newline is returned as
    written, and so are sequences inside a string literal (``sep = "\\n"``), behind an
    escaped backslash (``\\\\n``) and, with ``code_spans``, inside a backtick code span
    (prose fields). Runs of three or more backticks are code fences, not spans.
    """
    if "\n" in text or "\\" not in text:
        return text
    out: list[str] = []
    quote: str | None = None
    changed = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if quote is None and nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                changed = True
            else:  # escaped backslash or quote, or an escape inside a literal: keep
                out.append(text[i : i + 2])
            i += 2
            continue
        if ch == "`":
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            if code_spans and j - i < 3 and quote in (None, "`"):
                quote = None if quote == "`" else "`"
            out.append(text[i:j])
            i = j
            continue
        if ch in "\"'":
            if quote == ch:
                quote = None
            elif quote is None and not (ch == "'" and _is_apostrophe(text, i)):
                quote = ch
        out.append(ch)
        i += 1
    return "".join(out) if changed else text


def _code_value(text: str) -> str:
    """An evidence value: one layer of wrapping removed and one-line escapes undone.

    Backticks around the value are wrapping, not a code span, so they do not stop the
    unescaping: ``if x:\\n    raise E()`` in backticks becomes two lines, and the
    anchoring step then matches its first non-blank line (``evidence_variants``).
    """
    return _unwrap(_unescape(text.strip(), code_spans=False))


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _prose(text: str) -> str:
    """Strip the indentation of prose lines (it would render as a code block on GitHub)
    but keep fenced code as written."""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        if fence is None:
            m = _FENCE.match(line)
            if m:
                fence = m.group(1)
            out.append(line.strip())
        else:
            out.append(line)
            if _is_closing_fence(line, fence):
                fence = None
    return "\n".join(out).strip()


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    m = re.search(r"(?<=[.!?])\s", flat)
    return flat[: m.start()] if m else flat


_NONE_WORDS = frozenset(
    {"", "none", "n/a", "na", "-", "unchanged", "same", "no change", "null", "nil", "correct"}
)


def _is_none_value(text: str) -> bool:
    return text.strip().strip(".").strip("`*_").strip().lower() in _NONE_WORDS


_PRIOR_STATUS = re.compile(
    rf"^(?P<status>still[\s_-]*open|not[\s_-]*fixed|open|fixed|resolved|addressed|unsure"
    r"|unknown|unclear|uncertain|can'?t\s+tell)\b"
    rf"\s*(?:[{_DASHES}:;,(]\s*)?(?P<reason>.*)$",
    re.I,
)
_PRIOR_MAP: dict[str, PriorStatusValue] = {
    "stillopen": "still_open",
    "notfixed": "still_open",
    "open": "still_open",
    "fixed": "fixed",
    "resolved": "fixed",
    "addressed": "fixed",
}

_VERIFY_STATUS = re.compile(
    r"^(?P<status>confirmed|confirm|valid|true[\s_-]*positive|real|correct"
    r"|refuted|refute|invalid|false[\s_-]*positive|rejected|not[\s_-]*a[\s_-]*bug|incorrect"
    r"|uncertain|unsure|unclear|unknown|inconclusive|unconfirmed|can'?t\s+(?:confirm|tell))\b",
    re.I,
)
_VERIFY_MAP: dict[str, VerifyStatus] = {
    "confirmed": "confirmed",
    "confirm": "confirmed",
    "valid": "confirmed",
    "truepositive": "confirmed",
    "real": "confirmed",
    "correct": "confirmed",
    "refuted": "refuted",
    "refute": "refuted",
    "invalid": "refuted",
    "falsepositive": "refuted",
    "rejected": "refuted",
    "notabug": "refuted",
    "incorrect": "refuted",
}


def _prior_status(text: str) -> tuple[PriorStatusValue, str] | None:
    m = _PRIOR_STATUS.match(_bare(text))
    if m is None:
        return None
    word = re.sub(r"[\s_-]+", "", m.group("status").lower())
    status: PriorStatusValue = _PRIOR_MAP.get(word, "unsure")
    reason = m.group("reason").strip().rstrip(")").strip()
    return status, reason


def _verify_status(text: str) -> VerifyStatus | None:
    m = _VERIFY_STATUS.match(_bare(text))
    if m is None:
        return None
    word = re.sub(r"[\s_-]+", "", m.group("status").lower())
    return _VERIFY_MAP.get(word, "uncertain")


def _clean_id(raw: str) -> str:
    return raw.strip().strip("`*_[]()#:.,").strip()


# --------------------------------------------------------------------------------------
# FINDING blocks
# --------------------------------------------------------------------------------------


@dataclass
class _Block:
    raw: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    current: str | None = None  # field that continuation lines belong to
    stray: list[str] = field(default_factory=list)
    marker_rest: str = ""
    in_fence: bool = False
    awaiting_suggestion: bool = False  # saw "suggestion:" with no value yet
    suggestion: list[str] | None = None  # lines while capturing <<< ... >>>
    suggestion_close: str = ">>>"  # ">>>" or the code fence that opened it
    suggestion_text: str | None = None
    suggestion_dropped: int = 0

    def add(self, key: str, value: str) -> None:
        self.fields.setdefault(key, []).append(value)
        self.current = key

    def text(self, key: str) -> str:
        return "\n".join(self.fields.get(key, [])).strip()


def _is_closing_fence(line: str, opener: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) == {opener[0]} and len(s) >= len(opener)


def _start_suggestion(b: _Block, value: str) -> None:
    b.current = None
    v = value.strip()
    if b.suggestion_text is not None:  # only the first suggestion counts
        b.suggestion_dropped += 1
        b.current = None
        return
    if v.startswith("<<<"):
        rest = v[3:]
        if ">>>" in rest:
            _finish_suggestion(b, [rest.split(">>>", 1)[0]])
            return
        b.suggestion = [rest] if rest.strip() else []
        b.suggestion_close = ">>>"
        return
    fence = _FENCE.match(v)
    if fence:
        b.suggestion = []
        b.suggestion_close = fence.group(1)
        return
    if _is_none_value(v):
        b.awaiting_suggestion = not v
        return
    b.add("fix", v)  # a prose suggestion is a fix description


def _finish_suggestion(b: _Block, lines: list[str]) -> None:
    b.suggestion = None
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    text = "\n".join(line.rstrip() for line in lines)
    if not text.strip() or len(text) > _SUGGESTION_MAX:
        b.suggestion_dropped += 1  # never render an empty or truncated suggestion
        return
    b.suggestion_text = text


def _feed_finding(b: _Block, line: str) -> None:
    """One line inside a FINDING block (markers were handled by the caller)."""
    b.raw.append(line)
    if b.suggestion is not None:
        stripped = line.strip()
        if b.suggestion_close == ">>>":
            done = stripped in (">>>", "&gt;&gt;&gt;")
        else:
            done = _is_closing_fence(line, b.suggestion_close)
        if done:
            _finish_suggestion(b, b.suggestion)
        else:
            b.suggestion.append(line)
        return
    if b.awaiting_suggestion:
        stripped = line.strip()
        if not stripped:
            return
        b.awaiting_suggestion = False
        if stripped.startswith("<<<") or _FENCE.match(stripped):
            _start_suggestion(b, stripped)
            return
        if not _FIELD.match(line[:_SCAN]):  # "suggestion:" then prose: a fix description
            b.fields.setdefault("fix", [])
            b.current = "fix"
    if b.in_fence:
        b.fields.setdefault(b.current or "why", []).append(line.rstrip())
        if _FENCE.match(line):
            b.in_fence = False
        return
    m = _FIELD.match(line[:_SCAN])
    key = _FINDING_KEYS.get(_key(m.group("key"))) if m else None
    if m is not None and key is not None:
        value = line[m.start("val") :].strip()
        if key == "suggestion":
            _start_suggestion(b, value)
            return
        already = b.text(key)
        if already and key != b.current and b.current in ("why", "fix", "evidence"):
            # "Code: ..." inside an explanation is prose, not a second evidence field.
            b.fields[b.current].append(line.rstrip())
            return
        b.add(key, value)
        if value and _FENCE.match(value):
            b.in_fence = True
        return
    if b.current is None or b.current in _SINGLE_VALUE:
        if line.strip():
            b.stray.append(line.strip())
        return
    b.fields[b.current].append(line.rstrip())
    if _FENCE.match(line):
        b.in_fence = True


_TITLE_SEVERITY = re.compile(
    rf"^\s*[\[(]?\s*(?P<sev>critical|high|medium|low)\s*[\])]?\s*[:{_DASHES}]?\s*",
    re.I,
)


def _build_finding(b: _Block) -> tuple[DraftFinding | None, int]:
    """The finding for a block (or None if it lacks a path, severity or text), and the
    number of suggestions that were dropped."""
    dropped = b.suggestion_dropped + (1 if b.suggestion is not None else 0)
    path, line, end = (
        _path_and_line(b.text("file").split("\n")[0])
        if "file" in b.fields
        else (
            "",
            None,
            None,
        )
    )
    if b.text("line"):
        line, end = _line_range(b.text("line"))
    if b.text("end_line") and line is not None:
        last, _ = _line_range(b.text("end_line"))
        end = last if last is not None and last > line else None
    if b.text("title"):
        title = _unwrap(_unescape(b.text("title").split("\n")[0], code_spans=True))
    else:
        title = _unescape(b.marker_rest, code_spans=True)
    severity = _severity(b.text("severity")) if b.text("severity") else None
    tm = _TITLE_SEVERITY.match(title)
    if tm:  # "FINDING 2 (high): ..." or "title: [HIGH] ..."
        severity = severity or _SEVERITY_ALIASES[tm.group("sev").lower()]
        title = title[tm.end() :]
    title = _unwrap(title.strip())
    why = _prose(_unescape(b.text("why"), code_spans=True))
    stray = "\n".join(b.stray).strip()
    explanation = "\n\n".join(part for part in (why, stray) if part)
    if not title and explanation:
        title = _first_sentence(explanation)
    if not explanation:
        explanation = title
    if not path or len(path) > 500 or severity is None or not title:
        return None, dropped
    fix = _prose(_unescape(b.text("fix"), code_spans=True))
    finding = DraftFinding(
        path=path,
        line=line,
        end_line=end,
        severity=severity,
        category=_category(b.text("category")),
        title=_cap(" ".join(title.split()), _TITLE_MAX),
        evidence=_cap(_code_value(b.text("evidence")), _EVIDENCE_MAX),
        explanation=_cap(explanation, _EXPLANATION_MAX),
        fix=_cap(fix, _FIX_MAX) if fix and not _is_none_value(fix) else None,
        suggestion=b.suggestion_text,
    )
    return finding, dropped


def _finding_marker(bare: str) -> re.Match[str] | None:
    m = _FINDING.match(bare)
    if m is None:
        return None
    # "Finding the bug took..." is prose: a marker has a number, a separator or nothing.
    if m.group("num") or m.group("sep") or not m.group("rest").strip():
        return m
    return None


def _prior_line(bare: str) -> PriorStatus | str | None:
    """A PriorStatus, the raw text of an unreadable PRIOR line, or None for prose."""
    m = _PRIOR.match(bare)
    if m is None:
        return None
    pid = _clean_id(m.group("id"))
    rest = re.sub(r"^status\s*[:=]\s*", "", m.group("rest").strip(), flags=re.I)
    parsed = _prior_status(rest)
    if parsed is None or len(pid) > _ID_MAX:
        # Ids carry digits; "Prior to this change ..." is prose, not a broken PRIOR line.
        return bare if any(ch.isdigit() for ch in pid) else None
    status, reason = parsed
    return PriorStatus(id=pid, status=status, reason=_cap(reason, _PRIOR_REASON_MAX))


def _normalise_newlines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_notes(notes: str) -> ParsedNotes:
    """Parse explorer notes written in the ``GRAMMAR_HELP`` format (tolerantly)."""
    out = ParsedNotes()
    priors: dict[str, PriorStatus] = {}
    block: _Block | None = None
    in_summary = False
    summary_lines: list[str] = []
    preamble: list[str] = []
    tail: list[str] = []  # outside text since the last block or NO FINDINGS
    structure_seen = False
    sentinel_at = -1
    last_marker_at = -1

    def close() -> None:
        nonlocal block
        if block is None:
            return
        finding, dropped = _build_finding(block)
        out.dropped_suggestions += dropped
        if finding is None:
            out.unparsed.append(_cap("\n".join(block.raw), _BLOCK_MAX))
        else:
            out.findings.append(finding)
        block = None

    for index, line in enumerate(_normalise_newlines(notes)):
        strict = block is not None and (block.in_fence or block.suggestion is not None)
        if block is not None and strict and not line.startswith(("FINDING", "END")):
            # Inside code only an unindented upper-case marker ends the block (a model
            # that forgot ">>>"); code such as "    finding: Finding" stays code.
            _feed_finding(block, line)
            continue
        bare = " ".join(line.split()) if strict else _bare(line)
        if _END_FINDING.match(bare):
            if block is not None:
                block.raw.append(line)
                close()
                tail = []
            continue
        marker = _finding_marker(bare)
        if marker is not None:
            close()
            in_summary = False
            structure_seen = True
            out.marker_count += 1
            last_marker_at = index
            tail = []
            out.ends_with_summary = False
            block = _Block(raw=[line], marker_rest=marker.group("rest").strip())
            continue
        if not strict:
            prior = _prior_line(bare)
            if prior is not None:
                close()
                in_summary = False
                structure_seen = True
                if isinstance(prior, PriorStatus):
                    priors.pop(prior.id, None)
                    priors[prior.id] = prior  # the last line for an id wins
                else:
                    out.unparsed.append(_cap(prior, _BLOCK_MAX))
                continue
            if _NO_FINDINGS.match(bare):
                close()
                in_summary = False
                structure_seen = True
                out.no_findings_declared = True
                tail = []
                continue
            summary = _SUMMARY.match(bare)
            if summary is not None:
                close()
                in_summary = True
                structure_seen = True
                # "**SUMMARY:** text" leaves the closing bold in front of the text.
                rest = (summary.group("rest") or "").strip().lstrip("*_").strip()
                if rest:
                    summary_lines.append(rest)
                    out.ends_with_summary = True
                continue
            if _REVIEW_COMPLETE.match(bare):
                close()
                in_summary = False
                sentinel_at = index
                continue
        if block is not None:
            _feed_finding(block, line)
        elif in_summary:
            summary_lines.append(line.rstrip())
            if line.strip():
                out.ends_with_summary = True
        elif line.strip() and sentinel_at < 0:
            if structure_seen:
                tail.append(line.strip())
                out.ends_with_summary = True
            else:
                preamble.append(line.strip())
    if block is not None:  # the notes stop inside a block: cut off, not complete
        out.closed_blocks = False
    close()
    out.priors = list(priors.values())
    out.sentinel = sentinel_at >= 0 and sentinel_at > last_marker_at
    explicit = "\n".join(summary_lines).strip()
    fallback = "\n".join(tail).strip() or "\n".join(preamble).strip()
    out.summary = _cap(explicit or fallback, _SUMMARY_MAX)
    return out


_LOCATION = re.compile(
    r"[\w./-]{1,200}\.[A-Za-z0-9]{1,10}(?::\d+|#L\d+|,?\s{0,3}\(?(?:line|L)\s{0,3}\d+)", re.I
)
_SEVERITY_WORDS = re.compile(
    r"\bseverity\b|^\W*(?:\[|\*\*|\()?(?:critical|high|medium|low)(?:\]|\*\*|\))?"
    rf"\s*[:{_DASHES}]",
    re.I | re.M,
)


def looks_like_unformatted_findings(notes: str) -> bool:
    """True when text outside FINDING blocks looks like findings: ``path:line``
    locations or severity labels. Used to decide whether "zero findings" is believable."""
    outside: list[str] = []
    in_block = False
    for line in _normalise_newlines(notes):
        bare = _bare(line)
        if _END_FINDING.match(bare):
            in_block = False
        elif _finding_marker(bare) is not None:
            in_block = True
        elif in_block and (_SUMMARY.match(bare) or _REVIEW_COMPLETE.match(bare)):
            in_block = False
            outside.append(line)
        elif not in_block:
            outside.append(line)
    text = "\n".join(outside)
    return bool(_LOCATION.search(text) or _SEVERITY_WORDS.search(text))


_PLACEHOLDERS = frozenset(
    {
        "lgtm",
        "looks good",
        "looks good to me",
        "placeholder",
        "summary",
        "todo",
        "tbd",
        "none",
        "na",
        "no summary",
        "ok",
        "done",
    }
)


def is_placeholder_summary(summary: str) -> bool:
    """True for an empty or boilerplate summary ("LGTM", "placeholder", ...).

    A review whose only summary is a placeholder must not be published as clean
    (hands-on lesson 1).
    """
    words = re.sub(r"[^a-z0-9 ]+", " ", summary.lower()).split()
    flat = " ".join(words)
    squashed = "".join(words)  # "N/A" -> "na"
    if not flat or "placeholder" in words or flat in _PLACEHOLDERS or squashed in _PLACEHOLDERS:
        return True
    return words[0] == "lgtm" and len(words) <= 6


# --------------------------------------------------------------------------------------
# VERDICT blocks (verify phase, ADDENDUM D)
# --------------------------------------------------------------------------------------


def _resolve_id(raw: str, known: Collection[str] | None) -> str:
    cid = _clean_id(raw)
    if known is None or cid in known:
        return cid
    lowered = {k.lower(): k for k in known}
    if cid.lower() in lowered:
        return lowered[cid.lower()]
    if cid.isdigit() and f"c{cid}" in known:  # "VERDICT 3" for candidate c3
        return f"c{cid}"
    return cid


@dataclass
class _Verdict:
    cid: str
    raw: list[str]
    fields: dict[str, list[str]] = field(default_factory=dict)
    current: str | None = None

    def text(self, key: str) -> str:
        return "\n".join(self.fields.get(key, [])).strip()


def _feed_verdict(v: _Verdict, line: str) -> None:
    v.raw.append(line)
    m = _FIELD.match(line[:_SCAN])
    key = _VERDICT_KEYS.get(_key(m.group("key"))) if m else None
    if m is not None and key is not None and not (key == v.current == "reason"):
        v.fields.setdefault(key, []).append(line[m.start("val") :].strip())
        v.current = key
    elif v.current is not None and v.current not in _SINGLE_VALUE:
        v.fields[v.current].append(line.rstrip())
    elif line.strip():
        v.fields.setdefault("reason", []).append(line.strip())


_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _build_verdict(v: _Verdict) -> VerifyEntry | None:
    status = _verify_status(v.text("status")) if v.text("status") else None
    if status is None or not _CANDIDATE_ID_RE.match(v.cid):
        return None
    line_text = v.text("line")
    line = None if _is_none_value(line_text) else _line_range(line_text)[0]
    evidence_text = _code_value(v.text("evidence"))
    evidence = None if _is_none_value(evidence_text) else _cap(evidence_text, _EVIDENCE_MAX)
    severity_text = v.text("severity")
    severity = None if _is_none_value(severity_text) else _severity(severity_text)
    return VerifyEntry(
        candidate_id=v.cid,
        status=status,
        line=line,
        evidence=evidence,
        severity=severity,
        reason=_cap(_prose(v.text("reason")), _VERIFY_REASON_MAX),
    )


_PROSE_VERDICT = re.compile(r"\b(?:confirmed|refuted|uncertain|false positive)\b", re.I)


def _verdict_marker(bare: str, known: Collection[str] | None) -> tuple[str, str] | None:
    """(candidate id, rest of line) for a VERDICT marker, or None.

    "Verdict: confirmed" is the status field, not a marker: a marker names an id,
    and ids carry a digit (c1, c2, ...) or are known candidate ids.
    """
    m = _VERDICT.match(bare)
    if m is None:
        return None
    cid = _resolve_id(m.group("id"), known)
    if not (any(ch.isdigit() for ch in cid) or (known is not None and cid in known)):
        return None
    return cid, m.group("rest").strip()


def parse_verdicts(text: str, candidate_ids: Collection[str] | None = None) -> ParsedVerdicts:
    """Parse verifier output written in the ``VERDICT_GRAMMAR_HELP`` format (tolerantly).

    With ``candidate_ids``, ids are matched case-insensitively (and ``3`` means ``c3``);
    verdicts for unknown ids are reported in ``unknown_ids`` and dropped. When an id
    has several verdicts, the last one wins.
    """
    out = ParsedVerdicts()
    known = set(candidate_ids) if candidate_ids is not None else None
    entries: dict[str, VerifyEntry] = {}
    current: _Verdict | None = None
    sentinel_at = -1
    last_marker_at = -1

    def close(explicit: bool) -> None:
        nonlocal current
        if current is None:
            return
        if not explicit:
            out.closed_blocks = False
        entry = _build_verdict(current)
        if entry is None:
            out.unparsed.append(_cap("\n".join(current.raw), _BLOCK_MAX))
        elif known is not None and entry.candidate_id not in known:
            out.unknown_ids.append(entry.candidate_id)
        else:
            if entry.candidate_id in entries:
                out.duplicate_ids.append(entry.candidate_id)
                del entries[entry.candidate_id]
            entries[entry.candidate_id] = entry
        current = None

    for index, line in enumerate(_normalise_newlines(text)):
        bare = _bare(line)
        if _END_VERDICT.match(bare):
            if current is not None:
                current.raw.append(line)
                close(True)
            continue
        marker = _verdict_marker(bare, known)
        if marker is not None:
            close(False)
            out.marker_count += 1
            last_marker_at = index
            cid, rest = marker
            current = _Verdict(cid=cid, raw=[line])
            if rest and _verify_status(rest) is not None:  # "VERDICT c3: confirmed"
                current.fields["status"] = [rest]
            continue
        if _VERIFY_COMPLETE.match(bare):
            close(False)
            sentinel_at = index
            continue
        if current is not None:
            _feed_verdict(current, line)
    close(False)
    out.entries = list(entries.values())
    out.sentinel = sentinel_at >= 0 and sentinel_at > last_marker_at
    out.unformatted = out.marker_count == 0 and bool(_PROSE_VERDICT.search(text))
    return out
