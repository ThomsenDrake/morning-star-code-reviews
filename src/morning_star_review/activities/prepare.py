"""Activity that checks out the PR and builds the review prompt plan (DESIGN 7, ADDENDUM H).

Steps: checkout (head, merge base and, when known, the prior head) -> patches (GitHub's,
local ``git diff`` for files GitHub sent without one) -> pre-pass plan -> anchor map ->
scope -> per-file diff files -> related definitions -> prompt. Large data stays in the
run directory; the ``Prepared`` payload carries paths and digests.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from mistralai import workflows

from morning_star_review.activities.common import (
    RELATED_JSON,
    activity_errors,
    blocking,
    check_payload_budget,
    progress,
    require_dir,
    write_changed_files,
)
from morning_star_review.core.contract import (
    ChangedFileRef,
    Prepared,
    PrepareInput,
    PrSnapshot,
    ReviewRequest,
)
from morning_star_review.core.errors import MsreviewGitError
from morning_star_review.core.guard import guard
from morning_star_review.core.jsonio import write_json_atomic, write_model, write_text_atomic
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.seal import seal
from morning_star_review.diff import prepass, related
from morning_star_review.diff.anchors import build_anchor_map
from morning_star_review.diff.incremental import ScopeDecision, decide_scope
from morning_star_review.diff.patch import FilePatch, local_patches, parse_patch
from morning_star_review.diff.prepass import nothing_to_review
from morning_star_review.diff.render import prompt_priors, render_prompt
from morning_star_review.github.fetch import load_pr_patches
from morning_star_review.gitops import (
    CheckoutSpec,
    GitAuth,
    checkout,
    diff_names,
    has_commit,
    head_first_lines,
)

__all__ = [
    "ANCHORS_JSON",
    "PROMPT_TXT",
    "REPO_DIR",
    "clone_url",
    "nothing_to_review",
    "prepare",
    "prepare_sync",
    "set_test_clone_url",
]

REPO_DIR = "repo"
ANCHORS_JSON = "anchors.json"
PROMPT_TXT = "prompt.txt"

_test_clone_url: Callable[[PrSnapshot], str] | None = None


def set_test_clone_url(fn: Callable[[PrSnapshot], str] | None) -> None:
    """Tests only: fetch from another URL (for example a local ``file://`` repository)."""
    global _test_clone_url
    _test_clone_url = fn


def clone_url(snap: PrSnapshot) -> str:
    """The base repository: fork heads are reachable there through ``refs/pull/<n>/head``."""
    if _test_clone_url is not None:
        return _test_clone_url(snap)
    return f"https://github.com/{snap.owner}/{snap.repo}.git"


@dataclass
class _Patches:
    github: dict[str, FilePatch]  # authoritative for anchors
    best: dict[str, FilePatch]  # GitHub's, else the local diff (for the prompt)


def _patches(snap: PrSnapshot, run_dir: Path, repo_dir: str) -> _Patches:
    texts = load_pr_patches(run_dir, snap.files_json_sha256)
    github = {
        f.path: parse_patch(f.path, texts[f.path], f.status, f.previous_path)
        for f in snap.files
        if f.path in texts
    }
    missing = [f.path for f in snap.files if f.path not in github and not f.binary]
    local: dict[str, FilePatch] = {}
    if missing:
        try:
            local = local_patches(repo_dir, snap.merge_base_sha, snap.head_sha, missing)
        except (MsreviewGitError, ValueError):
            # GitHub's patches stay authoritative; the rest show a "diff not available"
            # stand-in and remain readable through the file tools.
            local = {}
    return _Patches(github=github, best={**local, **github})


def _prior_changes(snap: PrSnapshot, repo_dir: str) -> tuple[bool, list[str] | None]:
    """(prior head fetchable, paths changed since it) for the scope decision."""
    prior = snap.prior
    if prior is None:
        return False, None
    try:
        if not has_commit(repo_dir, prior.head_sha):
            return False, None
        return True, diff_names(repo_dir, prior.head_sha, snap.head_sha)
    except (MsreviewGitError, ValueError):
        return False, None


def _incremental_plan(
    snap: PrSnapshot,
    repo_dir: str,
    scope: ScopeDecision,
    first: dict[str, str],
    base_first: dict[str, str],
) -> tuple[prepass.PromptPlan, dict[str, FilePatch]] | None:
    assert scope.since_sha is not None  # noqa: S101 - set for incremental scopes
    try:
        inc = local_patches(repo_dir, scope.since_sha, snap.head_sha, scope.diff_paths)
    except (MsreviewGitError, ValueError):
        return None
    files = prepass.incremental_files(list(snap.files), inc, scope.diff_paths)
    # Generated markers are corroborated against the merge base, never the prior head.
    plan = prepass.plan(files, inc, snap.config, first_lines=first, base_first_lines=base_first)
    return plan, inc


def _changed_refs(snap: PrSnapshot, full: prepass.PromptPlan) -> list[ChangedFileRef]:
    return [
        ChangedFileRef(
            path=f.path,
            previous_path=f.previous_path,
            status=f.status,
            diff_file=full.per_file_diff_files.get(f.path),
        )
        for f in snap.files
    ]


def prepare_sync(req: ReviewRequest, snap: PrSnapshot, ctx: RunContext) -> Prepared:
    """The blocking body of ``prepare`` (returns the payload unsealed)."""
    run_dir = require_dir(snap.run_dir)
    cfg = snap.config
    repo_dir = str(run_dir / REPO_DIR)
    token = ctx.github_token or ctx.read_token
    prior = snap.prior
    checkout(
        CheckoutSpec(
            repo_dir=repo_dir,
            clone_url=clone_url(snap),
            shas=[snap.head_sha, snap.merge_base_sha],
            head_sha=snap.head_sha,
            optional_shas=[prior.head_sha] if prior is not None else [],
        ),
        GitAuth(token) if token else None,
        lambda: None,  # the activity heartbeats from the event loop (see blocking())
    )
    patches = _patches(snap, run_dir, repo_dir)
    files = list(snap.files)
    first = head_first_lines(repo_dir, [f.path for f in files if f.status != "removed"])
    # A generated marker excludes a file only when its merge-base version had one too.
    base_first = prepass.base_first_lines(repo_dir, snap.merge_base_sha, files, first)
    full = prepass.plan(
        files,
        patches.best,
        cfg,
        first_lines=first,
        base_first_lines=base_first,
        total_files=snap.changed_files,
    )
    anchors_sha = write_model(run_dir / ANCHORS_JSON, build_anchor_map(files, patches.github))
    prepass.write_diff_files(full, run_dir)
    write_changed_files(run_dir, _changed_refs(snap, full))

    fetchable, changed_since = _prior_changes(snap, repo_dir)
    scope = decide_scope(
        prior,
        snap.compare_from_prior,
        snap.prior_to_head_merges,
        cfg,
        changed_since,
        full.reviewable,
        req.force,
        prior_fetchable=fetchable,
    )
    if not full.reviewable and scope.scope == "carry_over":
        # Nothing in the pull request is reviewable, so the verdict cannot depend on an
        # earlier review (which may have been incomplete): decide it afresh.
        scope = ScopeDecision("full", None, [], nothing_to_review(full.excluded))
    prompt_plan, prompt_patches = full, patches.best
    if scope.scope == "incremental":
        inc = _incremental_plan(snap, repo_dir, scope, first, base_first)
        if inc is None:
            scope = ScopeDecision("full", None, [], "the diff since the last review failed")
        else:
            prompt_plan, prompt_patches = inc

    # No model runs for a carry-over or when no file is reviewable (all of them lock
    # files, generated, binary, ...): the workflow then skips explore, merge and verify.
    no_models = scope.scope == "carry_over" or not prompt_plan.reviewable
    boundary = secrets.token_hex(4)
    defs: list[related.RelatedDefinition] = []
    if cfg.related_definitions and not no_models:
        ranked = [prompt_patches[p] for p in prompt_plan.ranked if p in prompt_patches]
        defs = related.collect(repo_dir, ranked, {f.path for f in files})
    write_json_atomic(run_dir / RELATED_JSON, related.dump_related(defs))

    priors = list(prior.findings) if prior is not None else []
    if no_models:
        prompt = ""  # no model call: nothing (more) that needs a review
    else:
        prompt = render_prompt(
            snap, prompt_plan, scope, priors, cfg, boundary, cfg.budgets, related=defs
        )
    prompt_sha = write_text_atomic(run_dir / PROMPT_TXT, prompt)
    progress(
        f"msreview: scope={scope.scope} inlined={len(prompt_plan.inlined)} "
        f"on_demand={len(prompt_plan.on_demand)} excluded={len(full.excluded)} "
        f"related={len(defs)} prompt_chars={len(prompt)}"
    )
    if not prompt_plan.reviewable and scope.scope != "carry_over":
        progress(f"msreview: nothing to review: {nothing_to_review(prompt_plan.excluded)}")
    return Prepared(
        job_root=str(run_dir),
        repo_dir=repo_dir,
        scope=scope.scope,
        since_sha=scope.since_sha,
        prompt_sha256=prompt_sha,
        prompt_chars=len(prompt),
        inlined_paths=list(prompt_plan.inlined),
        on_demand_paths=list(prompt_plan.on_demand),
        excluded=list(full.excluded),
        reviewable_paths=list(prompt_plan.reviewable),
        anchors_sha256=anchors_sha,
        prior_for_prompt=[p.id for p in prompt_priors(priors)],
        partial_reason=(prompt_plan.partial_reason or "")[:500] or None,
        roles=cfg.roles,
        budgets=cfg.budgets,
        boundary=boundary,
    )


@workflows.activity(
    name="msreview_prepare",
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2.0,
)
async def prepare(inp: PrepareInput) -> Prepared:
    """Checkout, pre-pass, anchors, scope and the explorer prompt."""
    ctx = guard(inp.req, inp.snap)
    with activity_errors():
        prep = await blocking(lambda: prepare_sync(inp.req, inp.snap, ctx), {"stage": "prepare"})
        # Explore, verify and finalize carry both; keep their inputs under the limit.
        check_payload_budget(inp.snap, prep)
        return seal(prep, ctx)
