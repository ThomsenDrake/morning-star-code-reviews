"""Builds the pull request snapshot from GitHub (the ``fetch_pr`` activity body).

Reads only. Our earlier state is trusted only from comments authored by our bot;
anyone can paste the same markers into a PR.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from morning_star_review._version import __version__
from morning_star_review.core.config import (
    REPO_CONFIG_FILE,
    REPO_CONFIG_MAX_BYTES,
    ConfigError,
    RepoConfig,
    load_repo_config,
    resolve,
)
from morning_star_review.core.contract import (
    MAX_FILES,
    CompareStatus,
    PrFile,
    PriorFinding,
    PriorState,
    PrSnapshot,
    ResolvedConfig,
    ReviewRequest,
    SkipReason,
)
from morning_star_review.core.errors import MsreviewBadRequest
from morning_star_review.core.jsonio import read_verified, write_json_atomic
from morning_star_review.core.naming import parse_pr_url
from morning_star_review.core.runctx import RunContext
from morning_star_review.github import graphql
from morning_star_review.github.commands import Commands, collect_commands
from morning_star_review.github.http import GitHubGraphQLError, GitHubHttp
from morning_star_review.github.permissions import PermissionCache, can_dismiss
from morning_star_review.github.publish import report_reason
from morning_star_review.github.state import (
    decode_state,
    find_summary_comment,
    finding_ids_in,
    same_login,
)

__all__ = [
    "PR_FILES_JSON",
    "build_snapshot",
    "decide_skip",
    "load_pr_patches",
    "read_client",
]

log = logging.getLogger(__name__)

PR_FILES_JSON = "pr_files.json"
_BODY_LIMIT = 4000
_TITLE_LIMIT = 300
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_STATUSES = frozenset({"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"})
_COMPARE = frozenset({"ahead", "diverged", "behind", "identical"})
ConfigSource = Literal["base_file", "default", "invalid_fell_back"]


def read_client(ctx: RunContext) -> GitHubHttp:
    """Client for the reads of ``fetch_pr``: the App token, else the read-only token."""
    return GitHubHttp(ctx.github_token or ctx.read_token)


def _repo_path(owner: str, repo: str) -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _strip_html_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", text or "")


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pr_file(item: dict[str, Any]) -> tuple[PrFile, str | None]:
    patch = item.get("patch") if isinstance(item.get("patch"), str) else None
    status = str(item.get("status") or "modified")
    additions = int(item.get("additions") or 0)
    deletions = int(item.get("deletions") or 0)
    previous = item.get("previous_filename")
    # GitHub omits the patch for binary files and for very large diffs; only the
    # former report no line changes at all.
    binary = patch is None and additions == 0 and deletions == 0 and status != "renamed"
    pr_file = PrFile(
        path=str(item["filename"]),
        previous_path=str(previous) if isinstance(previous, str) and previous else None,
        status=cast(Any, status if status in _STATUSES else "changed"),
        additions=additions,
        deletions=deletions,
        has_patch=patch is not None,
        binary=binary,
    )
    return pr_file, patch


def _write_pr_files(run_dir: Path, files: list[tuple[PrFile, str | None]]) -> str:
    """Write ``pr_files.json``: a list of ``PrFile`` fields plus ``patch`` (or null)."""
    data = [{**f.model_dump(mode="json"), "patch": patch} for f, patch in files]
    return write_json_atomic(run_dir / PR_FILES_JSON, data)


def load_pr_patches(run_dir: str | Path, expected_sha256: str) -> dict[str, str]:
    """GitHub patches by path from ``pr_files.json`` (digest checked; no-patch files omitted)."""
    data = json.loads(read_verified(Path(run_dir) / PR_FILES_JSON, expected_sha256))
    return {
        str(item["path"]): str(item["patch"])
        for item in data
        if isinstance(item, dict) and isinstance(item.get("patch"), str)
    }


async def _load_config(
    gh: GitHubHttp, owner: str, repo: str, base_sha: str
) -> tuple[RepoConfig | None, ConfigSource, list[str]]:
    """The repository config file at the base commit (never from the PR head)."""
    data = await gh.get_json(
        f"{_repo_path(owner, repo)}/contents/{REPO_CONFIG_FILE}",
        {"ref": base_sha},
        allow_404=True,
    )
    if data is None:
        return load_repo_config(None)
    fallback = f"{REPO_CONFIG_FILE} is invalid and was ignored (defaults used)"
    if not isinstance(data, dict) or data.get("type") != "file":
        return None, "invalid_fell_back", [f"{fallback}: not a regular file"]
    if int(data.get("size") or 0) > REPO_CONFIG_MAX_BYTES or data.get("encoding") != "base64":
        return None, "invalid_fell_back", [f"{fallback}: larger than 64 KiB"]
    try:
        text = base64.b64decode(str(data.get("content") or ""), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None, "invalid_fell_back", [f"{fallback}: not UTF-8 text"]
    return load_repo_config(text)


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def decide_skip(
    req: ReviewRequest,
    *,
    head_sha: str,
    state: str,
    merged: bool,
    draft: bool,
    cfg: ResolvedConfig,
    prior: PriorState | None,
) -> SkipReason | None:
    """DESIGN 9.4 order: superseded, closed, draft, already reviewed.

    ``req.force`` bypasses all but ``superseded``. A refresh (``req.refresh``) only
    re-applies commands to the review of the current head, so it needs one: without
    it the refresh is skipped as ``not_reviewed`` (no model ever runs in a refresh).
    """
    if req.expected_head_sha and req.expected_head_sha != head_sha:
        return "superseded"
    if req.force:
        return None
    if state == "closed" or merged:
        return "closed"
    if req.refresh:
        return None if prior is not None and prior.head_sha == head_sha else "not_reviewed"
    if draft and not cfg.review_drafts:
        return "draft"
    if (
        prior is not None
        and prior.head_sha == head_sha
        and prior.complete
        and prior.config_hash == cfg.config_hash
        and _major(prior.tool_version) == _major(__version__)
    ):
        return "already_reviewed"
    return None


async def _prior_state(
    gh: GitHubHttp, owner: str, repo: str, number: int, bot_login: str | None
) -> tuple[PriorState | None, int | None, list[str]]:
    """Our previous state from the bot's summary comment, its id, and warnings."""
    if not bot_login:
        return None, None, []
    comments = await gh.paginate(f"{_repo_path(owner, repo)}/issues/{number}/comments")
    return _prior_from_comments(comments, bot_login)


def _prior_from_comments(
    comments: list[Any], bot_login: str | None
) -> tuple[PriorState | None, int | None, list[str]]:
    """``_prior_state`` from the PR's issue comments, already read."""
    if not bot_login:
        return None, None, []
    summary = find_summary_comment(comments, bot_login)
    if summary is None:
        return None, None, []
    state = decode_state(str(summary.get("body") or ""))
    if state is None:
        return (
            None,
            int(summary["id"]),
            ["the previous review state could not be read; this review starts from scratch"],
        )
    if state.summary_comment_id != int(summary["id"]):
        data = state.model_dump(mode="json")
        data["summary_comment_id"] = int(summary["id"])
        state = PriorState.model_validate(data)
    return state, int(summary["id"]), []


async def _live_priors(
    gh: GitHubHttp,
    snap_owner: str,
    snap_repo: str,
    number: int,
    prior: PriorState,
    bot_login: str,
    pr_author: str,
    cfg: ResolvedConfig,
    perms: PermissionCache | None = None,
) -> PriorState:
    """``prior`` with live thread data (resolution, resolver rights) from GraphQL."""
    threads = await graphql.list_review_threads(gh, snap_owner, snap_repo, number)
    by_id: dict[str, graphql.ReviewThread] = {}
    for t in threads:
        if t.started_by(bot_login):
            for fid in finding_ids_in(t.first_body):
                by_id.setdefault(fid, t)
    perms = perms or PermissionCache(gh, snap_owner, snap_repo)
    findings: list[PriorFinding] = []
    for f in prior.findings:
        thread = by_id.get(f.id)
        if thread is None:
            findings.append(f)
            continue
        resolver = thread.resolved_by if thread.is_resolved else None
        dismiss = False
        if resolver and not same_login(resolver, bot_login):
            permission = await perms.get(resolver)
            dismiss = can_dismiss(permission, resolver, pr_author, cfg.author_may_dismiss)
        data = f.model_dump(mode="json")
        data.update(
            thread_id=thread.id,
            comment_id=thread.first_comment_id or f.comment_id,
            thread_resolved=thread.is_resolved,
            resolved_by=resolver,
            resolver_can_dismiss=dismiss,
        )
        findings.append(PriorFinding.model_validate(data))
    data = prior.model_dump(mode="json")
    data["findings"] = [f.model_dump(mode="json") for f in findings]
    return PriorState.model_validate(data)


async def _compare_from_prior(
    gh: GitHubHttp, owner: str, repo: str, prior_head: str, head: str
) -> tuple[CompareStatus, bool]:
    """Status of ``prior_head...head`` and whether merge commits are in between.

    Unknown histories (prior head gone, truncated commit list) count as merges, so
    the review falls back to a full one.
    """
    if prior_head == head:
        return "identical", False
    data = await gh.get_json(
        f"{_repo_path(owner, repo)}/compare/{quote(prior_head, safe='')}...{quote(head, safe='')}",
        {"per_page": 100},
        allow_404=True,
    )
    if not isinstance(data, dict):
        return "unknown", True
    status = str(data.get("status") or "")
    raw_commits = data.get("commits")
    commits: list[Any] = raw_commits if isinstance(raw_commits, list) else []
    merges = any(len(c.get("parents") or []) > 1 for c in commits if isinstance(c, dict))
    if int(data.get("total_commits") or 0) > len(commits):
        merges = True
    return cast(CompareStatus, status if status in _COMPARE else "unknown"), merges


def _cannot_post_reason(req: ReviewRequest, ctx: RunContext) -> str | None:
    if req.post == "never":
        return report_reason("post_never")
    if ctx.cannot_post_reason:
        return ctx.cannot_post_reason[:500]
    if ctx.github_token is None:
        return report_reason("no_credentials")
    if not ctx.bot_login:
        # Without our bot login, earlier comments cannot be recognised and a retry
        # would post duplicates.
        return "Not posted: the GitHub App's bot login is unknown (app slug missing)."
    return None


async def build_snapshot(
    req: ReviewRequest, ctx: RunContext, gh: GitHubHttp, run_dir: Path
) -> PrSnapshot:
    """Read the PR, its files, config and our earlier state; decide skip and can_post.

    Writes ``run_dir/pr_files.json``. The result is unsealed (the activity seals it).
    """
    ref = parse_pr_url(req.pr_url)
    owner, repo, number = ref.owner, ref.repo, ref.number
    base = _repo_path(owner, repo)
    pr = await gh.get_json(f"{base}/pulls/{number}")
    head_sha = str(pr["head"]["sha"])
    base_sha = str(pr["base"]["sha"])
    head_repo_obj = pr["head"].get("repo") or {}
    pr_author = str((pr.get("user") or {}).get("login") or "")

    items = await gh.paginate(f"{base}/pulls/{number}/files", max_pages=MAX_FILES // 100)
    files = [_pr_file(i) for i in items if isinstance(i, dict) and i.get("filename")][:MAX_FILES]
    run_dir.mkdir(parents=True, exist_ok=True)
    files_sha = _write_pr_files(run_dir, files)

    compare = await gh.get_json(
        f"{base}/compare/{quote(base_sha, safe='')}...{quote(head_sha, safe='')}",
        {"per_page": 1},
    )
    merge_base_sha = str(((compare or {}).get("merge_base_commit") or {}).get("sha") or base_sha)

    repo_cfg, source, warnings = await _load_config(gh, owner, repo, base_sha)
    try:
        cfg, resolve_warnings = resolve(repo_cfg, req.overrides, req.policy)
    except ConfigError as exc:
        raise MsreviewBadRequest(str(exc)) from None
    warnings = [*warnings, *resolve_warnings]

    comments: list[Any] = []
    if ctx.bot_login:
        comments = await gh.paginate(f"{base}/issues/{number}/comments")
    prior, _summary_id, prior_warnings = _prior_from_comments(comments, ctx.bot_login)
    warnings.extend(prior_warnings)
    perms = PermissionCache(gh, owner, repo)
    if prior is not None and ctx.bot_login and gh.has_token:
        try:
            prior = await _live_priors(
                gh, owner, repo, number, prior, ctx.bot_login, pr_author, cfg, perms
            )
        except GitHubGraphQLError as exc:
            log.warning("review threads unavailable: %s", exc)
            warnings.append("review threads could not be read; earlier findings kept as recorded")
    commands = Commands()
    if prior is not None and gh.has_token:
        # Commands only act on the findings of an earlier review.
        commands = await collect_commands(
            comments,
            bot_login=ctx.bot_login,
            pr_author=pr_author,
            author_may_dismiss=cfg.author_may_dismiss,
            permission=perms.get,
        )

    compare_from_prior: CompareStatus | None = None
    merges = False
    if prior is not None:
        compare_from_prior, merges = await _compare_from_prior(
            gh, owner, repo, prior.head_sha, head_sha
        )

    state: Literal["open", "closed"] = "closed" if pr.get("state") == "closed" else "open"
    merged = bool(pr.get("merged"))
    draft = bool(pr.get("draft"))
    skip = decide_skip(
        req, head_sha=head_sha, state=state, merged=merged, draft=draft, cfg=cfg, prior=prior
    )
    reason = _cannot_post_reason(req, ctx)
    return PrSnapshot(
        owner=owner,
        repo=repo,
        number=number,
        html_url=str(pr.get("html_url") or ref.canonical_url)[:300],
        title=_cap(str(pr.get("title") or ""), _TITLE_LIMIT),
        body_excerpt=_cap(_strip_html_comments(str(pr.get("body") or "")).strip(), _BODY_LIMIT),
        author=pr_author[:100],
        author_association=str(pr.get("author_association") or "NONE")[:50],
        draft=draft,
        state=state,
        merged=merged,
        head_sha=head_sha,
        head_repo=str(head_repo_obj.get("full_name") or "")[:200],
        base_ref=str(pr["base"].get("ref") or "")[:300],
        base_sha=base_sha,
        merge_base_sha=merge_base_sha,
        additions=int(pr.get("additions") or 0),
        deletions=int(pr.get("deletions") or 0),
        changed_files=int(pr.get("changed_files") or len(files)),
        files=[f for f, _ in files],
        files_json_sha256=files_sha,
        can_post=reason is None,
        cannot_post_reason=reason,
        bot_login=ctx.bot_login,
        prior=prior,
        compare_from_prior=compare_from_prior,
        prior_to_head_merges=merges,
        config=cfg,
        config_source=source,
        config_warnings=[w[:500] for w in warnings][:20],
        skip=skip,
        run_dir=str(run_dir),
        dismiss_commands=commands.dismissals,
        ignored_commands=[n[:500] for n in commands.ignored][:20],
    )
