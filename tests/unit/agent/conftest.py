"""Fixtures for agent tool tests: a git repository with base and head commits.

Agent modules are imported inside the fixtures: the worker venv loads this conftest
even though it skips the tests in this directory.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from morning_star_review.core.contract import AgentBudgets, AgentJob, ChangedFileRef

if TYPE_CHECKING:
    from morning_star_review.agent.tools import ToolContext


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LANG": "C.UTF-8",
    }
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
    res = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def budgets(**overrides: object) -> AgentBudgets:
    values: dict[str, object] = {
        "max_tool_calls": 10,
        "max_llm_calls": 12,
        "grace_llm_calls": 2,
        "explore_seconds": 300,
        "grace_seconds": 60,
        "rescue_seconds": 60,
        "max_context_tokens": 140_000,
        "auto_compact_threshold": 200_000,
        "max_tool_output_chars": 24_000,
        "max_cost_usd": 5.0,
    }
    values.update(overrides)
    return AgentBudgets.model_validate(values)


@dataclass
class Repo:
    root: Path
    repo: Path
    base: str
    head: str
    job_dir: Path

    def job(self, **budget_overrides: object) -> AgentJob:
        return AgentJob(
            phase="explore",
            execution_id="msreview-test-1",
            repo_dir=str(self.repo),
            head_sha=self.head,
            merge_base_sha=self.base,
            input_path="prompt.txt",
            input_sha256="0" * 64,
            variant="full",
            model="mistral-large-4",
            effort="default",
            budgets=budgets(**budget_overrides),
            price_in_per_m=0.70,
            price_out_per_m=2.10,
            changed_files=[
                ChangedFileRef(
                    path="pkg/new_name.py",
                    previous_path="pkg/old_name.py",
                    status="renamed",
                    diff_file="diffs/0.txt",
                ),
                ChangedFileRef(
                    path="pkg/big.py",
                    previous_path=None,
                    status="modified",
                    diff_file="diffs/1.txt",
                ),
                ChangedFileRef(path="img.bin", previous_path=None, status="added", diff_file=None),
            ],
            meter_path="meter.json",
            coverage_path="coverage.json",
            result_path="result.json",
            partial_notes_path="notes.partial.txt",
        )

    def configure(self, **budget_overrides: object) -> ToolContext:
        from morning_star_review.agent import tools

        return tools.configure(
            self.job(**budget_overrides), self.job_dir, self.job_dir / "coverage.json"
        )


@pytest.fixture(scope="session")
def _repo_dirs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str, str]:
    root = tmp_path_factory.mktemp("toolrepo")
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg" / "old_name.py").write_text("def old():\n    return 1\n")
    (repo / "pkg" / "big.py").write_text("".join(f"line_{n} = {n}\n" for n in range(1, 1001)))
    (repo / "README.md").write_text("-v flag\nhello world\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "pkg/old_name.py", "pkg/new_name.py")
    (repo / "pkg" / "new_name.py").write_text("def old():\n    return 2\n")
    (repo / "img.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / "empty.txt").write_text("")
    (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    # Symlinks that try to leave the checkout or reach .git (not committed).
    (repo / "link_etc").symlink_to("/etc")
    (repo / "link_hosts").symlink_to("/etc/hosts")
    (repo / "link_git").symlink_to(repo / ".git" / "config")
    (repo / "link_up").symlink_to("..")
    return root, repo, base, head


@pytest.fixture
def repo(tmp_path: Path, _repo_dirs: tuple[Path, Path, str, str]) -> Iterator[Repo]:
    root, repo_dir, base, head = _repo_dirs
    job_dir = tmp_path / "job"
    (job_dir / "diffs").mkdir(parents=True)
    (job_dir / "diffs" / "0.txt").write_text(
        "=== pkg/new_name.py (renamed)\n    2 +     return 2\n"
    )
    big = "".join(f"  {n:>4} + line_{n} = {n}\n" for n in range(1, 3001))
    (job_dir / "diffs" / "1.txt").write_text("=== pkg/big.py (modified)\n" + big)
    yield Repo(root=root, repo=repo_dir, base=base, head=head, job_dir=job_dir)
    from morning_star_review.agent import hooks, tools

    tools._CTX = None
    hooks.install_meter(None)
