"""The answer keys must agree with the fixture they describe."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from msreview_eval.build import build_repo, fixture_files, git, head_tree
from msreview_eval.cases import (
    CHANGES_DIR,
    FIXTURE_DIR,
    BugKey,
    CaseKey,
    CosmeticKey,
    load_cases,
    select_cases,
)
from msreview_eval.large_pr import large_pr_files

from morning_star_review.core.contract import PrFile, ResolvedConfig, ResolvedRoles
from morning_star_review.diff import prepass
from morning_star_review.diff.patch import local_patches
from morning_star_review.diff.prepass import PromptPlan


def _window(lines: list[str], span: tuple[int, int]) -> str:
    start, end = span
    assert 1 <= start <= end <= len(lines), f"range {span} outside a {len(lines)}-line file"
    return "\n".join(lines[start - 1 : end])


def test_all_cases_load_and_have_unique_branches(cases: dict[str, CaseKey]) -> None:
    assert set(cases) == {"e1", "e2", "e4", "e5", "e6"}
    branches = [c.pull_request.branch for c in cases.values()]
    assert len(branches) == len(set(branches))


def test_every_patch_is_referenced_and_exists(cases: dict[str, CaseKey]) -> None:
    used = {p for c in cases.values() for spec in c.build for p in spec.patches}
    on_disk = {p.name for p in CHANGES_DIR.glob("*.patch")}
    assert used == on_disk


def test_expected_text_is_inside_each_bug_range(
    cases: dict[str, CaseKey], heads: dict[str, dict[str, list[str]]]
) -> None:
    for cid, case in cases.items():
        head = heads[cid]
        keys: list[BugKey | CosmeticKey] = [*case.bugs, *case.fixed, *case.cosmetic]
        for key in keys:
            assert key.path in head, f"{cid}/{key.id}: {key.path} missing at head"
            window = _window(head[key.path], key.lines)
            if key.expect_text is not None:
                assert key.expect_text in window, f"{cid}/{key.id}: expect_text not in range"
        for bug in [*case.bugs, *case.fixed]:
            for loc in bug.alt_locations:
                _window(head[loc.path], loc.lines)
        for path in case.cosmetic_paths:
            assert path in head


def test_cross_file_callee_is_unchanged_by_the_pr(heads: dict[str, dict[str, list[str]]]) -> None:
    base = fixture_files()
    callee = "ledger/money.py"
    assert "\n".join(heads["e1"][callee]) + "\n" == base[callee].decode()
    assert (
        "def prorate(amount_cents: int, used_days: int, period_days: int)" in base[callee].decode()
    )


def test_e2_fix_keeps_carried_bug_lines(heads: dict[str, dict[str, list[str]]]) -> None:
    for path in ("ledger/service.py", "ledger/pagination.py"):
        assert heads["e1"][path] == heads["e2"][path]
    assert heads["e1"]["ledger/db.py"] != heads["e2"]["ledger/db.py"]


def test_default_eval_set_and_run_overrides(cases: dict[str, CaseKey]) -> None:
    assert [c.case for c in select_cases(cases, None)] == ["e1", "e4", "e5", "e6"]
    assert cases["e6"].eval.runs == 1
    assert cases["e2"].initial_stage == 1
    with pytest.raises(SystemExit):
        select_cases(cases, ["nope"])


def test_injection_strings_are_planted(
    cases: dict[str, CaseKey], heads: dict[str, dict[str, list[str]]]
) -> None:
    case = cases["e5"]
    planted = case.pull_request.body + "\n".join(heads["e5"]["ledger/db.py"])
    for text in case.expect.forbidden_output:
        assert text in case.pull_request.body
        assert text in planted
    for pattern in case.expect.forbidden_patterns:
        assert re.search(pattern, case.pull_request.body)
        assert re.search(pattern, "\n".join(heads["e5"]["ledger/db.py"]))


def test_large_pr_shape(heads: dict[str, dict[str, list[str]]]) -> None:
    base = set(fixture_files())
    added = set(heads["e6"]) - base
    assert 78 <= len(added) <= 82
    assert "uv.lock" in added
    assert any(p.startswith("vendor/") for p in added)
    generated = [p for p in added if p.startswith("ledger/generated/")]
    assert generated
    assert all("DO NOT EDIT" in heads["e6"][p][0] for p in generated)
    assert large_pr_files() == large_pr_files()  # deterministic


def _e6_prompt_plan(case: CaseKey, tmp_path: Path) -> PromptPlan:
    """Plan E6 with the real diff pre-pass (DESIGN 7.3) and the default budgets."""
    repo = tmp_path / "repo"
    base, commits = build_repo(repo, [case], "main")
    head = commits[case.case][-1]
    git(repo, "checkout", "-q", head)
    files: list[PrFile] = []
    for row in git(repo, "diff", "--numstat", base, head).splitlines():
        added, deleted, path = row.split("\t")
        files.append(
            PrFile(
                path=path,
                status="added",
                additions=int(added),
                deletions=int(deleted),
                has_patch=True,
            )
        )
    patches = local_patches(str(repo), base, head, [f.path for f in files])
    first_lines = {
        f.path: "\n".join((repo / f.path).read_text(encoding="utf-8").splitlines()[:3])
        for f in files
    }
    roles = ResolvedRoles(
        explorer_model="zai-glm-5-3",
        explorer_effort="high",
        formatter_model="mistral-large-4",
        formatter_effort="low",
    )
    return prepass.plan(files, patches, ResolvedConfig(roles=roles), first_lines=first_lines)


def test_large_pr_bug_file_falls_outside_the_inline_budget(
    cases: dict[str, CaseKey], tmp_path: Path
) -> None:
    case = cases["e6"]
    plan = _e6_prompt_plan(case, tmp_path)
    bug_path = case.bugs[0].path
    assert bug_path in case.expect.on_demand
    assert bug_path in plan.on_demand, "the E6 bug file must not be inlined"
    assert bug_path not in plan.inlined
    excluded = {e.path: e.reason for e in plan.excluded}
    assert excluded["uv.lock"] == "lock_file"
    # E6 adds its generated tables, so no merge-base version vouches for their
    # markers: they stay reviewable (on demand), never excluded as "generated".
    generated = [p for p in plan.line_counts if p.startswith("ledger/generated/")]
    assert generated
    assert not any(p in excluded for p in generated)
    assert set(generated) <= set(plan.inlined) | set(plan.on_demand)
    assert plan.partial_reason is None  # in scope: the file is reviewable on demand


@pytest.mark.parametrize("case_id", [None, "e1", "e4", "e5", "e6"])
def test_ledger_test_suite(case_id: str | None, tmp_path: Path) -> None:
    """The fixture's own tests pass at base and at every head (bugs are untested)."""
    if case_id is None:
        files: dict[str, str | bytes] = dict(fixture_files(FIXTURE_DIR))
    else:
        files = dict(head_tree(load_cases()[case_id]))
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:asyncio"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=110,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def test_fixture_copy_skips_caches_and_os_files(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"x")
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    assert sorted(fixture_files(tmp_path)) == [".gitignore", "pkg/m.py"]
