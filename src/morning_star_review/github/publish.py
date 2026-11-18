"""Idempotent publishing of reviews, summaries and labels (DESIGN 9.4, 9.6; ADDENDUM C).

Every write can be found again through a marker on a bot-authored comment, so a
``publish`` retried after a crash completes the work without duplicating it. The
summary comment, which carries the state, is written after the inline comments
and replies, so the state only claims what was actually posted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from morning_star_review.core.contract import (
    FinalFinding,
    FinalReview,
    PriorFinding,
    PriorState,
    PrSnapshot,
    ResolvedPrior,
    ReviewOutcome,
    ReviewRequest,
    Stage,
)
from morning_star_review.core.jsonio import write_json_atomic, write_text_atomic
from morning_star_review.core.runctx import RunContext
from morning_star_review.core.sanitize import code_span, sanitize_inline, strip_markers
from morning_star_review.github import graphql
from morning_star_review.github.http import GitHubGraphQLError, GitHubHttp, GitHubLasting
from morning_star_review.github.labels import set_verdict_label
from morning_star_review.github.state import (
    SUMMARY_MARKER,
    encode_state,
    failure_marker,
    find_summary_comment,
    finding_ids_in,
    finding_marker,
    is_bot_comment,
    is_summary_body,
    reply_ids_in,
    reply_marker,
)

__all__ = [
    "MAX_COMMENT_CHARS",
    "REPORT_REASONS",
    "SKIP_REASONS",
    "InlineRenderer",
    "fresh_write_client",
    "publish",
    "report_outcome",
    "report_reason",
    "skipped_outcome",
    "write_client",
    "write_failure_header",
    "write_report_files",
]

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 65_000  # GitHub's limit is 65,536
_REPORT_LIMIT = 262_144

REPORT_REASONS: dict[str, str] = {
    "not_installed": (
        "Not posted: the GitHub App '{slug}' is not installed on {owner}/{repo} "
        "(GET /repos/{owner}/{repo}/installation returned 404). Install it there, "
        "or use this report."
    ),
    "no_credentials": (
        "Not posted: no GitHub App credentials were configured (client id and private key)."
    ),
    "token_step_failed": (
        "Not posted: the GitHub App token step did not succeed ({outcome}). Check the "
        "client id, the private key secret, and that the App is installed on this repository."
    ),
    "insufficient": (
        "Not posted: the App installation on {owner}/{repo} lacks Pull requests: write."
    ),
    "post_never": "Not posted: posting was disabled (post: never).",
    "token_expired": (
        "Not posted: the GitHub App token expired before the review was ready and a new "
        "one could not be minted."
    ),
    "bot_mismatch": (
        "Not posted: the publish step's GitHub App ({bot}) is not the App this review "
        "was prepared for ({expected})."
    ),
}

SKIP_REASONS: dict[str, str] = {
    "closed": "Skipped: the pull request is closed.",
    "draft": "Skipped: the pull request is a draft (enable review.drafts or use --force).",
    "superseded": "Skipped: the pull request has a newer head commit than this run expected.",
    "already_reviewed": (
        "Skipped: this head commit was already reviewed with the same configuration."
    ),
}

_SUPERSEDED_DURING_PUBLISH = (
    "Not posted: the pull request received new commits while this review ran; "
    "the review of the older commit is in this report."
)

InlineRenderer = Callable[[FinalFinding, PrSnapshot], str]


def report_reason(kind: str, **fields: str) -> str:
    """The exact report-mode reason text for ``kind`` (see ``REPORT_REASONS``)."""
    return REPORT_REASONS[kind].format(**fields)


def write_client(ctx: RunContext) -> GitHubHttp | None:
    """Client for writes: the App installation token only (never the read token)."""
    token = ctx.current_github_token()
    return GitHubHttp(token) if token else None


# Re-mint when less than this is left of the token's one-hour life: publishing (with
# GitHub rate-limit waits) must finish before it expires.
TOKEN_MIN_VALID_S = 900.0


async def fresh_write_client(ctx: RunContext) -> GitHubHttp | None:
    """``write_client`` after re-minting a token that is close to its expiry (CLI).

    Returns None when the token can no longer be used (expired and the re-mint
    failed), so the caller reports instead of posting with a dead token.
    """
    box = ctx.token_box
    if box is not None and not await box.ensure_fresh(TOKEN_MIN_VALID_S):
        log.warning("the GitHub App token expired and could not be re-minted")
        return None
    return write_client(ctx)


def _repo_path(snap: PrSnapshot) -> str:
    return f"/repos/{quote(snap.owner, safe='')}/{quote(snap.repo, safe='')}"


def _execution_id(snap: PrSnapshot) -> str:
    # The run directory is <run_root>/<execution_id>.
    return Path(snap.run_dir).name


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def skipped_outcome(req: ReviewRequest, snap: PrSnapshot, execution_id: str) -> ReviewOutcome:
    """Outcome for a skipped snapshot (no review was produced, nothing is written)."""
    skip = snap.skip or "closed"
    return ReviewOutcome(
        status="superseded" if skip == "superseded" else "skipped",
        reason=SKIP_REASONS[skip],
        verdict=snap.prior.verdict if skip == "already_reviewed" and snap.prior else None,
        pr_url=req.pr_url,
        head_sha=snap.head_sha,
        execution_id=execution_id,
    )


def _banner(reason: str) -> str:
    return f"> **{reason}**\n\n"


def report_outcome(
    req: ReviewRequest,
    snap: PrSnapshot,
    final: FinalReview,
    reason: str,
    *,
    execution_id: str | None = None,
    status: str = "report",
) -> ReviewOutcome:
    """Report-mode outcome: the "Not posted" banner plus the full report; no writes."""
    return ReviewOutcome.model_validate(
        {
            "status": status,
            "reason": _cap(reason, 1000),
            "verdict": final.verdict,
            "pr_url": req.pr_url,
            "head_sha": snap.head_sha,
            "execution_id": execution_id or _execution_id(snap),
            "findings": [f.model_dump(mode="json") for f in final.findings],
            "report_markdown": _cap(_banner(reason) + final.report_md, _REPORT_LIMIT),
            "usage": {k: u.model_dump(mode="json") for k, u in final.usage.items()},
            "cost_usd": final.cost_usd,
            "refuted": [r.model_dump(mode="json") for r in final.refuted],
            "verdict_reasons": list(final.verdict_reasons),
        }
    )


def _models(snap: PrSnapshot) -> dict[str, Any]:
    roles = snap.config.roles
    return {
        "explorers": [r.model for r in roles.explorers],
        "verifier": roles.verifier.model if roles.verifier else None,
        "formatter": roles.formatter_model,
    }


def write_report_files(
    run_dir: str | Path, snap: PrSnapshot, final: FinalReview | None, outcome: ReviewOutcome
) -> Path:
    """Write ``report/report.md``, ``report/findings.json`` and ``report/run.json``."""
    out = Path(run_dir) / "report"
    write_text_atomic(
        out / "report.md", outcome.report_markdown or (final.report_md if final else "")
    )
    write_json_atomic(
        out / "findings.json",
        {
            "pr_url": outcome.pr_url,
            "head_sha": outcome.head_sha,
            "verdict": outcome.verdict,
            "execution_id": outcome.execution_id,
            "models": _models(snap),
            "findings": [f.model_dump(mode="json") for f in outcome.findings],
            "refuted": [r.model_dump(mode="json") for r in outcome.refuted],
        },
    )
    write_json_atomic(
        out / "run.json",
        {
            "status": outcome.status,
            "reason": outcome.reason,
            "execution_id": outcome.execution_id,
            "pr_url": outcome.pr_url,
            "head_sha": outcome.head_sha,
            "verdict": outcome.verdict,
            "verdict_reasons": list(final.verdict_reasons) if final else [],
            "possible_issues": outcome.possible_issues,
            "coverage": dict(final.coverage) if final else {},
            "usage": {k: u.model_dump(mode="json") for k, u in outcome.usage.items()},
            "cost_usd": outcome.cost_usd,
            "notes": list(outcome.notes),
            "config_hash": snap.config.config_hash,
            "models": _models(snap),
        },
    )
    return out


# ----------------------------------------------------------------------------- bodies


def default_inline_renderer(finding: FinalFinding, snap: PrSnapshot) -> str:
    """The inline comment body from ``review.render.inline_body`` (WP5)."""
    from morning_star_review.review import render  # lazy: keeps github/ testable alone

    model = ", ".join(finding.votes) or snap.config.roles.explorer_model
    body: str = render.inline_body(finding, model, snap.head_sha[:7])
    return body


def with_finding_marker(body: str, finding_id: str) -> str:
    """``body`` ending with the finding marker (added when the renderer left it out)."""
    marker = finding_marker(finding_id)
    text = body.rstrip()
    if finding_id in finding_ids_in(text):
        return _cap(text, MAX_COMMENT_CHARS)
    return _cap(text, MAX_COMMENT_CHARS - len(marker) - 1) + "\n" + marker


def summary_body(summary_md: str, unplaced: list[FinalFinding], state: PriorState) -> str:
    """Summary comment: marker, rendered summary, unplaced findings, then the state."""
    state_marker = encode_state(state)
    text = summary_md.strip()
    if not is_summary_body(text):
        text = f"{SUMMARY_MARKER}\n{text}"
    if unplaced:
        lines = ["", "", "#### Could not be placed on the diff"]
        for f in unplaced:
            where = f"{f.path}:{f.line}" if f.line else f.path
            title = sanitize_inline(f.title, 200)
            lines.append(f"- **{f.severity}** {title} ({sanitize_inline(where, 300)})")
        text += "\n".join(lines)
    limit = MAX_COMMENT_CHARS - len(state_marker) - 1
    if len(text) > limit:
        cut = text[: limit - 40].rsplit("\n", 1)[0]
        text = cut + "\n\n*(summary truncated)*"
    return f"{text}\n{state_marker}"


def reply_body(finding_id: str, action: str, head_sha: str) -> str:
    if action == "file_deleted":
        text = "File removed."
    else:
        text = f"Looks addressed in `{head_sha[:7]}`: the flagged code changed."
    return f"{text}\n{reply_marker(finding_id)}"


RESOLVED_HEADING = "#### Resolved since the last review"
UNCLOSED_SUFFIX = "; the thread could not be closed automatically"
UNCLOSED_NOTE = (
    "<sub>GitHub did not let this app resolve the threads marked above, so it replied to "
    "each and hid the comment as resolved instead. You can resolve them by hand.</sub>"
)
_FOOTER_START = "<sub>Automated review"


def _resolved_line(rp: ResolvedPrior) -> str:
    """The line ``review.render`` writes for ``rp`` under ``RESOLVED_HEADING``."""
    why = "file deleted" if rp.action == "file_deleted" else "code changed"
    return f"- {sanitize_inline(rp.title, 200)} ({code_span(strip_markers(rp.path))}): {why}"


def with_unclosed_notes(summary_md: str, unclosed: Sequence[ResolvedPrior]) -> str:
    """``summary_md`` noting, under "Resolved since the last review", each fixed prior
    whose thread could not be resolved (closed by reply + minimize instead).

    The rendered line of each prior gets ``UNCLOSED_SUFFIX``; a prior without a line
    (a shortened summary) gets one; ``UNCLOSED_NOTE`` closes the section.
    """
    if not unclosed:
        return summary_md
    lines = summary_md.split("\n")
    missing: list[str] = []
    for rp in unclosed:
        want = _resolved_line(rp)
        for i, line in enumerate(lines):
            if line == want:
                lines[i] = want + UNCLOSED_SUFFIX
                break
        else:
            missing.append(want + UNCLOSED_SUFFIX)
    addition = [*missing, "", UNCLOSED_NOTE]
    if RESOLVED_HEADING in lines:
        end = lines.index(RESOLVED_HEADING) + 2
        while end < len(lines) and lines[end].strip():
            end += 1
        lines[end:end] = addition
        return "\n".join(lines)
    section = [RESOLVED_HEADING, "", *addition, ""]
    footer = next((i for i, ln in enumerate(lines) if ln.startswith(_FOOTER_START)), None)
    if footer is None:
        return "\n".join([*lines, "", *section]).rstrip()
    lines[footer:footer] = section
    return "\n".join(lines)


def _comment_payload(finding: FinalFinding, body: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": finding.path,
        "line": finding.line,
        "side": "RIGHT",
        "body": with_finding_marker(body, finding.id),
    }
    if finding.start_line is not None and finding.line and finding.start_line < finding.line:
        payload["start_line"] = finding.start_line
        payload["start_side"] = "RIGHT"
    return payload


def posted_inline(comments: list[Any], bot_login: str | None) -> dict[str, dict[str, Any]]:
    """Our top-level inline comments by finding id (bot author and finding marker)."""
    out: dict[str, dict[str, Any]] = {}
    for c in comments:
        if is_bot_comment(c, bot_login) and c.get("in_reply_to_id") is None:
            for fid in finding_ids_in(str(c.get("body") or "")):
                out.setdefault(fid, c)
    return out


def replied_ids(comments: list[Any], bot_login: str | None) -> set[str]:
    """Finding ids we already answered with a "looks addressed" reply."""
    out: set[str] = set()
    for c in comments:
        if is_bot_comment(c, bot_login) and c.get("in_reply_to_id") is not None:
            out.update(reply_ids_in(str(c.get("body") or "")))
    return out


_MAX_STATE_FINDINGS = 60  # PriorState.findings holds at most 60 entries
_LIVE_FIELDS = {"thread_resolved": False, "resolved_by": None, "resolver_can_dismiss": False}


def _updated_state(
    state: PriorState,
    comment_ids: dict[str, int],
    unplaced: set[str],
    fixed: dict[str, PriorFinding],
    refused: list[str],
) -> PriorState:
    """``state`` with posted comment ids, unplaced findings and fallback-closed priors.

    Priors in ``fixed`` (closed by reply + minimize because the thread could not be
    resolved) are recorded with state ``fixed``, so a later run never touches them
    again. Open findings keep priority for the 60 entries the state holds.
    """
    findings: list[dict[str, Any]] = []
    for f in state.findings:
        data = f.model_dump(mode="json")
        if f.id in comment_ids:
            data["comment_id"] = comment_ids[f.id]
        if f.id in unplaced:
            data.update(state="summary_only", comment_id=None)
        findings.append(data)
    listed = {d["id"]: d for d in findings}
    for fid, pf in fixed.items():
        if fid in listed:
            if listed[fid]["state"] != "open":
                listed[fid].update(state="fixed", comment_id=pf.comment_id, thread_id=pf.thread_id)
        elif len(findings) < _MAX_STATE_FINDINGS:
            findings.append({**pf.model_dump(mode="json"), **_LIVE_FIELDS, "state": "fixed"})
    data = state.model_dump(mode="json")
    # summary_comment_id stays as finalize set it: a new summary's id is only known
    # after it is posted, and fetch finds the summary by its marker anyway.
    data.update(findings=findings, refused_resolves=list(dict.fromkeys(refused))[-60:])
    return PriorState.model_validate(data)


# ----------------------------------------------------------------------------- steps


REPLY_MUTATION = """
mutation($reviewId: ID!, $threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewId: $reviewId, pullRequestReviewThreadId: $threadId, body: $body}
  ) { comment { id } }
}
"""


def _review_request(
    snap: PrSnapshot, comments: list[dict[str, Any]], *, pending: bool
) -> dict[str, Any]:
    # Never APPROVE or REQUEST_CHANGES (v1). An empty body is accepted with comments.
    # Without an event GitHub creates the review PENDING; it is submitted later.
    body: dict[str, Any] = {"commit_id": snap.head_sha, "body": "", "comments": comments}
    if not pending:
        body["event"] = "COMMENT"
    return body


async def _drop_pending_reviews(gh: GitHubHttp, snap: PrSnapshot) -> None:
    """Delete our PENDING reviews (left by a run that stopped before submitting).

    Nothing in a pending review is visible to anyone else, and GitHub allows one
    pending review per user and PR, so a leftover one would block this run's review.
    """
    path = f"{_repo_path(snap)}/pulls/{snap.number}/reviews"
    for review in await gh.paginate(path):
        if review.get("state") == "PENDING" and is_bot_comment(review, snap.bot_login):
            log.warning("deleting a pending review left by an earlier attempt")
            await gh.delete(f"{path}/{int(review['id'])}")


async def _post_singly(
    gh: GitHubHttp, snap: PrSnapshot, payloads: list[tuple[FinalFinding, dict[str, Any]]]
) -> tuple[str | None, list[FinalFinding]]:
    """After a 422 on the batch: one COMMENT review per inline comment, skipping any
    that a crashed earlier attempt already posted; returns (url, the ones GitHub
    still rejects)."""
    path = f"{_repo_path(snap)}/pulls/{snap.number}"
    already = posted_inline(await gh.paginate(f"{path}/comments"), snap.bot_login)
    url: str | None = None
    unplaced: list[FinalFinding] = []
    for finding, payload in payloads:
        if finding.id in already:
            continue
        try:
            data = await gh.post_json(
                f"{path}/reviews", _review_request(snap, [payload], pending=False)
            )
            url = url or (data or {}).get("html_url")
        except GitHubLasting as exc:
            if exc.status != 422:
                raise
            unplaced.append(finding)
    return url, unplaced


@dataclass
class _FixedPrior:
    """A prior whose code changed or whose file was deleted, with an open thread."""

    resolved: ResolvedPrior
    finding: PriorFinding
    # An earlier run already replied and minimized (its thread is in refused_resolves).
    done_before: bool = False


def _fixed_priors(snap: PrSnapshot, final: FinalReview, comments: list[Any]) -> list[_FixedPrior]:
    """The fixed priors whose threads this run closes (or an earlier run closed).

    A prior whose inline comment is gone (deleted) has nothing left to close.
    """
    prior = snap.prior
    if prior is None:
        return []
    by_id = {f.id: f for f in prior.findings}
    earlier_refused = set(prior.refused_resolves)
    present = {int(c["id"]) for c in comments if isinstance(c, dict) and c.get("id")}
    out: list[_FixedPrior] = []
    for rp in final.priors:
        pf = by_id.get(rp.id)
        if rp.action not in ("resolve", "file_deleted") or pf is None or pf.comment_id is None:
            continue
        if pf.state == "fixed" or pf.thread_resolved:
            continue
        if pf.thread_id and pf.thread_id in earlier_refused:
            out.append(_FixedPrior(rp, pf, done_before=True))
        elif pf.comment_id in present:
            out.append(_FixedPrior(rp, pf))
    return out


@dataclass
class _Posted:
    """What ``_post_review`` wrote."""

    review_url: str | None = None  # the review holding this run's new inline comments
    unplaced: list[FinalFinding] = field(default_factory=list)
    gone: set[str] = field(default_factory=set)  # priors whose thread no longer exists
    notes: list[str] = field(default_factory=list)


async def _post_review(
    gh: GitHubHttp,
    snap: PrSnapshot,
    payloads: list[tuple[FinalFinding, dict[str, Any]]],
    replies: list[_FixedPrior],
) -> _Posted:
    """Post the new inline comments and the "looks addressed" replies as one review.

    Without replies the review is created and submitted in one request (DESIGN 9.4
    step 5). With replies it is created PENDING, each reply is added to it with
    GraphQL ``addPullRequestReviewThreadReply`` and it is submitted once, so a run
    adds one review however many threads it answers. If GitHub rejects the batch of
    inline comments (422), they are posted one review each and the replies share one
    more review. A reply GitHub cannot place in a review (a prior without a thread
    id) falls back to the REST reply endpoint, which makes a review of its own.
    """
    out = _Posted()
    reviews = f"{_repo_path(snap)}/pulls/{snap.number}/reviews"
    threaded = [r for r in replies if r.finding.thread_id]
    review: dict[str, Any] | None = None
    if payloads:
        try:
            created = await gh.post_json(
                reviews,
                _review_request(snap, [p for _, p in payloads], pending=bool(threaded)),
            )
            review = created if isinstance(created, dict) else {}
        except GitHubLasting as exc:
            if exc.status != 422:
                raise
            log.warning("review rejected (%s); posting comments one by one", exc.github_message)
            out.review_url, out.unplaced = await _post_singly(gh, snap, payloads)
    has_inline = review is not None
    if threaded:
        if review is None:
            created = await gh.post_json(reviews, {"commit_id": snap.head_sha})
            review = created if isinstance(created, dict) else {}
        url = await _reply_and_submit(gh, snap, reviews, review, threaded, has_inline, out)
        if has_inline:
            out.review_url = url
    elif review is not None:
        out.review_url = review.get("html_url")
    for fp in replies:
        if not fp.finding.thread_id:
            await _rest_reply(gh, snap, fp, out)
    return out


async def _reply_and_submit(
    gh: GitHubHttp,
    snap: PrSnapshot,
    reviews: str,
    review: dict[str, Any],
    replies: list[_FixedPrior],
    has_inline: bool,
    out: _Posted,
) -> str | None:
    """Add ``replies`` to the pending ``review`` and submit it once; returns its url.

    A thread that no longer exists is recorded in ``out.gone``; any other refused
    reply becomes a note (the prior is still closed). A review left with nothing in
    it is deleted instead of submitted. On an error the pending review is deleted
    (best effort) so it cannot block the retry.
    """
    review_id = int(review["id"])
    node_id = str(review.get("node_id") or "")
    try:
        added = 0
        for fp in replies:
            rp = fp.resolved
            variables = {
                "reviewId": node_id,
                "threadId": fp.finding.thread_id,
                "body": reply_body(rp.id, rp.action, snap.head_sha),
            }
            try:
                await gh.graphql(REPLY_MUTATION, variables)
                added += 1
            except GitHubGraphQLError as exc:
                if "NOT_FOUND" in exc.types:
                    out.gone.add(rp.id)  # the thread was deleted: nothing left to close
                else:
                    out.notes.append(f"could not reply on fixed finding {rp.id} ({exc})"[:300])
        if not (has_inline or added):
            await gh.delete(f"{reviews}/{review_id}")
            return None
        data = await gh.post_json(f"{reviews}/{review_id}/events", {"event": "COMMENT"})
        return (data or {}).get("html_url") if isinstance(data, dict) else None
    except Exception:
        try:
            await gh.delete(f"{reviews}/{review_id}")
        except Exception as cleanup:  # already submitted, or GitHub unreachable
            log.warning("could not delete the pending review: %s", type(cleanup).__name__)
        raise


async def _rest_reply(gh: GitHubHttp, snap: PrSnapshot, fp: _FixedPrior, out: _Posted) -> None:
    rp = fp.resolved
    path = f"{_repo_path(snap)}/pulls/{snap.number}/comments/{fp.finding.comment_id}/replies"
    try:
        await gh.post_json(path, {"body": reply_body(rp.id, rp.action, snap.head_sha)})
    except GitHubLasting as exc:
        if exc.status != 404:
            raise
        out.gone.add(rp.id)  # our comment was deleted; nothing left to close


@dataclass
class _Closed:
    """What ``_close_priors`` did to the threads of fixed priors."""

    # Priors closed without resolving the thread (reply + minimize), recorded as
    # ``fixed`` in the state so later runs leave them alone.
    fallback: dict[str, PriorFinding] = field(default_factory=dict)
    # Their summary lines get the "could not be closed automatically" note.
    unclosed: list[ResolvedPrior] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # thread ids GitHub refused to resolve
    notes: list[str] = field(default_factory=list)


async def _close_priors(
    gh: GitHubHttp, fixed: list[_FixedPrior], comments: list[Any], gone: set[str]
) -> _Closed:
    """Resolve the threads of fixed priors, after the replies (DESIGN 9.4 step 6).

    GitHub refuses ``resolveReviewThread`` unless the token has Contents: write
    ("Resource not accessible by integration"). Then the fallback runs once: the
    "looks addressed" reply (already posted in this run's review, and found again by
    its marker) and our comment minimized as RESOLVED. The prior is recorded as
    ``fixed`` (and its thread id in ``refused_resolves``), so later runs neither reply
    again nor retry the resolve or the minimize.
    """
    out = _Closed()
    node_ids = {int(c["id"]): str(c.get("node_id") or "") for c in comments if c.get("id")}
    for fp in fixed:
        rp, pf = fp.resolved, fp.finding
        if rp.id in gone:
            continue
        if fp.done_before:
            # An earlier run already replied and minimized: nothing more to write.
            out.fallback[rp.id] = pf
            out.unclosed.append(rp)
            continue
        if pf.thread_id:
            try:
                if await graphql.resolve_thread(gh, pf.thread_id):
                    continue
                out.refused.append(pf.thread_id)
            except GitHubGraphQLError as exc:
                out.notes.append(f"could not resolve a thread ({exc})"[:300])
        node_id = node_ids.get(pf.comment_id or 0, "")
        try:
            if not (node_id and await graphql.minimize_comment(gh, node_id, "RESOLVED")):
                out.notes.append(f"could not minimize fixed finding {rp.id}")
        except GitHubGraphQLError as exc:
            out.notes.append(f"could not minimize fixed finding {rp.id} ({exc})"[:300])
        out.fallback[rp.id] = pf
        out.unclosed.append(rp)
    if out.unclosed:
        out.notes.append(
            f"{len(out.unclosed)} thread(s) of fixed findings could not be resolved "
            "automatically; replied and hid the comment as resolved instead"
        )
    if out.refused:
        out.notes.append(
            "GitHub refused resolveReviewThread: the token needs Contents: write "
            "(Pull requests: write alone is not enough)"
        )
    return out


async def publish(
    req: ReviewRequest,
    snap: PrSnapshot,
    final: FinalReview | None,
    ctx: RunContext,
    gh: GitHubHttp | None,
    *,
    render_inline: InlineRenderer | None = None,
    no_post_reason: str | None = None,
) -> ReviewOutcome:
    """Post the review (idempotent) or return a report outcome (DESIGN 9.4 / 9.6).

    ``gh`` must carry the App installation token when ``snap.can_post``.
    ``no_post_reason`` is the report reason when ``gh`` is None although the snapshot
    could post (for example an expired token).
    """
    execution_id = ctx.execution_id
    if snap.skip is not None:
        return skipped_outcome(req, snap, execution_id)
    if final is None:
        raise ValueError("final review missing for a snapshot that is not skipped")
    if not snap.can_post or gh is None or not gh.has_token or not snap.bot_login:
        reason = (
            snap.cannot_post_reason
            or (no_post_reason if snap.can_post else None)
            or report_reason("no_credentials")
        )
        outcome = report_outcome(req, snap, final, reason, execution_id=execution_id)
        write_report_files(snap.run_dir, snap, final, outcome)
        return outcome

    base = _repo_path(snap)
    pr = await gh.get_json(f"{base}/pulls/{snap.number}")
    if str(((pr or {}).get("head") or {}).get("sha") or "") != snap.head_sha:
        outcome = report_outcome(
            req,
            snap,
            final,
            _SUPERSEDED_DURING_PUBLISH,
            execution_id=execution_id,
            status="superseded",
        )
        write_report_files(snap.run_dir, snap, final, outcome)
        return outcome

    render = render_inline or default_inline_renderer
    comments_path = f"{base}/pulls/{snap.number}/comments"
    await _drop_pending_reviews(gh, snap)
    comments = await gh.paginate(comments_path)
    posted = posted_inline(comments, snap.bot_login)
    new = [
        f
        for f in final.findings
        if f.state == "open"
        and f.anchor in ("inline", "snapped")
        and f.line is not None
        and not f.carried
        and f.id not in posted
    ][: snap.config.max_inline_comments]
    fixed = _fixed_priors(snap, final, comments)
    replied = replied_ids(comments, snap.bot_login)
    replies = [fp for fp in fixed if not fp.done_before and fp.resolved.id not in replied]
    payloads = [(f, _comment_payload(f, render(f, snap))) for f in new]
    sent = await _post_review(gh, snap, payloads, replies)
    review_url, unplaced = sent.review_url, sent.unplaced

    comments = await gh.paginate(comments_path)
    posted = posted_inline(comments, snap.bot_login)
    closed = await _close_priors(gh, fixed, comments, sent.gone)
    notes = [*sent.notes, *closed.notes]

    issue_comments = await gh.paginate(f"{base}/issues/{snap.number}/comments")
    existing = find_summary_comment(issue_comments, snap.bot_login)
    prior_refused = list(snap.prior.refused_resolves) if snap.prior else []
    state = _updated_state(
        final.state,
        {fid: int(c["id"]) for fid, c in posted.items()},
        {f.id for f in unplaced},
        closed.fallback,
        [*prior_refused, *closed.refused],
    )
    body = summary_body(with_unclosed_notes(final.summary_md, closed.unclosed), unplaced, state)
    if existing is not None:
        summary_url = existing.get("html_url")
        if existing.get("body") != body:
            await gh.patch_json(f"{base}/issues/comments/{int(existing['id'])}", {"body": body})
    else:
        created = await gh.post_json(f"{base}/issues/{snap.number}/comments", {"body": body})
        summary_url = (created or {}).get("html_url")

    if snap.config.labels:
        notes.extend(await set_verdict_label(gh, snap.owner, snap.repo, snap.number, final.verdict))
    if unplaced:
        notes.append(f"{len(unplaced)} finding(s) could not be placed on the diff")

    unplaced_ids = {f.id for f in unplaced}
    findings = []
    for f in final.findings:
        data = f.model_dump(mode="json")
        if f.id in unplaced_ids:
            data["state"] = "summary_only"
        elif f.id in posted:
            data["comment_id"] = int(posted[f.id]["id"])
        findings.append(data)
    outcome = ReviewOutcome.model_validate(
        {
            "status": "posted",
            "verdict": final.verdict,
            "pr_url": req.pr_url,
            "head_sha": snap.head_sha,
            "execution_id": execution_id,
            "findings": findings,
            "report_markdown": final.report_md,
            "review_url": review_url,
            "summary_url": summary_url,
            "usage": {k: u.model_dump(mode="json") for k, u in final.usage.items()},
            "cost_usd": final.cost_usd,
            "refuted": [r.model_dump(mode="json") for r in final.refuted],
            "notes": [n[:300] for n in notes][:50],
            "verdict_reasons": list(final.verdict_reasons),
        }
    )
    write_report_files(snap.run_dir, snap, final, outcome)
    return outcome


# ----------------------------------------------------------------------------- failure

_FAILURE_LINE = re.compile(re.escape(failure_marker()) + r"\n> [^\n]*\n?")


def failure_banner(stage: Stage, head_sha: str) -> str:
    """Fixed failure text: only the stage name and the short sha are variable."""
    sha7 = re.sub(r"[^0-9a-f]", "", head_sha.lower())[:7]
    return (
        f"{failure_marker()}\n> **The latest review run did not finish (stage `{stage}`, "
        f"commit `{sha7}`).** What follows is from an earlier run and may be out of date.\n"
    )


def with_failure_banner(body: str, banner: str) -> str:
    """``body`` with ``banner`` right after the summary marker (replacing an old one)."""
    text = _FAILURE_LINE.sub("", body)
    first, sep, rest = text.partition("\n")
    if first.strip() != SUMMARY_MARKER:
        return f"{SUMMARY_MARKER}\n{banner}{text}"
    return f"{first}\n{banner}{rest if sep else ''}"


async def write_failure_header(
    req: ReviewRequest,
    snap: PrSnapshot | None,
    stage: Stage,
    ctx: RunContext,
    gh: GitHubHttp | None,
) -> None:
    """Mark our existing summary as stale and set the incomplete label. Never raises."""
    del req
    try:
        if snap is None or not snap.can_post or gh is None or not gh.has_token:
            return
        base = _repo_path(snap)
        comments = await gh.paginate(f"{base}/issues/{snap.number}/comments")
        existing = find_summary_comment(comments, snap.bot_login or ctx.bot_login)
        if existing is not None:
            old = str(existing.get("body") or "")
            new = with_failure_banner(old, failure_banner(stage, snap.head_sha))
            if new != old and len(new) <= MAX_COMMENT_CHARS:
                await gh.patch_json(f"{base}/issues/comments/{int(existing['id'])}", {"body": new})
        if snap.config.labels:
            await set_verdict_label(gh, snap.owner, snap.repo, snap.number, "incomplete")
    except Exception as exc:  # report_failure must never raise
        log.warning("could not mark the failed run on the PR: %s", type(exc).__name__)
