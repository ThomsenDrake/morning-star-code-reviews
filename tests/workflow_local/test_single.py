"""Single-explorer runs of the workflow, in-process (DESIGN 15.4)."""

from __future__ import annotations

import json

import httpx
import pytest
from fakegh import BOT
from harness import Harness
from wfkit import AUTHZ, SQLI, SWAP, TOKEN, notes_for

from morning_star_review.core.contract import FailureInput
from morning_star_review.github.publish import report_reason

pytestmark = pytest.mark.timeout(120)


def _single_explorer(h: Harness, notes: str, **extra: object) -> None:
    h.spec("explore", notes=notes, opened_paths=["app/db.py", "app/service.py"], **extra)


async def test_posted_fresh_review(harness: Harness) -> None:
    h = harness
    _single_explorer(h, notes_for(h.repo, SQLI, AUTHZ, SWAP))
    out = await h.run()

    assert out.status == "posted", out.reason
    assert out.verdict == "findings"
    assert out.head_sha == h.repo.head
    titles = {f.title for f in out.findings}
    assert {SQLI[2], AUTHZ[2], SWAP[2]} <= titles
    # One explorer call; the notes parsed, so no formatter call.
    assert [c["phase"] for c in h.calls()] == ["explore"]
    call = h.calls("explore")[0]
    assert call["cwd_empty"] and call["diffs_present"]
    # GitHub: one review with three inline comments, one summary, the verdict label.
    assert len(h.fake.reviews) == 1
    assert {c["path"] for c in h.fake.review_comments} == {
        "app/db.py",
        "app/handlers.py",
        "app/service.py",
    }
    summary = [c for c in h.fake.issue_comments if c["user"]["login"] == BOT]
    assert len(summary) == 1
    assert "msreview: findings" in h.fake.pr_labels
    # Run directory artefacts.
    outcome = h.outcome_file()
    assert outcome["status"] == "posted"
    assert (h.run_dir / "report" / "findings.json").is_file()
    assert (h.run_dir / "anchors.json").is_file()
    job = json.loads((h.run_dir / "explore-zai-glm-5-3" / "job.json").read_text())
    assert job["phase"] == "explore" and job["model"] == "zai-glm-5-3"


async def test_inline_anchor_matches_the_evidence_line(harness: Harness) -> None:
    h = harness
    _single_explorer(h, notes_for(h.repo, SQLI))
    out = await h.run()
    assert out.status == "posted"
    (f,) = out.findings
    assert f.path == "app/db.py"
    assert f.line == h.repo.line(*SQLI[:2])
    assert f.anchor == "inline"


async def test_report_mode_when_the_app_is_not_installed(harness: Harness) -> None:
    h = harness
    _single_explorer(h, notes_for(h.repo, SQLI))
    reason = report_reason("not_installed", slug="msr-test-app", owner="octo-org", repo="widgets")
    out = await h.run(
        token=None, read_token="ghp_readonly0123456789abcdef", cannot_post_reason=reason
    )

    assert out.status == "report"
    assert out.reason == reason
    assert out.report_markdown
    assert h.fake.writes == []
    assert (h.run_dir / "report" / "report.md").is_file()


@pytest.mark.parametrize(
    ("change", "skip"),
    [
        ({"draft": True}, "draft"),
        ({"state": "closed"}, "closed"),
    ],
)
async def test_skipped_pull_requests_make_no_model_call(
    harness: Harness, change: dict[str, object], skip: str
) -> None:
    h = harness
    h.fake.pr.update(change)
    _single_explorer(h, notes_for(h.repo, SQLI))
    out = await h.run()
    assert out.status == "skipped"
    assert h.calls() == []
    assert h.fake.writes == []
    assert h.outcome_file()["status"] == "skipped"


async def test_rerun_on_the_same_head_is_already_reviewed(harness: Harness) -> None:
    h = harness
    _single_explorer(h, notes_for(h.repo, SQLI))
    first = await h.run()
    assert first.status == "posted"
    writes = len(h.fake.writes)
    h.reset_calls()

    again = await h.run()
    assert again.status == "skipped"
    assert again.reason and "already" in again.reason.lower()
    assert h.calls() == []
    assert len(h.fake.writes) == writes


async def test_superseded_during_publish(harness: Harness) -> None:
    h = harness
    _single_explorer(h, notes_for(h.repo, SQLI))
    seen = {"n": 0}
    moved = dict(h.fake.pr, head={**h.fake.pr["head"], "sha": "d" * 40})

    def head_moves(request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET" and request.url.path.endswith("/pulls/7"):
            seen["n"] += 1
            if seen["n"] >= 2:  # fetch_pr reads it first; publish re-reads it
                return httpx.Response(200, json=moved)
        return None

    h.fake.rules.append(head_moves)
    out = await h.run()
    assert out.status == "superseded"
    assert h.fake.writes == []
    assert out.report_markdown


def test_failure_input_type_bounds(harness: Harness) -> None:
    # The workflow passes failure_type(exc)[:100]; the model enforces the bound.
    req = harness.request()
    base = {"req": req, "snap": None, "stage": "explore", "error_text": ""}
    FailureInput.model_validate({**base, "error_type": "x" * 100})
    with pytest.raises(ValueError, match="error_type"):
        FailureInput.model_validate({**base, "error_type": "x" * 101})


def test_token_constant_is_synthetic() -> None:
    assert TOKEN.startswith("ghs_workflowtest")
