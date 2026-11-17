"""Checkout against a local bare repository served via file:// (DESIGN 7.1)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from morning_star_review.core.errors import MsreviewGitError
from morning_star_review.gitops import (
    CheckoutSpec,
    GitAuth,
    checkout,
    diff_names,
    has_commit,
    head_first_lines,
    show_file,
)

TOKEN = "ghs_FAKEtoken0123456789abcdefABCDEF"
_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **_ENV}, capture_output=True, check=True
    )
    return res.stdout.decode().strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def remote(tmp_path: Path) -> dict[str, str]:
    """A bare repo with base, head (on a PR-like ref) and an orphaned commit."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    _write(src, {"a.py": "# @generated\nx = 1\n", "lib/b.py": "def b():\n    return 1\n"})
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "base")
    base = git(src, "rev-parse", "HEAD")
    git(src, "checkout", "-q", "-b", "feature")
    _write(src, {"lib/b.py": "def b():\n    return 2\n", "c.py": "print('c')\n"})
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "head")
    head = git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    git(bare, "update-ref", "refs/pull/1/head", head)
    return {"url": bare.as_uri(), "base": base, "head": head, "tmp": str(tmp_path)}


def _spec(remote: dict[str, str], repo_dir: Path, **kw) -> CheckoutSpec:
    return CheckoutSpec(
        repo_dir=str(repo_dir),
        clone_url=kw.pop("url", remote["url"]),
        shas=[remote["head"], remote["base"]],
        head_sha=kw.pop("head", remote["head"]),
        **kw,
    )


def _all_git_bytes(repo_dir: Path) -> bytes:
    out = b""
    for p in (repo_dir / ".git").rglob("*"):
        if p.is_file():
            out += p.read_bytes()
    return out


def test_checkout_pins_head_and_persists_no_credentials(remote, tmp_path) -> None:
    beats: list[int] = []
    repo = tmp_path / "run" / "repo"
    checkout(_spec(remote, repo), GitAuth(TOKEN), lambda: beats.append(1))
    assert git(repo, "rev-parse", "HEAD") == remote["head"]
    assert (repo / "c.py").read_text() == "print('c')\n"
    assert has_commit(str(repo), remote["base"])
    assert beats  # heartbeats between steps
    config = (repo / ".git" / "config").read_text()
    assert "extraheader" not in config.lower() and "remote" not in config
    assert TOKEN.encode() not in _all_git_bytes(repo)
    assert not (repo / ".git" / "FETCH_HEAD").exists()
    assert not (repo / ".git" / "hooks").exists()  # --template= : no sample hooks


def test_gitauth_repr_hides_token() -> None:
    assert TOKEN not in repr(GitAuth(TOKEN))


def test_checkout_is_repeatable_in_the_same_directory(remote, tmp_path) -> None:
    repo = tmp_path / "repo"
    checkout(_spec(remote, repo), None, lambda: None)
    (repo / "c.py").write_text("tampered\n")
    checkout(_spec(remote, repo), None, lambda: None)
    assert (repo / "c.py").read_text() == "print('c')\n"


def test_checkout_never_runs_hooks_or_fsmonitor(remote, tmp_path) -> None:
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", str(repo))
    marker = tmp_path / "pwned"
    script = f"#!/bin/sh\necho ran >> {marker}\n"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction", "post-index-change", "pre-auto-gc"):
        hook = hooks / name
        hook.write_text(script)
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    monitor = tmp_path / "monitor.sh"
    monitor.write_text(script)
    monitor.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(monitor))
    git(repo, "config", "core.hooksPath", str(hooks))
    checkout(_spec(remote, repo), None, lambda: None)
    assert git(repo, "rev-parse", "HEAD") == remote["head"]
    assert not marker.exists()


def test_optional_prior_sha_may_be_missing(remote, tmp_path) -> None:
    repo = tmp_path / "repo"
    gone = "d" * 40
    checkout(_spec(remote, repo, optional_shas=[gone, remote["base"]]), None, lambda: None)
    assert not has_commit(str(repo), gone)
    assert has_commit(str(repo), remote["base"])


def test_missing_required_sha_fails(remote, tmp_path) -> None:
    spec = _spec(remote, tmp_path / "repo")
    spec.shas = [remote["head"], "e" * 40]
    with pytest.raises(MsreviewGitError):
        checkout(spec, None, lambda: None)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@github.com/o/r.git",
        "https://x-access-token@github.com/o/r.git",
        "http://github.com/o/r.git",
        "ssh://git@github.com/o/r.git",
        "ext::sh -c touch% /tmp/pwned",
    ],
)
def test_bad_clone_urls_are_refused(remote, tmp_path, url: str) -> None:
    with pytest.raises(ValueError):
        checkout(_spec(remote, tmp_path / "repo", url=url), None, lambda: None)


def test_bad_shas_and_dirs_are_refused(remote, tmp_path) -> None:
    with pytest.raises(ValueError):
        checkout(_spec(remote, tmp_path / "r1", head="--upload-pack=x"), None, lambda: None)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "file.txt").write_text("x")
    with pytest.raises(MsreviewGitError):
        checkout(_spec(remote, busy), None, lambda: None)


def test_diff_names_and_show_file(remote, tmp_path) -> None:
    repo = tmp_path / "repo"
    checkout(_spec(remote, repo), None, lambda: None)
    assert sorted(diff_names(str(repo), remote["base"], remote["head"])) == ["c.py", "lib/b.py"]
    assert show_file(str(repo), remote["base"], "lib/b.py") == "def b():\n    return 1\n"
    assert show_file(str(repo), remote["base"], "c.py") is None  # not present at base
    with pytest.raises(ValueError):
        show_file(str(repo), remote["base"], "/etc/passwd")


def test_head_first_lines_is_confined(remote, tmp_path) -> None:
    repo = tmp_path / "repo"
    checkout(_spec(remote, repo), None, lambda: None)
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET\n")
    (repo / "link.py").symlink_to(outside)
    got = head_first_lines(str(repo), ["a.py", "link.py", "../secret.txt", ".git/config", "nope"])
    assert got == {"a.py": "# @generated\nx = 1"}


def test_slow_git_steps_keep_heartbeating(remote, tmp_path, monkeypatch) -> None:
    import time

    import morning_star_review.gitops as gitops

    monkeypatch.setattr(gitops, "HEARTBEAT_EVERY", 0.01)
    real = gitops._checkout_head

    def slow_checkout(repo_dir: str, head: str) -> None:
        time.sleep(0.2)
        real(repo_dir, head)

    monkeypatch.setattr(gitops, "_checkout_head", slow_checkout)
    beats: list[int] = []
    checkout(_spec(remote, tmp_path / "repo"), None, lambda: beats.append(1))
    assert len(beats) >= 5  # heartbeats while the checkout step ran, not only between steps

    def failing(repo_dir: str, head: str) -> None:
        raise MsreviewGitError("checkout failed")

    monkeypatch.setattr(gitops, "_checkout_head", failing)
    with pytest.raises(MsreviewGitError, match="checkout failed"):
        checkout(_spec(remote, tmp_path / "repo2"), None, lambda: None)


def test_in_tree_gitattributes_are_neutralised(tmp_path: Path) -> None:
    # Security review GIT-1: the PR head's .gitattributes must not hide a text file
    # from the local diff and git grep, nor re-encode files on checkout.
    from morning_star_review.diff.patch import local_patches

    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    # Commit the hostile .gitattributes without applying it in the source repo.
    (src / ".git" / "info").mkdir(exist_ok=True)
    (src / ".git" / "info" / "attributes").write_text("* !working-tree-encoding !diff\n")
    _write(src, {"auth.py": "def check(user):\n    return user.ok\n"})
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "base")
    base = git(src, "rev-parse", "HEAD")
    _write(
        src,
        {
            "auth.py": "def check(user):\n    return True\n",
            "util.py": "def helper():\n    return 'needle'\n",
            ".gitattributes": "auth.py -diff\n[attr]hide -diff\nutil.py hide\n"
            "*.py working-tree-encoding=UTF-16LE\n",
        },
    )
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "head")
    head = git(src, "rev-parse", "HEAD")
    repo = tmp_path / "co"
    spec = CheckoutSpec(
        repo_dir=str(repo), clone_url=src.as_uri(), shas=[head, base], head_sha=head
    )
    checkout(spec, None, lambda: None)
    assert (repo / "auth.py").read_bytes() == b"def check(user):\n    return True\n"
    patch = local_patches(str(repo), base, head, ["auth.py"])["auth.py"]
    assert not patch.binary and patch.hunks
    from morning_star_review.core.proc import run_git

    grep = run_git(repo, ["grep", "-n", "-I", "-e", "needle", head], timeout=30)
    assert "util.py" in grep.stdout


def test_a_tree_bomb_is_refused_before_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Security review GIT-2: many entries for one well-compressed blob.
    import morning_star_review.gitops as gitops
    from morning_star_review.core.errors import MsreviewTooLarge

    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    blob = "0" * 1_000_000
    _write(src, {f"bomb/f{i}.txt": blob for i in range(20)})
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "bomb")
    head = git(src, "rev-parse", "HEAD")
    repo = tmp_path / "co"
    spec = CheckoutSpec(repo_dir=str(repo), clone_url=src.as_uri(), shas=[head], head_sha=head)
    assert gitops.tree_size(str(src), head) == (20, 20_000_000)
    monkeypatch.setattr(gitops, "MAX_TREE_BYTES", 10_000_000)
    with pytest.raises(MsreviewTooLarge, match="too large to check out"):
        checkout(spec, None, lambda: None)
    assert not (repo / "bomb").exists()
    monkeypatch.setattr(gitops, "MAX_TREE_BYTES", 100_000_000)
    monkeypatch.setattr(gitops, "MAX_TREE_ENTRIES", 10)
    with pytest.raises(MsreviewTooLarge):
        checkout(spec, None, lambda: None)
