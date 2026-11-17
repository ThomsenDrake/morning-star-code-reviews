"""Build the file tree and the git commits of each evaluation case.

Commits are deterministic: fixed author, committer and dates, no signing and
no user git configuration. Building the same case twice therefore yields the
same commit ids, which is what makes ``make_test_prs.py`` idempotent.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from msreview_eval.cases import CHANGES_DIR, FIXTURE_DIR, CaseKey, CommitSpec
from msreview_eval.large_pr import large_pr_files

GENERATORS: dict[str, Callable[[], dict[str, str]]] = {"large_pr": large_pr_files}

AUTHOR_NAME = "msreview eval"
AUTHOR_EMAIL = "eval@example.invalid"
BASE_DATE = 1_767_225_600  # 2026-01-01T00:00:00Z; commit n is dated BASE_DATE + n minutes
BASE_MESSAGE = "Ledger service (evaluation fixture)"

# Never copied from the fixture directory into a case tree.
_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_SKIP_NAMES = {".DS_Store", "Thumbs.db"}


class BuildError(RuntimeError):
    pass


def fixture_files(root: Path = FIXTURE_DIR) -> dict[str, bytes]:
    """The ledger fixture as ``{relative path: bytes}``, without caches."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or _SKIP_PARTS.intersection(rel.parts):
            continue
        if rel.name.endswith(_SKIP_SUFFIXES) or rel.name in _SKIP_NAMES:
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def git_env(date_offset_minutes: int = 0) -> dict[str, str]:
    """Environment for reproducible git commands, isolated from user configuration."""
    stamp = f"{BASE_DATE + 60 * date_offset_minutes} +0000"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME=AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
        GIT_AUTHOR_DATE=stamp,
        GIT_COMMITTER_NAME=AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
        GIT_COMMITTER_DATE=stamp,
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def git(repo: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=dict(env) if env is not None else git_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write_files(root: Path, files: Mapping[str, str | bytes]) -> None:
    for rel, content in files.items():
        target = root / rel
        if root.resolve() not in target.resolve().parents:
            raise BuildError(f"refusing to write outside the tree: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_bytes(content)


def apply_commit_spec(root: Path, spec: CommitSpec, changes_dir: Path = CHANGES_DIR) -> None:
    """Apply one commit's generators, then its patches, to the files under ``root``."""
    for name in spec.generators:
        write_files(root, GENERATORS[name]())
    for patch in spec.patches:
        patch_path = changes_dir / patch
        if not patch_path.is_file():
            raise BuildError(f"missing patch {patch_path}")
        # --ignore-whitespace keeps patches usable if an editor strips trailing blanks.
        git(root, "apply", "--ignore-whitespace", "--whitespace=nowarn", str(patch_path))


def head_tree(case: CaseKey, upto: int | None = None) -> dict[str, str]:
    """Text files of the case head (after ``upto`` commits; all by default)."""
    with tempfile.TemporaryDirectory(prefix="msreview-eval-") as tmp:
        root = Path(tmp)
        git(root, "init", "-q")  # so that `git apply` never resolves an enclosing repo
        write_files(root, fixture_files())
        for spec in case.build[: upto if upto is not None else len(case.build)]:
            apply_commit_spec(root, spec)
        out: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and ".git" not in path.relative_to(root).parts:
                try:
                    out[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return out


def _commit_all(repo: Path, message: str, offset: int) -> str:
    env = git_env(offset)
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD", env=env)


def build_repo(
    repo: Path,
    cases: list[CaseKey],
    base_branch: str,
    stages: Mapping[str, int] | None = None,
    fixture: Path = FIXTURE_DIR,
) -> tuple[str, dict[str, list[str]]]:
    """Create ``repo`` with a base commit and one branch per case.

    Returns the base commit id and, per case, the commit ids of its branch
    (oldest first). ``stages`` limits a case to its first N commits (E2 is
    pushed in two steps). Local branch names equal the PR branch names.
    """
    stages = stages or {}
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", base_branch)
    write_files(repo, fixture_files(fixture))
    base_sha = _commit_all(repo, BASE_MESSAGE, 0)
    commits: dict[str, list[str]] = {}
    for case in cases:
        upto = stages.get(case.case, len(case.build))
        if not 1 <= upto <= len(case.build):
            raise BuildError(f"stage {upto} is out of range for case {case.case}")
        git(repo, "checkout", "-q", "-B", case.pull_request.branch, base_sha)
        shas: list[str] = []
        for index, spec in enumerate(case.build[:upto], start=1):
            apply_commit_spec(repo, spec)
            shas.append(_commit_all(repo, spec.message, index))
        commits[case.case] = shas
    git(repo, "checkout", "-q", base_branch)
    return base_sha, commits
