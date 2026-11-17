"""Formatter fallback, crashes, tampering and the agent environment (DESIGN 15.4)."""

from __future__ import annotations

import json
import os

import pytest
from fakegh import BOT
from harness import Harness
from wfkit import MISTRAL_KEY, SQLI, SWAP, TOKEN, notes_for

from morning_star_review.activities.agent_launcher import AgentCrashed
from morning_star_review.core.contract import (
    DraftFinding,
    FormatterDraft,
    PrSnapshot,
    ReviewRequest,
)
from morning_star_review.core.errors import MsreviewSealInvalid
from morning_star_review.workflow import _fetch_pr

pytestmark = pytest.mark.timeout(120)

PROSE_NOTES = (
    "I looked at the change. In app/db.py:6 the query is built with an f-string from the "
    "name, which is a SQL injection; severity high.\n"
    "SUMMARY: One injection problem.\nREVIEW COMPLETE\n"
)


def _formatter_draft(h: Harness) -> dict[str, object]:
    path, evidence, title, severity = SQLI
    finding = DraftFinding(
        path=path,
        line=h.repo.line(path, evidence),
        severity=severity,  # type: ignore[arg-type]
        category="security",
        title=title,
        evidence=evidence.strip(),
        explanation="The name is pasted into the SQL text.",
        fix="Use a bound parameter.",
    )
    draft = FormatterDraft(findings=[finding], prior_status=[], summary="One injection.")
    return draft.model_dump(mode="json")


async def test_lossy_notes_run_the_formatter(harness: Harness) -> None:
    h = harness
    h.spec("explore", notes=PROSE_NOTES, opened_paths=["app/db.py"])
    h.spec("format:review", draft=_formatter_draft(h), usage={"llm_calls": 1})
    out = await h.run()

    assert out.status == "posted", out.reason
    assert [c["phase"] for c in h.calls()] == ["explore", "format"]
    fmt = h.calls("format")[0]
    assert fmt["model"] == "mistral-large-4" and fmt["target"] == "review"
    assert fmt["max_tool_calls"] == 0
    assert [f.title for f in out.findings] == [SQLI[2]]
    assert any(k.startswith("formatter:") for k in out.usage)


async def test_failed_formatter_keeps_the_parsed_findings_and_blocks_clean(
    harness: Harness,
) -> None:
    h = harness
    broken = notes_for(h.repo, SWAP) + "FINDING\nfile: app/db.py\nthis block is broken\n"
    h.spec("explore", notes=broken, opened_paths=["app/db.py"])
    h.spec("format:review", exit_code=1)
    out = await h.run()

    assert out.status == "posted"
    assert [f.title for f in out.findings] == [SWAP[2]]
    assert out.verdict == "incomplete"


async def test_explore_crash_reports_failure_and_raises(harness: Harness) -> None:
    h = harness
    # A summary from an earlier run, so the failure banner has something to mark.
    h.fake.add_issue_comment(
        "<!-- morning-star-review:summary v1 -->\n### Morning Star Code Review\n"
    )
    h.spec("explore", crash_runs=99)
    with pytest.raises(AgentCrashed):
        await h.run()

    assert len(h.calls("explore")) == 2  # retry_policy_max_attempts=2
    outcome = h.outcome_file()
    assert outcome["status"] == "failed"
    assert "explore" in outcome["reason"] and "AgentCrashed" in outcome["reason"]
    body = next(c["body"] for c in h.fake.issue_comments if c["user"]["login"] == BOT)
    assert "did not finish (stage `explore`" in body
    assert "msreview: incomplete" in h.fake.pr_labels
    assert MISTRAL_KEY not in json.dumps(outcome) and TOKEN not in json.dumps(outcome)


async def test_explore_crash_then_success_on_retry(harness: Harness) -> None:
    h = harness
    h.spec("explore", crash_runs=1, notes=notes_for(h.repo, SQLI), opened_paths=["app/db.py"])
    out = await h.run()
    assert out.status == "posted"
    assert [c["run"] for c in h.calls("explore")] == [1, 2]


async def test_seal_tampering_is_rejected(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI))
    original = _fetch_pr.fetch_pr

    async def tampered(req: ReviewRequest) -> PrSnapshot:
        snap = await original(req)
        return snap.model_copy(update={"title": "changed after sealing"})

    monkeypatch.setattr(_fetch_pr, "fetch_pr", tampered)
    with pytest.raises(MsreviewSealInvalid):
        await h.run()
    assert h.calls() == []
    assert h.outcome_file()["status"] == "failed"
    assert "MsreviewSealInvalid" in h.outcome_file()["reason"]


async def test_agent_environment_is_an_allowlist(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    # Variables the worker process may hold; none of them may reach the agent.
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("MSREVIEW_APP_PRIVATE_KEY_COMMAND", "print-the-key")
    monkeypatch.setenv("MISTRAL_SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTS_SDK_COMPLETION_GATEWAY_API_BASE", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-the-agent")
    h.env_dump = h.root
    h.save()
    h.spec("explore", notes=notes_for(h.repo, SQLI))
    out = await h.run()
    assert out.status == "posted"

    env = json.loads((h.root / "env-explore.json").read_text())
    cwd = env.pop("__cwd__")
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # The agent ran in its own scratch directory, which is removed afterwards.
    assert os.path.realpath(cwd) == os.path.realpath(env["HOME"])
    assert not os.path.exists(cwd)
    assert h.calls("explore")[0]["cwd_empty"]
    assert env["MISTRAL_API_KEY"] == MISTRAL_KEY
    assert env["HOME"] == env["TMPDIR"]
    assert env["AGENTS_SDK_TELEMETRY_ENABLED"] == "0"
    forbidden_prefixes = (
        "GITHUB",
        "MSREVIEW_",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY",
        "GIT_CONFIG_VALUE",
        "PYTEST",
        "VIRTUAL_ENV",
    )
    assert not [k for k in env if k.startswith(forbidden_prefixes)]
    for name in (
        "SERVER_URL",
        "MISTRAL_API_URL",
        "MISTRAL_SERVER_URL",
        "MISTRAL_PROVIDER_API_BASE",
        "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE",
    ):
        assert name not in env
    assert all(TOKEN not in v for v in env.values())
