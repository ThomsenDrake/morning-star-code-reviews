"""A pull request with no reviewable file runs no model (G5: zero reviewable files).

When every changed file is excluded (lock files, generated files, binaries, ...), the
workflow skips explore, structure, merge and verify, so no explorer reads (and reports
on) excluded or unrelated files and no model cost is spent.
"""

from __future__ import annotations

import pytest
from harness import Harness
from wfkit import AUTHZ, BASE_FILES, SQLI, compare_body, git, notes_for, point_fake_at

from morning_star_review.activities.prepare import nothing_to_review
from morning_star_review.core.contract import ExcludedFile
from morning_star_review.workflow import needs_models

pytestmark = pytest.mark.timeout(120)

LOCKS = {
    "poetry.lock": '[[package]]\nname = "requests"\nversion = "2.33.0"\n',
    "web/package-lock.json": '{\n  "name": "web",\n  "lockfileVersion": 3\n}\n',
}


def _lock_only_pr(h: Harness) -> None:
    """Point the PR at a branch off ``main`` that only changes lock files."""
    repo = h.repo
    git(repo.path, "checkout", "-q", "main")
    git(repo.path, "checkout", "-q", "-b", "locks")
    repo.base = git(repo.path, "rev-parse", "HEAD")
    repo.head = repo.base
    repo.commit(LOCKS, "bump the lock files")
    point_fake_at(h.fake, repo)


@pytest.mark.parametrize("preset", ["single", "best"])
async def test_no_model_runs_when_every_file_is_excluded(harness: Harness, preset: str) -> None:
    h = harness
    _lock_only_pr(h)
    # A stub explorer that would report a finding on a file the PR does not change.
    h.spec("explore", notes=notes_for(h.repo, SQLI), opened_paths=["app/db.py"])
    out = await h.run(h.request(preset=preset))

    assert out.status == "posted", out.reason
    assert h.calls() == []  # no explore, format or verify phase ran
    assert out.findings == []
    assert not any(h.run_dir.glob("explore-*")) and not (h.run_dir / "verify").exists()
    assert (h.run_dir / "prompt.txt").read_text(encoding="utf-8") == ""
    report = out.report_markdown or ""
    assert "Excluded (2)" in report
    assert out.usage == {} and out.cost_usd == 0
    assert h.fake.review_comments == []  # nothing inline


async def test_a_pr_reverted_to_lock_files_is_not_carried_over(harness: Harness) -> None:
    """A complete earlier review must not be carried over once nothing is reviewable.

    The PR first changed source files (reviewed completely, two findings); a later push
    reverts them and only bumps a lock file. A carry-over would keep the old verdict
    and findings for files the PR no longer changes; the review is decided afresh.
    """
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=["app/db.py"])
    first = await h.run()
    assert first.status == "posted" and first.verdict == "findings", first.reason
    h.reset_calls()
    prior = h.repo.head
    h.repo.commit({**BASE_FILES, "poetry.lock": LOCKS["poetry.lock"]}, "revert; bump a lock")
    point_fake_at(h.fake, h.repo)
    h.fake.compare[f"{prior}...{h.repo.head}"] = compare_body(prior)
    out = await h.run()
    assert out.status == "posted", out.reason
    assert h.calls() == []
    headline = (out.report_markdown or "").splitlines()[1]
    assert headline.endswith("· full review"), headline  # not "no reviewable changes since"


def test_needs_models() -> None:
    class P:
        def __init__(self, scope: str, paths: list[str]) -> None:
            self.scope = scope
            self.reviewable_paths = paths

    assert needs_models(P("full", ["a.py"]))  # type: ignore[arg-type]
    assert needs_models(P("incremental", ["a.py"]))  # type: ignore[arg-type]
    assert not needs_models(P("full", []))  # type: ignore[arg-type]
    assert not needs_models(P("carry_over", ["a.py"]))  # type: ignore[arg-type]


def _ex(path: str, reason: str) -> ExcludedFile:
    return ExcludedFile(path=path, reason=reason, changed_lines=3)


def test_nothing_to_review_reason() -> None:
    assert nothing_to_review([]) == "the pull request changes no files"
    assert nothing_to_review([_ex("poetry.lock", "lock_file")]) == (
        "the only changed file is excluded from review (lock file)"
    )
    five = [_ex(f"gen/m{i}.py", "generated") for i in range(5)]
    assert nothing_to_review(five) == "all 5 changed files are generated"
    mixed = [*five[:3], _ex("a.lock", "lock_file"), _ex("b.lock", "lock_file")]
    mixed.append(_ex("x.cfg", "repository_config: *.cfg"))
    assert nothing_to_review(mixed) == (
        "all 6 changed files are excluded from review "
        "(3 generated, 2 lock files, 1 excluded by the repository config)"
    )


async def test_unreviewed_lock_files_are_not_called_clean(harness: Harness) -> None:
    # Security review EXCL-1: lock files can change what runs; nothing was reviewed.
    h = harness
    _lock_only_pr(h)
    out = await h.run()
    assert out.verdict == "incomplete"
    report = out.report_markdown or ""
    assert "all 2 changed files are lock files" in report
    assert "can change what runs" in report


async def test_verdict_is_clean_with_the_reason(harness: Harness) -> None:
    h = harness
    repo = h.repo
    git(repo.path, "checkout", "-q", "main")
    git(repo.path, "checkout", "-q", "-b", "snapshots")
    repo.base = git(repo.path, "rev-parse", "HEAD")
    repo.head = repo.base
    repo.commit({"web/__snapshots__/app.test.js.snap": "exports[`app 1`] = `<div />`;\n"}, "snap")
    point_fake_at(h.fake, repo)
    out = await h.run()
    assert out.verdict == "clean"
    assert "the only changed file is excluded from review (snapshot)" in (out.report_markdown or "")
