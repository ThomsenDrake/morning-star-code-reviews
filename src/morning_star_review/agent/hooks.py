"""Budget hook and usage meter for agent runs (DESIGN 6.4, ADDENDUM D).

The ``Meter`` is one module-level object per agent process (``METER``). The SDK
rebuilds a hook from its fields for every call, so the hook keeps no state of its
own and reads the meter instead. The meter is saved to ``meter.json`` (atomic
replace) after every model call and tool call, so a retried process continues
the counts and keeps the deadline fixed on the first start.

Budget rules:
- Tools close when the tool-call budget, the deadline, the context budget or the
  cost reserve is reached. After that the model gets ``grace_llm_calls`` more model
  calls to write its notes, then ``pre_llm_call`` ends the turn.
- ``pre_llm_call`` also ends the turn at ``deadline + grace_seconds`` (or the rescue
  deadline) and when the projected cost of the next call would pass the cap.
- Model calls that the SDK makes without a ``post_llm_call`` (its own retries after
  an output-limit stop) are charged at the projected cost, so the cap stays a cap.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import mistralai.agents as agents
from pydantic import BaseModel, ConfigDict, Field

from morning_star_review.core.contract import AgentBudgets, ExhaustReason, Usage
from morning_star_review.core.jsonio import write_text_atomic

__all__ = [
    "CONTEXT_WITHHELD",
    "METER",
    "OUTPUT_ALLOWANCE_TOKENS",
    "TOOLS_CLOSED",
    "BudgetHook",
    "Meter",
    "MeterState",
    "install_meter",
    "log",
    "meter",
]

OUTPUT_ALLOWANCE_TOKENS = 8_000  # output tokens assumed for the next call when projecting cost
CHARS_PER_TOKEN = 3  # deliberately low, so the context estimate errs high

TOOLS_CLOSED = (
    "Exploration budget is used up. Do not call any more tools. "
    "Write your answer now, in the required format."
)
CONTEXT_WITHHELD = (
    "[budget] Context budget reached; output withheld. Do not call tools again; "
    "write your answer now, in the required format."
)


class MeterState(BaseModel):
    """What ``meter.json`` holds. Unknown keys are ignored so old files still load."""

    model_config = ConfigDict(extra="ignore")

    phase: str
    model: str
    started_epoch: float
    deadline_epoch: float
    rescue_deadline_epoch: float | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    llm_call_limit: int
    tools_closed: bool = False
    exhausted_by: list[ExhaustReason] = Field(default_factory=list)
    prompt_chars: int = 0
    tool_output_chars: int = 0
    assistant_chars: int = 0
    last_input_tokens: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_calls: int = 0  # calls the SDK made without reporting usage to the hook
    cost_usd: float = 0.0
    finish_reasons: list[str] = Field(default_factory=list)
    seconds: float = 0.0  # active seconds of earlier processes
    pending_call: bool = False  # a model call started and has not reported back
    runs: int = 0  # processes that loaded this meter


class Meter:
    """Counts model calls, tool calls, tokens, cost and time for one job."""

    def __init__(
        self,
        state: MeterState,
        budgets: AgentBudgets,
        *,
        price_in_per_m: float,
        price_out_per_m: float,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self.budgets = budgets
        self.price_in = price_in_per_m
        self.price_out = price_out_per_m
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        self.process_started = clock()
        self.on_text: Callable[[str], None] | None = None  # partial-notes sink

    # ---------------------------------------------------------------- construction

    @classmethod
    def start(
        cls,
        *,
        phase: str,
        model: str,
        budgets: AgentBudgets,
        price_in_per_m: float,
        price_out_per_m: float,
        path: Path | None,
        clock: Callable[[], float] = time.time,
    ) -> Meter:
        """Load ``meter.json`` if a previous process left one, else start a new meter."""
        state: MeterState | None = None
        if path is not None and path.exists():
            try:
                state = MeterState.model_validate_json(path.read_bytes())
            except ValueError:
                state = None  # a torn or foreign file: start over, the deadline moves
            if state is not None and (state.phase != phase or state.model != model):
                state = None
        now = clock()
        if state is None:
            state = MeterState(
                phase=phase,
                model=model,
                started_epoch=now,
                deadline_epoch=now + budgets.explore_seconds,
                llm_call_limit=budgets.max_llm_calls,
            )
        meter = cls(
            state,
            budgets,
            price_in_per_m=price_in_per_m,
            price_out_per_m=price_out_per_m,
            path=path,
            clock=clock,
        )
        with meter.lock:
            state.runs += 1
            if state.pending_call:  # the last process died during a model call
                meter._charge_estimate()
            meter.save()
        return meter

    # ---------------------------------------------------------------- persistence

    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            snapshot = self.state.model_copy(
                update={"seconds": self.state.seconds + self.elapsed_this_process()}
            )
            write_text_atomic(self.path, snapshot.model_dump_json(indent=1) + "\n")

    def elapsed_this_process(self) -> float:
        return max(0.0, self.clock() - self.process_started)

    # ---------------------------------------------------------------- estimates

    def context_estimate_tokens(self) -> int:
        s = self.state
        chars = s.prompt_chars + s.tool_output_chars + s.assistant_chars
        return max(chars // CHARS_PER_TOKEN, s.last_input_tokens)

    def call_cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def projected_next_call_cost(self) -> float:
        return self.call_cost(self.context_estimate_tokens(), OUTPUT_ALLOWANCE_TOKENS)

    def can_afford_rescue(self) -> bool:
        b = self.budgets
        with self.lock:
            affordable = self.state.cost_usd + self.projected_next_call_cost() <= b.max_cost_usd
        return affordable and b.grace_llm_calls >= 1 and b.rescue_seconds > 0

    # ---------------------------------------------------------------- state changes

    def note_exhausted(self, reason: ExhaustReason) -> None:
        with self.lock:
            if reason not in self.state.exhausted_by:
                self.state.exhausted_by.append(reason)

    def close_tools(self, reason: ExhaustReason | None = None) -> None:
        """No more tools; ``grace_llm_calls`` more model calls to write the answer."""
        with self.lock:
            s = self.state
            if reason is not None:
                self.note_exhausted(reason)
            if not s.tools_closed:
                s.tools_closed = True
                s.llm_call_limit = min(s.llm_call_limit, s.llm_calls + self.budgets.grace_llm_calls)
            self.save()

    def start_rescue(self) -> None:
        """Tools stay closed; allow ``grace_llm_calls`` calls within ``rescue_seconds``."""
        with self.lock:
            s = self.state
            s.tools_closed = True
            s.llm_call_limit = s.llm_calls + self.budgets.grace_llm_calls
            s.rescue_deadline_epoch = self.clock() + self.budgets.rescue_seconds
            self.save()

    def reset_context(self, prompt_chars: int) -> None:
        """A new session starts: its context holds the instructions and the prompt only."""
        with self.lock:
            s = self.state
            s.prompt_chars = prompt_chars
            s.tool_output_chars = 0
            s.assistant_chars = 0
            s.last_input_tokens = 0
            self.save()

    def _charge_estimate(self) -> None:
        """Charge a model call whose usage never reached the hook."""
        s = self.state
        tokens_in = self.context_estimate_tokens()
        s.input_tokens += tokens_in
        s.output_tokens += OUTPUT_ALLOWANCE_TOKENS
        s.cost_usd += self.call_cost(tokens_in, OUTPUT_ALLOWANCE_TOKENS)
        s.estimated_calls += 1
        s.pending_call = False

    # ---------------------------------------------------------------- gates

    def tool_gate(self) -> ExhaustReason | Literal["closed"] | None:
        """Admit one tool call (count it) or say why tools are closed."""
        with self.lock:
            s, b = self.state, self.budgets
            reason: ExhaustReason | None = None
            if s.tools_closed:
                return "closed"
            if s.tool_calls >= b.max_tool_calls:
                reason = "tool_calls"
            elif self.clock() >= s.deadline_epoch:
                reason = "deadline"
            elif (
                s.cost_usd + (b.grace_llm_calls + 1) * self.projected_next_call_cost()
                > b.max_cost_usd
            ):
                reason = "cost"  # keep money for the notes and a rescue
            if reason is not None:
                self.close_tools(reason)
                return reason
            s.tool_calls += 1
            self.save()
            return None

    def llm_gate(self) -> str | None:
        """Admit one model call (count it) or return the reason to end the turn."""
        with self.lock:
            s, b = self.state, self.budgets
            if s.pending_call:  # the SDK called again without a post_llm_call in between
                self._charge_estimate()
            now = self.clock()
            if s.llm_calls >= s.llm_call_limit:
                if not s.tools_closed:
                    self.note_exhausted("iterations")
                return "budget: llm calls"
            limit = (
                s.rescue_deadline_epoch
                if s.rescue_deadline_epoch is not None
                else s.deadline_epoch + b.grace_seconds
            )
            if now >= limit:
                self.note_exhausted("deadline")
                self.save()
                return "budget: time"
            if s.cost_usd + self.projected_next_call_cost() > b.max_cost_usd:
                self.note_exhausted("cost")
                self.save()
                return "budget: cost"
            s.llm_calls += 1
            s.pending_call = True
            self.save()
            return None

    def admit_tool_output(self, n_chars: int) -> bool:
        """Account for a tool result of ``n_chars``; False = withhold it (context budget).

        The SDK runs every ``pre_tool_call`` of a parallel batch before any tool, so a
        later call of the batch may close tools (tool count, deadline, cost) after an
        earlier one was admitted. The admitted call keeps its output; only a context
        closure withholds the rest of the batch, so the context cap stays a cap.
        """
        with self.lock:
            s = self.state
            if s.tools_closed and "context" in s.exhausted_by:
                return False
            projected = self.context_estimate_tokens() + n_chars // CHARS_PER_TOKEN
            if projected > self.budgets.max_context_tokens:
                self.close_tools("context")
                return False
            s.tool_output_chars += n_chars
            self.save()
            return True

    def record_llm_reply(
        self,
        *,
        usage: Any,
        finish_reason: str,
        text: str,
        extra_chars: int = 0,
    ) -> None:
        """Book one model reply (``usage`` is an ``agents.TokenUsage`` or ``None``)."""
        with self.lock:
            s = self.state
            s.pending_call = False
            s.finish_reasons = [*s.finish_reasons, finish_reason][-200:]
            s.assistant_chars += len(text) + extra_chars
            if usage is None:
                self._charge_estimate()
            else:
                tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
                cached = int(getattr(usage, "cached_input_tokens", 0) or 0)
                tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
                reasoning = int(getattr(usage, "reasoning_tokens", 0) or 0)
                s.input_tokens += tokens_in
                s.cached_input_tokens += cached
                s.output_tokens += tokens_out
                s.reasoning_tokens += reasoning
                s.last_input_tokens = tokens_in
                s.cost_usd += self.call_cost(max(tokens_in, cached), max(tokens_out, reasoning))
            self.save()
        if text.strip() and self.on_text is not None:
            self.on_text(text)

    # ---------------------------------------------------------------- reporting

    def usage(self) -> Usage:
        s = self.state
        return Usage(
            model=s.model,
            llm_calls=s.llm_calls,
            tool_calls=s.tool_calls,
            input_tokens=s.input_tokens,
            cached_input_tokens=s.cached_input_tokens,
            output_tokens=s.output_tokens,
            reasoning_tokens=s.reasoning_tokens,
            cost_usd=round(s.cost_usd, 6),
            seconds=round(s.seconds + self.elapsed_this_process(), 3),
            finish_reasons=[r[:20] for r in s.finish_reasons][-200:],
        )


# ------------------------------------------------------------------------ module meter

METER: Meter | None = None


def install_meter(m: Meter | None) -> None:
    global METER
    METER = m


def meter() -> Meter:
    if METER is None:
        raise RuntimeError("no meter is installed in this agent process")
    return METER


def log(message: str) -> None:
    """One progress line for ``agent.log`` (never prompts, notes or secrets)."""
    print(f"msreview-agent: {message}", file=sys.stderr, flush=True)


class BudgetHook(agents.Hook):
    """Enforces the job's budgets through the module-level ``METER`` (no fields: no state)."""

    async def pre_tool_call(self, ctx: Any, call: Any) -> Any:
        reason = meter().tool_gate()
        if reason is not None:
            log(f"tool {call.tool_name} skipped ({reason})")
            return agents.skip(TOOLS_CLOSED)
        log(f"tool {call.tool_name} {_short_args(call.arguments)}")
        return None

    async def pre_llm_call(self, ctx: Any, invocation: Any) -> Any:
        reason = meter().llm_gate()
        if reason is not None:
            log(f"model call refused ({reason})")
            return agents.skip(reason)
        return None

    async def post_llm_call(self, ctx: Any, invocation: Any) -> Any:
        m = meter()
        calls_chars = sum(len(str(c.arguments)) + len(c.name) for c in invocation.tool_calls)
        m.record_llm_reply(
            usage=invocation.usage,
            finish_reason=str(invocation.finish_reason),
            text=invocation.text or "",
            extra_chars=calls_chars + len(invocation.reasoning or ""),
        )
        u = invocation.usage
        log(
            f"model call {m.state.llm_calls}: finish={invocation.finish_reason} "
            f"in={getattr(u, 'input_tokens', '?')} out={getattr(u, 'output_tokens', '?')} "
            f"tools={len(invocation.tool_calls)} cost=${m.state.cost_usd:.4f}"
        )
        return None


def _short_args(arguments: Any) -> str:
    """Tool arguments for the log: short scalar values only (paths, line numbers)."""
    if not isinstance(arguments, dict):
        return ""
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:80]}")
    return " ".join(parts)[:300]
