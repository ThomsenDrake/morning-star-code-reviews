"""Shared fixtures for the diff tests: local git repositories and an E1-like PR."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from morning_star_review.core.contract import (
    PrFile,
    PrSnapshot,
    ResolvedConfig,
    ResolvedRoles,
)
from morning_star_review.diff.patch import FilePatch, parse_patch

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "patches"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_GIT_ENV}
    res = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)
    return res.stdout.decode("utf-8", "replace")


def write_tree(repo: Path, tree: Mapping[str, str | bytes]) -> None:
    """Make the working tree exactly ``tree`` (except .git)."""
    for p in sorted(repo.rglob("*"), reverse=True):
        if ".git" in p.relative_to(repo).parts:
            continue
        if p.is_file() or p.is_symlink():
            p.unlink()
        elif p.is_dir():
            p.rmdir()
    for rel, content in tree.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            f.write_bytes(content)
        else:
            f.write_bytes(content.encode("utf-8"))


def commit_tree(repo: Path, tree: Mapping[str, str | bytes], message: str) -> str:
    write_tree(repo, tree)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


RepoFactory = Callable[..., tuple[Path, list[str]]]


@pytest.fixture
def make_repo(tmp_path: Path) -> RepoFactory:
    """``make_repo(tree1, tree2, ...) -> (repo_dir, [sha1, sha2, ...])``; HEAD is the last."""

    def factory(*trees: Mapping[str, str | bytes], name: str = "repo") -> tuple[Path, list[str]]:
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        shas = [commit_tree(repo, t, f"c{i}") for i, t in enumerate(trees)]
        return repo, shas

    return factory


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def e1_pr_files() -> list[dict[str, Any]]:
    return list(load_json("e1_pr_files.json"))


def e1_files() -> list[PrFile]:
    return [
        PrFile(
            path=f["path"],
            previous_path=f["previous_path"],
            status=f["status"],
            additions=f["additions"],
            deletions=f["deletions"],
            has_patch=f["has_patch"],
            binary=f["binary"],
        )
        for f in e1_pr_files()
    ]


def e1_patches() -> dict[str, FilePatch]:
    return {
        f["path"]: parse_patch(f["path"], f["patch"], f["status"], f["previous_path"])
        for f in e1_pr_files()
        if f["patch"] is not None
    }


def e1_trees() -> tuple[dict[str, str], dict[str, str]]:
    trees = load_json("e1_trees.json")
    return dict(trees["base"]), dict(trees["head"])


def make_cfg(**kw: Any) -> ResolvedConfig:
    roles = ResolvedRoles(
        explorer_model="zai-glm-5-3",
        explorer_effort="high",
        formatter_model="mistral-large-4",
        formatter_effort="low",
    )
    return ResolvedConfig(roles=roles, **kw)


def make_snapshot(files: list[PrFile], cfg: ResolvedConfig, **kw: Any) -> PrSnapshot:
    data: dict[str, Any] = {
        "owner": "octo",
        "repo": "ledger",
        "number": 7,
        "html_url": "https://github.com/octo/ledger/pull/7",
        "title": "Add partial charges",
        "body_excerpt": "Adds prorated charges for partial periods.",
        "author": "dev1",
        "author_association": "CONTRIBUTOR",
        "draft": False,
        "state": "open",
        "merged": False,
        "head_sha": "b" * 40,
        "head_repo": "octo/ledger",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "merge_base_sha": "a" * 40,
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
        "changed_files": len(files),
        "files": files,
        "files_json_sha256": "0" * 64,
        "can_post": True,
        "bot_login": "morning-star-code-reviews[bot]",
        "config": cfg,
        "config_source": "default",
        "run_dir": "/tmp/run",
    }
    data.update(kw)
    return PrSnapshot(**data)


@pytest.fixture
def h() -> SimpleNamespace:
    """Helper functions for the tests (test modules cannot import conftest by name)."""
    return SimpleNamespace(
        git=git,
        commit_tree=commit_tree,
        write_tree=write_tree,
        load_json=load_json,
        e1_files=e1_files,
        e1_patches=e1_patches,
        e1_trees=e1_trees,
        e1_pr_files=e1_pr_files,
        make_cfg=make_cfg,
        make_snapshot=make_snapshot,
        fixtures=FIXTURES,
    )
