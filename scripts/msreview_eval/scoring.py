"""Score one ``msreview review --json`` outcome against a case's answer key.

Pure functions only: the inputs are the parsed outcome JSON, the captured
stderr (for the ``msreview usage:`` / ``msreview agent:`` / ``msreview:``
progress lines), the head file contents and a little run metadata.

Definitions used in the table:

- A finding **detects** a bug when it is on the bug's path (or an alternative
  location), its line range overlaps the bug's lines widened by the case
  tolerance, and its title, explanation or fix mentions at least one of the
  bug's keywords (not the evidence, which quotes the code). A finding without
  a line detects a bug on keywords and path alone.
- **Inline** detection additionally needs ``state == "open"`` and an
  ``inline``/``snapped`` anchor: the comment would sit on the diff.
- A **false positive** is a finding of severity medium or higher that does
  not touch any current bug. Hits on cosmetic edits and on bugs that a later
  commit fixed are false positives too and are also counted separately.
- An **anchor is valid** when the normalised evidence occurs in the
  normalised text of the anchored head lines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from morning_star_review.core.ids import norm
from msreview_eval.cases import BugKey, CaseKey, Location

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ANCHORED_KINDS = ("inline", "snapped")
MatchKind = Literal["detected", "located", "none"]


def at_least_medium(severity: str) -> bool:
    return SEVERITY_RANK.get(severity, 3) <= SEVERITY_RANK["medium"]


@dataclass(frozen=True)
class Finding:
    """The subset of ``FinalFinding`` the scorer needs, parsed leniently."""

    path: str
    line: int | None
    start_line: int | None
    anchor: str
    severity: str
    title: str
    evidence: str
    explanation: str
    fix: str
    state: str
    verify_status: str | None = None
    votes: tuple[str, ...] = ()
    suggestion: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Finding:
        suggestion = data.get("suggestion")
        if isinstance(suggestion, Mapping):
            suggestion = str(suggestion.get("text") or "")
        return cls(
            path=str(data.get("path") or ""),
            line=_opt_int(data.get("line")),
            start_line=_opt_int(data.get("start_line")),
            anchor=str(data.get("anchor") or ""),
            severity=str(data.get("severity") or "low"),
            title=str(data.get("title") or ""),
            evidence=str(data.get("evidence") or ""),
            explanation=str(data.get("explanation") or ""),
            fix=str(data.get("fix") or ""),
            state=str(data.get("state") or ""),
            verify_status=data.get("verify_status"),
            votes=tuple(str(v) for v in data.get("votes") or ()),
            suggestion=str(suggestion) if suggestion else None,
        )

    def span(self) -> tuple[int, int] | None:
        if self.line is None:
            return None
        start = self.start_line if self.start_line is not None else self.line
        return (min(start, self.line), max(start, self.line))

    def text(self) -> str:
        """The finding's own words, for keyword matching. The evidence is left out: it
        quotes the code, so on a bug line it would contain the keywords by itself."""
        return "\n".join((self.title, self.explanation, self.fix)).lower()

    @property
    def is_inline(self) -> bool:
        return self.state == "open" and self.anchor in ANCHORED_KINDS and self.line is not None

    def label(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"[{self.severity}] {where} {self.title}"[:200]


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def keyword_hit(text: str, keywords: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def _overlaps(span: tuple[int, int], loc: Location, tolerance: int) -> bool:
    start, end = span
    return start <= loc.lines[1] + tolerance and end >= loc.lines[0] - tolerance


def match_bug(finding: Finding, bug: BugKey, tolerance: int) -> MatchKind:
    """How well ``finding`` matches ``bug``: detected, located (place only) or none."""
    keywords_ok = keyword_hit(finding.text(), bug.keywords)
    best: MatchKind = "none"
    span = finding.span()
    for loc in bug.locations():
        if finding.path != loc.path:
            continue
        if span is None:
            if keywords_ok:
                return "detected"
            continue
        if _overlaps(span, loc, tolerance):
            if keywords_ok:
                return "detected"
            best = "located"
    return best


def cosmetic_hit(finding: Finding, case: CaseKey) -> bool:
    if finding.path in case.cosmetic_paths:
        return True
    span = finding.span()
    if span is None:
        return False
    return any(c.path == finding.path and _overlaps(span, c, 1) for c in case.cosmetic)


def anchor_valid(finding: Finding, head_lines: Sequence[str] | None) -> bool:
    """True when the evidence occurs in the anchored head line(s)."""
    span = finding.span()
    if span is None or head_lines is None:
        return False
    start, end = span
    if start < 1 or end > len(head_lines):
        return False
    evidence = norm(finding.evidence)
    if not evidence:
        return False
    return evidence in norm(" ".join(head_lines[start - 1 : end]))


# --------------------------------------------------------------------------------------
# Progress lines on stderr (DESIGN 14, with the msreview prefix)
# --------------------------------------------------------------------------------------

_KV = re.compile(r"([A-Za-z_][\w.-]*)=(\S+)")


def parse_kv(text: str) -> dict[str, str]:
    """``a=1 b=x;`` -> ``{"a": "1", "b": "x"}`` (trailing ``;``/``,`` stripped)."""
    return {k: v.rstrip(";,") for k, v in _KV.findall(text)}


@dataclass(frozen=True)
class StderrInfo:
    scope: str | None = None
    agents: tuple[dict[str, str], ...] = ()
    usage_lines: tuple[str, ...] = ()

    @property
    def complete(self) -> bool | None:
        if not self.agents:
            return None
        return all(a.get("complete", "").lower() == "true" for a in self.agents)

    @property
    def budget_exhausted(self) -> bool | None:
        if not self.agents:
            return None
        return any(a.get("budget_exhausted", "").lower() == "true" for a in self.agents)


def parse_stderr(text: str) -> StderrInfo:
    scope: str | None = None
    agents: list[dict[str, str]] = []
    usage: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("msreview agent:"):
            agents.append(parse_kv(line.removeprefix("msreview agent:")))
        elif line.startswith("msreview usage:"):
            usage.append(line.removeprefix("msreview usage:").strip())
        elif line.startswith("msreview:") and "scope=" in line:
            scope = parse_kv(line).get("scope", scope)
    return StderrInfo(scope=scope, agents=tuple(agents), usage_lines=tuple(usage))


# --------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------


@dataclass
class UsageTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    formatter_used: bool | None = None

    @classmethod
    def from_outcome(cls, usage: Mapping[str, Any]) -> UsageTotals:
        totals = cls()
        if not usage:
            return totals
        totals.formatter_used = False
        for key, entry in usage.items():
            if not isinstance(entry, Mapping):
                continue
            totals.input_tokens += int(entry.get("input_tokens") or 0)
            totals.cached_input_tokens += int(entry.get("cached_input_tokens") or 0)
            totals.output_tokens += int(entry.get("output_tokens") or 0)
            totals.reasoning_tokens += int(entry.get("reasoning_tokens") or 0)
            totals.llm_calls += int(entry.get("llm_calls") or 0)
            totals.tool_calls += int(entry.get("tool_calls") or 0)
            if str(key).startswith("formatter") and int(entry.get("llm_calls") or 0) > 0:
                totals.formatter_used = True
        return totals


@dataclass
class RunScore:
    case: str
    config: str
    run: int
    status: str | None = None
    verdict: str | None = None
    error: str | None = None
    bug_ids: list[str] = field(default_factory=list)
    detected: dict[str, bool] = field(default_factory=dict)
    inline_detected: dict[str, bool] = field(default_factory=dict)
    located_only: list[str] = field(default_factory=list)
    refuted_bugs: list[str] = field(default_factory=list)
    findings: int = 0
    findings_medium: int = 0
    false_positives: list[str] = field(default_factory=list)
    cosmetic_hits: list[str] = field(default_factory=list)
    stale_hits: list[str] = field(default_factory=list)
    duplicates: int = 0
    anchored: int = 0
    anchors_valid: int = 0
    invalid_anchors: list[str] = field(default_factory=list)
    invalid_inline: int = 0  # open inline comments whose evidence is not on their lines
    empty_suggestions: int = 0  # rendered suggestion blocks with no replacement text
    forbidden_hits: list[str] = field(default_factory=list)
    scope: str | None = None
    complete: bool | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    cost_usd: float = 0.0
    seconds: float | None = None
    head_sha_ok: bool | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def n_detected(self) -> int:
        return sum(self.detected.values())

    @property
    def n_inline(self) -> int:
        return sum(self.inline_detected.values())

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[^\n]*$", re.MULTILINE | re.DOTALL)
# A code span never crosses a blank line (paragraph end); without that limit a stray
# backtick would pair with a later span and hide the prose in between.
_CODE_SPAN = re.compile(r"(`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)\1(?!`)", re.DOTALL)
# An opening ```suggestion fence followed directly (blank lines only) by its closing fence.
_EMPTY_SUGGESTION = re.compile(
    r"^ {0,3}(`{3,})suggestion[^\n]*\n(?:[ \t]*\n)*? {0,3}\1`*[ \t]*$", re.MULTILINE
)


def strip_code(markdown: str) -> str:
    """Markdown without fenced blocks and inline code spans (their text is inert)."""
    return _CODE_SPAN.sub(" ", _FENCE.sub(" ", markdown))


def _rendered_output(outcome: Mapping[str, Any]) -> str:
    """What the bot publishes. Findings are sanitised when rendered, so the raw
    finding fields are not checked; the rendered report contains every finding."""
    return "\n".join((str(outcome.get("report_markdown") or ""), str(outcome.get("reason") or "")))


def empty_suggestion_blocks(markdown: str) -> int:
    """Suggestion blocks with nothing inside: GitHub renders them as "delete these lines"."""
    return len(_EMPTY_SUGGESTION.findall(markdown))


def leaked_strings(case: CaseKey, outcome: Mapping[str, Any]) -> list[str]:
    rendered = _rendered_output(outcome)
    prose = strip_code(rendered).lower()
    hits = [s for s in case.expect.forbidden_output if s.lower() in prose]
    hits += [
        p
        for p in case.expect.forbidden_patterns
        if re.search(p, rendered, re.IGNORECASE | re.DOTALL)
    ]
    return hits


def _refuted_findings(outcome: Mapping[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    for raw in outcome.get("refuted") or ():
        if isinstance(raw, Mapping):
            out.append(
                Finding.from_json(
                    {
                        "path": raw.get("path"),
                        "line": raw.get("line"),
                        "severity": raw.get("severity"),
                        "title": raw.get("title"),
                        "explanation": raw.get("reason"),
                        "state": "refuted",
                    }
                )
            )
    return out


def score_run(
    case: CaseKey,
    outcome: Mapping[str, Any] | None,
    *,
    config: str,
    run: int,
    stderr: str = "",
    head: Mapping[str, Sequence[str]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> RunScore:
    """Score one run. ``outcome`` is None when the CLI produced no JSON."""
    meta = meta or {}
    info = parse_stderr(stderr)
    score = RunScore(case=case.case, config=config, run=run, scope=info.scope)
    score.bug_ids = [b.id for b in case.bugs]
    score.detected = dict.fromkeys(score.bug_ids, False)
    score.inline_detected = dict.fromkeys(score.bug_ids, False)
    score.complete = info.complete
    seconds = meta.get("seconds")
    score.seconds = float(seconds) if isinstance(seconds, int | float) else None
    if outcome is None:
        score.error = str(meta.get("error") or "no JSON outcome")
        score.checks = {"outcome": False}
        return score

    score.status = outcome.get("status")
    score.verdict = outcome.get("verdict")
    if score.status not in ("posted", "report"):
        score.error = f"status={score.status}: {outcome.get('reason') or ''}"[:300]
    score.cost_usd = float(outcome.get("cost_usd") or 0.0)
    score.usage = UsageTotals.from_outcome(outcome.get("usage") or {})
    expected_sha = meta.get("expected_head_sha")
    if expected_sha and outcome.get("head_sha"):
        score.head_sha_ok = outcome.get("head_sha") == expected_sha

    findings = [
        Finding.from_json(f) for f in outcome.get("findings") or () if isinstance(f, Mapping)
    ]
    score.findings = len(findings)
    hits_per_bug: dict[str, int] = dict.fromkeys(score.bug_ids, 0)
    for finding in findings:
        medium = at_least_medium(finding.severity)
        score.findings_medium += medium
        matched_current = False
        for bug in case.bugs:
            kind = match_bug(finding, bug, case.tolerance_for(bug))
            if kind == "detected":
                matched_current = True
                hits_per_bug[bug.id] += 1
                score.detected[bug.id] = True
                if finding.is_inline:
                    score.inline_detected[bug.id] = True
            elif kind == "located":
                matched_current = True
                score.located_only.append(f"{bug.id}: {finding.label()}")
        if finding.anchor in ANCHORED_KINDS and finding.line is not None:
            score.anchored += 1
            lines = head.get(finding.path) if head is not None else None
            if anchor_valid(finding, lines):
                score.anchors_valid += 1
            else:
                score.invalid_anchors.append(finding.label())
                score.invalid_inline += finding.is_inline and lines is not None
        if matched_current or not medium:
            continue
        score.false_positives.append(finding.label())
        if any(match_bug(finding, b, case.tolerance_for(b)) != "none" for b in case.fixed):
            score.stale_hits.append(finding.label())
        if cosmetic_hit(finding, case):
            score.cosmetic_hits.append(finding.label())
    score.duplicates = sum(max(0, n - 1) for n in hits_per_bug.values())
    # Located-only entries for bugs that were detected anyway are not interesting.
    score.located_only = [
        entry for entry in score.located_only if not score.detected[entry.split(":", 1)[0]]
    ]
    for refuted in _refuted_findings(outcome):
        for bug in case.bugs:
            if (
                not score.detected[bug.id]
                and match_bug(refuted, bug, case.tolerance_for(bug)) != "none"
            ):
                score.refuted_bugs.append(bug.id)
    score.refuted_bugs = sorted(set(score.refuted_bugs))

    score.forbidden_hits = leaked_strings(case, outcome)
    score.empty_suggestions = empty_suggestion_blocks(str(outcome.get("report_markdown") or ""))
    score.checks = _checks(case, score, head_known=head is not None)
    return score


def _checks(case: CaseKey, score: RunScore, *, head_known: bool = True) -> dict[str, bool]:
    expect = case.expect
    checks: dict[str, bool] = {"status": score.status in ("posted", "report")}
    checks["suggestions"] = score.empty_suggestions == 0  # hands-on lesson 5
    if expect.valid_anchors and head_known:
        checks["anchors"] = score.invalid_inline == 0
    allowed = list(expect.verdicts)
    if expect.verdicts_if_missed is not None and score.n_detected < len(case.bugs):
        allowed = list(expect.verdicts_if_missed)
    if allowed:
        checks["verdict"] = score.verdict in allowed
    if expect.min_detected_per_run is not None:
        checks["detected"] = score.n_detected >= expect.min_detected_per_run
    if expect.max_fp_medium is not None:
        checks["false_positives"] = len(score.false_positives) <= expect.max_fp_medium
    if expect.max_cosmetic_hits is not None:
        checks["cosmetic"] = len(score.cosmetic_hits) <= expect.max_cosmetic_hits
    if expect.forbidden_output or expect.forbidden_patterns:
        checks["forbidden_output"] = not score.forbidden_hits
    if expect.max_cost_usd is not None:
        checks["cost"] = score.cost_usd <= expect.max_cost_usd
    if expect.scope is not None and score.scope is not None:
        checks["scope"] = score.scope == expect.scope
    if score.head_sha_ok is not None:
        checks["head_sha"] = score.head_sha_ok
    return checks
