"""Rendering of patches and the explorer prompt.

``render_file`` produces the per-file diff text used both inline in the prompt
and by the ``show_file_diff`` tool: head line numbers in a 5-wide gutter, then the
line kind, then the text. Removed lines have no number.

``render_prompt`` produces the explorer's user message (DESIGN 6.5). Every piece of
text that comes from the change author (PR title and body, diff, file contents)
sits inside ``<untrusted-{boundary} ...>`` blocks, and any ``untrusted-`` inside
that text is neutralised so it cannot close or open a block.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from morning_star_review.core.contract import PrFile, PriorFinding
from morning_star_review.diff.patch import FilePatch

if TYPE_CHECKING:
    from morning_star_review.core.contract import (
        PrSnapshot,
        ResolvedBudgets,
        ResolvedConfig,
    )
    from morning_star_review.diff.incremental import ScopeDecision
    from morning_star_review.diff.prepass import PromptPlan
    from morning_star_review.diff.related import RelatedDefinition

__all__ = [
    "BOUNDARY_PATTERN",
    "defang",
    "missing_diff_text",
    "neutralize",
    "prompt_dismissed",
    "prompt_priors",
    "render_file",
    "render_prompt",
    "untrusted_block",
]

BOUNDARY_PATTERN = re.compile(r"^[0-9a-f]{8,64}$")
ZWJ = "\u200d"  # zero-width joiner
_UNTRUSTED = re.compile(r"(untrusted)(-)", re.IGNORECASE)

# The review_scope file table is bounded so a 3,000-file PR does not swamp the prompt;
# files past the cap are counted, and all of them stay reachable through the tools.
SCOPE_TABLE_MAX_ROWS = 400
SCOPE_TABLE_MAX_CHARS = 24_000
EXCLUDED_TABLE_MAX_CHARS = 8_000  # the excluded-files list in the scope block
EXCLUDED_PATH_MAX_CHARS = 200  # one excluded path as shown there
PRIOR_MAX = 30
_PRIOR_FIELD_MAX = 200


def neutralize(text: str) -> str:
    """Break every ``untrusted-`` so author text cannot close or open an untrusted block.

    A zero-width joiner goes between the word and the hyphen: invisible to readers,
    but the tag no longer matches the boundary the instructions name.
    """
    return _UNTRUSTED.sub(lambda m: f"{m.group(1)}{ZWJ}{m.group(2)}", text)


def untrusted_block(boundary: str, kind: str, body: str) -> str:
    """``body`` (neutralised) wrapped in an untrusted block for ``boundary``."""
    if not BOUNDARY_PATTERN.match(boundary):
        raise ValueError("boundary must be 8-64 lowercase hex characters")
    if not re.fullmatch(r"[a-z_]{1,40}", kind):
        raise ValueError(f"invalid block kind: {kind!r}")
    inner = neutralize(body).rstrip("\n")
    return f'<untrusted-{boundary} kind="{kind}">\n{inner}\n</untrusted-{boundary}>'


def _file_title(fp: FilePatch) -> str:
    if fp.status == "renamed" and fp.previous_path:
        what = f"renamed from {fp.previous_path}"
    elif fp.status == "copied" and fp.previous_path:
        what = f"copied from {fp.previous_path}"
    else:
        what = fp.status
    return f"=== {fp.path} ({what})"


def render_file(fp: FilePatch) -> str:
    """``=== path (status)`` and the hunks, numbered with head lines.

    ```
    === pkg/mod.py (modified)
    @@ new line 120 @@ def handler(request):
      120   unchanged line
      121 + added line
          - removed line
    ```
    """
    out = [_file_title(fp)]
    if fp.binary:
        out.append("(binary file; no text diff)")
    elif not fp.hunks:
        out.append("(no line changes in this diff)")
    for h in fp.hunks:
        if h.new_len > 0:
            header = f"@@ new line {h.new_start} @@"
        elif h.new_start > 0:
            header = f"@@ removed after new line {h.new_start} @@"
        else:
            header = "@@ removed lines @@"
        out.append(f"{header} {h.header}" if h.header else header)
        for kind, num, text in h.lines:
            gutter = f"{num:>5}" if num is not None else "     "
            # No trailing blanks on empty lines: golden files and editors strip them.
            out.append(f"{gutter} {kind} {text}" if text else f"{gutter} {kind}".rstrip())
    return "\n".join(out) + "\n"


def missing_diff_text(path: str, status: str) -> str:
    """Stand-in for a reviewable file whose diff is not available from either source."""
    return (
        f"=== {path} ({status})\n"
        "(diff not available; read the head version with read_repo_file and the "
        "previous version with read_base_file)\n"
    )


# --------------------------------------------------------------------------------------
# Explorer prompt
# --------------------------------------------------------------------------------------


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TAG_START = re.compile(r"<(?=[/A-Za-z_!?])")


def defang(text: str) -> str:
    """Author text placed outside an untrusted block: no tag can open or close there.

    Control characters (newlines included) become ``?`` so a path cannot add lines,
    a zero-width joiner after ``<`` breaks tag-like text such as ``</review_scope>``,
    and ``untrusted-`` is neutralised.
    """
    return neutralize(_TAG_START.sub("<" + ZWJ, _CONTROL.sub("?", text)))


def _one_line(text: str, limit: int = _PRIOR_FIELD_MAX) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def prompt_priors(priors: Sequence[PriorFinding]) -> list[PriorFinding]:
    """Prior findings shown to the explorer: still open (inline or summary-only), max 30."""
    return [p for p in priors if p.state in ("open", "summary_only")][:PRIOR_MAX]


def prompt_dismissed(priors: Sequence[PriorFinding]) -> list[PriorFinding]:
    """Dismissed findings shown to the explorer, so it does not report them again."""
    return [p for p in priors if p.state == "dismissed"][:PRIOR_MAX]


def _pr_block(snap: PrSnapshot, boundary: str) -> str:
    body = snap.body_excerpt.strip() or "(no description)"
    text = (
        f"title: {_one_line(snap.title, 300)}\n"
        f"author: {snap.author} ({snap.author_association.lower()})\n"
        f"base branch: {snap.base_ref}\n"
        f"description:\n{body}"
    )
    return untrusted_block(boundary, "pull_request", text)


def _guidance(cfg: ResolvedConfig) -> str:
    parts: list[str] = []
    if cfg.instructions.strip():
        parts.append(cfg.instructions.strip())
    if cfg.path_instructions:
        parts.append("path rules:")
        parts.extend(f"- {glob}: {_one_line(text, 1024)}" for glob, text in cfg.path_instructions)
    if not parts:
        return ""
    inner = neutralize("\n".join(parts))
    return (
        f'<repository_guidance source="base branch configuration">\n{inner}\n</repository_guidance>'
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scope_line(scope: ScopeDecision) -> str:
    if scope.scope == "incremental" and scope.since_sha:
        return f"scope: incremental since {scope.since_sha[:7]} (changes after the last review)"
    if scope.scope == "carry_over" and scope.since_sha:
        return f"scope: carry_over (no reviewable changes since {scope.since_sha[:7]})"
    return "scope: full"


def _scope_block(snap: PrSnapshot, plan: PromptPlan, scope: ScopeDecision) -> str:
    by_path: dict[str, PrFile] = {f.path: f for f in snap.files}
    lines = ["<review_scope>", _scope_line(scope)]
    if plan.partial_reason:
        lines.append(f"partial review: {defang(plan.partial_reason)}")
    rows: list[str] = []
    used = 0
    where = [(p, "inline") for p in plan.inlined] + [(p, "show_file_diff") for p in plan.on_demand]
    for path, how in where:
        f = by_path.get(path)
        # The plan's own counts: for an incremental scope, the lines since the prior head.
        added_removed = plan.line_counts.get(path) or ((f.additions, f.deletions) if f else None)
        counts = f"+{added_removed[0]} -{added_removed[1]}" if added_removed else "+? -?"
        status = f.status if f else "modified"
        row = f"{defang(path)} | {status} | {counts} | {how}"
        if len(rows) >= SCOPE_TABLE_MAX_ROWS or used + len(row) > SCOPE_TABLE_MAX_CHARS:
            break
        rows.append(row)
        used += len(row) + 1
    lines.append(
        f"changed files ({len(plan.inlined)} inline, {len(plan.on_demand)} via show_file_diff;"
        " path | status | +added -removed | where to read):"
    )
    lines.extend(rows)
    if len(rows) < len(where):
        lines.append(
            f"... and {len(where) - len(rows)} more changed files not listed here (still in scope)"
        )
    if plan.excluded:
        lines.append(f"excluded from review by default ({len(plan.excluded)}):")
        # A character budget as well as a row cap: the PR author picks these paths, and
        # this text is resent on every model call (security review PL-6).
        shown = 0
        used = 0
        for e in plan.excluded[:SCOPE_TABLE_MAX_ROWS]:
            row = f"{defang(_clip(e.path, EXCLUDED_PATH_MAX_CHARS))} ({defang(e.reason)})"
            if used + len(row) > EXCLUDED_TABLE_MAX_CHARS:
                break
            lines.append(row)
            used += len(row) + 1
            shown += 1
        if shown < len(plan.excluded):
            lines.append(f"... and {len(plan.excluded) - shown} more excluded files")
    lines.append("</review_scope>")
    return "\n".join(lines)


def _prior_rows(shown: Sequence[PriorFinding]) -> list[str]:
    # Titles and evidence came from model output over author text: defang them.
    return [
        f"{defang(p.id)} | {defang(p.path)}:{p.line if p.line is not None else '-'} | "
        f"{p.severity} | {defang(_one_line(p.title))} | evidence: {defang(_one_line(p.evidence))}"
        for p in shown
    ]


def _prior_block(priors: Sequence[PriorFinding]) -> str:
    shown = prompt_priors(priors)
    if not shown:
        return ""
    return "<prior_findings>\n" + "\n".join(_prior_rows(shown)) + "\n</prior_findings>"


def _dismissed_block(priors: Sequence[PriorFinding]) -> str:
    """Findings the maintainers dismissed. Their reasons are PR text and stay out."""
    shown = prompt_dismissed(priors)
    if not shown:
        return ""
    rows = "\n".join(_prior_rows(shown))
    return f"<dismissed_findings>\n{rows}\n</dismissed_findings>"


def render_prompt(
    snap: PrSnapshot,
    plan: PromptPlan,
    scope: ScopeDecision,
    priors: Sequence[PriorFinding],
    cfg: ResolvedConfig,
    boundary: str,
    budgets: ResolvedBudgets,
    *,
    related: Sequence[RelatedDefinition] | None = None,
) -> str:
    """The explorer's user message (DESIGN 6.5, ADDENDUM H).

    Order: PR block, repository guidance, review scope, prior findings, dismissed
    findings, the inlined diff, the related-definitions block (when enabled and
    non-empty), the budget line.
    """
    # Imported here: related.py imports this module for untrusted_block.
    from morning_star_review.diff.related import render_block

    diff_text = plan.rendered_diff.strip("\n") or (
        "(no diff inlined; every changed file is listed in review_scope, read it with "
        "show_file_diff)"
    )
    sections = [
        _pr_block(snap, boundary),
        _guidance(cfg),
        _scope_block(snap, plan, scope),
        _prior_block(priors),
        _dismissed_block(priors),
        untrusted_block(boundary, "diff", diff_text),
        render_block(related or [], boundary) if cfg.related_definitions else "",
        f"Budget: about {budgets.max_tool_calls} tool calls.",
    ]
    return "\n".join(s for s in sections if s) + "\n"
