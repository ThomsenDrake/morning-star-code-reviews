"""Markdown and JSON rendering of the final review (DESIGN 8.5, ADDENDUM D and E).

Every piece of model-written text goes through ``core.sanitize`` before it is placed
in Markdown: titles and table cells with ``sanitize_inline``, explanations and fixes
with ``sanitize_md``. Paths (attacker-controlled file names) are shown as code spans
with markers removed and pipes escaped, and only links we build ourselves (to
github.com) appear. The only HTML comments in the output are our own markers.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from morning_star_review.core.contract import (
    SEVERITY_ORDER,
    DroppedCandidate,
    ExcludedFile,
    FinalFinding,
    PriorState,
    ResolvedPrior,
    ResolvedRoles,
    Scope,
    Severity,
    Usage,
    Verdict,
)
from morning_star_review.core.sanitize import (
    code_span,
    sanitize_inline,
    sanitize_md,
    strip_markers,
    without_controls,
)
from morning_star_review.review import suggest
from morning_star_review.review.anchoring import line_text
from morning_star_review.review.verdict import resolved_without_fix

__all__ = [
    "OTHER_HEADING",
    "REPORT_LIMIT",
    "SUMMARY_LIMIT",
    "SUMMARY_MARKER",
    "TOOL_NAME",
    "Coverage",
    "RunDetails",
    "SummaryInput",
    "blob_url",
    "finding_marker",
    "findings_json",
    "inline_body",
    "report",
    "summary",
    "verdict_headline",
]

TOOL_NAME = "Morning Star Code Review"
SUMMARY_MARKER = "<!-- morning-star-review:summary v1 -->"
# Findings without an inline comment that are neither main nor "possible" findings:
# outside the changed files, not on a changed line, or below the inline threshold.
OTHER_HEADING = "#### Other findings"
# The summary comment holds the summary plus the state marker (<= 20 KB) and must
# stay under 60,000 characters.
SUMMARY_LIMIT = 38_000
REPORT_LIMIT = 262_144
MAX_LISTED_PATHS = 20
_EXCERPT_CONTEXT = 2
_EXCERPT_LINE_MAX = 300
_SEVERITY_LABEL: dict[str, str] = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}


def finding_marker(finding_id: str) -> str:
    """Hidden marker that identifies our inline comment for ``finding_id``."""
    safe = re.sub(r"[^A-Za-z0-9_]", "", finding_id)[:40]  # no "-": "--" ends comments
    return f"<!-- morning-star-review:finding {safe} -->"


def _severity(sev: Severity) -> str:
    return _SEVERITY_LABEL[sev]


def _path_span(
    path: str, line: int | None = None, start: int | None = None, *, table: bool = False
) -> str:
    """A path (and line or range) as a code span; pipes escaped inside table cells."""
    text = strip_markers(path)
    if line is not None:
        text += f":{start}-{line}" if start is not None and start != line else f":{line}"
    span = code_span(text)
    return span.replace("|", "\\|") if table else span


def blob_url(
    owner: str, repo: str, head_sha: str, path: str, line: int | None, start: int | None = None
) -> str:
    """Link to the head version of a file (and line range) on github.com."""
    url = f"https://github.com/{quote(owner)}/{quote(repo)}/blob/{quote(head_sha)}/{quote(path)}"
    if line is not None:
        url += f"#L{start}-L{line}" if start is not None and start != line else f"#L{line}"
    return url


def _fix_block(fix: str | None) -> str:
    if not fix or not fix.strip():
        return ""
    text = sanitize_md(fix, 3000)
    if "\n" in text or text.startswith(("```", "~~~", "    ", "\t")):
        return f"**Suggested fix:**\n\n{text}"
    return f"**Suggested fix:** {text}"


def _verification_note(f: FinalFinding) -> str:
    if f.verify_status == "confirmed":
        return "confirmed by the verifier"
    if f.verify_status == "uncertain":
        return "not confirmed by the verifier"
    return ""


def _heading(f: FinalFinding) -> str:
    """``Severity · category: title``; a carried prior has no category of its own."""
    tag = " (unconfirmed)" if f.verify_status == "uncertain" else ""
    title = sanitize_inline(f.title, 400)
    if f.carried and not f.explanation.strip():
        return f"{_severity(f.severity)} · {title}{tag}"
    return f"{_severity(f.severity)} · {f.category}: {title}{tag}"


def inline_body(f: FinalFinding, model: str, sha7: str) -> str:
    """Body of the inline review comment for ``f`` (DESIGN 8.5, ADDENDUM D/E).

    ``model`` names the explorer model(s) that reported the finding.
    """
    parts = [f"**{_heading(f)}**"]
    if f.explanation.strip():
        parts.append(sanitize_md(f.explanation, 6000))
    fix = _fix_block(f.fix)
    if f.suggestion is not None:
        block = suggest.render_block(f.suggestion)
        parts.append(f"{fix}\n\n{block}" if fix else f"**Suggested fix:**\n\n{block}")
    elif fix:
        parts.append(fix)
    who = sanitize_inline(model, 300) if model.strip() else ""
    meta = " · ".join(p for p in (TOOL_NAME, who, code_span(sha7[:7]), _verification_note(f)) if p)
    parts.append(f"<sub>{meta}</sub>\n{finding_marker(f.id)}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------------
# Summary comment
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    reviewable: int
    covered: int
    excluded: Sequence[ExcludedFile] = ()
    not_opened: Sequence[str] = ()  # reviewable paths that no explorer opened

    def as_dict(self) -> dict[str, int]:
        return {
            "reviewable": self.reviewable,
            "covered": self.covered,
            "excluded": len(self.excluded),
        }


@dataclass(frozen=True, slots=True)
class RunDetails:
    execution_id: str
    run_label: str
    roles: ResolvedRoles | None
    usage: Mapping[str, Usage]
    cost_usd: float
    duration_seconds: float | None = None
    tool_version: str = ""
    verify_counts: Mapping[str, int] = field(default_factory=dict)  # confirmed/uncertain/refuted
    merge_note: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryInput:
    owner: str
    repo: str
    number: int
    head_sha: str
    verdict: Verdict
    verdict_reasons: Sequence[str]
    scope: Scope
    since_sha: str | None
    findings: Sequence[FinalFinding]
    priors: Sequence[ResolvedPrior]
    inline_min_severity: Severity
    coverage: Coverage
    details: RunDetails | None  # None when show_usage is off
    over_cap: int = 0
    author_may_dismiss: bool = True
    # False when the review has blockers (verdict.Assessment.complete); a "findings"
    # review can be incomplete too, and then its reasons say what was not covered.
    complete: bool = True


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _counts(findings: Sequence[FinalFinding]) -> str:
    by: dict[str, int] = {}
    for f in findings:
        by[f.severity] = by.get(f.severity, 0) + 1
    return ", ".join(f"{by[s]} {s}" for s in sorted(by, key=lambda s: SEVERITY_ORDER[s]))


def _is_possible(f: FinalFinding) -> bool:
    """Summary-only because the verifier could not confirm it (ADDENDUM D)."""
    return f.verify_status == "uncertain" and f.state == "summary_only" and not f.carried


def _below_threshold(f: FinalFinding, threshold: Severity) -> bool:
    return SEVERITY_ORDER[f.severity] > SEVERITY_ORDER[threshold]


def _is_main(f: FinalFinding, threshold: Severity) -> bool:
    if _is_possible(f):
        return False
    if f.state == "open":
        return True
    return f.anchor in ("inline", "snapped") and not _below_threshold(f, threshold)


def verdict_headline(s: SummaryInput) -> str:
    """The bold verdict line, with the head commit and the scope of the review."""
    possible = [f for f in s.findings if _is_possible(f)]
    open_ = [f for f in s.findings if f.state in ("open", "summary_only") and not _is_possible(f)]
    if s.verdict == "clean":
        text = "clean"
    elif s.verdict == "incomplete":
        text = "incomplete"
    elif open_:
        text = f"{_plural(len(open_), 'open finding')} ({_counts(open_)})"
    elif possible:
        text = f"{_plural(len(possible), 'possible issue')}, not confirmed"
    else:
        text = "findings"
    extra = ""
    if s.verdict == "findings" and open_ and possible:
        extra = f" · {_plural(len(possible), 'possible issue')} not confirmed"
    sha7 = code_span(s.head_sha[:7])
    since = code_span((s.since_sha or "")[:7])
    scope = {
        "full": "full review",
        "incremental": f"incremental since {since}",
        "carry_over": f"no reviewable changes since {since}",
    }[s.scope]
    return f"**Verdict: {text}**{extra} · {sha7} · {scope}"


def _location(s: SummaryInput, f: FinalFinding) -> str:
    label = _path_span(f.path, f.line, f.start_line, table=True)
    if f.anchor == "outside":
        return label
    url = blob_url(s.owner, s.repo, s.head_sha, f.path, f.line, f.start_line)
    return f"[{label}]({url})" if len(url) <= 400 else label


def _thread_url(s: SummaryInput, comment_id: int) -> str:
    return f"https://github.com/{quote(s.owner)}/{quote(s.repo)}/pull/{s.number}#discussion_r{comment_id}"


def _cell(text: str, limit: int = 200) -> str:
    return sanitize_inline(text, limit)


def _main_status(s: SummaryInput, f: FinalFinding, notes: Mapping[str, str]) -> str:
    if f.carried:
        status = "still open"
        if f.id in notes:
            status = f"still open ({_cell(notes[f.id], 150)})"
        if f.comment_id is not None:
            status = f"[{status}]({_thread_url(s, f.comment_id)})"
        return status
    if f.state == "summary_only":
        return "listed here only (inline comment limit reached)"
    return "new, not confirmed" if f.verify_status == "uncertain" else "new"


# Where a finding is anchored, in the words of the report's detail lines.
_ANCHOR_WORDS = {
    "inline": "on a changed line",
    "snapped": "on a changed line (moved to the line that matches the evidence)",
    "file": "in a changed file, not on a changed line",
    "outside": "outside this PR's changed files",
}


def _other_reason(f: FinalFinding, threshold: Severity) -> str:
    """Where an "Other findings" item is and why it has no inline comment.

    The location words match the finding's detail line in the report.
    """
    if f.anchor in ("outside", "file"):
        return _ANCHOR_WORDS[f.anchor]
    where = _ANCHOR_WORDS["inline"]
    if _below_threshold(f, threshold):
        return f"{where}, below the inline threshold ({threshold})"
    return where


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "| " + " | ".join(header) + " |"
    rule = "|" + "|".join("---" for _ in header) + "|"
    return "\n".join([head, rule, *("| " + " | ".join(r) + " |" for r in rows)])


def _findings_section(s: SummaryInput) -> str:
    notes = {p.id: p.note for p in resolved_without_fix(s.priors)}
    rows = [
        [_severity(f.severity), _cell(f.title), _location(s, f), _main_status(s, f, notes)]
        for f in s.findings
        if _is_main(f, s.inline_min_severity)
    ]
    if not rows:
        return ""
    out = _table(["Severity", "Finding", "Location", "Status"], rows)
    if s.over_cap:
        out += f"\n\n_{_plural(s.over_cap, 'more finding')} not listed (at most 50 are shown)._"
    return out


def _possible_section(s: SummaryInput) -> str:
    rows = [
        [_severity(f.severity), _cell(f.title), _location(s, f), _plural(len(f.votes), "explorer")]
        for f in s.findings
        if _is_possible(f)
    ]
    if not rows:
        return ""
    intro = "The verifier could neither confirm nor rule these out. Check them yourself."
    table = _table(["Severity", "Finding", "Location", "Reported by"], rows)
    return f"#### Possible issues (not confirmed)\n\n{intro}\n\n{table}"


def _other_section(s: SummaryInput) -> str:
    rows = [
        [
            _severity(f.severity),
            _cell(f.title),
            _location(s, f),
            _other_reason(f, s.inline_min_severity),
        ]
        for f in s.findings
        if not _is_main(f, s.inline_min_severity) and not _is_possible(f)
    ]
    if not rows:
        return ""
    return f"{OTHER_HEADING}\n\n" + _table(
        ["Severity", "Finding", "Location", "Why not inline"], rows
    )


_PRIOR_HEADINGS = {"resolve": "code changed", "file_deleted": "file deleted"}


def _resolved_section(s: SummaryInput) -> str:
    lines: list[str] = []
    for p in s.priors:
        if p.action in _PRIOR_HEADINGS:
            why = _PRIOR_HEADINGS[p.action]
        elif p.action == "dismissed" and p.note.startswith("dismissed by "):
            why = _cell(p.note, 150)
        else:
            continue
        lines.append(f"- {_cell(p.title)} ({_path_span(p.path)}): {why}")
    if not lines:
        return ""
    return "#### Resolved since the last review\n\n" + "\n".join(lines)


def _path_list(paths: Sequence[str], reasons: Mapping[str, str] | None = None) -> str:
    shown = [
        f"{_path_span(p)} ({_cell(reasons[p], 100)})" if reasons and p in reasons else _path_span(p)
        for p in paths[:MAX_LISTED_PATHS]
    ]
    more = len(paths) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def _coverage_section(s: SummaryInput) -> str:
    c = s.coverage
    lines = [f"{c.covered} of {_plural(c.reviewable, 'reviewable changed file')} reviewed."]
    if c.not_opened:
        lines.append(f"Not opened: {_path_list(list(c.not_opened))}.")
    if c.excluded:
        reasons = {e.path: e.reason for e in c.excluded}
        lines.append(
            f"Excluded ({len(c.excluded)}): {_path_list([e.path for e in c.excluded], reasons)}."
        )
    return "#### Coverage\n\n" + "\n\n".join(lines)


def _effort(roles: ResolvedRoles | None, role: str, model: str) -> str:
    if roles is None:
        return ""
    if role == "explorer":
        found = [r.effort for r in roles.explorers if r.model == model]
        return found[0] if found else ""
    if role == "verifier" and roles.verifier is not None and roles.verifier.model == model:
        return roles.verifier.effort
    if role == "formatter" and roles.formatter_model == model:
        return roles.formatter_effort
    return ""


_ROLE_ORDER = {"explorer": 0, "verifier": 1, "formatter": 2}


def _usage_order(d: RunDetails, key: str) -> tuple[int, int, str]:
    role, _, model = key.partition(":")
    explorers = [r.model for r in d.roles.explorers] if d.roles else []
    position = explorers.index(model) if role == "explorer" and model in explorers else 99
    return _ROLE_ORDER.get(role, 9), position, key


def _usage_rows(d: RunDetails) -> list[list[str]]:
    rows: list[list[str]] = []
    for key in sorted(d.usage, key=lambda k: _usage_order(d, k)):
        role, _, model = key.partition(":")
        u = d.usage[key]
        rows.append(
            [
                _cell(role, 40),
                _cell(model or u.model, 120),
                _cell(_effort(d.roles, role, model or u.model) or "-", 20),
                str(u.llm_calls),
                str(u.tool_calls),
                f"{u.input_tokens:,} ({u.cached_input_tokens:,})",
                f"{u.output_tokens:,} ({u.reasoning_tokens:,})",
                f"${u.cost_usd:.2f}",
                f"{u.seconds:.0f} s",
            ]
        )
    return rows


def _span(text: str) -> str:
    """A run fact as a code span; markers removed so it can never fake our own."""
    return code_span(strip_markers(text))


def _details_section(d: RunDetails) -> str:
    lines = [f"Execution {_span(d.execution_id)} · run {_span(d.run_label)}"]
    if d.tool_version:
        lines[0] += f" · version {_span(d.tool_version)}"
    rows = _usage_rows(d)
    if rows:
        header = [
            "Role",
            "Model",
            "Effort",
            "LLM calls",
            "Tool calls",
            "Input tokens (cached)",
            "Output tokens (reasoning)",
            "Est. cost",
            "Time",
        ]
        lines.append(_table(header, rows))
    total = f"Estimated cost: ${d.cost_usd:.2f} (list prices)"
    if d.duration_seconds is not None:
        total += f" · duration {d.duration_seconds:.0f} s"
    lines.append(total)
    if d.merge_note:
        lines.append(_cell(d.merge_note, 300))
    if d.verify_counts:
        vc = d.verify_counts
        lines.append(
            f"Verifier: {vc.get('confirmed', 0)} confirmed, "
            f"{vc.get('uncertain', 0)} not confirmed, "
            f"{vc.get('refuted', 0)} refuted and dropped"
        )
    body = "\n\n".join(lines)
    return f"<details><summary>Run details</summary>\n\n{body}\n\n</details>"


def _footer(s: SummaryInput) -> str:
    who = (
        "the PR author or anyone with write access"
        if s.author_may_dismiss
        else "write access required"
    )
    return (
        "<sub>Automated review; findings can be wrong. "
        f"Resolve a thread to dismiss a finding ({who}).</sub>"
    )


def _reasons_block(s: SummaryInput) -> str:
    """``verdict_reasons``: why the review is incomplete, or notes about it."""
    if not s.verdict_reasons:
        return ""
    items = "\n".join(f"- {_cell(r, 400)}" for r in s.verdict_reasons)
    if s.verdict == "incomplete":
        return f"The review is incomplete, so it cannot be called clean:\n{items}"
    if not s.complete:
        return f"The review did not cover everything, so there may be more findings:\n{items}"
    return f"Notes on this review:\n{items}"


def _shorten(text: str, limit: int) -> str:
    """Cut at a line boundary to fit ``limit``, closing an open ``<details>``."""
    if len(text) <= limit:
        return text
    note = "\n\n_Shortened to fit GitHub's comment size limit._"
    cut = text.rfind("\n", 0, max(limit - len(note) - 20, 0))
    head = text[: max(cut, 0)]
    if head.count("<details>") > head.count("</details>"):
        head += "\n\n</details>"
    return head + note


def _summary_sections(s: SummaryInput) -> list[str]:
    return [
        _reasons_block(s),
        _findings_section(s),
        _possible_section(s),
        _resolved_section(s),
        _other_section(s),
        _coverage_section(s),
    ]


def summary(s: SummaryInput, *, limit: int = SUMMARY_LIMIT) -> str:
    """The summary comment body without the state marker (``publish`` appends it)."""
    parts = [f"{SUMMARY_MARKER}\n### {TOOL_NAME}\n{verdict_headline(s)}", *_summary_sections(s)]
    if s.details is not None:
        parts.append(_details_section(s.details))
    body = without_controls("\n\n".join(p for p in parts if p))
    footer = _footer(s)
    return _shorten(body, limit - len(footer) - 2) + "\n\n" + footer


# --------------------------------------------------------------------------------------
# Report (report mode, step summary, artifacts)
# --------------------------------------------------------------------------------------

HeadText = Callable[[str], list[str] | None]


def _language(path: str) -> str:
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


def _fenced(text: str, language: str) -> str:
    fence = suggest.fence_for(text)
    return f"{fence}{language}\n{text}\n{fence}"


def _excerpt(path: str, f: FinalFinding, head: HeadText) -> str:
    """Head lines around the finding (two before and after), numbered, markers removed."""
    if f.line is None or f.anchor == "outside":
        return ""
    lines = head(f.path)
    if not lines:
        return ""
    first = max((f.start_line or f.line) - _EXCERPT_CONTEXT, 1)
    last = min(f.line + _EXCERPT_CONTEXT, len(lines))
    if first > last:
        return ""
    width = len(str(last))
    body = []
    for n in range(first, last + 1):
        text = strip_markers(line_text(lines, n) or "")
        if len(text) > _EXCERPT_LINE_MAX:
            text = text[: _EXCERPT_LINE_MAX - 1] + "…"
        body.append(f"{n:>{width}}  {text}".rstrip())
    return "Code at head:\n\n" + _fenced("\n".join(body), _language(path))


def _detail(i: int, f: FinalFinding, head: HeadText) -> str:
    meta = [_path_span(f.path, f.line, f.start_line), _ANCHOR_WORDS[f.anchor]]
    if f.votes:
        meta.append("reported by " + sanitize_inline(", ".join(f.votes), 300))
    if f.carried:
        meta.append("carried from the previous review")
    note = _verification_note(f)
    if note:
        meta.append(note)
    parts = [f"##### {i}. {_heading(f)}", " · ".join(meta)]
    if f.explanation.strip():
        parts.append(sanitize_md(f.explanation, 6000))
    fix = _fix_block(f.fix)
    if fix:
        parts.append(fix)
    if f.suggestion is not None:
        sg = f.suggestion
        where = (
            f"lines {sg.start_line}-{sg.end_line}"
            if sg.end_line != sg.start_line
            else f"line {sg.start_line}"
        )
        parts.append(
            f"Suggested replacement for {where}:\n\n" + _fenced(sg.text, _language(f.path))
        )
    excerpt = _excerpt(f.path, f, head)
    if excerpt:
        parts.append(excerpt)
    return "\n\n".join(parts)


def report(s: SummaryInput, head_text: HeadText, *, limit: int = REPORT_LIMIT) -> str:
    """The full report: summary, every finding in detail, coverage and run details.

    ``publish`` puts a "Not posted" banner with its reason in front of it.
    """
    sections = _summary_sections(s)
    coverage = sections.pop()  # moved after the details, as the report reads top-down
    head_part = "\n\n".join(
        p for p in [f"### {TOOL_NAME} report\n{verdict_headline(s)}", *sections] if p
    )
    tail = [coverage]
    if s.details is not None:
        tail.append(_details_section(s.details))  # its <summary> is the heading
    tail_text = "\n\n".join(tail)
    budget = limit - len(head_part) - len(tail_text) - 400
    details: list[str] = []
    used = 0
    for i, f in enumerate(s.findings, 1):
        block = _detail(i, f, head_text)
        if used + len(block) + 2 > budget:
            omitted = len(s.findings) - i + 1
            details.append(
                f"_{_plural(omitted, 'more finding')} omitted to fit the size limit; "
                "see findings.json._"
            )
            break
        details.append(block)
        used += len(block) + 2
    middle = "#### Findings in detail\n\n" + "\n\n".join(details) if details else ""
    text = "\n\n".join(p for p in (head_part, middle, tail_text) if p)
    # Paths and code excerpts are PR text: no terminal control sequences in a report
    # that `msreview review` prints (security review TERM-1).
    return _shorten(without_controls(text), limit)


def findings_json(
    *,
    findings: Sequence[FinalFinding],
    verdict: Verdict,
    verdict_reasons: Sequence[str],
    pr_url: str,
    head_sha: str,
    execution_id: str,
    models: Mapping[str, Any],
    refuted: Sequence[DroppedCandidate] = (),
    priors: Sequence[ResolvedPrior] = (),
    coverage: Mapping[str, int] | None = None,
    usage: Mapping[str, Usage] | None = None,
    cost_usd: float = 0.0,
    state: PriorState | None = None,
) -> dict[str, Any]:
    """Machine-readable findings (``findings.json``): findings plus run metadata."""
    del state  # the state marker is GitHub-side data; not part of the report
    return {
        "schema_version": 1,
        "pr_url": pr_url,
        "head_sha": head_sha,
        "execution_id": execution_id,
        "verdict": verdict,
        "verdict_reasons": list(verdict_reasons),
        "models": dict(models),
        "findings": [f.model_dump(mode="json") for f in findings],
        "refuted": [r.model_dump(mode="json") for r in refuted],
        "priors": [p.model_dump(mode="json") for p in priors],
        "coverage": dict(coverage or {}),
        "usage": {k: u.model_dump(mode="json") for k, u in (usage or {}).items()},
        "cost_usd": round(cost_usd, 6),
    }
