"""The ``Harness`` that runs ``CodeReviewWorkflow`` in-process against fakes."""

from __future__ import annotations

import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fakegh import BOT, FakeGitHub
from mistralai import workflows
from wfkit import MISTRAL_KEY, PR_URL, STUB, TOKEN, Repo

from morning_star_review.core.contract import (
    OperatorPolicy,
    ReviewOutcome,
    ReviewRequest,
    RunOverrides,
)
from morning_star_review.core.naming import execution_id, parse_pr_url
from morning_star_review.core.runctx import RunContext, clear_run_context, set_run_context
from morning_star_review.core.seal import request_digest
from morning_star_review.workflow import CodeReviewWorkflow


@dataclass
class Harness:
    root: Path
    repo: Repo
    fake: FakeGitHub
    scenario_path: Path
    log_path: Path
    agent_python: Path
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    env_dump: Path | None = None
    last_ctx: RunContext | None = None

    @classmethod
    def create(cls, root: Path, repo: Repo, fake: FakeGitHub) -> Harness:
        scenario = root / "scenario.json"
        log = root / "calls.jsonl"
        wrapper = root / "agent-python"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" --scenario "{scenario}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        h = cls(root, repo, fake, scenario, log, wrapper)
        h.save()
        return h

    # ------------------------------------------------------------------ scenario

    def save(self) -> None:
        data = {"log": str(self.log_path), "specs": self.specs}
        if self.env_dump is not None:
            data["env_dump"] = str(self.env_dump)
        self.scenario_path.write_text(json.dumps(data), encoding="utf-8")

    def spec(self, key: str, **spec: Any) -> None:
        self.specs[key] = spec
        self.save()

    def calls(self, phase: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(x) for x in self.log_path.read_text().splitlines() if x.strip()]
        return [r for r in rows if phase is None or r["phase"] == phase]

    def reset_calls(self) -> None:
        self.log_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ runs

    def request(self, preset: str = "single", **kw: Any) -> ReviewRequest:
        data: dict[str, Any] = {
            "pr_url": PR_URL,
            "repo": "octo-org/widgets",
            "mode": "local",
            "policy": OperatorPolicy(trust_repo_config_models=True),
            "overrides": RunOverrides(preset=preset),  # type: ignore[arg-type]
            "run_label": "local:test",
        }
        data.update(kw)
        return ReviewRequest(**data)

    def context(
        self,
        req: ReviewRequest,
        *,
        token: str | None = TOKEN,
        read_token: str | None = None,
        cannot_post_reason: str | None = None,
        **extra: Any,
    ) -> RunContext:
        """The run context of a local run; ``extra`` sets more fields (run clock epochs)."""
        ref = parse_pr_url(req.pr_url)
        return RunContext(
            execution_id=execution_id("local", ref, self.repo.head, secrets.token_hex(4)),
            request_sha256=request_digest(req),
            seal_key=secrets.token_bytes(32),
            run_root=str(self.root / "runs"),
            mode="local",
            on_worker=False,
            github_token=token,
            read_token=read_token,
            bot_login=BOT,
            mistral_api_key=MISTRAL_KEY,
            agent_python=str(self.agent_python),
            cannot_post_reason=cannot_post_reason,
            **extra,
        )

    async def run(self, req: ReviewRequest | None = None, **ctx_kw: Any) -> ReviewOutcome:
        req = req or self.request()
        ctx = self.context(req, **ctx_kw)
        clear_run_context()
        set_run_context(ctx)
        self.last_ctx = ctx
        result = await workflows.execute_workflow(CodeReviewWorkflow, params=req)
        assert isinstance(result, ReviewOutcome)
        return result

    @property
    def run_dir(self) -> Path:
        assert self.last_ctx is not None
        return self.last_ctx.run_dir

    def outcome_file(self) -> dict[str, Any]:
        return json.loads((self.run_dir / "outcome.json").read_text())
