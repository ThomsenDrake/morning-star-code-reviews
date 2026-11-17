"""Builds and runs the explorer, verifier and formatter agents (DESIGN 6.2, 6.7, ADDENDUM D).

One process runs one phase of one job directory:

- ``explore``: the explorer reads the PR with the five read-only tools and writes notes
  in the FINDING grammar. A rescue turn (tools closed) recovers notes when the budget
  ends the first turn early.
- ``verify``: the verifier checks merged candidates with the same tools and its own
  budget and writes VERDICT blocks. Candidates left without a verdict get one
  follow-up turn; any still missing count as ``uncertain`` downstream.
- ``format``: a tool-less formatter turns lossy notes into ``FormatterDraft`` or, for
  verifier notes, ``VerifyDraft`` (strict ``output_type``).

Both agents use ``Builtins.none()``, an explicit ``auto_compact_threshold`` and no
``reasoning_effort`` when the effort is ``default``.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mistralai import agents
from mistralai.agents.pattern.hook import StructuredOutputHook
from pydantic import BaseModel, ValidationError

from morning_star_review.agent import hooks, jobio, prompts, tools
from morning_star_review.core.contract import (
    AgentJob,
    AgentRunResult,
    FormatterDraft,
    Usage,
    VerifyDraft,
)
from morning_star_review.core.notes import (
    is_placeholder_summary,
    parse_notes,
    parse_verdicts,
)
from morning_star_review.core.redact import redact

__all__ = [
    "EXPLORER_NAME",
    "FORMATTER_NAME",
    "SMALL_DIFF_LINES",
    "VERIFIER_NAME",
    "build_explorer",
    "build_formatter",
    "build_verifier",
    "explore_complete",
    "normalize_finish_reason",
    "run_job",
    "verify_complete",
]

EXPLORER_NAME = "msreview-explorer"
VERIFIER_NAME = "msreview-verifier"
FORMATTER_NAME = "msreview-formatter"

FIRST_OUTPUT_TIMEOUT = 180.0
FORMATTER_FIRST_OUTPUT_TIMEOUT = 120.0
FORMATTER_TIMEOUT = 240.0
FORMATTER_MAX_ITERATIONS = 4
SETTLE_SECONDS = 30.0  # wait for an interrupted turn to end
SMALL_DIFF_LINES = 60  # a fully inlined diff this small may be reviewed without tools
MAX_ERROR_CHARS = 2000

# Turn endings after which the model should get a rescue turn.
_EARLY_STOPS = frozenset({"skipped", "iteration_limit", "rejected", "output_limit"})

Phase = Literal["explore", "verify", "format"]


def _effort(job: AgentJob) -> Any:
    return None if job.effort == "default" else job.effort


def _hook_list(extra: Sequence[agents.Hook] = ()) -> list[agents.Hook]:
    return [hooks.BudgetHook(), *extra]


def build_explorer(job: AgentJob) -> agents.Agent:
    return agents.Agent(
        model=job.model,
        name=EXPLORER_NAME,
        instructions=prompts.explorer_instructions(job.variant, job.budgets.max_tool_calls),
        tools=list(tools.ALL_TOOLS),
        builtins=agents.Builtins.none(),
        hooks=_hook_list(),
        max_iterations=job.budgets.max_llm_calls + 4,  # backstop; the hook stops first
        reasoning_effort=_effort(job),
        first_output_timeout_seconds=FIRST_OUTPUT_TIMEOUT,
        tool_errors="hide",
        auto_compact_threshold=job.budgets.auto_compact_threshold,
    )


def build_verifier(job: AgentJob) -> agents.Agent:
    return agents.Agent(
        model=job.model,
        name=VERIFIER_NAME,
        instructions=prompts.verifier_instructions(job.budgets.max_tool_calls),
        tools=list(tools.ALL_TOOLS),
        builtins=agents.Builtins.none(),
        hooks=_hook_list(),
        max_iterations=job.budgets.max_llm_calls + 4,
        reasoning_effort=_effort(job),
        first_output_timeout_seconds=FIRST_OUTPUT_TIMEOUT,
        tool_errors="hide",
        auto_compact_threshold=job.budgets.auto_compact_threshold,
    )


def _unwrapped_json(text: str, target: type[BaseModel]) -> str | None:
    """Bare JSON for an answer that fails validation only because of its wrapping."""
    try:
        target.model_validate_json(text)
        return None  # already valid: nothing to fix
    except ValidationError:
        pass
    draft = salvage_json(text, target)
    return draft.model_dump_json() if draft is not None else None


class _ReviewHook(StructuredOutputHook[FormatterDraft]):
    """Strict output check with one correction; unwraps fenced or prose-wrapped JSON first."""

    async def post_llm_call(self, ctx: Any, invocation: Any) -> Any:
        fixed = None if invocation.tool_calls else _unwrapped_json(invocation.text, FormatterDraft)
        if fixed is not None:
            return _replace_with(fixed)
        return await super().post_llm_call(ctx, invocation)


class _VerifyHook(StructuredOutputHook[VerifyDraft]):
    """The same for verifier notes."""

    async def post_llm_call(self, ctx: Any, invocation: Any) -> Any:
        fixed = None if invocation.tool_calls else _unwrapped_json(invocation.text, VerifyDraft)
        if fixed is not None:
            return _replace_with(fixed)
        return await super().post_llm_call(ctx, invocation)


def _replace_with(text: str) -> Any:
    return agents.hook.PostLlmCallHookAccept(
        acceptance=agents.hook.ReplaceAssistantContent(content=[agents.TextContentBlock(text=text)])
    )


def build_formatter(job: AgentJob) -> agents.Agent:
    target: type[BaseModel] = FormatterDraft if job.format_target == "review" else VerifyDraft
    structured: agents.Hook = (
        _ReviewHook(max_retries=1, on_limit="reject", schema_in_prompt=False)
        if job.format_target == "review"
        else _VerifyHook(max_retries=1, on_limit="reject", schema_in_prompt=False)
    )
    return agents.Agent(
        model=job.model,
        name=FORMATTER_NAME,
        instructions=(
            prompts.FORMATTER_REVIEW if job.format_target == "review" else prompts.FORMATTER_VERIFY
        ),
        builtins=agents.Builtins.none(),
        output_type=target,
        hooks=_hook_list([structured]),
        max_iterations=FORMATTER_MAX_ITERATIONS,
        reasoning_effort=_effort(job),
        first_output_timeout_seconds=FORMATTER_FIRST_OUTPUT_TIMEOUT,
        auto_compact_threshold=job.budgets.auto_compact_threshold,
    )


# ------------------------------------------------------------------------ turn helpers


@dataclass
class _Turn:
    """What one ``session.run`` produced, reduced to what the runner needs."""

    kind: str  # Completed | Failed | Interrupted | Running | Blocked | UnknownOutcome
    stop_reason: str | None
    text: str  # "" for early stops whose text is not model output
    error: str | None = None
    retryable: bool = False


def _reduce(state: Any) -> _Turn:
    kind = type(state).__name__
    if isinstance(state, agents.Completed):
        stop = state.stop_reason
        # After a skip or reject the text is our hook's reason, not model notes.
        text = "" if stop in ("skipped", "rejected") else state.text or ""
        return _Turn(kind=kind, stop_reason=stop, text=text)
    if isinstance(state, agents.Failed):
        err = state.error
        code = f"{err.code}: " if err.code else ""
        return _Turn(
            kind=kind,
            stop_reason="failed",
            text="",
            error=f"{code}{err.message}",
            retryable=bool(err.retryable),
        )
    if isinstance(state, agents.Interrupted):
        return _Turn(kind=kind, stop_reason=state.cause, text="")
    return _Turn(kind=kind, stop_reason=kind.lower(), text="")


async def _settled(session: Any, content: str, timeout: float) -> tuple[Any, bool]:
    """Run one turn and return its record; interrupt it when it outlives ``timeout``.

    ``session.run`` returns ``Running`` at the timeout and leaves the turn running, so
    the turn is interrupted to stop its model calls. The flag says it timed out.
    """
    state = await session.run(content, timeout=max(1.0, timeout))
    if isinstance(state, agents.Running | agents.Blocked):
        turn = session.turn(state.turn_id)
        await turn.interrupt()
        return await turn.result(timeout=SETTLE_SECONDS), True
    return state, False


async def _run_turn(session: Any, content: str, timeout: float) -> _Turn:
    """Run one turn; interrupt it when it outlives ``timeout``."""
    state, timed_out = await _settled(session, content, timeout)
    reduced = _reduce(state)
    if timed_out and reduced.kind in ("Interrupted", "Running", "Blocked"):
        reduced.stop_reason = "timeout"
    return reduced


# Usage.finish_reasons caps each entry at 20 characters and the list at 200 entries.
FINISH_REASON_MAX_CHARS = 20
MAX_FINISH_REASONS = 200
_ENUM_STYLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)$")
_NOT_REASON_CHARS = re.compile(r"[^a-z0-9_]+")


def normalize_finish_reason(value: object) -> str:
    """A model call's finish reason as the contract stores it.

    The SDK reports ``stop``, ``tool_call``, ``length``, ``content_filter`` or
    ``other``. An enum member, or its ``FinishReason.STOP`` string form, becomes the
    lower-case value; other characters collapse to ``_``; an empty value becomes
    ``unknown``; anything longer than the contract's 20-character cap is cut, so an
    unexpected reason can never make a finished run fail validation.
    """
    raw = getattr(value, "value", value)
    text = "" if raw is None else str(raw).strip()
    enum_style = _ENUM_STYLE.match(text)
    if enum_style:
        text = enum_style.group(1)
    text = _NOT_REASON_CHARS.sub("_", text.lower()).strip("_")
    return text[:FINISH_REASON_MAX_CHARS].rstrip("_") or "unknown"


def _finish_reasons(m: hooks.Meter) -> list[str]:
    return [normalize_finish_reason(r) for r in m.state.finish_reasons][-MAX_FINISH_REASONS:]


def _usage(m: hooks.Meter) -> Usage:
    """The meter's usage with normalised finish reasons (see ``normalize_finish_reason``)."""
    data = m.usage().model_dump(mode="json")
    data["finish_reasons"] = _finish_reasons(m)
    return Usage.model_validate(data)


def _last_finish(m: hooks.Meter) -> str | None:
    reasons = _finish_reasons(m)
    return reasons[-1] if reasons else None


def _ended_normally(turn: _Turn, m: hooks.Meter) -> bool:
    return turn.kind == "Completed" and turn.stop_reason is None and _last_finish(m) == "stop"


def _changed_lines(job: AgentJob, job_dir: Path) -> int | None:
    """Added plus removed lines over all diff files, or ``None`` when one is unreadable."""
    total = 0
    for ref in job.changed_files:
        if not ref.diff_file:
            return None
        try:
            text = (job_dir / ref.diff_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        total += sum(1 for line in text.splitlines() if _CHANGE_LINE.match(line))
    return total


# Rendered diff lines (DESIGN 7.3): "  121 + added" and "      - removed".
_CHANGE_LINE = re.compile(r"^\s*\d*\s[+-](?:\s|$)")


def explore_complete(
    *,
    ended_normally: bool,
    notes: str,
    tool_calls: int,
    small_diff: bool,
) -> bool:
    """DESIGN 6.6 plus hands-on lesson 1 (no placeholder or unsupported clean review)."""
    if not ended_normally or not notes.strip():
        return False
    parsed = parse_notes(notes)
    if not parsed.looks_complete:
        return False
    if tool_calls < 1 and not small_diff:
        return False
    return not (not parsed.findings and is_placeholder_summary(parsed.summary))


def _needs_explore_rescue(turn: _Turn) -> bool:
    if turn.kind == "Failed":
        return False  # a provider refusal repeats in the same session
    if turn.kind != "Completed" or turn.stop_reason in _EARLY_STOPS:
        return True
    if not turn.text.strip():
        return True
    parsed = parse_notes(turn.text)
    return not (parsed.marker_count or parsed.no_findings_declared or parsed.looks_complete)


def _partial_notes(files: jobio.JobFiles) -> str:
    """The last model text of an earlier call, used only when it holds FINDING blocks."""
    try:
        text = files.partial_notes.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if parse_notes(text).marker_count else ""


@dataclass
class _Outcome:
    final: _Turn
    notes: str
    rescued: bool = False
    rescue_attempted: bool = False
    extra_exhausted: list[str] = field(default_factory=list)


def _exhausted(m: hooks.Meter, outcome: _Outcome) -> list[str]:
    reasons: list[str] = list(m.state.exhausted_by)
    for r in outcome.extra_exhausted:
        if r not in reasons:
            reasons.append(r)
    return reasons


def _state_of(turn: _Turn, ran: bool) -> Literal["Completed", "Failed", "Error", "NotRun"]:
    if not ran:
        return "NotRun"
    if turn.kind == "Completed":
        return "Completed"
    if turn.kind == "Failed":
        return "Failed"
    return "Error"


def _timeout_for(m: hooks.Meter, clock: float) -> float:
    return max(5.0, m.state.deadline_epoch - clock) + m.budgets.grace_seconds


def _stop_reason_extras(turn: _Turn) -> list[str]:
    if turn.stop_reason == "iteration_limit":
        return ["iterations"]
    if turn.stop_reason == "output_limit":
        return ["length"]
    if turn.stop_reason == "timeout":
        return ["deadline"]
    return []


async def _explore_turns(
    agent: agents.Agent, job: AgentJob, prompt: str, m: hooks.Meter
) -> _Outcome:
    async with agents.local_runtime():
        session = await agent.start_session()
        try:
            first = await _run_turn(session, prompt, _timeout_for(m, m.clock()))
            out = _Outcome(
                final=first, notes=first.text, extra_exhausted=_stop_reason_extras(first)
            )
            if _needs_explore_rescue(first) and m.can_afford_rescue():
                m.start_rescue()
                early = first.kind != "Completed" or first.stop_reason in _EARLY_STOPS
                if first.stop_reason == "output_limit":
                    message = prompts.RESCUE_EXPLORE_SHORT
                elif early:
                    message = prompts.RESCUE_EXPLORE
                else:  # the turn ended normally but without notes in the grammar
                    message = prompts.RESCUE_EXPLORE_FORMAT
                out.rescue_attempted = early
                second = await _run_turn(session, message, m.budgets.rescue_seconds + 30.0)
                out.extra_exhausted += _stop_reason_extras(second)
                if second.text.strip():
                    out.notes = second.text
                out.final = second
                out.rescued = True
            return out
        finally:
            await session.cancel()


async def _explore(
    job: AgentJob, files: jobio.JobFiles, prompt: str, model: Any | None
) -> AgentRunResult:
    ctx = tools.configure(job, files.job_dir, files.coverage)
    m = hooks.meter()
    agent = build_explorer(job)
    m.reset_context(len(prompt) + len(agent.instructions or ""))
    if model is not None:
        agent = agent.override(model=model)
    calls_before = m.state.llm_calls
    out = await _explore_turns(agent, job, prompt, m)
    ran = m.state.llm_calls > calls_before
    notes = out.notes
    if not notes.strip():
        notes = _partial_notes(files)
    exhausted = _exhausted(m, out)
    ended = _ended_normally(out.final, m)
    changed = _changed_lines(job, files.job_dir)
    small = changed is not None and changed <= SMALL_DIFF_LINES
    complete = explore_complete(
        ended_normally=ended, notes=notes, tool_calls=m.state.tool_calls, small_diff=small
    )
    budget_exhausted = bool(exhausted) or out.rescue_attempted
    return AgentRunResult(
        phase="explore",
        state=_state_of(out.final, ran),
        stop_reason=out.final.stop_reason,
        notes=notes or None,
        rescued=out.rescued,
        complete=complete,
        budget_exhausted=budget_exhausted,
        exhausted_by=exhausted,
        opened_paths=list(ctx.opened),
        usage=_usage(m),
        error=_clean_error(out.final.error),
    )


def _clean_error(error: str | None) -> str | None:
    if not error:
        return None
    key = os.environ.get("MISTRAL_API_KEY", "")
    return redact(error, [key] if key else None)[:MAX_ERROR_CHARS]


# ------------------------------------------------------------------------ verify


def _candidate_ids(files: jobio.JobFiles, prompt: str) -> tuple[list[str], bool]:
    """Ids to verify and whether they came from ``candidates.json``."""
    candidates = jobio.load_candidates(files.job_dir)
    if candidates is not None:
        return [c.candidate_id for c in candidates], True
    return jobio.candidate_ids_from_prompt(prompt), False


def verify_complete(
    *, ended_normally: bool, notes: str, ids: Sequence[str], tool_calls: int
) -> bool:
    """Every candidate has a verdict, blocks are closed, and the verifier read code."""
    if not ended_normally or not notes.strip():
        return False
    parsed = parse_verdicts(notes, ids or None)
    if parsed.unparsed:
        return False
    # Models often drop END VERDICT; the parser handles that, and the final
    # VERIFY COMPLETE line still shows the answer was not cut off.
    if not parsed.closed_blocks and not parsed.sentinel:
        return False
    if ids:
        return not parsed.missing(ids) and tool_calls >= 1
    return parsed.sentinel


def _join(*texts: str) -> str:
    return "\n\n".join(t.strip() for t in texts if t.strip())


async def _verify_turns(
    agent: agents.Agent, prompt: str, ids: list[str], m: hooks.Meter
) -> _Outcome:
    async with agents.local_runtime():
        session = await agent.start_session()
        try:
            first = await _run_turn(session, prompt, _timeout_for(m, m.clock()))
            out = _Outcome(
                final=first, notes=first.text, extra_exhausted=_stop_reason_extras(first)
            )
            if first.kind == "Failed":
                return out
            missing = parse_verdicts(first.text, ids or None).missing(ids) if ids else []
            early = first.kind != "Completed" or first.stop_reason in _EARLY_STOPS
            if (early or missing or not first.text.strip()) and m.can_afford_rescue():
                m.start_rescue()
                if early or not first.text.strip():
                    message = prompts.rescue_verify(missing or ids)
                else:
                    message = prompts.verify_missing_message(missing)
                out.rescue_attempted = early
                second = await _run_turn(session, message, m.budgets.rescue_seconds + 30.0)
                out.extra_exhausted += _stop_reason_extras(second)
                out.notes = _join(out.notes, second.text)
                out.final = second
                out.rescued = True
            return out
        finally:
            await session.cancel()


async def _verify(
    job: AgentJob, files: jobio.JobFiles, prompt: str, model: Any | None
) -> AgentRunResult:
    ctx = tools.configure(job, files.job_dir, files.coverage)
    m = hooks.meter()
    ids, from_file = _candidate_ids(files, prompt)
    if from_file and not ids:  # nothing to check: no model call
        return AgentRunResult(
            phase="verify",
            state="Completed",
            stop_reason=None,
            notes="VERIFY COMPLETE",
            complete=True,
            usage=_usage(m),
        )
    agent = build_verifier(job)
    m.reset_context(len(prompt) + len(agent.instructions or ""))
    if model is not None:
        agent = agent.override(model=model)
    calls_before = m.state.llm_calls
    out = await _verify_turns(agent, prompt, ids, m)
    ran = m.state.llm_calls > calls_before
    exhausted = _exhausted(m, out)
    complete = verify_complete(
        ended_normally=_ended_normally(out.final, m),
        notes=out.notes,
        ids=ids,
        tool_calls=m.state.tool_calls,
    )
    return AgentRunResult(
        phase="verify",
        state=_state_of(out.final, ran),
        stop_reason=out.final.stop_reason,
        notes=out.notes or None,
        rescued=out.rescued,
        complete=complete,
        budget_exhausted=bool(exhausted) or out.rescue_attempted,
        exhausted_by=exhausted,
        opened_paths=list(ctx.opened),
        usage=_usage(m),
        error=_clean_error(out.final.error),
    )


# ------------------------------------------------------------------------ format

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.S)
_MAX_JSON_STARTS = 50  # "{" positions tried when salvaging JSON from prose


def salvage_json(raw: Any, target: type[BaseModel]) -> BaseModel | None:
    """Recover a draft from an answer that failed validation only because of its wrapping.

    Handles a fenced ```json block and leading or trailing prose around the first
    complete JSON object (hands-on lesson 8).
    """
    if isinstance(raw, dict):
        candidates: list[Any] = [raw]
    elif isinstance(raw, str):
        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1)
        candidates = []
        decoder = json.JSONDecoder()
        start = text.find("{")
        # Prose before the JSON may hold braces of its own ("the {path} field ..."):
        # take the first position where a complete JSON object decodes.
        for _ in range(_MAX_JSON_STARTS):
            if start < 0:
                break
            try:
                obj, _end = decoder.raw_decode(text, start)
            except ValueError:
                start = text.find("{", start + 1)
                continue
            candidates.append(obj)
            break
    else:
        return None
    for obj in candidates:
        try:
            return target.model_validate(obj)
        except ValidationError:
            continue
    return None


def _draft_of(state: Any, target: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
    """The validated draft of a formatter turn (or ``None``) and why it is missing."""
    if not isinstance(state, agents.Completed):
        return None, None
    output = state.output
    if isinstance(output, target):
        return output, None
    if isinstance(output, agents.InvalidOutput):
        draft = salvage_json(output.raw, target)
        return draft, None if draft is not None else f"invalid output: {output.error}"
    if isinstance(output, str):  # defensive: an untyped answer
        return salvage_json(output, target), None
    return None, None


def _finding_count(draft: BaseModel | None) -> int:
    return len(draft.findings) if isinstance(draft, FormatterDraft) else 0


@dataclass
class _FormatOutcome:
    state: Any
    draft: BaseModel | None
    error: str | None
    retried: bool = False


async def _format_turns(
    agent: agents.Agent,
    message: str,
    target: type[BaseModel],
    expected_findings: int,
    timeout: float,
    m: hooks.Meter,
) -> _FormatOutcome:
    """One formatter turn, plus one retry when it dropped FINDING blocks (lesson 4)."""
    ends = m.clock() + timeout
    async with agents.local_runtime():
        session = await agent.start_session()
        try:
            state, _ = await _settled(session, message, timeout)
            draft, error = _draft_of(state, target)
            out = _FormatOutcome(state=state, draft=draft, error=error)
            got = _finding_count(draft)
            remaining = ends - m.clock()
            if draft is not None and got < expected_findings and remaining >= 20.0:
                out.retried = True
                retry = prompts.formatter_dropped(expected_findings, got)
                state2, _ = await _settled(session, retry, remaining)
                draft2, _error2 = _draft_of(state2, target)
                if draft2 is not None and _finding_count(draft2) > got:
                    out.state, out.draft = state2, draft2
            return out
        finally:
            await session.cancel()


async def _format(
    job: AgentJob, files: jobio.JobFiles, notes: str, model: Any | None
) -> AgentRunResult:
    m = hooks.meter()
    ids: list[str] | None = None
    expected = 0
    if job.format_target == "verify":
        candidates = jobio.load_candidates(files.job_dir)
        ids = [c.candidate_id for c in candidates] if candidates is not None else None
    else:
        expected = parse_notes(notes).marker_count
    target: type[BaseModel] = FormatterDraft if job.format_target == "review" else VerifyDraft
    agent = build_formatter(job)
    message = prompts.formatter_message(notes, ids)
    m.reset_context(len(message) + len(agent.instructions or ""))
    if model is not None:
        agent = agent.override(model=model)
    calls_before = m.state.llm_calls
    timeout = min(FORMATTER_TIMEOUT, max(30.0, float(m.budgets.explore_seconds)))
    out = await _format_turns(agent, message, target, expected, timeout, m)
    state, draft, error = out.state, out.draft, out.error
    ran = m.state.llm_calls > calls_before
    result_state: Literal["Completed", "Failed", "InvalidOutput", "Error", "NotRun"]
    stop_reason: str | None = None
    if isinstance(state, agents.Completed):
        stop_reason = state.stop_reason
        if draft is not None:
            result_state = "Completed"
        elif not ran:
            result_state = "NotRun"
        elif isinstance(state.output, agents.InvalidOutput):
            result_state = "InvalidOutput"
        else:
            result_state = "Failed"
            error = error or f"no answer (stop reason: {stop_reason})"
    else:
        reduced = _reduce(state)
        stop_reason = reduced.stop_reason
        result_state = "NotRun" if not ran else ("Failed" if reduced.kind == "Failed" else "Error")
        error = reduced.error or f"turn ended as {reduced.kind}"
    return AgentRunResult(
        phase="format",
        state=result_state,
        stop_reason=stop_reason,
        draft=draft.model_dump(mode="json") if draft is not None else None,
        rescued=out.retried,
        complete=draft is not None,
        budget_exhausted=bool(m.state.exhausted_by),
        exhausted_by=list(m.state.exhausted_by),
        usage=_usage(m),
        error=_clean_error(error),
    )


# ------------------------------------------------------------------------ entry


# SDK errors that a retry of the whole process cannot fix: report them in result.json.
_FINAL_ERRORS: tuple[type[BaseException], ...] = (
    agents.AuthenticationError,
    agents.PermissionDeniedError,
    agents.UnsupportedAgentFeatureError,
)


def _error_result(phase: Phase, m: hooks.Meter, exc: BaseException) -> AgentRunResult:
    return AgentRunResult(
        phase=phase,
        state="Error",
        stop_reason="error",
        usage=_usage(m),
        exhausted_by=list(m.state.exhausted_by),
        error=_clean_error(f"{type(exc).__name__}: {exc}"),
    )


async def run_job(
    phase: Phase,
    job_dir: str | Path,
    *,
    model: Any | None = None,
    clock: Any = time.time,
) -> AgentRunResult:
    """Run one phase of a job directory and write ``result.json``.

    ``model`` replaces the real model (an ``agents.testing.ScriptedModel`` in tests).
    Raises ``jobio.InvalidJob`` for jobs that cannot run; other exceptions are crashes
    that the launcher may retry (``meter.json`` keeps the counts).
    """
    job, files = jobio.load_job(job_dir, phase)
    text = jobio.read_input(job, files)
    m = hooks.Meter.start(
        phase=phase,
        model=job.model,
        budgets=job.budgets,
        price_in_per_m=job.price_in_per_m,
        price_out_per_m=job.price_out_per_m,
        path=files.meter,
        clock=clock,
    )
    hooks.install_meter(m)
    if phase in ("explore", "verify"):
        m.on_text = lambda t: jobio.write_partial_notes(files, t)
    hooks.log(f"{phase} start: model={job.model} effort={job.effort} run={m.state.runs}")
    try:
        if phase == "explore":
            result = await _explore(job, files, text, model)
        elif phase == "verify":
            result = await _verify(job, files, text, model)
        else:
            result = await _format(job, files, text, model)
    except _FINAL_ERRORS as exc:
        result = _error_result(phase, m, exc)
    finally:
        m.save()
    jobio.write_result(files, result)
    u = result.usage
    hooks.log(
        f"{phase} done: state={result.state} stop={result.stop_reason} "
        f"complete={result.complete} llm_calls={u.llm_calls} tool_calls={u.tool_calls} "
        f"in={u.input_tokens} out={u.output_tokens} cost=${u.cost_usd:.4f} seconds={u.seconds:.1f}"
    )
    return result
