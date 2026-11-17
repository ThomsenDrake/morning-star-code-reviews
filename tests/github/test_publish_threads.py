"""Fixed priors whose thread GitHub will not resolve: closed quietly, once, and recorded.

When ``resolveReviewThread`` is refused, ``publish`` replies once, minimizes our
comment as RESOLVED once, records the prior as ``fixed`` (and the thread id in
``refused_resolves``), and notes it under "Resolved since the last review". Later
runs must not reply again nor retry the resolve or the minimize.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from builders import make_ctx, make_final, make_req, make_snap, make_state, no_sleep
from builders import prior_finding as base_prior
from fakegh import BOT, HEAD, FakeGitHub

from morning_star_review.core.contract import FinalReview, PrSnapshot, ResolvedPrior
from morning_star_review.github import state as st
from morning_star_review.github.http import GitHubHttp
from morning_star_review.github.publish import (
    RESOLVED_HEADING,
    UNCLOSED_NOTE,
    UNCLOSED_SUFFIX,
    publish,
    with_unclosed_notes,
)
from morning_star_review.review import render
from morning_star_review.review.reconcile import reconcile_priors

FID = "f0000000000a1"
TITLE = "Page end includes one extra item"
PATH = "src/pagination.py"
NEXT_HEAD = "c" * 40


async def run(fake: FakeGitHub, tmp_path: Path, snap: PrSnapshot, final: FinalReview) -> Any:
    ctx = make_ctx(tmp_path)
    async with GitHubHttp("ghs_t0123456789", transport=fake.transport, sleep=no_sleep) as gh:
        return await publish(make_req(), snap, final, ctx, gh)


def summary_comment(fake: FakeGitHub) -> dict[str, Any]:
    ours = [c for c in fake.issue_comments if c["user"]["login"] == BOT]
    assert len(ours) == 1
    return ours[0]


def resolved(fid: str = FID, action: str = "resolve") -> ResolvedPrior:
    return ResolvedPrior(id=fid, title=TITLE, path=PATH, action=action, note="")  # type: ignore[arg-type]


def rendered_summary(priors: list[ResolvedPrior]) -> str:
    """The summary exactly as ``review.render`` writes it (so the note finds its line)."""
    return render.summary(
        render.SummaryInput(
            owner="octo-org",
            repo="widgets",
            number=7,
            head_sha=HEAD,
            verdict="clean",
            verdict_reasons=[],
            scope="incremental",
            since_sha="e" * 40,
            findings=[],
            priors=priors,
            inline_min_severity="medium",
            coverage=render.Coverage(reviewable=1, covered=1),
            details=None,
        )
    )


def fixed_run_final(priors: list[ResolvedPrior]) -> FinalReview:
    """Like ``finalize``: resolved priors are not in the new state."""
    final = make_final([], priors=priors, verdict="clean", state=make_state([]))
    return final.model_copy(update={"summary_md": rendered_summary(priors)})


def setup_open_prior(fake: FakeGitHub, tmp_path: Path) -> tuple[PrSnapshot, dict[str, Any], str]:
    """Run 1 posted one finding; its thread is open."""
    comment = fake.add_review_comment(PATH, 12, st.finding_marker(FID))
    thread = fake.thread_id_of(comment["id"])
    prior = make_state(
        [base_prior(FID, title=TITLE, path=PATH, comment_id=comment["id"], thread_id=thread)],
        head_sha="e" * 40,
    )
    return make_snap(tmp_path, prior=prior), comment, thread


def snap_from_summary(fake: FakeGitHub, tmp_path: Path) -> PrSnapshot:
    """What ``fetch_pr`` reads back on the next push: our last state marker."""
    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None
    fake.pr["head"]["sha"] = NEXT_HEAD
    return make_snap(tmp_path, prior=state, head_sha=NEXT_HEAD)


async def test_refused_resolve_replies_and_minimizes_once(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, comment, thread = setup_open_prior(fake, tmp_path)
    outcome = await run(fake, tmp_path, snap, fixed_run_final([resolved()]))

    replies = [c for c in fake.review_comments if c["in_reply_to_id"] == comment["id"]]
    assert len(replies) == 1
    assert replies[0]["body"].startswith("Looks addressed in `aaaaaaa`: the flagged code changed.")
    assert st.reply_ids_in(replies[0]["body"]) == [FID]
    assert fake.minimized == {comment["node_id"]}
    assert thread not in fake.resolved
    mutations = [p for _, p in fake.mutations]
    assert len(mutations) == 2  # one resolve attempt, one minimize

    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None
    assert state.refused_resolves == [thread]
    [recorded] = state.findings
    assert recorded.id == FID and recorded.state == "fixed"
    assert recorded.comment_id == comment["id"] and recorded.thread_id == thread
    assert not recorded.thread_resolved and recorded.resolved_by is None

    body = summary_comment(fake)["body"]
    section = body.split(RESOLVED_HEADING, 1)[1]
    assert f"`{PATH}`): code changed{UNCLOSED_SUFFIX}" in section
    assert body.count(TITLE) == 1
    assert UNCLOSED_NOTE in section
    assert any("could not be resolved automatically" in n for n in outcome.notes)
    assert any("Contents: write" in n for n in outcome.notes)


async def test_later_runs_do_not_repeat_the_fallback(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, comment, thread = setup_open_prior(fake, tmp_path)
    await run(fake, tmp_path, snap, fixed_run_final([resolved()]))
    writes, mutations = len(fake.writes), len(fake.mutations)

    # The next push: reconcile skips a prior recorded as fixed ...
    later = snap_from_summary(fake, tmp_path)
    assert later.prior is not None
    assert reconcile_priors(later.prior, [], lambda path: ["changed"]) == []
    # ... and even if a resolve action reached publish, nothing is written again.
    await run(fake, tmp_path, later, fixed_run_final([resolved()]))
    assert len(fake.mutations) == mutations  # no resolve, no minimize
    replies = [c for c in fake.review_comments if c["in_reply_to_id"] == comment["id"]]
    assert len(replies) == 1
    # Only the summary was rewritten; no reply, no review, no GraphQL mutation.
    assert {(m, p.rsplit("/", 1)[0]) for m, p in fake.writes[writes:]} == {
        ("PATCH", "/repos/octo-org/widgets/issues/comments")
    }
    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None and state.refused_resolves == [thread]


async def test_open_prior_with_a_refused_thread_is_left_alone(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    """A state that lists the prior as open but its thread as refused: no writes."""
    comment = fake.add_review_comment(PATH, 12, st.finding_marker(FID))
    thread = fake.thread_id_of(comment["id"])
    fake.add_review_comment(
        PATH, 12, "Looks addressed.\n" + st.reply_marker(FID), in_reply_to=comment["id"]
    )
    fake.minimized.add(comment["node_id"])
    prior = make_state(
        [base_prior(FID, title=TITLE, path=PATH, comment_id=comment["id"], thread_id=thread)],
        head_sha="e" * 40,
        refused_resolves=[thread],
    )
    snap = make_snap(tmp_path, prior=prior)
    await run(fake, tmp_path, snap, fixed_run_final([resolved()]))
    assert fake.mutations == []
    assert fake.posted("POST", "/replies") == []
    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None
    assert [(f.id, f.state) for f in state.findings] == [(FID, "fixed")]
    assert state.refused_resolves == [thread]
    assert UNCLOSED_NOTE in summary_comment(fake)["body"]


async def test_retry_of_the_same_run_does_not_reply_twice(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, comment, _ = setup_open_prior(fake, tmp_path)
    final = fixed_run_final([resolved(action="file_deleted")])
    await run(fake, tmp_path, snap, final)
    before = fake.snapshot()
    await run(fake, tmp_path, snap, final)
    assert fake.snapshot() == before
    replies = [c for c in fake.review_comments if c["in_reply_to_id"] == comment["id"]]
    assert len(replies) == 1 and replies[0]["body"].startswith("File removed.")
    body = summary_comment(fake)["body"]
    assert f"`{PATH}`): file deleted{UNCLOSED_SUFFIX}" in body


async def test_resolved_thread_is_not_recorded_as_unclosed(
    fake: FakeGitHub, tmp_path: Path
) -> None:
    fake.resolve_forbidden = False
    snap, _, thread = setup_open_prior(fake, tmp_path)
    outcome = await run(fake, tmp_path, snap, fixed_run_final([resolved()]))
    assert thread in fake.resolved and fake.minimized == set()
    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None and state.findings == [] and state.refused_resolves == []
    assert UNCLOSED_NOTE not in summary_comment(fake)["body"]
    assert not any("automatically" in n for n in outcome.notes)


def test_unclosed_notes_mark_the_rendered_lines() -> None:
    other = ResolvedPrior(id="f0000000000a2", title="Other", path="b.py", action="resolve", note="")
    summary = rendered_summary([resolved(), other])
    out = with_unclosed_notes(summary, [resolved()])
    lines = out.split("\n")
    marked = [ln for ln in lines if ln.endswith(UNCLOSED_SUFFIX)]
    assert len(marked) == 1 and TITLE in marked[0]
    assert sum(TITLE in ln for ln in lines) == 1  # render's own line was found and marked
    assert "- Other (`b.py`): code changed" in lines  # not marked
    start = lines.index(RESOLVED_HEADING)
    note = lines.index(UNCLOSED_NOTE)
    assert start < note < next(i for i, ln in enumerate(lines) if ln.startswith("#### Coverage"))
    assert with_unclosed_notes(summary, []) == summary


def test_unclosed_notes_add_missing_lines_and_section() -> None:
    # The section exists but the line was cut: the line is added inside the section.
    other = ResolvedPrior(id="f0000000000a2", title="Other", path="b.py", action="resolve", note="")
    summary = rendered_summary([other])
    out = with_unclosed_notes(summary, [resolved()])
    section = out.split(RESOLVED_HEADING, 1)[1].split("\n#### ", 1)[0]
    assert f"- {TITLE} (`{PATH}`): code changed{UNCLOSED_SUFFIX}" in section
    assert UNCLOSED_NOTE in section
    # No section at all: one is added before the footer.
    bare = rendered_summary([])
    assert RESOLVED_HEADING not in bare
    out = with_unclosed_notes(bare, [resolved(action="file_deleted")])
    assert out.index(RESOLVED_HEADING) < out.index("<sub>Automated review")
    assert f"(`{PATH}`): file deleted{UNCLOSED_SUFFIX}" in out
    # And at the end when there is no footer either.
    out = with_unclosed_notes("<!-- morning-star-review:summary v1 -->\nshort", [resolved()])
    assert out.endswith(UNCLOSED_NOTE) and RESOLVED_HEADING in out


async def test_full_state_keeps_open_findings_first(fake: FakeGitHub, tmp_path: Path) -> None:
    snap, _, thread = setup_open_prior(fake, tmp_path)
    full = make_state([base_prior(f"f{i:012x}", state="summary_only") for i in range(60)])
    final = fixed_run_final([resolved()]).model_copy(update={"state": full})
    await run(fake, tmp_path, snap, final)
    state = st.decode_state(summary_comment(fake)["body"])
    assert state is not None
    assert len(state.findings) == 60 and FID not in {f.id for f in state.findings}
    assert state.refused_resolves == [thread]  # still protects the thread
