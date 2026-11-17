"""Re-reviews: incremental scope with priors, and carry-over without a model call."""

from __future__ import annotations

import json

import pytest
from fakegh import BOT
from harness import Harness
from wfkit import AUTHZ, SQLI, compare_body, notes_for, point_fake_at

pytestmark = pytest.mark.timeout(120)

# The injection fixed in a way that keeps app/db.py changed by the PR.
FIXED_DB = (
    '"""Database access."""\n'
    "\n"
    "\n"
    "def find_user(conn, name):\n"
    '    """Look a user up by name."""\n'
    '    query = "SELECT * FROM users WHERE name = ?"\n'
    "    return conn.execute(query, (name,)).fetchone()\n"
)


async def _first_review(h: Harness) -> dict[str, str]:
    """Post a review with two findings; return title -> finding id."""
    h.spec("explore", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=["app/db.py"])
    out = await h.run()
    assert out.status == "posted", out.reason
    assert len(h.fake.review_comments) == 2
    h.reset_calls()
    return {f.title: f.id for f in out.findings}


def _push(h: Harness, files: dict[str, str], message: str) -> str:
    """A new head commit on the PR branch, and the fake GitHub moved along with it."""
    prior = h.repo.head
    h.repo.commit(files, message)
    point_fake_at(h.fake, h.repo)
    h.fake.compare[f"{prior}...{h.repo.head}"] = compare_body(prior)
    return prior


async def test_incremental_review_resolves_the_fixed_prior_and_carries_the_other(
    harness: Harness,
) -> None:
    h = harness
    ids = await _first_review(h)
    sqli_id, authz_id = ids[SQLI[2]], ids[AUTHZ[2]]
    prior_head = _push(h, {"app/db.py": FIXED_DB}, "fix the injection")

    priors = (
        f"PRIOR {sqli_id}: fixed - the query uses a bound parameter now\n"
        f"PRIOR {authz_id}: still_open - not touched\n"
    )
    h.spec("explore", notes=notes_for(h.repo, priors=priors), opened_paths=["app/db.py"])
    out = await h.run()
    assert out.status == "posted", out.reason

    job = json.loads((h.run_dir / "explore-zai-glm-5-3" / "job.json").read_text())
    assert job["variant"] == "incremental"
    prompt = (h.run_dir / "prompt.txt").read_text()
    assert prior_head[:7] in prompt
    assert sqli_id in prompt and authz_id in prompt

    # No duplicate inline comment for the carried finding; one reply for the fixed one.
    top_level = [c for c in h.fake.review_comments if c["in_reply_to_id"] is None]
    assert len(top_level) == 2
    replies = [c for c in h.fake.review_comments if c["in_reply_to_id"] is not None]
    assert len(replies) == 1
    assert sqli_id in replies[0]["body"]
    # Resolving is refused to Apps by GitHub, so the fixed comment is minimized instead.
    assert h.fake.minimized
    carried = [f for f in out.findings if f.id == authz_id]
    assert carried and carried[0].carried
    summaries = [c for c in h.fake.issue_comments if c["user"]["login"] == BOT]
    assert len(summaries) == 1


async def test_carry_over_makes_no_model_call(harness: Harness) -> None:
    h = harness
    await _first_review(h)
    _push(h, {"uv.lock": "version = 2\n"}, "lock file only")
    out = await h.run()

    assert out.status == "posted", out.reason
    assert h.calls() == []
    assert not (h.run_dir / "explore-zai-glm-5-3").exists()
    assert (h.run_dir / "prompt.txt").read_text() == ""
    assert {f.title for f in out.findings} == {SQLI[2], AUTHZ[2]}
    assert all(f.carried for f in out.findings)
    assert len([c for c in h.fake.review_comments if c["in_reply_to_id"] is None]) == 2


async def test_force_gives_a_full_review(harness: Harness) -> None:
    h = harness
    await _first_review(h)
    _push(h, {"app/db.py": FIXED_DB}, "fix the injection")
    h.spec("explore", notes=notes_for(h.repo, AUTHZ), opened_paths=["app/db.py"])
    out = await h.run(h.request(force=True))

    assert out.status == "posted"
    job = json.loads((h.run_dir / "explore-zai-glm-5-3" / "job.json").read_text())
    assert job["variant"] == "full"
