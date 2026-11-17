"""Helpers shared by the activities: error conversion, run-directory files, agent jobs.

Nothing here is an activity. Activities call ``guard`` first, then do their work inside
``activity_errors()`` so that a non-retryable error reaches Temporal as a
non-retryable ``ApplicationError`` (local in-process runs see the raw exception).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import TypeAdapter

from morning_star_review.core.contract import (
    AgentBudgets,
    AgentJob,
    AgentPhase,
    AgentState,
    ChangedFileRef,
    ExhaustReason,
    ModelRole,
    ResolvedBudgets,
    ResolvedRoles,
    Role,
    Usage,
)
from morning_star_review.core.errors import MsreviewError, MsreviewRunDirMissing, for_activity
from morning_star_review.core.guard import heartbeat
from morning_star_review.core.jsonio import read_bytes, write_json_atomic
from morning_star_review.core.models import price
from morning_star_review.core.naming import slug

__all__ = [
    "CHANGED_FILES_JSON",
    "HEARTBEAT_EVERY",
    "JOB_JSON",
    "RELATED_JSON",
    "STARTED_FILE",
    "activity_errors",
    "agent_budgets",
    "agent_job",
    "agent_line",
    "agent_state",
    "blocking",
    "cap_bytes",
    "cap_paths",
    "cost_share",
    "exhaust_reasons",
    "head_text_reader",
    "job_dir_name",
    "launch_timeout",
    "link_diffs",
    "load_changed_files",
    "mark_started",
    "progress",
    "read_partial_notes",
    "require_dir",
    "started_epoch",
    "usage_for",
    "write_changed_files",
]

HEARTBEAT_EVERY = 15.0  # seconds; every heartbeat timeout is 60 s
CHANGED_FILES_JSON = "changed_files.json"  # ChangedFileRef list, diff_file relative to run_dir
RELATED_JSON = "related.json"  # related definitions (ADDENDUM H), reused by the verifier
STARTED_FILE = "started_at"  # epoch seconds of the first fetch_pr attempt

# Share of the run's max_cost_usd kept for formatter calls (cheap, at most a few per run)
# and for the verifier; the explorers split the rest equally.
FORMATTER_COST_SHARE = 0.05
VERIFIER_COST_SHARE = 0.20

HEAD_FILE_MAX_BYTES = 2 * 1024 * 1024  # finalize/merge read head files up to this size

_CHANGED = TypeAdapter(list[ChangedFileRef])
_SAFE_JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


@contextmanager
def activity_errors() -> Iterator[None]:
    """Re-raise a non-retryable ``MsreviewError`` as Temporal expects (see ``for_activity``)."""
    try:
        yield
    except Exception as exc:
        converted = for_activity(exc)
        if converted is exc:
            raise
        raise converted from exc


def progress(line: str) -> None:
    """One progress line on stderr (``msreview: ...`` / ``msreview agent: ...``).

    The worker runs inside the driver process, so these reach the user's terminal and
    the eval script. They never carry prompts, notes or secrets.
    """
    print(line, file=sys.stderr, flush=True)


def require_dir(path: str | Path) -> Path:
    """``path`` as a directory; ``MsreviewRunDirMissing`` (non-retryable) if it is gone."""
    p = Path(path)
    if not p.is_dir():
        raise MsreviewRunDirMissing(f"run directory is missing: {p.name}")
    return p


async def blocking[T](
    fn: Callable[[], T], details: dict[str, Any], every: float = HEARTBEAT_EVERY
) -> T:
    """Run blocking ``fn`` in a thread and heartbeat from the event loop meanwhile.

    Temporal's heartbeat must be called on the event loop thread, so a blocking call
    (git, file scans) cannot heartbeat by itself.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn))
    while True:
        done, _ = await asyncio.wait({task}, timeout=every)
        if done:
            return task.result()
        heartbeat(details)


def job_dir_name(prefix: str, model: str) -> str:
    """``<prefix>-<model>`` for a job directory; odd model ids get a slug plus a hash."""
    name = f"{prefix}-{model}"
    if _SAFE_JOB_NAME.match(name):
        return name
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{slug(model)[:48] or 'model'}-{digest}"


def cost_share(roles: ResolvedRoles, total: float, role: Role) -> float:
    """Part of the run's ``max_cost_usd`` one agent job of ``role`` may spend."""
    formatter = total * FORMATTER_COST_SHARE
    verifier = total * VERIFIER_COST_SHARE if roles.verify_enabled else 0.0
    explorers = len(roles.explorers)
    if role == "verifier":
        return max(verifier, 0.01)
    if role == "formatter":
        # One formatter job per explorer at most, plus one for the verifier.
        return max(formatter / (explorers + (1 if roles.verify_enabled else 0)), 0.01)
    return max((total - formatter - verifier) / explorers, 0.01)


def agent_budgets(
    b: ResolvedBudgets,
    *,
    max_tool_calls: int,
    seconds: int,
    max_cost_usd: float,
) -> AgentBudgets:
    """Agent budgets for one explore or verify job."""
    return AgentBudgets(
        max_tool_calls=max_tool_calls,
        max_llm_calls=b.max_llm_calls,
        grace_llm_calls=b.grace_llm_calls,
        explore_seconds=seconds,
        grace_seconds=b.grace_seconds,
        rescue_seconds=b.rescue_seconds,
        max_context_tokens=b.max_context_tokens,
        auto_compact_threshold=b.auto_compact_threshold,
        max_tool_output_chars=b.max_tool_output_chars,
        max_cost_usd=max_cost_usd,
    )


def write_changed_files(run_dir: Path, refs: Sequence[ChangedFileRef]) -> None:
    write_json_atomic(run_dir / CHANGED_FILES_JSON, [r.model_dump(mode="json") for r in refs])


def load_changed_files(run_dir: Path) -> list[ChangedFileRef]:
    return _CHANGED.validate_json(read_bytes(run_dir / CHANGED_FILES_JSON))


def link_diffs(run_dir: Path, job_dir: Path, refs: Sequence[ChangedFileRef]) -> None:
    """Make every rendered diff available inside ``job_dir`` at the same relative path.

    The agent's ``show_file_diff`` only reads files inside its job directory (symlinks
    are resolved and refused), so each job gets hard links, or copies where the file
    system has no hard links.
    """
    for ref in refs:
        if not ref.diff_file:
            continue
        src = run_dir / ref.diff_file
        dst = job_dir / ref.diff_file
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)


def _inside(root: Path, rel: str) -> Path | None:
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = Path(rel).parts
    if ".." in parts or (parts and parts[0] == ".git"):
        return None
    full = Path(os.path.realpath(root / rel))
    if root not in full.parents:
        return None
    return full


def head_text_reader(repo_dir: str | Path) -> Callable[[str], list[str] | None]:
    """``head_text(path)`` for ``review``: head lines of a file in the checkout.

    Confined to ``repo_dir`` (no ``..``, no ``.git``, symlinks resolved and checked);
    missing, binary or oversized files give ``None``.
    """
    root = Path(os.path.realpath(repo_dir))
    cache: dict[str, list[str] | None] = {}

    def head_text(path: str) -> list[str] | None:
        if path in cache:
            return cache[path]
        lines: list[str] | None = None
        full = _inside(root, path)
        if full is not None and full.is_file():
            try:
                if full.stat().st_size <= HEAD_FILE_MAX_BYTES:
                    data = full.read_bytes()
                    if b"\x00" not in data[:8192]:
                        lines = data.decode("utf-8", "replace").splitlines()
            except OSError:
                lines = None
        cache[path] = lines
        return lines

    return head_text


def started_epoch(run_dir: Path) -> float | None:
    """Epoch of the first ``fetch_pr`` attempt (for the duration in the run details)."""
    try:
        return float((run_dir / STARTED_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def mark_started(run_dir: Path) -> None:
    """Record the start time once (a retried ``fetch_pr`` keeps the first value)."""
    path = run_dir / STARTED_FILE
    if not path.exists():
        path.write_text(f"{time.time():.3f}\n", encoding="utf-8")


JOB_JSON = "job.json"
_EXHAUST_REASONS: frozenset[str] = frozenset(
    {"tool_calls", "deadline", "context", "cost", "iterations", "length"}
)


def agent_job(
    *,
    phase: AgentPhase,
    execution_id: str,
    repo_dir: str,
    head_sha: str,
    merge_base_sha: str,
    input_path: Path,
    input_sha256: str,
    variant: Literal["full", "incremental"],
    role: ModelRole,
    budgets: AgentBudgets,
    changed_files: Sequence[ChangedFileRef],
    format_target: Literal["review", "verify"] = "review",
) -> AgentJob:
    """An ``AgentJob`` whose runner files live in the job directory itself."""
    price_in, price_out = price(role.model)
    return AgentJob(
        phase=phase,
        execution_id=execution_id,
        repo_dir=repo_dir,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        input_path=str(input_path),
        input_sha256=input_sha256,
        variant=variant,
        model=role.model,
        effort=role.effort,
        budgets=budgets,
        price_in_per_m=price_in,
        price_out_per_m=price_out,
        changed_files=list(changed_files),
        meter_path="meter.json",
        coverage_path="coverage.json",
        result_path="result.json",
        partial_notes_path="notes.partial.txt",
        format_target=format_target,
    )


def launch_timeout(budgets: AgentBudgets, activity_limit: timedelta) -> float:
    """Wall-clock allowance for the agent process inside one activity attempt.

    The runner's own budget (explore + grace + rescue) plus room for process start and
    the SDK's interrupt settling, but always ending before the activity times out.
    """
    wanted = budgets.explore_seconds + budgets.grace_seconds + budgets.rescue_seconds + 90
    return float(max(60, min(wanted, activity_limit.total_seconds() - 60)))


def exhaust_reasons(values: Sequence[str]) -> list[ExhaustReason]:
    out: list[ExhaustReason] = []
    for v in values:
        if v in _EXHAUST_REASONS and v not in out:
            out.append(cast(ExhaustReason, v))
    return out


def agent_state(state: str) -> AgentState:
    """Explore/verify state for the payload (``InvalidOutput`` only exists for format)."""
    if state in ("Completed", "Failed", "Error", "NotRun"):
        return cast(AgentState, state)
    return "Failed"


def usage_for(usage: Usage, model: str) -> Usage:
    """``usage`` attributed to ``model`` (the job's model, whatever the runner wrote)."""
    return Usage.model_validate({**usage.model_dump(mode="json"), "model": model})


def cap_bytes(text: str, max_chars: int, max_bytes: int) -> tuple[str, bool]:
    """``text`` cut to ``max_chars`` characters and ``max_bytes`` UTF-8 bytes (head kept)."""
    out = text[:max_chars]
    data = out.encode("utf-8", "surrogatepass")
    if len(data) > max_bytes:
        out = data[:max_bytes].decode("utf-8", "ignore")
    return out, out != text


def cap_paths(paths: Sequence[str], known: set[str], max_items: int, max_bytes: int) -> list[str]:
    """Known paths only, deduplicated, at most ``max_items`` and ``max_bytes`` in total."""
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for p in paths:
        if p in seen or p not in known:
            continue
        total += len(p.encode("utf-8", "surrogatepass")) + 4
        if len(out) >= max_items or total > max_bytes:
            break
        seen.add(p)
        out.append(p)
    return out


def read_partial_notes(job_dir: Path, max_bytes: int = 1024 * 1024) -> str:
    """``notes.partial.txt`` of an agent job (empty when absent)."""
    try:
        return read_bytes(job_dir / "notes.partial.txt", max_bytes=max_bytes).decode(
            "utf-8", "replace"
        )
    except (MsreviewError, ValueError):
        return ""


def agent_line(
    phase: str,
    model: str,
    *,
    complete: bool,
    rescued: bool,
    budget_exhausted: bool,
    exhausted_by: Sequence[str],
    stop_reason: str | None,
    state: str,
) -> str:
    """The ``msreview agent:`` progress line (space-separated ``key=value`` pairs)."""
    reasons = ",".join(exhausted_by) or "-"
    stop = re.sub(r"\s+", "_", stop_reason or "-")[:60]
    return (
        f"msreview agent: phase={phase} model={model} state={state} complete={complete} "
        f"rescued={rescued} budget_exhausted={budget_exhausted} exhausted_by={reasons} "
        f"stop_reason={stop}"
    )
