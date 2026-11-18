"""Deferred publishing (the Action): plan files, ``publish_plan`` and the activity branches."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "github"))

from builders import (  # noqa: E402
    EXEC_ID,
    SUMMARY_MD,
    final_finding,
    make_final,
    make_req,
    make_snap,
    make_state,
)
from fakegh import BOT, FakeGitHub  # noqa: E402

from morning_star_review import deferred  # noqa: E402
from morning_star_review.core.contract import (  # noqa: E402
    FailureInput,
    PublishInput,
    ReviewOutcome,
)
from morning_star_review.github import http as gh_http  # noqa: E402
from morning_star_review.github import state as st  # noqa: E402
from morning_star_review.github.publish import REPORT_REASONS, report_outcome  # noqa: E402

SLUG = BOT.removesuffix("[bot]")
TOKEN = "ghs_writetoken0123456789abcdef"
DEFERRED = "Posting deferred to the publish step."


@pytest.fixture(autouse=True)
def _reset_transport() -> Iterator[None]:
    yield
    gh_http.set_test_transport(None)


def review_plan(tmp_path: Path, **snap_kw: Any) -> deferred.DeferredPlan:
    snap = make_snap(tmp_path, **snap_kw)
    final = make_final([final_finding("f00000000000a", 12)])
    inp = PublishInput(req=make_req(mode="ci"), snap=snap, final=final)
    outcome = report_outcome(inp.req, snap, final, DEFERRED, execution_id=EXEC_ID)
    return deferred.DeferredPlan(kind="review", execution_id=EXEC_ID, review=inp, outcome=outcome)


def failure_plan(tmp_path: Path, *, with_snap: bool = True) -> deferred.DeferredPlan:
    snap = make_snap(tmp_path) if with_snap else None
    inp = FailureInput(
        req=make_req(mode="ci"), snap=snap, stage="verify", error_type="E", error_text="boom"
    )
    outcome = ReviewOutcome(
        status="failed",
        reason="verify failed: E: boom",
        pr_url=inp.req.pr_url,
        execution_id=EXEC_ID,
    )
    return deferred.DeferredPlan(kind="failure", execution_id=EXEC_ID, failure=inp, outcome=outcome)


# ----------------------------------------------------------------------------- plan files


def test_plan_round_trip(tmp_path: Path) -> None:
    for plan in (
        review_plan(tmp_path),
        failure_plan(tmp_path),
        failure_plan(tmp_path, with_snap=False),
    ):
        path = deferred.write_plan(tmp_path / "run", plan)
        assert path == tmp_path / "run" / deferred.PUBLISH_PLAN_JSON
        assert path.stat().st_mode & 0o777 == 0o600
        assert deferred.load_plan(path) == plan


def test_plan_carries_exactly_one_payload(tmp_path: Path) -> None:
    good = review_plan(tmp_path)
    failure = failure_plan(tmp_path)
    data = good.model_dump(mode="json")
    bad: list[dict[str, Any]] = [
        {**data, "review": None},
        {**data, "failure": failure.failure.model_dump(mode="json")},  # type: ignore[union-attr]
        {**data, "kind": "failure"},
        {**data, "schema_version": 2},
        {**data, "unknown": 1},
        {**data, "execution_id": "../elsewhere"},
        {**failure.model_dump(mode="json"), "outcome": good.outcome.model_dump(mode="json")},
    ]
    for item in bad:
        with pytest.raises(ValidationError):
            deferred.DeferredPlan.model_validate(item)


def test_load_plan_refuses_big_files_and_links(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (deferred.MAX_PLAN_BYTES + 1))
    with pytest.raises(Exception, match=r"(?i)large|bytes"):
        deferred.load_plan(big)
    real = deferred.write_plan(tmp_path / "run", failure_plan(tmp_path))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symbolic link"):
        deferred.load_plan(link)


def test_export_and_remove_plan(tmp_path: Path) -> None:
    run_dir, dest = tmp_path / "run", tmp_path / "publish"
    run_dir.mkdir()
    assert deferred.export_plan(run_dir, dest) is None  # no plan: nothing to post
    plan = failure_plan(tmp_path)
    deferred.write_plan(run_dir, plan)
    out = deferred.export_plan(run_dir, dest)
    assert out == dest / deferred.PUBLISH_PLAN_JSON
    assert deferred.load_plan(out) == plan
    assert out.stat().st_mode & 0o777 == 0o600
    deferred.remove_plan(dest)
    assert not out.exists()
    deferred.remove_plan(dest)  # already gone: fine
    (run_dir / deferred.PUBLISH_PLAN_JSON).write_text('{"kind": "review"}')
    with pytest.raises(ValidationError):
        deferred.export_plan(run_dir, dest)  # an invalid plan is never handed on
    assert not out.exists()


# ----------------------------------------------------------------------------- publish_plan


async def no_sleep(seconds: float) -> None:
    del seconds


async def post(plan: deferred.DeferredPlan, tmp_path: Path, **kw: Any) -> ReviewOutcome:
    args: dict[str, Any] = {"token": TOKEN, "app_slug": SLUG, "token_step": "success"}
    args.update(kw)
    return await deferred.publish_plan(
        plan,
        args["token"],
        args["app_slug"],
        args["token_step"],
        plan_dir=tmp_path / "publish",
        sleep=no_sleep,
    )


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


async def test_publish_plan_posts_once(fake: FakeGitHub, tmp_path: Path) -> None:
    gh_http.set_test_transport(fake.transport)
    auth: list[str | None] = []
    fake.rules.append(lambda request: auth.append(request.headers.get("authorization")))
    plan = review_plan(tmp_path)
    outcome = await post(plan, tmp_path)
    assert outcome.status == "posted" and outcome.execution_id == EXEC_ID
    assert len(fake.posted("POST", "/pulls/7/reviews")) == 1
    assert fake.pr_labels == ["msreview: findings"]
    run_dir = tmp_path / "publish" / EXEC_ID
    assert (run_dir / "report" / "report.md").is_file()
    assert ReviewOutcome.model_validate_json((run_dir / "outcome.json").read_text()) == outcome
    # Every request carries the write token minted after the review.
    assert auth and set(auth) == {f"Bearer {TOKEN}"}
    writes = len(fake.writes)
    again = await post(plan, tmp_path)  # idempotent (a re-run of the step)
    assert again.status == "posted" and len(fake.writes) == writes


async def test_publish_plan_superseded_head_is_a_report(fake: FakeGitHub, tmp_path: Path) -> None:
    gh_http.set_test_transport(fake.transport)
    fake.pr["head"]["sha"] = "d" * 40  # a push while the review ran
    outcome = await post(review_plan(tmp_path), tmp_path)
    assert outcome.status == "superseded"
    assert fake.writes == []


async def test_publish_plan_failure_marks_the_pr(fake: FakeGitHub, tmp_path: Path) -> None:
    gh_http.set_test_transport(fake.transport)
    summary = fake.add_issue_comment(f"{SUMMARY_MD}\n{st.encode_state(make_state())}")
    plan = failure_plan(tmp_path)
    outcome = await post(plan, tmp_path)
    assert outcome == plan.outcome and outcome.status == "failed"
    assert summary["body"].startswith(st.SUMMARY_MARKER + "\n" + st.failure_marker())
    assert "stage `verify`" in summary["body"]
    assert fake.pr_labels == ["msreview: incomplete"]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"app_slug": "another-app"}, "is not the App this review was prepared for"),
        ({"token_step": "failure"}, "token step did not succeed (failure)"),
        ({"token": ""}, REPORT_REASONS["no_credentials"]),
        ({"app_slug": ""}, "bot login is unknown"),
    ],
)
async def test_publish_plan_that_cannot_post_is_a_report(
    fake: FakeGitHub, tmp_path: Path, kw: dict[str, Any], reason: str
) -> None:
    gh_http.set_test_transport(fake.transport)
    outcome = await post(review_plan(tmp_path), tmp_path, **kw)
    assert outcome.status == "report"
    assert reason in (outcome.reason or "")
    assert outcome.report_markdown.startswith(f"> **{outcome.reason}**")
    assert fake.requests == []
    # A failure plan keeps its failed outcome and says why the PR was not marked.
    failed = await post(failure_plan(tmp_path), tmp_path, **kw)
    assert failed.status == "failed" and reason in failed.notes[-1]
    assert fake.requests == []


async def test_publish_plan_retries_then_fails_and_marks_the_pr(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    gh_http.set_test_transport(fake.transport)

    def summary_down(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/issues/7/comments"):
            return httpx.Response(502)
        return None

    fake.rules.append(summary_down)
    outcome = await post(review_plan(tmp_path), tmp_path)
    assert outcome.status == "failed"
    assert (outcome.reason or "").startswith("Not posted: publishing to GitHub failed (")
    assert "## Report" in outcome.report_markdown  # the review itself is kept
    attempts = [
        r for r in fake.requests if r == ("POST", "/repos/octo-org/widgets/issues/7/comments")
    ]
    assert len(attempts) == len(deferred.RETRY_DELAYS_S) + 1
    assert fake.pr_labels == ["msreview: incomplete"]
    assert TOKEN not in (outcome.reason or "")


# ----------------------------------------------------------------------------- activities


class NoGitHub:
    """A transport that records and refuses every request (the branches make none)."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(599)


@pytest.fixture
def no_github() -> NoGitHub:
    rec = NoGitHub()
    gh_http.set_test_transport(httpx.MockTransport(rec))
    return rec


@pytest.fixture
def activity_ctx(tmp_path: Path) -> Iterator[Any]:
    """Install a run context for ``make_req(mode="ci")``; yields a context factory."""
    from builders import make_ctx

    from morning_star_review.core.runctx import clear_run_context, set_run_context
    from morning_star_review.core.seal import request_digest

    def install(**kw: Any) -> Any:
        clear_run_context()
        data: dict[str, Any] = {
            "mode": "ci",
            "request_sha256": request_digest(make_req(mode="ci")),
            "seal_key": b"s" * 32,
        }
        data.update(kw)
        ctx = make_ctx(tmp_path, **data)
        set_run_context(ctx)
        return ctx

    yield install
    clear_run_context()


def sealed_input(ctx: Any, tmp_path: Path) -> PublishInput:
    from morning_star_review.core.seal import seal

    snap = seal(make_snap(tmp_path), ctx)
    final = seal(make_final([final_finding("f00000000000a", 12)]), ctx)
    return PublishInput(req=make_req(mode="ci"), snap=snap, final=final)


async def test_deferred_publish_writes_the_plan_and_no_github_call(
    tmp_path: Path, no_github: NoGitHub, activity_ctx: Any
) -> None:
    from morning_star_review.activities import publish as publish_activity

    ctx = activity_ctx(defer_publish=True)
    inp = sealed_input(ctx, tmp_path)
    outcome = await publish_activity.publish(inp)
    assert outcome.status == "report" and outcome.reason == DEFERRED
    assert outcome.verdict == "findings" and len(outcome.findings) == 1
    plan = deferred.load_plan(ctx.run_dir / deferred.PUBLISH_PLAN_JSON)
    assert plan.kind == "review" and plan.review == inp and plan.outcome == outcome
    assert (ctx.run_dir / "outcome.json").is_file()
    assert (ctx.run_dir / "report" / "report.md").is_file()
    assert no_github.seen == []


async def test_deferred_report_failure_writes_a_failure_plan(
    tmp_path: Path, no_github: NoGitHub, activity_ctx: Any
) -> None:
    from morning_star_review.activities import report_failure

    ctx = activity_ctx(defer_publish=True)
    snap = sealed_input(ctx, tmp_path).snap
    inp = FailureInput(
        req=make_req(mode="ci"), snap=snap, stage="explore", error_type="E", error_text="boom"
    )
    outcome = await report_failure.report_failure(inp)
    assert outcome.status == "failed" and "explore failed" in (outcome.reason or "")
    plan = deferred.load_plan(ctx.run_dir / deferred.PUBLISH_PLAN_JSON)
    assert plan.kind == "failure" and plan.outcome == outcome
    assert plan.failure is not None and plan.failure.snap == snap
    # The in-process backstop path (driver) behaves the same; an unsealed snapshot is
    # dropped from the plan.
    other = make_snap(tmp_path)
    again = await report_failure.record_failure(inp.model_copy(update={"snap": other}), ctx)
    assert again.status == "failed"
    plan = deferred.load_plan(ctx.run_dir / deferred.PUBLISH_PLAN_JSON)
    assert plan.failure is not None and plan.failure.snap is None
    assert no_github.seen == []


async def test_expired_token_turns_posting_into_a_report(
    tmp_path: Path, no_github: NoGitHub, activity_ctx: Any
) -> None:
    import time

    from morning_star_review.activities import publish as publish_activity
    from morning_star_review.core.runctx import TokenBox

    async def failed_remint() -> str | None:
        return None

    box = TokenBox(token=TOKEN, minted_epoch=time.time() - 3590, refresh=failed_remint)
    ctx = activity_ctx(token=TOKEN, token_box=box)
    outcome = await publish_activity.publish(sealed_input(ctx, tmp_path))
    assert outcome.status == "report"
    assert outcome.reason == REPORT_REASONS["token_expired"]
    assert not (ctx.run_dir / deferred.PUBLISH_PLAN_JSON).exists()
    assert no_github.seen == []


def test_review_step_then_publish_step(
    tmp_path: Path, activity_ctx: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Action's two steps end to end: plan, export, run dir removed, then post."""
    import asyncio
    import shutil

    from morning_star_review import cli, outputs
    from morning_star_review.activities import publish as publish_activity
    from morning_star_review.core.contract import OperatorPolicy, RunOverrides
    from morning_star_review.core.runctx import clear_run_context
    from morning_star_review.driver import DriverConfig

    no_github = NoGitHub()
    gh_http.set_test_transport(httpx.MockTransport(no_github))
    ctx = activity_ctx(defer_publish=True)
    outcome = asyncio.run(publish_activity.publish(sealed_input(ctx, tmp_path)))
    cfg = DriverConfig(
        mode="ci",
        pr_url=outcome.pr_url,
        expected_head_sha=None,
        post="auto",
        force=False,
        overrides=RunOverrides(),
        policy=OperatorPolicy(trust_repo_config_models=True),
        run_label="gha:test",
        deadline_s=5400,
        out_dir=tmp_path / "report",
        fail_on="never",
        defer_publish_dir=tmp_path / "publish",
    )
    review_out = tmp_path / "review-output"
    outputs.emit(cfg, outcome, ctx.run_dir, deployment="d", env={"GITHUB_OUTPUT": str(review_out)})
    plan_path = dict(line.split("=", 1) for line in review_out.read_text().splitlines())[
        "publish-plan"
    ]
    assert DEFERRED in (tmp_path / "report" / "report.md").read_text()
    shutil.rmtree(ctx.run_dir)  # the driver removes the run directory
    clear_run_context()
    assert no_github.seen == []

    fake = FakeGitHub()
    gh_http.set_test_transport(fake.transport)
    publish_out = tmp_path / "publish-output"
    env = {
        "MSREVIEW_GITHUB_TOKEN": TOKEN,
        "MSREVIEW_APP_SLUG": SLUG,
        "MSREVIEW_TOKEN_STEP": "success",
        "MSREVIEW_PUBLISH_PLAN": plan_path,
        "MSREVIEW_FAIL_ON": "findings",
        "MSREVIEW_REMOVE_LABEL": "",
        "MSREVIEW_PR_URL": outcome.pr_url,
        "MSREVIEW_REPORT_DIR": str(tmp_path / "report"),
        "GITHUB_OUTPUT": str(publish_out),
    }
    assert cli.cmd_ci_publish(env) == 3  # fail-on findings, applied by the publish step
    lines = dict(line.split("=", 1) for line in publish_out.read_text().splitlines())
    assert lines["status"] == "posted" and lines["findings"] == "1"
    assert len(fake.posted("POST", "/pulls/7/reviews")) == 1
    assert DEFERRED not in (tmp_path / "report" / "report.md").read_text()
    assert not ctx.run_dir.exists()  # nothing is written into the removed run directory
    out = capsys.readouterr()
    assert "posting in the next step" in out.err and "msreview result: status=posted" in out.err
    assert TOKEN not in out.out + out.err
