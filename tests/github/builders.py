"""Builders shared by the GitHub client tests (import as ``builders``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fakegh import BOT, HEAD, MERGE_BASE

from morning_star_review._version import __version__
from morning_star_review.core.config import resolve
from morning_star_review.core.contract import (
    FinalFinding,
    FinalReview,
    OperatorPolicy,
    PriorFinding,
    PriorState,
    PrSnapshot,
    ResolvedConfig,
    ResolvedPrior,
    ReviewRequest,
    RunOverrides,
    Verdict,
)
from morning_star_review.core.runctx import RunContext

PR_URL = "https://github.com/octo-org/widgets/pull/7"
EXEC_ID = "msreview-cli-octo-org-widgets-pr7-aaaaaaa-0123abcd"
SUMMARY_MD = "<!-- morning-star-review:summary v1 -->\n### Morning Star Code Review\nSummary."


def make_req(**kw: Any) -> ReviewRequest:
    data: dict[str, Any] = {
        "pr_url": PR_URL,
        "repo": "octo-org/widgets",
        "mode": "cli",
        "policy": OperatorPolicy(trust_repo_config_models=True),
        "run_label": "cli:test",
    }
    data.update(kw)
    return ReviewRequest(**data)


def make_cfg(**kw: Any) -> ResolvedConfig:
    cfg, _ = resolve(None, RunOverrides(), OperatorPolicy(trust_repo_config_models=True))
    if not kw:
        return cfg
    data = cfg.model_dump(mode="json")
    data.update(kw)
    data["config_hash"] = ""
    return ResolvedConfig.model_validate(data)


def make_ctx(tmp_path: Path, *, token: str | None = "ghs_testtoken123456", **kw: Any) -> RunContext:  # noqa: S107
    data: dict[str, Any] = {
        "execution_id": EXEC_ID,
        "request_sha256": "0" * 64,
        "seal_key": b"k" * 32,
        "run_root": str(tmp_path),
        "mode": "cli",
        "on_worker": False,
        "github_token": token,
        "read_token": None,
        "bot_login": BOT,
        "mistral_api_key": "test-mistral-key",
        "agent_python": sys.executable,
    }
    data.update(kw)
    return RunContext(**data)


def make_state(findings: list[PriorFinding] | None = None, **kw: Any) -> PriorState:
    data: dict[str, Any] = {
        "version": 1,
        "tool_version": __version__,
        "head_sha": HEAD,
        "merge_base_sha": MERGE_BASE,
        "complete": True,
        "verdict": "findings",
        "config_hash": make_cfg().config_hash,
        "reviewed_at": "2026-01-01T00:00:00Z",
        "findings": findings or [],
    }
    data.update(kw)
    return PriorState(**data)


def prior_finding(fid: str, **kw: Any) -> PriorFinding:
    data: dict[str, Any] = {
        "id": fid,
        "path": "src/pagination.py",
        "line": 12,
        "severity": "high",
        "title": "Off-by-one",
        "evidence": "end = start + size + 1",
        "state": "open",
    }
    data.update(kw)
    return PriorFinding(**data)


def final_finding(fid: str, line: int | None = 12, **kw: Any) -> FinalFinding:
    data: dict[str, Any] = {
        "id": fid,
        "path": "src/pagination.py",
        "line": line,
        "anchor": "inline",
        "severity": "high",
        "category": "correctness",
        "title": f"Problem {fid}",
        "evidence": "end = start + size + 1",
        "explanation": "The page includes one item too many.",
        "state": "open",
        "votes": ["zai-glm-5-3"],
    }
    data.update(kw)
    return FinalFinding(**data)


def make_final(
    findings: list[FinalFinding],
    *,
    priors: list[ResolvedPrior] | None = None,
    verdict: Verdict = "findings",
    state: PriorState | None = None,
) -> FinalReview:
    if state is None:
        state = make_state(
            [prior_finding(f.id, path=f.path, line=f.line, state=f.state) for f in findings]
        )
    return FinalReview(
        verdict=verdict,
        verdict_reasons=[],
        findings=findings,
        priors=priors or [],
        summary_md=SUMMARY_MD,
        report_md="## Report\nAll findings.",
        state=state,
        usage={},
        cost_usd=0.5,
        coverage={"reviewable": 2, "covered": 2, "excluded": 1},
    )


def make_snap(tmp_path: Path, **kw: Any) -> PrSnapshot:
    data: dict[str, Any] = {
        "owner": "octo-org",
        "repo": "widgets",
        "number": 7,
        "html_url": PR_URL,
        "title": "t",
        "body_excerpt": "",
        "author": "pr-author",
        "author_association": "MEMBER",
        "draft": False,
        "state": "open",
        "merged": False,
        "head_sha": HEAD,
        "head_repo": "octo-org/widgets",
        "base_ref": "main",
        "base_sha": "b" * 40,
        "merge_base_sha": MERGE_BASE,
        "additions": 1,
        "deletions": 1,
        "changed_files": 1,
        "files": [],
        "files_json_sha256": "0" * 64,
        "can_post": True,
        "bot_login": BOT,
        "config": make_cfg(),
        "config_source": "default",
        "run_dir": str(tmp_path / EXEC_ID),
    }
    data.update(kw)
    return PrSnapshot(**data)


def render_inline(f: FinalFinding, snap: PrSnapshot) -> str:
    """Stand-in for review.render.inline_body (WP5)."""
    return f"**{f.severity}: {f.title}**\n\n{f.explanation}"


async def no_sleep(seconds: float) -> None:
    """Retry/backoff sleep that returns at once (tests never wait)."""
    del seconds
