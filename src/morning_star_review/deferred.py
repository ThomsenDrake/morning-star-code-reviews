"""Deferred publishing in the GitHub Action: review first, mint the write token, then post.

A GitHub App installation token lives one hour, and a slow review (models that are
rate-limited) can take longer. So in the Action the review step only holds a
read-only App token: its ``publish`` (or ``report_failure``) activity writes
``publish_plan.json`` instead of writing to GitHub, and ``outputs.emit`` copies the plan
out of the run directory. The next steps mint a fresh write token and run
``msreview ci-publish``, which posts the plan (``publish_plan``).

Trust: the plan is written by our own activity on the same runner, in the same job,
like ``outcome.json``. It is not sealed (the seal key lives only in the review step's
process). ``publish_plan`` checks that the App it posts as is the App the review was
prepared for, and re-checks the PR head before it writes anything.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from morning_star_review.core.contract import (
    Contract,
    FailureInput,
    PublishInput,
    ReviewOutcome,
)
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.jsonio import (
    read_bytes,
    write_bytes_atomic,
    write_model,
    write_text_atomic,
)
from morning_star_review.core.redact import redact
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.seal import request_digest
from morning_star_review.github.http import GitHubHttp, GitHubRateLimited
from morning_star_review.github.publish import (
    publish,
    report_outcome,
    report_reason,
    write_failure_header,
    write_report_files,
)
from morning_star_review.github.state import same_login

__all__ = [
    "MAX_PLAN_BYTES",
    "PUBLISH_PLAN_JSON",
    "DeferredPlan",
    "export_plan",
    "load_plan",
    "publish_plan",
    "remove_plan",
    "write_plan",
]

log = logging.getLogger(__name__)

PUBLISH_PLAN_JSON = "publish_plan.json"
OUTCOME_JSON = "outcome.json"  # same name as activities.publish.OUTCOME_JSON
MAX_PLAN_BYTES = 4 * 1024 * 1024
# GitHub rate-limit waits of the publish step end this long after it starts.
PUBLISH_WAIT_S = 600.0
# Retries of a publish that failed on a temporary error (publish is idempotent).
RETRY_DELAYS_S = (5.0, 15.0, 45.0, 120.0)
_TOKEN_STEP_FAILED = ("failure", "cancelled")
_NO_BOT_LOGIN = "Not posted: the GitHub App's bot login is unknown (app slug missing)."

Sleep = Callable[[float], Awaitable[None]]


class DeferredPlan(Contract):
    """What the publish step posts: a finished review, or a failed run to mark."""

    schema_version: Literal[1] = 1
    kind: Literal["review", "failure"]
    # A file name: report files go to <plan dir>/<execution_id>.
    execution_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    review: PublishInput | None = None
    failure: FailureInput | None = None
    # The review step's own outcome: a report (review) or the failed outcome (failure).
    outcome: ReviewOutcome

    @model_validator(mode="after")
    def _one_payload(self) -> DeferredPlan:
        if self.kind == "review" and (self.review is None or self.failure is not None):
            raise ValueError("a review plan carries the review and nothing else")
        if self.kind == "failure":
            if self.failure is None or self.review is not None:
                raise ValueError("a failure plan carries the failure and nothing else")
            if self.outcome.status != "failed":
                raise ValueError("a failure plan carries a failed outcome")
        return self


def write_plan(run_dir: str | Path, plan: DeferredPlan) -> Path:
    """Write ``<run_dir>/publish_plan.json`` (atomic, mode 0600)."""
    text = plan.model_dump_json()
    if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("the publish plan is larger than 4 MiB")
    path = Path(run_dir) / PUBLISH_PLAN_JSON
    write_text_atomic(path, text, mode=0o600)
    return path


def load_plan(path: str | Path) -> DeferredPlan:
    """Read and validate a plan (at most 4 MiB; a symbolic link is refused)."""
    p = Path(path)
    if p.is_symlink():
        raise ValueError("the publish plan is a symbolic link")
    return DeferredPlan.model_validate_json(read_bytes(p, max_bytes=MAX_PLAN_BYTES))


def export_plan(run_dir: str | Path, dest_dir: str | Path) -> Path | None:
    """Copy a valid plan from the run directory to ``dest_dir``; None when there is none."""
    src = Path(run_dir) / PUBLISH_PLAN_JSON
    if src.is_symlink() or not src.is_file():
        return None
    data = read_bytes(src, max_bytes=MAX_PLAN_BYTES)
    DeferredPlan.model_validate_json(data)  # never hand on a plan that cannot load
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = dest / PUBLISH_PLAN_JSON
    write_bytes_atomic(out, data, mode=0o600)
    return out


def remove_plan(dest_dir: str | Path) -> None:
    """Remove a plan left in ``dest_dir`` by an earlier run (best effort)."""
    path = Path(dest_dir) / PUBLISH_PLAN_JSON
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove an old publish plan: %s", type(exc).__name__)


# ----------------------------------------------------------------------------- publish


def _context(plan: DeferredPlan, root: Path, token: str | None, bot: str | None) -> RunContext:
    """A minimal run context for posting: no seal checks, no agent, no Mistral key."""
    req = plan.review.req if plan.review is not None else plan.failure.req  # type: ignore[union-attr]
    return RunContext(
        execution_id=plan.execution_id,
        request_sha256=request_digest(req),
        seal_key=secrets.token_bytes(32),
        run_root=str(root),
        mode=req.mode,
        on_worker=False,
        github_token=token,
        read_token=None,
        bot_login=bot,
        mistral_api_key="",
        agent_python="",
    )


def _blocked_reason(token: str | None, bot: str | None, token_step: str) -> str | None:
    """Why the publish step cannot post at all, or None."""
    if token_step in _TOKEN_STEP_FAILED:
        return report_reason("token_step_failed", outcome=token_step)
    if not token:
        return report_reason("no_credentials")
    if not bot:
        return _NO_BOT_LOGIN
    return None


def _retryable(exc: BaseException, wait_until: float) -> bool:
    if isinstance(exc, MsreviewError) and exc.non_retryable:
        return False
    # A rate limit that outlasts the publish step's waiting time will not clear soon.
    return not (isinstance(exc, GitHubRateLimited) and time.time() >= wait_until)


async def publish_plan(
    plan: DeferredPlan,
    token: str | None,
    app_slug: str | None,
    token_step: str,
    *,
    plan_dir: str | Path | None = None,
    sleep: Sleep | None = None,
) -> ReviewOutcome:
    """Post the plan with the write token minted after the review (``ci-publish``).

    Review plans go through ``github.publish.publish`` (idempotent; a head that moved
    while the review ran makes it a report). Failure plans mark the PR (banner and
    ``msreview: incomplete``) and keep the failed outcome. Report files and
    ``outcome.json`` go to ``<plan_dir>/<execution_id>``. Never posts as another App
    than the one the review was prepared for.
    """
    root = Path(plan_dir) if plan_dir is not None else Path.cwd()
    token = (token or "").strip() or None
    bot = f"{app_slug.strip()}[bot]" if app_slug and app_slug.strip() else None
    blocked = _blocked_reason(token, bot, token_step)
    ctx = _context(plan, root, token if blocked is None else None, bot)
    ctx.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    wait_until = time.time() + PUBLISH_WAIT_S
    if plan.kind == "failure":
        outcome = await _publish_failure(plan, ctx, blocked, wait_until)
    else:
        outcome = await _publish_review(plan, ctx, blocked, wait_until, sleep or asyncio.sleep)
    try:
        write_model(ctx.run_dir / OUTCOME_JSON, outcome)
    except OSError as exc:
        log.warning("could not write outcome.json: %s", type(exc).__name__)
    return outcome


def _mismatch(bot: str | None, expected: str | None) -> str | None:
    if bot and expected and not same_login(bot, expected):
        return report_reason("bot_mismatch", bot=bot, expected=expected)
    return None


async def _publish_review(
    plan: DeferredPlan, ctx: RunContext, blocked: str | None, wait_until: float, sleep: Sleep
) -> ReviewOutcome:
    inp = plan.review
    assert inp is not None  # noqa: S101 - guaranteed by DeferredPlan's validator
    # Report files go next to the plan (the review step's run directory is gone).
    snap = inp.snap.model_copy(update={"run_dir": str(ctx.run_dir)})
    final = inp.final
    if snap.skip is not None or final is None or not snap.can_post:
        return await publish(inp.req, snap, final, ctx, None)
    blocked = blocked or _mismatch(ctx.bot_login, snap.bot_login)
    if blocked is not None:
        outcome = report_outcome(inp.req, snap, final, blocked, execution_id=ctx.execution_id)
        write_report_files(snap.run_dir, snap, final, outcome)
        return outcome
    error: Exception | None = None
    for delay in (*RETRY_DELAYS_S, None):
        try:
            async with GitHubHttp(ctx.github_token, wait_until_epoch=wait_until) as gh:
                return await publish(inp.req, snap, final, ctx, gh)
        except Exception as exc:
            error = exc
            if delay is None or not _retryable(exc, wait_until):
                break
            log.warning("publishing failed (%s); retrying in %.0fs", type(exc).__name__, delay)
            await sleep(delay)
    assert error is not None  # noqa: S101 - the loop only ends here after an exception
    text = redact(f"{type(error).__name__}: {error}", ctx.secrets())
    reason = f"Not posted: publishing to GitHub failed ({' '.join(text.split())[:600]})."
    outcome = report_outcome(
        inp.req, snap, final, reason, execution_id=ctx.execution_id, status="failed"
    )
    write_report_files(snap.run_dir, snap, final, outcome)
    async with GitHubHttp(ctx.github_token, wait_until_epoch=wait_until) as gh:
        await write_failure_header(inp.req, snap, "publish", ctx, gh)  # never raises
    return outcome


async def _publish_failure(
    plan: DeferredPlan, ctx: RunContext, blocked: str | None, wait_until: float
) -> ReviewOutcome:
    inp = plan.failure
    assert inp is not None  # noqa: S101 - guaranteed by DeferredPlan's validator
    snap = inp.snap
    if snap is None or not snap.can_post:
        return plan.outcome
    blocked = blocked or _mismatch(ctx.bot_login, snap.bot_login)
    if blocked is not None:
        note = f"The PR was not marked: {blocked}"[:300]
        return plan.outcome.model_copy(update={"notes": [*plan.outcome.notes, note][:50]})
    async with GitHubHttp(ctx.github_token, wait_until_epoch=wait_until) as gh:
        await write_failure_header(inp.req, snap, inp.stage, ctx, gh)  # never raises
    return plan.outcome
