#!/usr/bin/env python3
"""Create the evaluation PRs in a GitHub repository you own.

For the repository given with ``--repo owner/name`` this script:

1. builds the ledger fixture as a base commit and one branch per case from
   ``eval/cases/*.yaml`` in a temporary local repository; every commit is
   deterministic (fixed author, dates and messages), so re-running produces
   identical commit ids;
2. pushes the base branch (``msreview-eval/base`` by default) and the case
   branches (``msreview-eval/<case>``) with ``git push``, authenticated by
   ``gh auth git-credential``; a branch that is already at the wanted commit
   (or ahead of it on the same deterministic chain) is left alone, and a branch
   that diverged is only overwritten with ``--force``;
3. opens one PR per case with ``gh pr create`` unless an open PR for the branch
   already exists;
4. prints ``{"repo", "base_branch", "base_sha", "cases": {id: {number, url,
   branch, head_sha, ...}}}`` as JSON on stdout (the input of
   ``scripts/eval.py run --prs``).

It only writes branches whose names start with ``msreview-eval/``. Use a
throw-away or private repository: the PRs contain deliberately buggy code.

    python scripts/make_test_prs.py --repo you/review-playground > prs.json
    python scripts/make_test_prs.py --repo you/review-playground --cases e2 --stage e2=2
    python scripts/make_test_prs.py --repo you/review-playground --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from msreview_eval.build import build_repo, git, git_env
from msreview_eval.cases import CaseKey, load_cases

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
BRANCH_PREFIX = "msreview-eval/"
DEFAULT_BASE = "msreview-eval/base"


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class BranchPlan:
    branch: str
    want: str  # commit id to publish
    chain: tuple[str, ...]  # every deterministic commit of this branch, oldest first
    remote: str | None = None  # current remote commit id (None = branch missing)

    @property
    def action(self) -> str:
        """create | unchanged | ahead | fast_forward | diverged."""
        if self.remote is None:
            return "create"
        if self.remote == self.want:
            return "unchanged"
        if self.remote in self.chain:
            position = self.chain.index(self.remote)
            return "ahead" if position > self.chain.index(self.want) else "fast_forward"
        return "diverged"

    @property
    def needs_push(self) -> bool:
        return self.action in ("create", "fast_forward", "diverged")


class GhCli:
    """The few ``gh`` calls this script needs."""

    def __init__(self, gh: str) -> None:
        self.gh = gh

    def _run(self, args: Sequence[str], stdin: str | None = None) -> str:
        proc = subprocess.run(
            [self.gh, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise PlanError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def open_pr(self, repo: str, base: str, branch: str) -> dict[str, Any] | None:
        out = self._run(
            [
                "pr", "list", "--repo", repo, "--head", branch, "--base", base,
                "--state", "open", "--json", "number,url,headRefOid",
            ]
        )  # fmt: skip
        prs = json.loads(out or "[]")
        return prs[0] if prs else None

    def create_pr(self, repo: str, base: str, branch: str, title: str, body: str) -> str:
        out = self._run(
            [
                "pr", "create", "--repo", repo, "--base", base, "--head", branch,
                "--title", title, "--body-file", "-",
            ],
            stdin=body,
        )  # fmt: skip
        return out.strip().splitlines()[-1] if out.strip() else ""


def parse_stages(values: Sequence[str], cases: Mapping[str, CaseKey]) -> dict[str, int]:
    stages: dict[str, int] = {}
    for value in values:
        name, sep, number = value.partition("=")
        if not sep or not number.isdigit() or name not in cases:
            raise SystemExit(f"bad --stage {value!r}: use <case>=<number of commits>")
        stage = int(number)
        if not 1 <= stage <= len(cases[name].build):
            raise SystemExit(f"--stage {value}: case {name} has {len(cases[name].build)} commits")
        stages[name] = stage
    return stages


def pr_number(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", url)
    return int(match.group(1)) if match else None


def _remote_git_args(gh: str) -> list[str]:
    # Reset any inherited helper, then let gh supply the token for github.com.
    # git runs the helper through a shell, so quote the path (it may contain spaces).
    helper = f"!{shlex.quote(gh)} auth git-credential"
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]


def remote_heads(local: Path, url: str, branches: Sequence[str], gh: str) -> dict[str, str]:
    """Current commit of each branch on the remote (missing branches are absent)."""
    refs = [f"refs/heads/{b}" for b in branches]
    out = git(local, *_remote_git_args(gh), "ls-remote", url, *refs)
    heads: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def push(local: Path, url: str, plans: Sequence[BranchPlan], gh: str, force: bool) -> None:
    todo = [p for p in plans if p.needs_push]
    if not todo:
        return
    args = [*_remote_git_args(gh), "push", "--porcelain", url]
    for plan in todo:
        if plan.action == "diverged":
            if not force:
                raise PlanError(f"{plan.branch} diverged on the remote; use --force")
            args.append(f"--force-with-lease=refs/heads/{plan.branch}:{plan.remote}")
            args.append(f"+{plan.want}:refs/heads/{plan.branch}")
        else:
            args.append(f"{plan.want}:refs/heads/{plan.branch}")
    git(local, *args, env=git_env())


def make_plans(
    base_branch: str,
    base_sha: str,
    cases: Sequence[CaseKey],
    commits: Mapping[str, Sequence[str]],
    stages: Mapping[str, int],
    remote: Mapping[str, str],
) -> tuple[BranchPlan, list[BranchPlan]]:
    base = BranchPlan(base_branch, base_sha, (base_sha,), remote.get(base_branch))
    plans: list[BranchPlan] = []
    for case in cases:
        chain = tuple(commits[case.case])
        stage = stages.get(case.case, case.initial_stage)
        branch = case.pull_request.branch
        plans.append(BranchPlan(branch, chain[stage - 1], chain, remote.get(branch)))
    return base, plans


def check_plans(plans: Sequence[BranchPlan], force: bool) -> None:
    for plan in plans:
        if not plan.branch.startswith(BRANCH_PREFIX):
            raise PlanError(f"refusing to write branch {plan.branch!r} (not {BRANCH_PREFIX}*)")
        if plan.action == "diverged" and not force:
            raise PlanError(
                f"{plan.branch} is at {plan.remote} on the remote, which is not one of"
                " the commits this script builds; re-run with --force to overwrite it"
            )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not REPO_RE.match(args.repo):
        raise SystemExit(f"--repo must look like owner/name, got {args.repo!r}")
    if not args.base_branch.startswith(BRANCH_PREFIX):
        raise SystemExit(f"--base-branch must start with {BRANCH_PREFIX}")
    all_cases = load_cases()
    wanted = [c.strip() for c in (args.cases or "").split(",") if c.strip()]
    unknown = [c for c in wanted if c not in all_cases]
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(unknown)}")
    cases = [all_cases[c] for c in wanted] if wanted else list(all_cases.values())
    stages = parse_stages(args.stage, all_cases)
    url = args.remote_url or f"https://github.com/{args.repo}.git"

    with tempfile.TemporaryDirectory(prefix="msreview-prs-") as tmp:
        local = Path(tmp) / "repo"
        # Build every commit of every case; stages only pick which one to publish.
        base_sha, commits = build_repo(local, cases, args.base_branch)
        branches = [args.base_branch, *(c.pull_request.branch for c in cases)]
        remote = {} if args.dry_run else remote_heads(local, url, branches, args.gh)
        base_plan, plans = make_plans(args.base_branch, base_sha, cases, commits, stages, remote)
        check_plans([base_plan, *plans], args.force)
        result: dict[str, Any] = {
            "repo": args.repo,
            "base_branch": args.base_branch,
            "base_sha": base_sha,
            "dry_run": bool(args.dry_run),
            "base_action": base_plan.action,
            "cases": {},
        }
        if not args.dry_run:
            push(local, url, [base_plan, *plans], args.gh, args.force)
        gh = GhCli(args.gh)
        for case, plan in zip(cases, plans, strict=True):
            # A branch that is ahead stays where it is: report its real head and stage.
            head_sha = plan.remote if plan.action == "ahead" and plan.remote else plan.want
            entry: dict[str, Any] = {
                "branch": plan.branch,
                "head_sha": head_sha,
                "stage": plan.chain.index(head_sha) + 1,
                "commits": len(plan.chain),
                "branch_action": plan.action,
                "number": None,
                "url": None,
            }
            if not args.dry_run:
                entry.update(_ensure_pr(gh, args.repo, args.base_branch, case))
            result["cases"][case.case] = entry
        return result


def _ensure_pr(gh: GhCli, repo: str, base: str, case: CaseKey) -> dict[str, Any]:
    existing = gh.open_pr(repo, base, case.pull_request.branch)
    if existing is not None:
        return {"number": existing.get("number"), "url": existing.get("url"), "pr": "existing"}
    pr = case.pull_request
    url = gh.create_pr(repo, base, pr.branch, pr.title, pr.body)
    return {"number": pr_number(url), "url": url or None, "pr": "created"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the msreview evaluation PRs in a repository (idempotent)."
    )
    parser.add_argument("--repo", required=True, help="owner/name of a repository you own")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--base-branch", default=DEFAULT_BASE)
    parser.add_argument(
        "--stage", action="append", default=[], help="<case>=<n>: publish the first n commits"
    )
    parser.add_argument("--force", action="store_true", help="overwrite diverged branches")
    parser.add_argument("--dry-run", action="store_true", help="build locally; no network")
    parser.add_argument("--gh", default=shutil.which("gh") or "gh", help="gh executable")
    parser.add_argument("--remote-url", help=argparse.SUPPRESS)  # tests: a local bare repo
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(args)
    except PlanError as exc:
        print(f"make_test_prs: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
