"""Activity that turns explorer notes into a structured draft (DESIGN 6.7).

Parse first. Only when the parser loses something (unparsed blocks, fewer findings
than FINDING markers, or prose findings without markers) does the formatter agent run
(phase ``format``, no tools, strict schema). The formatter is best effort: when it
fails, the parsed findings are kept and the draft is marked ``parser_lossy``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from mistralai import workflows
from pydantic import ValidationError

from morning_star_review.activities.agent_launcher import (
    AgentCrashed,
    AgentJobInvalid,
    AgentTimedOut,
    run_agent,
)
from morning_star_review.activities.common import (
    JOB_JSON,
    activity_errors,
    agent_job,
    cost_share,
    job_dir_name,
    launch_timeout,
    require_dir,
    usage_for,
)
from morning_star_review.activities.finalize import keep_for_finalize
from morning_star_review.core.contract import (
    AgentBudgets,
    AgentRunResult,
    DraftFinding,
    DraftSource,
    FormatterDraft,
    ModelRole,
    Prepared,
    PriorStatus,
    ReviewDraft,
    StructureInput,
    Usage,
)
from morning_star_review.core.guard import guard
from morning_star_review.core.ids import norm
from morning_star_review.core.jsonio import write_json_atomic, write_model, write_text_atomic
from morning_star_review.core.notes import (
    ParsedNotes,
    looks_like_unformatted_findings,
    parse_notes,
)
from morning_star_review.core.redact import redact
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.seal import seal

__all__ = [
    "FORMAT_SECONDS",
    "STRUCTURE_TIMEOUT",
    "FormatterRun",
    "build_draft",
    "combine",
    "draft_budget",
    "fit_draft",
    "needs_formatter",
    "run_formatter",
    "structure",
]

STRUCTURE_TIMEOUT = timedelta(minutes=8)
FORMAT_SECONDS = 240  # the runner's formatter turn timeout
MIN_FORMAT_SECONDS = 60.0  # below this, a formatter run cannot finish: skip it
NOTES_TXT = "notes.txt"
CANDIDATES_JSON = "candidates.json"
MAX_BLOCKS = 20
BLOCK_CHARS = 2000
MAX_FINDINGS = 80  # ReviewDraft.findings bound
MAX_PRIORS = 60  # ReviewDraft.prior_status bound
# FinalFinding keeps at most this much of a draft's text, so longer text is never used.
EXPLANATION_KEEP = 2000
FIX_KEEP = 1000
# Byte budget for all drafts of one run together (they travel in MergeInput).
DRAFTS_TOTAL_BYTES = 300 * 1024
DRAFT_MIN_BYTES = 72 * 1024
# Shortened forms used only when a draft is over its budget.
SHORT_BLOCKS = 5
SHORT_BLOCK_CHARS = 1000
SHORT_REASON_CHARS = 200
SHORT_SUMMARY_CHARS = 2000


# ------------------------------------------------------------------------ formatter


class FormatterRun:
    """Outcome of one formatter job: the strict draft (or None), usage and error."""

    def __init__(self, draft: dict[str, Any] | None, usage: Usage | None, error: str | None):
        self.draft = draft
        self.usage = usage
        self.error = error


def _format_budgets(prep: Prepared) -> AgentBudgets:
    b = prep.budgets
    return AgentBudgets(
        max_tool_calls=0,
        max_llm_calls=4,  # one strict-output correction and one dropped-findings retry
        grace_llm_calls=0,
        explore_seconds=FORMAT_SECONDS,
        grace_seconds=30,
        rescue_seconds=0,
        max_context_tokens=b.max_context_tokens,
        auto_compact_threshold=b.auto_compact_threshold,
        max_tool_output_chars=b.max_tool_output_chars,
        max_cost_usd=cost_share(prep.roles, b.max_cost_usd, "formatter"),
    )


async def run_formatter(
    ctx: RunContext,
    prep: Prepared,
    *,
    head_sha: str,
    merge_base_sha: str,
    job_dir: Path,
    notes: str,
    target: Literal["review", "verify"],
    candidates: list[dict[str, Any]] | None = None,
    time_left: float | None = None,
) -> FormatterRun:
    """Run the formatter agent on ``notes``; never raises for agent failures.

    ``time_left`` is what remains of the calling activity's own limit (seconds); the
    formatter is stopped before it, or not started when too little remains.
    """
    if time_left is not None and time_left < MIN_FORMAT_SECONDS:
        return FormatterRun(None, None, "no time left in the activity for the formatter")
    job_dir.mkdir(mode=0o700, exist_ok=True)
    notes_sha = write_text_atomic(job_dir / NOTES_TXT, notes)
    if candidates is not None:
        write_json_atomic(job_dir / CANDIDATES_JSON, candidates)
    role = ModelRole(model=prep.roles.formatter_model, effort=prep.roles.formatter_effort)
    job = agent_job(
        phase="format",
        execution_id=ctx.execution_id,
        repo_dir=prep.repo_dir,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        input_path=job_dir / NOTES_TXT,
        input_sha256=notes_sha,
        variant="full",
        role=role,
        budgets=_format_budgets(prep),
        changed_files=[],
        format_target=target,
    )
    write_model(job_dir / JOB_JSON, job)
    timeout = launch_timeout(job.budgets, STRUCTURE_TIMEOUT)
    if time_left is not None:
        timeout = min(timeout, time_left)
    try:
        res: AgentRunResult = await run_agent("format", job_dir, ctx, timeout)
    except (AgentCrashed, AgentTimedOut, AgentJobInvalid) as exc:
        return FormatterRun(None, None, redact(f"{type(exc).__name__}: {exc}", ctx.secrets()))
    usage = usage_for(res.usage, role.model)
    if res.state != "Completed" or res.draft is None:
        error = res.error or f"formatter state {res.state}"
        return FormatterRun(None, usage, redact(error, ctx.secrets()))
    return FormatterRun(res.draft, usage, None)


# ------------------------------------------------------------------------ drafts


def _prose_findings(notes: str, p: ParsedNotes) -> bool:
    """Text outside the FINDING blocks looks like findings, and the notes do not say
    there are none."""
    return not p.no_findings_declared and looks_like_unformatted_findings(notes)


def needs_formatter(notes: str, p: ParsedNotes) -> bool:
    """DESIGN 6.7 step 2: the parser alone is enough unless it lost something."""
    if not notes.strip():
        return False
    return not p.lossless or (not p.findings and _prose_findings(notes, p))


def _blocks(p: ParsedNotes, notes: str) -> list[str]:
    blocks = [b[:BLOCK_CHARS] for b in p.unparsed if b.strip()][:MAX_BLOCKS]
    if not blocks and notes.strip():
        blocks = [notes.strip()[:BLOCK_CHARS]]  # prose notes: show their start
    return blocks


def combine(
    p: ParsedNotes, fmt: FormatterDraft | None, notes: str
) -> tuple[list[DraftFinding], list[PriorStatus], str, bool]:
    """DESIGN 6.7 step 4: parsed findings plus formatter findings not already present.

    Returns (findings, prior statuses, summary, lossy). The result is lossy when the
    formatter failed, returned fewer findings than there were FINDING markers, or
    returned none although the notes look like they hold findings (``path:line``
    locations or severity labels outside the blocks): zero findings from such notes
    is not believable, so it must not support a ``clean`` verdict.
    """
    findings = list(p.findings)
    priors = list(p.priors)
    if fmt is None:
        return findings, priors, p.summary, True
    places = {(f.path, f.line) for f in findings}
    evidence = {norm(f.evidence) for f in findings if norm(f.evidence)}
    for f in fmt.findings:
        ev = norm(f.evidence)
        if (f.path, f.line) in places or (ev and ev in evidence):
            continue
        findings.append(f)
        places.add((f.path, f.line))
        if ev:
            evidence.add(ev)
    ids = {s.id for s in priors}
    priors.extend(s for s in fmt.prior_status if s.id not in ids)
    lossy = len(fmt.findings) < p.marker_count or (not fmt.findings and _prose_findings(notes, p))
    return findings, priors, p.summary or fmt.summary, lossy


def _trimmed(f: DraftFinding) -> DraftFinding:
    if len(f.explanation) <= EXPLANATION_KEEP and len(f.fix or "") <= FIX_KEEP:
        return f
    fix = f.fix[:FIX_KEEP] if f.fix is not None else None
    return f.model_copy(update={"explanation": f.explanation[:EXPLANATION_KEEP], "fix": fix})


def draft_budget(explorers: int) -> int:
    """Byte budget of one draft when ``explorers`` drafts travel together."""
    return max(DRAFT_MIN_BYTES, DRAFTS_TOTAL_BYTES // max(explorers, 1))


def fit_draft(draft: ReviewDraft, budget: int) -> ReviewDraft:
    """``draft`` within ``budget`` JSON bytes (unsealed).

    Steps, each only while the draft is still too large: cut text a published finding
    cannot hold anyway; shorten the raw unparsed blocks, prior reasons and the summary;
    drop the least severe findings. Dropping marks the draft ``parser_lossy`` with a
    note, so the verdict cannot be ``clean``.
    """
    out = draft.model_copy(update={"findings": [_trimmed(f) for f in draft.findings]})
    if _size(out) > budget:
        out = out.model_copy(
            update={
                "unparsed_blocks": [b[:SHORT_BLOCK_CHARS] for b in out.unparsed_blocks][
                    :SHORT_BLOCKS
                ],
                "prior_status": [
                    s.model_copy(update={"reason": s.reason[:SHORT_REASON_CHARS]})
                    for s in out.prior_status
                ],
                "summary": out.summary[:SHORT_SUMMARY_CHARS],
            }
        )
    if _size(out) > budget:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ranked = sorted(out.findings, key=lambda f: order[f.severity])
        kept = list(ranked)
        blocks = list(out.unparsed_blocks[: SHORT_BLOCKS - 1])
        while True:
            note = f"{len(ranked) - len(kept)} findings were dropped to keep the review data small."
            out = out.model_copy(
                update={
                    "findings": kept,
                    "unparsed_blocks": [*blocks, note],
                    "source": "parser_lossy",
                }
            )
            if _size(out) <= budget or not kept:
                break
            kept = kept[:-1]
    return ReviewDraft.model_validate(out.model_dump())


def build_draft(
    p: ParsedNotes,
    notes: str,
    fmt: FormatterRun | None,
    explorer_model: str,
) -> ReviewDraft:
    """The draft of one explorer (unsealed, not yet fitted to a budget)."""
    if not notes.strip():
        return ReviewDraft(
            findings=[],
            prior_status=[],
            summary="",
            source="empty",
            marker_count=0,
            explorer_model=explorer_model,
        )
    if fmt is None:
        findings, priors, summary, lossy = list(p.findings), list(p.priors), p.summary, False
        source: DraftSource = "parser"
    else:
        parsed_fmt: FormatterDraft | None = None
        if fmt.draft is not None:
            try:
                parsed_fmt = FormatterDraft.model_validate(fmt.draft)
            except ValidationError:
                parsed_fmt = None
        findings, priors, summary, lossy = combine(p, parsed_fmt, notes)
        source = "parser+formatter"
    blocks = _blocks(p, notes) if lossy else []
    if len(findings) > MAX_FINDINGS:
        # More than a draft can carry: keep the most severe and say so (never clean).
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        dropped = len(findings) - MAX_FINDINGS
        findings = sorted(findings, key=lambda f: order[f.severity])[:MAX_FINDINGS]
        blocks = [*blocks[: MAX_BLOCKS - 1], f"{dropped} more findings were not kept."]
        lossy = True
    return ReviewDraft(
        findings=findings,
        prior_status=priors[:MAX_PRIORS],
        summary=summary[:4000],
        source="parser_lossy" if lossy else source,
        marker_count=p.marker_count,
        unparsed_blocks=blocks,
        formatter_usage=fmt.usage if fmt is not None else None,
        explorer_model=explorer_model,
    )


@workflows.activity(
    name="msreview_structure",
    start_to_close_timeout=STRUCTURE_TIMEOUT,
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
)
async def structure(inp: StructureInput) -> ReviewDraft:
    """Parse one explorer's notes; run the formatter only if the parse is lossy."""
    ctx = guard(inp.req, inp.prep, inp.explore)
    with activity_errors():
        notes = inp.explore.notes
        model = inp.explore.model
        parsed = parse_notes(notes)
        fmt: FormatterRun | None = None
        if needs_formatter(notes, parsed):
            run_dir = require_dir(inp.prep.job_root)
            fmt = await run_formatter(
                ctx,
                inp.prep,
                # The formatter has no tools and never reads the repository.
                head_sha="",
                merge_base_sha="",
                job_dir=run_dir / job_dir_name("format", model),
                notes=notes,
                target="review",
            )
        draft = build_draft(parsed, notes, fmt, model)
        fitted = fit_draft(draft, draft_budget(len(inp.prep.roles.explorers)))
        sealed = seal(fitted, ctx)
        keep_for_finalize(require_dir(inp.prep.job_root), sealed)
        return sealed


def _size(model: ReviewDraft) -> int:
    return len(model.model_dump_json().encode("utf-8", "surrogatepass"))
