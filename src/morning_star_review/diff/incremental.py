"""Incremental review scope since the previous reviewed commit (DESIGN 7.6).

``decide_scope`` is pure: the caller gathers the facts (prior state, compare status,
merge commits, files changed since the prior head, whether the prior head could be
fetched) and this module turns them into ``full``, ``incremental`` or ``carry_over``
with a short human-readable reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from morning_star_review._version import __version__
from morning_star_review.core.contract import PriorState, ResolvedConfig, Scope

__all__ = ["MAX_INCREMENTAL_FILES", "ScopeDecision", "decide_scope", "major_version"]

MAX_INCREMENTAL_FILES = 300  # compare API file lists above this are treated as unknown


@dataclass
class ScopeDecision:
    scope: Scope
    since_sha: str | None
    diff_paths: list[str] = field(default_factory=list)
    reason: str = ""


def major_version(version: str) -> str:
    """Compatibility key of a version: the major number, or ``0.<minor>`` before 1.0."""
    parts = version.strip().lstrip("v").split(".")
    if not parts or not parts[0]:
        return ""
    if parts[0] == "0" and len(parts) > 1:
        return f"0.{parts[1]}"
    return parts[0]


def _full(reviewable: Sequence[str], reason: str) -> ScopeDecision:
    return ScopeDecision(scope="full", since_sha=None, diff_paths=list(reviewable), reason=reason)


def _blocker(
    prior: PriorState | None,
    compare_status: str | None,
    merges: bool,
    cfg: ResolvedConfig,
    changed_since_prior: Sequence[str] | None,
    prior_fetchable: bool,
    tool_version: str,
) -> str | None:
    """Why an incremental review is not possible, or ``None`` when it is."""
    if not cfg.incremental:
        return "incremental reviews are turned off"
    if prior is None:
        return "no previous review"
    if not prior.complete:
        return "the previous review was incomplete"
    if major_version(prior.tool_version) != major_version(tool_version):
        return f"the previous review used version {prior.tool_version}"
    if prior.config_hash != cfg.config_hash:
        return "the review configuration changed"
    if compare_status == "identical":
        return "the head is the commit already reviewed (a re-run reviews it in full)"
    if compare_status != "ahead":
        what = compare_status or "unknown"
        return f"the head is not a descendant of the reviewed commit ({what}; force push?)"
    if merges:
        return "merge commits since the previous review"
    if not prior_fetchable:
        return "the previously reviewed commit is no longer available"
    if changed_since_prior is None:
        return "the files changed since the previous review are unknown"
    if len(changed_since_prior) > MAX_INCREMENTAL_FILES:
        return f"more than {MAX_INCREMENTAL_FILES} files changed since the previous review"
    return None


def decide_scope(
    prior: PriorState | None,
    compare_status: str | None,
    merges: bool,
    cfg: ResolvedConfig,
    changed_since_prior: list[str] | None,
    reviewable: list[str],
    force: bool,
    *,
    prior_fetchable: bool = True,
    tool_version: str = __version__,
) -> ScopeDecision:
    """Choose the review scope (DESIGN 7.6).

    - ``force`` always gives ``full``.
    - ``incremental`` when every condition holds: the prompt diff is then the
      reviewable files that changed since the prior head (``diff_paths``).
    - ``carry_over`` when the conditions hold but no reviewable file changed.
    - ``full`` otherwise, with the first failed condition as the reason.
    """
    if force:
        return _full(reviewable, "a full review was requested")
    blocker = _blocker(
        prior, compare_status, merges, cfg, changed_since_prior, prior_fetchable, tool_version
    )
    if blocker is not None or prior is None or changed_since_prior is None:
        return _full(reviewable, blocker or "no previous review")
    sha7 = prior.head_sha[:7]
    changed = set(changed_since_prior)
    diff_paths = [p for p in reviewable if p in changed]
    if not diff_paths:
        return ScopeDecision(
            scope="carry_over",
            since_sha=prior.head_sha,
            diff_paths=[],
            reason=f"no reviewable changes since {sha7}",
        )
    return ScopeDecision(
        scope="incremental",
        since_sha=prior.head_sha,
        diff_paths=diff_paths,
        reason=f"{len(diff_paths)} reviewable files changed since {sha7}",
    )
