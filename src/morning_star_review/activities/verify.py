"""Activity that runs the verifier agent over the merged candidates (ADDENDUM D).

The verifier (phase ``verify``) reads code with the same read-only tools as the
explorers, under its own budget, and writes one VERDICT block per candidate. Its notes
are parsed first; if the parse is lossy the formatter turns them into a ``VerifyDraft``.
Candidates without a verdict are listed as ``unaddressed`` (they count as uncertain).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from mistralai import workflows
from pydantic import ValidationError

from morning_star_review.activities.agent_launcher import run_agent
from morning_star_review.activities.common import (
    JOB_JSON,
    RELATED_JSON,
    activity_errors,
    agent_budgets,
    agent_job,
    agent_line,
    agent_state,
    cap_paths,
    cost_share,
    exhaust_reasons,
    launch_timeout,
    link_diffs,
    load_changed_files,
    progress,
    read_partial_notes,
    require_dir,
    usage_for,
)
from morning_star_review.activities.structure import FormatterRun, run_formatter
from morning_star_review.core.contract import (
    AgentRunResult,
    Candidate,
    PrSnapshot,
    ResolvedBudgets,
    Usage,
    VerifyDraft,
    VerifyEntry,
    VerifyInput,
    VerifyResult,
    VerifySource,
)
from morning_star_review.core.errors import MsreviewBadRequest
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_json_atomic, write_model, write_text_atomic
from morning_star_review.core.models import tool_call_limit
from morning_star_review.core.notes import ParsedVerdicts, parse_verdicts
from morning_star_review.core.redact import redact
from morning_star_review.core.seal import seal
from morning_star_review.diff import related
from morning_star_review.diff.render import untrusted_block

__all__ = [
    "VERIFY_DIR",
    "VERIFY_FORMAT_DIR",
    "VERIFY_TIMEOUT",
    "empty_verify_result",
    "needs_verify_formatter",
    "render_candidate",
    "render_verify_prompt",
    "time_left",
    "verifier_tool_calls",
    "verify",
    "verify_result",
]

# The longest verifier budget (25 min cap) plus grace, rescue and process overhead, then
# a verify formatter run (up to ~6 min) when the verdicts need it.
VERIFY_TIMEOUT = timedelta(minutes=40)
ACTIVITY_MARGIN = 60.0  # seconds kept free before the activity's own limit
VERIFY_DIR = "verify"
VERIFY_FORMAT_DIR = "verify-format"
PROMPT_TXT = "prompt.txt"
CANDIDATES_JSON = "candidates.json"
OPENED_MAX = 300
OPENED_MAX_BYTES = 32 * 1024
PR_BODY_CHARS = 1500
ERROR_MAX = 2000
REASON_KEEP = 300  # DroppedCandidate.reason bound


def verifier_tool_calls(b: ResolvedBudgets, model: str) -> int:
    """Tool-call budget of the verifier on ``model``.

    An explicit ``verifier_max_tool_calls`` applies as set; otherwise the per-model
    default applies when it is lower (presets set no verifier budget).
    """
    explicit = "verifier_max_tool_calls" in b.explicit
    return tool_call_limit(model, b.verifier_max_tool_calls, explicit=explicit)


# ------------------------------------------------------------------------ prompt


def _no_headers(text: str) -> str:
    """PR-derived text with no line that could be read as a ``CANDIDATE <id>`` header."""
    return "\n".join(
        " " + line if line.lstrip().upper().startswith("CANDIDATE") else line
        for line in text.splitlines()
    )


def _field(text: str) -> str:
    """One field value: continuation lines indented, so only headers start at column 0."""
    lines = text.strip().splitlines() or [""]
    return "\n".join([lines[0], *("  " + line for line in lines[1:])])


def render_candidate(c: Candidate, explorers: int) -> str:
    where = "unknown" if c.line is None else str(c.line)
    if c.line is not None and c.end_line is not None and c.end_line != c.line:
        where += f"-{c.end_line}"
    body = [
        f"reported by: {len(c.votes)} of {explorers} reviewers",
        f"file: {_field(c.path)}",
        f"line: {where}",
        f"severity: {c.severity}",
        f"category: {c.category}",
        f"title: {_field(c.title)}",
        f"evidence: {_field(c.evidence)}",
        f"why: {_field(c.explanation)}",
    ]
    return "\n".join([f"CANDIDATE {c.candidate_id}", _no_headers("\n".join(body)), "END CANDIDATE"])


def render_verify_prompt(
    snap: PrSnapshot,
    candidates: Sequence[Candidate],
    *,
    explorers: int,
    boundary: str,
    max_tool_calls: int,
    related_defs: Sequence[related.RelatedDefinition] = (),
) -> str:
    """The verifier's user message.

    Candidate text is model output derived from untrusted code, so it sits inside an
    untrusted block. Each candidate starts with a ``CANDIDATE <id>`` line at column 0;
    no other line of the prompt can look like one.
    """
    pr_text = f"title: {snap.title}\ndescription: {snap.body_excerpt[:PR_BODY_CHARS]}"
    parts = [
        untrusted_block(boundary, "pull_request", _no_headers(_field(pr_text))),
        "<candidates>\n"
        + untrusted_block(
            boundary, "candidates", "\n\n".join(render_candidate(c, explorers) for c in candidates)
        )
        + "\n</candidates>",
    ]
    block = related.render_block(list(related_defs), boundary)
    if block:
        parts.append(f"<related_definitions>\n{_no_headers(block)}\n</related_definitions>")
    ids = ", ".join(c.candidate_id for c in candidates)
    parts.append(
        f"Candidate ids: {ids}\n"
        f"Write one VERDICT block for each of the {len(candidates)} candidates. "
        f"Budget: about {max_tool_calls} tool calls."
    )
    return "\n\n".join(parts) + "\n"


def _load_related(run_dir: Path) -> list[related.RelatedDefinition]:
    """The related definitions ``prepare`` saved for the explorers (best effort)."""
    try:
        data = json.loads((run_dir / RELATED_JSON).read_text(encoding="utf-8"))
        return related.load_related(data) if isinstance(data, list) else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


# ------------------------------------------------------------------------ result


def needs_verify_formatter(notes: str, parsed: ParsedVerdicts, ids: Sequence[str]) -> bool:
    """Run the formatter when the parse lost blocks, or found none while verdicts are missing."""
    if not notes.strip():
        return False
    return parsed.lossy or (parsed.marker_count == 0 and bool(parsed.missing(ids)))


def _short(e: VerifyEntry) -> VerifyEntry:
    """A refuted candidate's reason is published at most 300 characters long; keep no more."""
    if len(e.reason) <= REASON_KEEP:
        return e
    return e.model_copy(update={"reason": e.reason[:REASON_KEEP]})


def _formatter_entries(fmt: FormatterRun | None, ids: Sequence[str]) -> list[VerifyEntry]:
    if fmt is None or fmt.draft is None:
        return []
    try:
        draft = VerifyDraft.model_validate(fmt.draft)
    except ValidationError:
        return []
    return [e for e in draft.entries if e.candidate_id in ids]


def verify_result(
    res: AgentRunResult,
    notes: str,
    parsed: ParsedVerdicts,
    fmt: FormatterRun | None,
    ids: Sequence[str],
    model: str,
    secrets: list[str],
) -> VerifyResult:
    """The verify payload (unsealed): parsed verdicts, plus formatter ones for the rest."""
    wanted = set(ids)
    entries: dict[str, VerifyEntry] = {
        e.candidate_id: _short(e) for e in parsed.entries if e.candidate_id in wanted
    }
    for e in _formatter_entries(fmt, ids):
        entries.setdefault(e.candidate_id, _short(e))
    source: VerifySource
    if not notes.strip():
        source = "empty"
    elif fmt is None:
        source = "parser"
    else:
        source = "parser+formatter" if fmt.draft is not None else "parser_lossy"
    unaddressed = [i for i in ids if i not in entries]
    error = res.error or (fmt.error if fmt is not None else None)
    return VerifyResult(
        entries=[entries[i] for i in ids if i in entries],
        unaddressed=unaddressed,
        source=source,
        state=agent_state(res.state),
        stop_reason=res.stop_reason[:200] if res.stop_reason else None,
        rescued=res.rescued,
        complete=res.complete and not unaddressed,
        budget_exhausted=res.budget_exhausted,
        exhausted_by=exhaust_reasons(res.exhausted_by),
        opened_paths=cap_paths(
            res.opened_paths, set(res.opened_paths), OPENED_MAX, OPENED_MAX_BYTES
        ),
        usage=usage_for(res.usage, model),
        formatter_usage=fmt.usage if fmt is not None else None,
        error=redact(error, secrets)[:ERROR_MAX] if error else None,
    )


def time_left(started: float, now: float | None = None) -> float:
    """Seconds this verify attempt may still use before its activity limit (minus a margin)."""
    elapsed = (time.monotonic() if now is None else now) - started
    return VERIFY_TIMEOUT.total_seconds() - ACTIVITY_MARGIN - elapsed


def empty_verify_result(model: str) -> VerifyResult:
    """Nothing to verify: no candidates, no model call."""
    return VerifyResult(
        entries=[],
        unaddressed=[],
        source="empty",
        state="NotRun",
        stop_reason=None,
        rescued=False,
        complete=True,
        budget_exhausted=False,
        exhausted_by=[],
        opened_paths=[],
        usage=Usage(model=model),
    )


@workflows.activity(
    name="msreview_verify",
    start_to_close_timeout=VERIFY_TIMEOUT,
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy_max_attempts=2,
    retry_policy_backoff_coefficient=2.0,
)
async def verify(inp: VerifyInput) -> VerifyResult:
    """Run the verifier on the merged candidates and return its verdicts."""
    ctx = guard(inp.req, inp.snap, inp.prep, inp.merge)
    started = time.monotonic()
    with activity_errors():
        role = inp.prep.roles.verifier
        if role is None:
            raise MsreviewBadRequest("verification is not enabled for this run")
        candidates = list(inp.merge.candidates)
        if not candidates:
            return seal(empty_verify_result(role.model), ctx)
        prep, snap = inp.prep, inp.snap
        run_dir = require_dir(prep.job_root)
        job_dir = run_dir / VERIFY_DIR
        job_dir.mkdir(mode=0o700, exist_ok=True)
        refs = load_changed_files(run_dir)
        link_diffs(run_dir, job_dir, refs)
        b = prep.budgets
        budgets = agent_budgets(
            b,
            max_tool_calls=verifier_tool_calls(b, role.model),
            seconds=b.verifier_minutes * 60,
            max_cost_usd=cost_share(prep.roles, b.max_cost_usd, "verifier"),
        )
        prompt = render_verify_prompt(
            snap,
            candidates,
            explorers=len(prep.roles.explorers),
            boundary=prep.boundary,
            max_tool_calls=budgets.max_tool_calls,
            related_defs=_load_related(run_dir),
        )
        prompt_sha = write_text_atomic(job_dir / PROMPT_TXT, prompt)
        candidate_data = [c.model_dump(mode="json") for c in candidates]
        write_json_atomic(job_dir / CANDIDATES_JSON, candidate_data)
        job = agent_job(
            phase="verify",
            execution_id=ctx.execution_id,
            repo_dir=prep.repo_dir,
            head_sha=snap.head_sha,
            merge_base_sha=snap.merge_base_sha,
            input_path=job_dir / PROMPT_TXT,
            input_sha256=prompt_sha,
            variant="full",
            role=role,
            budgets=budgets,
            changed_files=refs,
        )
        write_model(job_dir / JOB_JSON, job)
        res = await run_agent("verify", job_dir, ctx, launch_timeout(budgets, VERIFY_TIMEOUT))
        notes = res.notes or read_partial_notes(job_dir)
        ids = [c.candidate_id for c in candidates]
        parsed = parse_verdicts(notes, ids)
        fmt: FormatterRun | None = None
        if needs_verify_formatter(notes, parsed, ids):
            fmt = await run_formatter(
                ctx,
                prep,
                head_sha=snap.head_sha,
                merge_base_sha=snap.merge_base_sha,
                job_dir=run_dir / VERIFY_FORMAT_DIR,
                notes=notes,
                target="verify",
                candidates=candidate_data,
                time_left=time_left(started),
            )
        result = verify_result(res, notes, parsed, fmt, ids, role.model, ctx.secrets())
        progress(
            agent_line(
                "verify",
                role.model,
                state=result.state,
                complete=result.complete,
                rescued=result.rescued,
                budget_exhausted=result.budget_exhausted,
                exhausted_by=result.exhausted_by,
                stop_reason=result.stop_reason,
            )
        )
        return seal(result, ctx)
