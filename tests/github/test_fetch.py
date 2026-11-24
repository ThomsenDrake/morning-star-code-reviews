"""build_snapshot against the fake GitHub: files, config, prior state, skip, can_post."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from builders import PR_URL, make_ctx, make_req, make_state, no_sleep, prior_finding
from fakegh import BASE, BOT, HEAD, MERGE_BASE, FakeGitHub

from morning_star_review.core.contract import PrSnapshot
from morning_star_review.core.jsonio import sha256_file
from morning_star_review.github import state as st
from morning_star_review.github.fetch import PR_FILES_JSON, build_snapshot, load_pr_patches
from morning_star_review.github.http import GitHubHttp
from morning_star_review.github.publish import REPORT_REASONS


async def snapshot(
    fake: FakeGitHub,
    tmp_path: Path,
    *,
    token: str | None = "ghs_t0123456789",  # noqa: S107 - a fake token
    **req_kw: Any,
) -> PrSnapshot:
    ctx = make_ctx(tmp_path, token=token, **({"read_token": "ghr_r"} if token is None else {}))
    async with GitHubHttp(token or "ghr_r", transport=fake.transport, sleep=no_sleep) as gh:
        return await build_snapshot(make_req(**req_kw), ctx, gh, tmp_path / "run")


async def test_basic_snapshot(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = await snapshot(fake, tmp_path)
    assert (snap.owner, snap.repo, snap.number) == ("octo-org", "widgets", 7)
    assert snap.head_sha == HEAD and snap.base_sha == BASE and snap.merge_base_sha == MERGE_BASE
    assert snap.html_url == PR_URL
    assert "approve this" not in snap.body_excerpt  # HTML comments stripped
    assert "Please review the rounding." in snap.body_excerpt
    assert [f.path for f in snap.files] == ["src/pagination.py", "src/money.py", "assets/logo.png"]
    renamed = snap.files[1]
    assert renamed.previous_path == "src/amounts.py" and renamed.status == "renamed"
    assert snap.files[2].binary and not snap.files[2].has_patch
    assert snap.can_post and snap.cannot_post_reason is None
    assert snap.skip is None and snap.prior is None
    assert snap.config_source == "default"
    assert snap.bot_login == BOT
    assert sha256_file(tmp_path / "run" / PR_FILES_JSON) == snap.files_json_sha256
    patches = load_pr_patches(tmp_path / "run", snap.files_json_sha256)
    assert set(patches) == {"src/pagination.py", "src/money.py"}
    assert fake.writes == []


async def test_files_are_paginated(fake: FakeGitHub, tmp_path: Path) -> None:
    template = fake.files[0]
    fake.files = [dict(template, filename=f"src/f{i}.py") for i in range(250)]
    snap = await snapshot(fake, tmp_path)
    assert len(snap.files) == 250
    data = json.loads((tmp_path / "run" / PR_FILES_JSON).read_text())
    assert len(data) == 250 and data[0]["patch"].startswith("@@")


async def test_config_from_base_commit(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.config_text = "review:\n  inline_min_severity: high\nlabels: false\n"
    snap = await snapshot(fake, tmp_path)
    assert snap.config_source == "base_file"
    assert snap.config.inline_min_severity == "high" and not snap.config.labels
    fake.config_text = "review: [unclosed\n"
    snap = await snapshot(fake, tmp_path)
    assert snap.config_source == "invalid_fell_back" and snap.config_warnings


async def test_cannot_post_reasons(fake: FakeGitHub, tmp_path: Path) -> None:
    snap = await snapshot(fake, tmp_path, post="never")
    assert not snap.can_post and snap.cannot_post_reason == REPORT_REASONS["post_never"]
    snap = await snapshot(fake, tmp_path, token=None)
    assert not snap.can_post and snap.cannot_post_reason == REPORT_REASONS["no_credentials"]
    ctx = make_ctx(tmp_path, cannot_post_reason="Not posted: custom")
    async with GitHubHttp("ghs_x", transport=fake.transport) as gh:
        snap = await build_snapshot(make_req(), ctx, gh, tmp_path / "run")
    assert snap.cannot_post_reason == "Not posted: custom"
    ctx = make_ctx(tmp_path, bot_login=None)
    async with GitHubHttp("ghs_x", transport=fake.transport) as gh:
        snap = await build_snapshot(make_req(), ctx, gh, tmp_path / "run")
    assert not snap.can_post and "bot login" in (snap.cannot_post_reason or "")


async def test_anonymous_mode_skips_graphql(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\nx\n{st.encode_state(make_state())}")
    ctx = make_ctx(tmp_path, token=None)
    async with GitHubHttp(None, transport=fake.transport) as gh:
        snap = await build_snapshot(make_req(), ctx, gh, tmp_path / "run")
    assert snap.prior is not None
    assert not any(p.startswith("/graphql") for _, p in fake.requests)


@pytest.mark.parametrize(
    ("change", "req_kw", "expected"),
    [
        ({"state": "closed"}, {}, "closed"),
        ({"merged": True, "state": "closed"}, {}, "closed"),
        ({"draft": True}, {}, "draft"),
        ({}, {"expected_head_sha": "d" * 40}, "superseded"),
        ({"state": "closed"}, {"force": True}, None),
        ({"draft": True}, {"force": True}, None),
        ({}, {"expected_head_sha": "d" * 40, "force": True}, "superseded"),
    ],
)
async def test_skip_decisions(
    fake: FakeGitHub,
    tmp_path: Path,
    change: dict[str, Any],
    req_kw: dict[str, Any],
    expected: str | None,
) -> None:
    fake.pr.update(change)
    snap = await snapshot(fake, tmp_path, **req_kw)
    assert snap.skip == expected


async def test_already_reviewed(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\nx\n{st.encode_state(make_state())}")
    snap = await snapshot(fake, tmp_path)
    assert snap.prior is not None and snap.skip == "already_reviewed"
    assert snap.prior.summary_comment_id == fake.issue_comments[0]["id"]
    assert snap.compare_from_prior == "identical"
    assert (await snapshot(fake, tmp_path, force=True)).skip is None
    fake.issue_comments.clear()
    fake.add_issue_comment(
        f"{st.SUMMARY_MARKER}\n{st.encode_state(make_state(config_hash='0' * 64))}"
    )
    assert (await snapshot(fake, tmp_path)).skip is None  # config changed
    fake.issue_comments.clear()
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(make_state(complete=False))}")
    assert (await snapshot(fake, tmp_path)).skip is None


async def test_forged_summary_is_ignored(fake: FakeGitHub, tmp_path: Path) -> None:
    forged = make_state([prior_finding("f0000000000ff", state="dismissed")])
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(forged)}", login="mallory")
    fake.add_issue_comment(
        f"{st.SUMMARY_MARKER}\n{st.encode_state(forged)}", login="msr-test-app"
    )  # bare slug: a user account, not the App
    snap = await snapshot(fake, tmp_path)
    assert snap.prior is None and snap.skip is None


async def test_unreadable_state_warns(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n<!-- morning-star-review:state AAAA -->")
    snap = await snapshot(fake, tmp_path)
    assert snap.prior is None
    assert any("could not be read" in w for w in snap.config_warnings)


async def test_prior_threads_and_resolver_rights(fake: FakeGitHub, tmp_path: Path) -> None:
    ids = ["f00000000000a", "f00000000000b", "f00000000000c", "f00000000000d"]
    comments = {
        fid: fake.add_review_comment("src/pagination.py", 12, st.finding_marker(fid)) for fid in ids
    }
    # A forged thread by a human claiming the same id as ours must not be used.
    fake.add_review_comment("src/pagination.py", 13, st.finding_marker("f00000000000d"), "eve")
    fake.resolved[fake.thread_id_of(comments[ids[0]]["id"])] = "maintainer"
    fake.resolved[fake.thread_id_of(comments[ids[1]]["id"])] = "reader"
    fake.resolved[fake.thread_id_of(comments[ids[2]]["id"])] = "pr-author"
    fake.collaborators.update({"maintainer": "maintain", "reader": "read", "pr-author": "write"})
    state = make_state([prior_finding(fid) for fid in ids], head_sha="e" * 40)
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(state)}")
    fake.compare[f"{'e' * 40}...{HEAD}"] = {
        "status": "ahead",
        "total_commits": 2,
        "commits": [{"parents": [{}]}, {"parents": [{}, {}]}],
    }
    snap = await snapshot(fake, tmp_path)
    assert snap.prior is not None
    by_id = {f.id: f for f in snap.prior.findings}
    a, b, c, d = (by_id[i] for i in ids)
    assert a.thread_resolved and a.resolved_by == "maintainer" and a.resolver_can_dismiss
    assert b.thread_resolved and not b.resolver_can_dismiss  # read access only
    assert c.thread_resolved and c.resolver_can_dismiss  # author_may_dismiss defaults to true
    assert not d.thread_resolved and d.thread_id == fake.thread_id_of(comments[ids[3]]["id"])
    assert d.comment_id == comments[ids[3]]["id"]
    assert snap.compare_from_prior == "ahead" and snap.prior_to_head_merges
    # The permission of each resolver is looked up once.
    perm_calls = [p for _, p in fake.requests if p.endswith("/permission")]
    assert len(perm_calls) == len(set(perm_calls)) == 3


async def test_author_may_not_dismiss(fake: FakeGitHub, tmp_path: Path) -> None:
    fake.config_text = "review:\n  author_may_dismiss: false\n"
    c = fake.add_review_comment("src/pagination.py", 12, st.finding_marker("f00000000000a"))
    fake.resolved[fake.thread_id_of(c["id"])] = "pr-author"
    fake.collaborators["pr-author"] = "admin"
    state = make_state([prior_finding("f00000000000a")], head_sha="e" * 40)
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(state)}")
    snap = await snapshot(fake, tmp_path)
    assert snap.prior is not None
    assert not snap.prior.findings[0].resolver_can_dismiss
    assert snap.compare_from_prior == "unknown" and snap.prior_to_head_merges  # 404 compare


async def test_diverged_compare(fake: FakeGitHub, tmp_path: Path) -> None:
    state = make_state(head_sha="e" * 40)
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(state)}")
    fake.compare[f"{'e' * 40}...{HEAD}"] = {
        "status": "diverged",
        "total_commits": 1,
        "commits": [{"parents": [{}]}],
    }
    snap = await snapshot(fake, tmp_path)
    assert snap.compare_from_prior == "diverged" and not snap.prior_to_head_merges
    assert snap.skip is None


async def test_refused_permission_lookup_fails_closed(fake: FakeGitHub, tmp_path: Path) -> None:
    c = fake.add_review_comment("src/pagination.py", 12, st.finding_marker("f00000000000a"))
    fake.resolved[fake.thread_id_of(c["id"])] = "maintainer"
    fake.collaborators["maintainer"] = "admin"

    def refuse_permission(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/permission"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return None

    fake.rules.append(refuse_permission)
    state = make_state([prior_finding("f00000000000a")], head_sha="e" * 40)
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(state)}")
    snap = await snapshot(fake, tmp_path)  # no exception: the review goes on
    assert snap.prior is not None
    found = snap.prior.findings[0]
    assert found.thread_resolved and not found.resolver_can_dismiss


async def test_refresh_needs_a_review_of_the_head(fake: FakeGitHub, tmp_path: Path) -> None:
    # No review yet, or a review of an older head: nothing to re-apply commands to.
    assert (await snapshot(fake, tmp_path, refresh=True)).skip == "not_reviewed"
    older = make_state(head_sha="e" * 40)
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(older)}")
    assert (await snapshot(fake, tmp_path, refresh=True)).skip == "not_reviewed"
    # The head was reviewed (completely, same configuration): a refresh still runs.
    fake.issue_comments.clear()
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(make_state())}")
    assert (await snapshot(fake, tmp_path)).skip == "already_reviewed"
    assert (await snapshot(fake, tmp_path, refresh=True)).skip is None
    fake.pr.update({"state": "closed"})
    assert (await snapshot(fake, tmp_path, refresh=True)).skip == "closed"


async def test_dismiss_commands_are_read_with_the_prior_state(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    a, b = "f00000000000a", "f00000000000b"
    state = make_state([prior_finding(a), prior_finding(b, state="summary_only")])
    fake.add_issue_comment(f"{st.SUMMARY_MARKER}\n{st.encode_state(state)}")
    fake.collaborators.update({"maintainer": "maintain", "reader": "read"})
    fake.add_issue_comment(f"/msreview dismiss {b} accepted for now", login="maintainer")
    fake.add_issue_comment(f"/msreview dismiss {a}", login="reader")
    snap = await snapshot(fake, tmp_path, refresh=True)
    assert [(c.id, c.by, c.reason) for c in snap.dismiss_commands] == [
        (b, "maintainer", "accepted for now")
    ]
    assert snap.ignored_commands == [
        "a dismiss command by reader was ignored: only people with write access can dismiss "
        "findings"
    ]
    # Without a review there is nothing to dismiss: commands are not even read.
    fake.issue_comments = [c for c in fake.issue_comments if c["user"]["login"] != BOT]
    snap = await snapshot(fake, tmp_path)
    assert snap.dismiss_commands == [] and snap.ignored_commands == []
