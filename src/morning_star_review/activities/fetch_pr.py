"""Activity that fetches the pull request snapshot from GitHub (DESIGN 5.2, 9.4).

``early_skip`` is the same skip decision from a few cheap reads; the driver runs it
before it boots a worker, so a skipped run starts no worker and no execution.

``fetch_pr`` also saves the sealed snapshot as ``<run_dir>/snapshot.json``
(``runclock.SNAPSHOT_FILE``): when a run reaches its hard deadline, the driver's
backstop reads it (seal verified) to mark the pull request without the workflow.
Its timeout leaves room for GitHub rate-limit waits (``github.http``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from mistralai import workflows

from morning_star_review.activities.common import (
    activity_errors,
    check_payload_budget,
    mark_started,
    progress,
)
from morning_star_review.core.config import ConfigError, resolve
from morning_star_review.core.contract import (
    PriorState,
    PrSnapshot,
    ReviewRequest,
    SkipReason,
    Verdict,
)
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_model
from morning_star_review.core.naming import parse_pr_url
from morning_star_review.core.runclock import SNAPSHOT_FILE
from morning_star_review.core.seal import seal
from morning_star_review.github.fetch import (
    _load_config,
    _prior_state,
    build_snapshot,
    decide_skip,
    read_client,
)
from morning_star_review.github.http import GitHubHttp

__all__ = ["FETCH_PR_TIMEOUT", "EarlySkip", "early_skip", "fetch_pr", "save_snapshot"]

# Reads only; GitHub rate-limit waits (up to 15 minutes per request) fit inside.
FETCH_PR_TIMEOUT = timedelta(minutes=20)


@dataclass(frozen=True)
class EarlySkip:
    """A skip decided before any worker or execution exists."""

    skip: SkipReason
    head_sha: str
    prior_verdict: Verdict | None = None  # the verdict of the review already posted


async def early_skip(req: ReviewRequest, gh: GitHubHttp, bot_login: str | None) -> EarlySkip | None:
    """The skip ``fetch_pr`` would decide, from at most three reads (DESIGN 9.4 order).

    Reads the PR, then (only when needed) the base-branch config and our bot's summary
    comment, and applies ``decide_skip``: superseded, closed, draft, already reviewed
    with the same configuration. Read only. ``None`` = review it (``fetch_pr`` decides
    again on the worker, with the full snapshot). GitHub errors propagate; the driver
    then goes on as if there were no pre-check.
    """
    ref = parse_pr_url(req.pr_url)
    base = f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}"
    pr = await gh.get_json(f"{base}/pulls/{ref.number}")
    head_sha = str(pr["head"]["sha"])
    state: Literal["open", "closed"] = "closed" if pr.get("state") == "closed" else "open"
    merged = bool(pr.get("merged"))
    draft = bool(pr.get("draft"))
    # superseded and closed need no config: decide them before reading any more.
    superseded = bool(req.expected_head_sha) and req.expected_head_sha != head_sha
    if superseded or (not req.force and (state == "closed" or merged)):
        return EarlySkip("superseded" if superseded else "closed", head_sha)
    if req.force:
        return None
    # _load_config and _prior_state are build_snapshot's own readers: same reads, same rules.
    repo_cfg, _source, _warnings = await _load_config(
        gh, ref.owner, ref.repo, str(pr["base"]["sha"])
    )
    try:
        cfg, _resolve_warnings = resolve(repo_cfg, req.overrides, req.policy)
    except ConfigError:
        return None  # fetch_pr fails the run with the reason
    prior: PriorState | None = None
    if bot_login:
        prior, _summary_id, _prior_warnings = await _prior_state(
            gh, ref.owner, ref.repo, ref.number, bot_login
        )
    skip = decide_skip(
        req, head_sha=head_sha, state=state, merged=merged, draft=draft, cfg=cfg, prior=prior
    )
    if skip is None:
        return None
    verdict = prior.verdict if skip == "already_reviewed" and prior is not None else None
    return EarlySkip(skip, head_sha, verdict)


def save_snapshot(run_dir: Path, sealed: PrSnapshot) -> None:
    """Write the sealed snapshot to ``<run_dir>/snapshot.json`` (atomic; never raises).

    The same bytes ``keep_for_finalize`` would write. Only the driver's deadline
    backstop reads it, so a failed write is reported and the run goes on.
    """
    try:
        write_model(run_dir / SNAPSHOT_FILE, sealed)
    except (OSError, ValueError) as exc:
        progress(f"msreview: could not save {SNAPSHOT_FILE} ({type(exc).__name__})")


@workflows.activity(
    name="msreview_fetch_pr",
    start_to_close_timeout=FETCH_PR_TIMEOUT,
    retry_policy_max_attempts=6,
    retry_policy_backoff_coefficient=3.0,
)
async def fetch_pr(req: ReviewRequest) -> PrSnapshot:
    """Read the PR, its files, the base-branch config and our earlier state.

    Writes ``<run_dir>/pr_files.json``; decides ``skip`` and ``can_post``.
    """
    ctx = guard(req)
    with activity_errors():
        run_dir = ctx.run_dir
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        mark_started(run_dir)
        async with read_client(ctx) as gh:
            snap = await build_snapshot(req, ctx, gh, run_dir)
        check_payload_budget(snap)
        if snap.skip is not None:
            progress(f"msreview: skip={snap.skip} head={snap.head_sha[:7]}")
        sealed = seal(snap, ctx)
        save_snapshot(run_dir, sealed)
        return sealed
