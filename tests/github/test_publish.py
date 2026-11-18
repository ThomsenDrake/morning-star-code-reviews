"""publish(): idempotent posting, 422 fallback, priors, labels, report mode, failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from builders import (
    EXEC_ID,
    SUMMARY_MD,
    final_finding,
    make_ctx,
    make_final,
    make_req,
    make_snap,
    make_state,
    no_sleep,
    prior_finding,
    render_inline,
)
from fakegh import BOT, FakeGitHub

from morning_star_review.core.contract import FinalReview, PrSnapshot, ResolvedPrior
from morning_star_review.github import state as st
from morning_star_review.github.http import GitHubHttp, GitHubTransient
from morning_star_review.github.labels import LABEL_SPECS
from morning_star_review.github.publish import (
    MAX_COMMENT_CHARS,
    REPORT_REASONS,
    publish,
    summary_body,
    write_failure_header,
)


async def run(fake: FakeGitHub, tmp_path: Path, snap: PrSnapshot, final: FinalReview | None) -> Any:
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        return await publish(make_req(), snap, final, ctx, gh, render_inline=render_inline)


def summary_state(fake: FakeGitHub) -> Any:
    ours = [c for c in fake.issue_comments if c["user"]["login"] == BOT]
    assert len(ours) == 1
    return st.decode_state(ours[0]["body"])


def standard_final() -> FinalReview:
    return make_final(
        [
            final_finding("f00000000000a", 12),
            final_finding("f00000000000b", 14, start_line=13, severity="critical"),
            final_finding("f00000000000c", 3, path="src/money.py", state="summary_only"),
            final_finding("f00000000000d", None, anchor="file"),
        ]
    )


async def test_fresh_post(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path)
    outcome = await run(fake, tmp_path, snap, standard_final())
    assert outcome.status == "posted" and outcome.execution_id == EXEC_ID
    reviews = fake.posted("POST", "/pulls/7/reviews")
    assert len(reviews) == 1
    review = reviews[0]
    assert review["event"] == "COMMENT" and review["commit_id"] == snap.head_sha
    assert review["body"] == ""
    comments = review["comments"]
    assert [c["line"] for c in comments] == [12, 14]
    assert all(c["side"] == "RIGHT" for c in comments)
    assert comments[1]["start_line"] == 13 and comments[1]["start_side"] == "RIGHT"
    assert "start_line" not in comments[0]
    assert st.finding_ids_in(comments[0]["body"]) == ["f00000000000a"]
    # Summary: one bot comment with marker and a state that records the comment ids.
    state = summary_state(fake)
    assert state is not None
    ids = {f.id: f.comment_id for f in state.findings}
    assert ids["f00000000000a"] and ids["f00000000000b"]
    assert ids["f00000000000c"] is None
    assert fake.issue_comments[0]["body"].startswith(st.SUMMARY_MARKER)
    # Verdict label created with our color and set.
    assert fake.pr_labels == ["msreview: findings"]
    assert fake.repo_labels["msreview: findings"]["color"] == LABEL_SPECS["msreview: findings"][0]
    assert outcome.review_url and outcome.summary_url
    posted = {f.id: f.comment_id for f in outcome.findings}
    assert posted["f00000000000a"] == ids["f00000000000a"]
    assert (Path(snap.run_dir) / "report" / "run.json").exists()


async def test_publish_twice_is_idempotent(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path)
    final = standard_final()
    await run(fake, tmp_path, snap, final)
    first = fake.snapshot()
    writes = len(fake.writes)
    await run(fake, tmp_path, snap, final)
    assert fake.snapshot() == first
    assert len(fake.writes) == writes  # nothing written the second time


async def test_retry_after_crash_before_summary(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path)
    final = standard_final()
    failures = {"left": 1}

    def fail_summary_once(request: httpx.Request) -> httpx.Response | None:
        is_summary_post = request.method == "POST" and request.url.path.endswith(
            "/issues/7/comments"
        )
        if is_summary_post and failures["left"]:
            failures["left"] -= 1
            return httpx.Response(502)
        return None

    fake.rules.append(fail_summary_once)
    with pytest.raises(GitHubTransient):
        await run(fake, tmp_path, snap, final)
    assert len(fake.reviews) == 1 and not fake.issue_comments
    await run(fake, tmp_path, snap, final)
    assert len(fake.reviews) == 1  # no duplicate review
    assert len([c for c in fake.review_comments if c["in_reply_to_id"] is None]) == 2
    state = summary_state(fake)
    assert state is not None and all(
        f.comment_id for f in state.findings if f.id in ("f00000000000a", "f00000000000b")
    )


async def test_422_falls_back_to_single_comments(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.invalid_lines.add(("src/pagination.py", 14))
    snap = make_snap(tmp_path)
    outcome = await run(fake, tmp_path, snap, standard_final())
    assert outcome.status == "posted"
    assert len(fake.reviews) == 1  # the batch failed; one single-comment review succeeded
    by_id = {f.id: f for f in outcome.findings}
    assert by_id["f00000000000b"].state == "summary_only"
    assert by_id["f00000000000a"].comment_id
    body = fake.issue_comments[0]["body"]
    assert "Could not be placed on the diff" in body and "Problem f00000000000b" in body
    state = summary_state(fake)
    assert state is not None
    assert {f.id: f.state for f in state.findings}["f00000000000b"] == "summary_only"
    assert any("could not be placed" in n for n in outcome.notes)


async def test_superseded_during_publish(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.pr["head"]["sha"] = "f" * 40
    snap = make_snap(tmp_path)
    outcome = await run(fake, tmp_path, snap, standard_final())
    assert outcome.status == "superseded"
    assert "new commits" in (outcome.reason or "")
    assert fake.writes == []
    assert outcome.report_markdown.startswith("> **Not posted")
    assert (Path(snap.run_dir) / "report" / "report.md").exists()


@pytest.mark.parametrize("can_post", [True, False])
async def test_outcome_and_run_json_carry_reasons_and_possible_issues(
    fake: FakeGitHub, tmp_path: Path, can_post: bool
) -> None:
    maybe = final_finding(
        "f00000000000e", 20, state="summary_only", verify_status="uncertain", votes=["a"]
    )
    final = make_final([maybe], verdict="incomplete").model_copy(
        update={"verdict_reasons": ["the verifier did not finish"]}
    )
    snap = make_snap(tmp_path, can_post=can_post, cannot_post_reason=None if can_post else "no")
    outcome = await run(fake, tmp_path, snap, final)
    assert outcome.status == ("posted" if can_post else "report")
    assert outcome.verdict_reasons == ["the verifier did not finish"]
    assert outcome.possible_issues == 1
    run_json = json.loads((Path(snap.run_dir) / "report" / "run.json").read_text())
    assert run_json["verdict_reasons"] == ["the verifier did not finish"]
    assert run_json["possible_issues"] == 1
    if can_post:
        assert fake.pr_labels == ["msreview: incomplete"]


async def test_report_mode_makes_no_requests(fake: FakeGitHub, tmp_path: Path) -> None:
    reason = REPORT_REASONS["not_installed"].format(slug="app", owner="octo-org", repo="widgets")
    snap = make_snap(tmp_path, can_post=False, cannot_post_reason=reason)

    def no_writes(request: httpx.Request) -> httpx.Response | None:
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    fake.rules.append(no_writes)
    outcome = await run(fake, tmp_path, snap, standard_final())
    assert outcome.status == "report" and outcome.reason == reason
    assert outcome.report_markdown.startswith(f"> **{reason}**")
    assert "## Report" in outcome.report_markdown
    assert fake.requests == []
    report_dir = Path(snap.run_dir) / "report"
    findings = json.loads((report_dir / "findings.json").read_text())
    assert findings["pr_url"] and len(findings["findings"]) == 4
    assert json.loads((report_dir / "run.json").read_text())["status"] == "report"


async def test_skipped_snapshot_makes_no_requests(fake: FakeGitHub, tmp_path: Path) -> None:
    for skip, status in (("draft", "skipped"), ("superseded", "superseded")):
        snap = make_snap(tmp_path, skip=skip)
        outcome = await run(fake, tmp_path, snap, None)
        assert outcome.status == status and outcome.reason
    assert fake.requests == []


async def test_forged_markers_are_ignored(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.add_review_comment("src/pagination.py", 12, st.finding_marker("f00000000000a"), "eve")
    forged = fake.add_issue_comment(SUMMARY_MD, login="eve")
    await run(fake, tmp_path, make_snap(tmp_path), standard_final())
    ours = [c for c in fake.review_comments if c["user"]["login"] == BOT]
    assert {st.finding_ids_in(c["body"])[0] for c in ours} == {"f00000000000a", "f00000000000b"}
    assert forged["body"] == SUMMARY_MD  # never edited
    assert [c["user"]["login"] for c in fake.issue_comments] == ["eve", BOT]


def priors_setup(fake: FakeGitHub, tmp_path: Path) -> tuple[PrSnapshot, FinalReview, dict]:
    old = {
        fid: fake.add_review_comment("src/pagination.py", 12, st.finding_marker(fid))
        for fid in ("f0000000000f1", "f0000000000f2", "f0000000000f3")
    }
    fake.resolved[fake.thread_id_of(old["f0000000000f3"]["id"])] = "maintainer"
    prior = make_state(
        [
            prior_finding(
                fid,
                comment_id=c["id"],
                thread_id=fake.thread_id_of(c["id"]),
                thread_resolved=fid == "f0000000000f3",
            )
            for fid, c in old.items()
        ],
        head_sha="e" * 40,
    )
    snap = make_snap(tmp_path, prior=prior)
    priors = [
        ResolvedPrior(id="f0000000000f1", title="t", path="p", action="resolve", note=""),
        ResolvedPrior(id="f0000000000f2", title="t", path="p", action="file_deleted", note=""),
        ResolvedPrior(id="f0000000000f3", title="t", path="p", action="resolve", note=""),
    ]
    state = make_state(
        [prior_finding(fid, comment_id=c["id"], state="fixed") for fid, c in old.items()]
    )
    final = make_final([], priors=priors, verdict="clean", state=state)
    return snap, final, old


async def test_resolve_forbidden_falls_back_to_minimize(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, final, old = priors_setup(fake, tmp_path)
    outcome = await run(fake, tmp_path, snap, final)
    replies = [c for c in fake.review_comments if c["in_reply_to_id"]]
    assert sorted(c["in_reply_to_id"] for c in replies) == sorted(
        [old["f0000000000f1"]["id"], old["f0000000000f2"]["id"]]
    )
    assert any("File removed." in c["body"] for c in replies)
    assert any("Looks addressed in `aaaaaaa`" in c["body"] for c in replies)
    assert fake.minimized == {old["f0000000000f1"]["node_id"], old["f0000000000f2"]["node_id"]}
    state = summary_state(fake)
    assert state is not None
    assert set(state.refused_resolves) == {
        fake.thread_id_of(old["f0000000000f1"]["id"]),
        fake.thread_id_of(old["f0000000000f2"]["id"]),
    }
    assert all(f.state == "fixed" for f in state.findings)
    assert fake.pr_labels == ["msreview: clean"]
    # A retry neither replies nor minimizes twice.
    before = fake.snapshot()
    await run(fake, tmp_path, snap, final)
    assert fake.snapshot() == before
    assert outcome.status == "posted"


async def test_resolve_allowed(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.resolve_forbidden = False
    snap, final, old = priors_setup(fake, tmp_path)
    await run(fake, tmp_path, snap, final)
    assert fake.thread_id_of(old["f0000000000f1"]["id"]) in fake.resolved
    assert fake.minimized == set()
    state = summary_state(fake)
    assert state is not None and state.refused_resolves == []


async def test_minimize_failure_is_recorded_once(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, final, _ = priors_setup(fake, tmp_path)

    def minimize_fails(request: httpx.Request) -> httpx.Response | None:
        if b"minimizeComment" in request.content:
            return httpx.Response(200, json={"errors": [{"type": "FORBIDDEN", "message": "no"}]})
        return None

    fake.rules.append(minimize_fails)
    outcome = await run(fake, tmp_path, snap, final)
    state = summary_state(fake)
    assert state is not None
    # Replied once and recorded as fixed: a later run does not try again.
    states = {f.id: f.state for f in state.findings}
    assert states == dict.fromkeys(("f0000000000f1", "f0000000000f2", "f0000000000f3"), "fixed")
    assert any("minimize" in n for n in outcome.notes)
    assert len([c for c in fake.review_comments if c["in_reply_to_id"]]) == 2


async def test_label_hygiene(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.pr_labels = ["msreview: clean", "msreview: incomplete", "bug"]
    fake.repo_labels = {n: {"name": n} for n in fake.pr_labels}
    await run(fake, tmp_path, make_snap(tmp_path), standard_final())
    assert sorted(fake.pr_labels) == ["bug", "msreview: findings"]


async def test_label_failure_is_a_note(fake: FakeGitHub, tmp_path: Path) -> None:
    def deny_labels(request: httpx.Request) -> httpx.Response | None:
        if "/labels" in request.url.path and request.method != "GET":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return None

    fake.rules.append(deny_labels)
    outcome = await run(fake, tmp_path, make_snap(tmp_path), standard_final())
    assert outcome.status == "posted"
    assert any("verdict label" in n for n in outcome.notes)


async def test_labels_disabled(fake: FakeGitHub, tmp_path: Path) -> None:
    from builders import make_cfg

    snap = make_snap(tmp_path, config=make_cfg(labels=False))
    await run(fake, tmp_path, snap, standard_final())
    assert not any("/labels" in p for _, p in fake.requests)


async def test_marker_appended_when_renderer_omits_it(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path)
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await publish(
            make_req(), snap, standard_final(), ctx, gh, render_inline=lambda f, s: "plain"
        )
    bodies = [c["body"] for c in fake.review_comments]
    assert all(b.startswith("plain\n<!-- morning-star-review:finding ") for b in bodies)


def test_summary_body_limits() -> None:
    state = make_state([prior_finding("f00000000000a")])
    huge = SUMMARY_MD + "\n" + ("line of text\n" * 8000)
    body = summary_body(huge, [], state)
    assert len(body) <= MAX_COMMENT_CHARS
    assert body.startswith(st.SUMMARY_MARKER) and "(summary truncated)" in body
    assert st.decode_state(body) == state
    assert summary_body("no marker", [], state).startswith(st.SUMMARY_MARKER)


async def test_failure_header(fake: FakeGitHub, tmp_path: Path) -> None:
    original = f"{SUMMARY_MD}\n{st.encode_state(make_state())}"
    summary = fake.add_issue_comment(original)
    snap = make_snap(tmp_path)
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await write_failure_header(make_req(), snap, "explore", ctx, gh)
        once = summary["body"]
        await write_failure_header(make_req(), snap, "verify", ctx, gh)
    body = summary["body"]
    assert body.startswith(st.SUMMARY_MARKER + "\n" + st.failure_marker())
    assert "stage `verify`" in body and "stage `explore`" not in body
    assert body.count(st.failure_marker()) == 1
    assert "stage `explore`" in once
    assert st.decode_state(body) == make_state()  # the earlier state is kept
    assert fake.pr_labels == ["msreview: incomplete"]


async def test_failure_header_never_raises(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.rules.append(lambda request: httpx.Response(500))
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await write_failure_header(make_req(), make_snap(tmp_path), "publish", ctx, gh)
        await write_failure_header(make_req(), None, "fetch_pr", ctx, gh)
    report_snap = make_snap(tmp_path, can_post=False, cannot_post_reason="x")
    fake.requests.clear()
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await write_failure_header(make_req(), report_snap, "explore", ctx, gh)
    assert fake.requests == []


async def test_default_renderer_is_wp5_inline_body(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path)
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await publish(make_req(), snap, standard_final(), ctx, gh)
    bodies = [c["body"] for c in fake.review_comments]
    assert len(bodies) == 2
    for body in bodies:
        assert len(st.finding_ids_in(body)) == 1
        assert body.count("morning-star-review:finding") == 1  # not appended twice


async def test_unknown_bot_login_means_report(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = make_snap(tmp_path, bot_login=None)
    outcome = await run(fake, tmp_path, snap, standard_final())
    assert outcome.status == "report"
    assert fake.requests == []


async def test_label_created_concurrently(fake: FakeGitHub, tmp_path: Path) -> None:
    from morning_star_review.github.labels import ensure_label

    def created_meanwhile(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/labels"):
            return httpx.Response(
                422, json={"message": "Validation Failed", "errors": [{"code": "already_exists"}]}
            )
        return None

    fake.rules.append(created_meanwhile)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        await ensure_label(gh, "octo-org", "widgets", "msreview: clean")  # no exception
