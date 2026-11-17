"""One review per run: new inline comments and "looks addressed" replies together.

Replies to fixed priors go into the run's review (created PENDING, filled with GraphQL
``addPullRequestReviewThreadReply``, submitted once), so a run never adds one review
per reply. Leftover pending reviews are cleared, and retries stay idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from builders import (
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
from morning_star_review.github.publish import publish

OLD = ("f0000000000f1", "f0000000000f2")
NEW = ("f00000000000a", "f00000000000b")


async def run(fake: FakeGitHub, tmp_path: Path, snap: PrSnapshot, final: FinalReview) -> Any:
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        return await publish(make_req(), snap, final, ctx, gh, render_inline=render_inline)


def summary_state(fake: FakeGitHub) -> Any:
    [ours] = [c for c in fake.issue_comments if c["user"]["login"] == BOT]
    return st.decode_state(ours["body"])


def replies(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [c for c in fake.review_comments if c["in_reply_to_id"] is not None]


def top_level(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [c for c in fake.review_comments if c["in_reply_to_id"] is None]


def setup(
    fake: FakeGitHub, tmp_path: Path, *, new: tuple[str, ...] = NEW, thread_ids: bool = True
) -> tuple[PrSnapshot, FinalReview, dict[str, dict[str, Any]]]:
    """Run 1 posted OLD; both are fixed now. ``new`` findings are posted in this run."""
    old = {
        fid: fake.add_review_comment("src/pagination.py", 12, st.finding_marker(fid)) for fid in OLD
    }
    prior = make_state(
        [
            prior_finding(
                fid,
                comment_id=c["id"],
                thread_id=fake.thread_id_of(c["id"]) if thread_ids else None,
            )
            for fid, c in old.items()
        ],
        head_sha="e" * 40,
    )
    priors = [
        ResolvedPrior(id=OLD[0], title="t", path="p", action="resolve", note=""),
        ResolvedPrior(id=OLD[1], title="t", path="p", action="file_deleted", note=""),
    ]
    findings = [final_finding(fid, 20 + i) for i, fid in enumerate(new)]
    verdict = "findings" if new else "clean"
    final = make_final(findings, priors=priors, verdict=verdict)
    return make_snap(tmp_path, prior=prior), final, old


async def test_inline_comments_and_replies_share_one_review(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, old = setup(fake, tmp_path)
    outcome = await run(fake, tmp_path, snap, final)
    assert outcome.status == "posted"
    # One review, created pending with the inline comments, submitted once.
    [created] = fake.posted("POST", "/pulls/7/reviews")
    assert "event" not in created and len(created["comments"]) == 2
    [review] = fake.reviews
    assert review["state"] == "COMMENTED" and fake.pending == {}
    assert fake.posted("POST", f"/reviews/{review['id']}/events") == [{"event": "COMMENT"}]
    assert outcome.review_url == review["html_url"]
    # Both replies were added to that review, not posted as reviews of their own.
    assert fake.posted("POST", "/replies") == []
    assert fake.mutation_names.count("addPullRequestReviewThreadReply") == 2
    assert sorted(c["in_reply_to_id"] for c in replies(fake)) == sorted(
        c["id"] for c in old.values()
    )
    assert {st.finding_ids_in(c["body"])[0] for c in top_level(fake)} >= set(NEW)
    # Fallback closed both priors (resolve refused): minimized and recorded as fixed.
    assert fake.minimized == {c["node_id"] for c in old.values()}
    state = summary_state(fake)
    assert {f.id: f.state for f in state.findings if f.id in OLD} == dict.fromkeys(OLD, "fixed")
    ids = {f.id: f.comment_id for f in state.findings}
    assert all(ids[fid] for fid in NEW)
    # A retry of the same run (same snapshot) adds no review, comment or reply; only
    # the resolve and minimize, which leave no trace, are tried again.
    before, mutations = fake.snapshot(), len(fake.mutation_names)
    await run(fake, tmp_path, snap, final)
    assert fake.snapshot() == before
    assert "addPullRequestReviewThreadReply" not in fake.mutation_names[mutations:]


async def test_replies_without_new_findings_use_exactly_one_review(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, _ = setup(fake, tmp_path, new=())
    outcome = await run(fake, tmp_path, snap, final)
    [review] = fake.reviews
    assert review["state"] == "COMMENTED"
    [created] = fake.posted("POST", "/pulls/7/reviews")
    assert created == {"commit_id": snap.head_sha}  # pending, empty until the replies
    assert len(replies(fake)) == 2 and fake.posted("POST", "/replies") == []
    assert outcome.review_url is None  # the review holds no new inline comment
    assert any("could not be resolved automatically" in n for n in outcome.notes)


async def test_resolved_threads_still_get_their_reply_in_the_review(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    fake.resolve_forbidden = False
    snap, final, old = setup(fake, tmp_path, new=())
    await run(fake, tmp_path, snap, final)
    assert len(fake.reviews) == 1 and len(replies(fake)) == 2
    assert {fake.thread_id_of(c["id"]) for c in old.values()} <= set(fake.resolved)
    assert fake.minimized == set()
    # Replies first (in the submitted review), then the resolves.
    names = fake.mutation_names
    assert names.index("resolveReviewThread") > max(
        i for i, n in enumerate(names) if n == "addPullRequestReviewThreadReply"
    )


async def test_rejected_batch_posts_singly_and_replies_share_one_review(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    fake.invalid_lines.add(("src/pagination.py", 21))
    snap, final, _ = setup(fake, tmp_path)
    outcome = await run(fake, tmp_path, snap, final)
    # One single-comment review for the placeable finding, one review for both replies.
    assert sorted(r["state"] for r in fake.reviews) == ["COMMENTED", "COMMENTED"]
    reply_reviews = [b for b in fake.posted("POST", "/pulls/7/reviews") if "event" not in b]
    assert len(reply_reviews) == 2  # the rejected pending batch, then the replies' review
    assert len(replies(fake)) == 2
    by_id = {f.id: f for f in outcome.findings}
    assert by_id[NEW[1]].state == "summary_only" and by_id[NEW[0]].comment_id
    assert outcome.review_url  # the single-comment review


async def test_failed_submit_drops_the_pending_review_and_the_retry_posts_once(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, _ = setup(fake, tmp_path)
    failures = {"left": 1}

    def fail_submit_once(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/events") and failures["left"]:
            failures["left"] -= 1
            return httpx.Response(502)
        return None

    fake.rules.append(fail_submit_once)
    with pytest.raises(GitHubTransient):
        await run(fake, tmp_path, snap, final)
    # Nothing became visible, and the pending review was deleted.
    assert fake.reviews == [] and fake.pending == {}
    assert replies(fake) == [] and not fake.issue_comments
    await run(fake, tmp_path, snap, final)
    [review] = fake.reviews
    assert review["state"] == "COMMENTED"
    assert len(replies(fake)) == 2 and len(top_level(fake)) == 4  # 2 old + 2 new


async def test_leftover_pending_review_is_deleted_first(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, final, _ = setup(fake, tmp_path)
    leftover = {"id": 77, "node_id": "PRR_77", "state": "PENDING", "body": ""}
    fake.reviews.append({**leftover, "user": {"login": BOT, "type": "Bot"}})
    fake.pending[77] = [{"path": "src/pagination.py", "line": 20, "body": "x", "in_reply_to": None}]
    theirs = {"id": 78, "node_id": "PRR_78", "state": "PENDING", "body": ""}
    fake.reviews.append({**theirs, "user": {"login": "alice", "type": "User"}})
    await run(fake, tmp_path, snap, final)
    assert ("DELETE", "/repos/octo-org/widgets/pulls/7/reviews/77") in fake.requests
    assert ("DELETE", "/repos/octo-org/widgets/pulls/7/reviews/78") not in fake.requests
    ours = [r for r in fake.reviews if r["user"]["login"] == BOT]
    assert [r["state"] for r in ours] == ["COMMENTED"] and ours[0]["id"] != 77
    assert [r["id"] for r in fake.reviews if r["state"] == "PENDING"] == [78]  # alice's
    assert 77 not in {r["id"] for r in fake.reviews} and len(replies(fake)) == 2


async def test_deleted_thread_is_skipped_and_an_empty_review_is_dropped(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, _ = setup(fake, tmp_path, new=())
    # Both threads vanish between fetch and publish (comments still listed).
    fake.rules.append(
        lambda request: (
            httpx.Response(
                200, json={"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
            )
            if b"addPullRequestReviewThreadReply" in request.content
            else None
        )
    )
    outcome = await run(fake, tmp_path, snap, final)
    assert fake.reviews == [] and fake.pending == {}  # created, nothing added, deleted
    assert fake.minimized == set() and "resolveReviewThread" not in fake.mutation_names
    state = summary_state(fake)
    assert not any(f.id in OLD for f in state.findings) and state.refused_resolves == []
    assert outcome.status == "posted"


async def test_deleted_comment_means_no_writes_for_that_prior(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, old = setup(fake, tmp_path, new=())
    fake.review_comments.remove(old[OLD[0]])
    await run(fake, tmp_path, snap, final)
    assert len(fake.reviews) == 1
    assert [c["in_reply_to_id"] for c in replies(fake)] == [old[OLD[1]]["id"]]
    assert fake.minimized == {old[OLD[1]]["node_id"]}


async def test_refused_reply_is_a_note_and_the_prior_is_still_closed(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, old = setup(fake, tmp_path)
    fake.rules.append(
        lambda request: (
            httpx.Response(200, json={"errors": [{"type": "UNPROCESSABLE", "message": "locked"}]})
            if b"addPullRequestReviewThreadReply" in request.content
            else None
        )
    )
    outcome = await run(fake, tmp_path, snap, final)
    [review] = fake.reviews  # the inline comments were still submitted
    assert review["state"] == "COMMENTED" and replies(fake) == []
    assert fake.minimized == {c["node_id"] for c in old.values()}
    assert sum("could not reply on fixed finding" in n for n in outcome.notes) == 2


async def test_prior_without_thread_id_falls_back_to_a_rest_reply(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    snap, final, _ = setup(fake, tmp_path, thread_ids=False)
    outcome = await run(fake, tmp_path, snap, final)
    # The inline review is submitted directly; each reply is a REST reply.
    [created] = fake.posted("POST", "/pulls/7/reviews")
    assert created["event"] == "COMMENT"
    assert len(fake.posted("POST", "/replies")) == 2 and len(replies(fake)) == 2
    assert outcome.review_url
