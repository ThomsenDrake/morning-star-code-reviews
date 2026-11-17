"""Git checkout of the pull request into a private run directory (DESIGN 7.1).

The checkout never configures a remote: the fetch URL is passed on the command
line and the read token travels only as a per-command ``GIT_CONFIG_*`` header, so
neither ``.git/config`` nor ``FETCH_HEAD`` holds a credential, and the agent's
read-only ``git`` calls have nothing to contact. Hooks, filters, submodules and
LFS are disabled for every command (``core.proc.GIT_SAFE_CONFIG``).
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from morning_star_review.core.errors import MsreviewGitError, MsreviewTooLarge
from morning_star_review.core.proc import git_auth_env, run_git, run_git_bytes

__all__ = [
    "CheckoutSpec",
    "GitAuth",
    "checkout",
    "diff_names",
    "has_commit",
    "head_first_lines",
    "show_file",
]

FETCH_TIMEOUT = 600.0
MAX_TREE_ENTRIES = 1_000_000  # files in the head tree
MAX_TREE_BYTES = 4 * 2**30  # their total size: what the checkout writes to disk
LOCAL_TIMEOUT = 60.0
HEARTBEAT_EVERY = 15.0
_FIRST_LINES_BYTES = 1024

_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# Things that must never appear in .git/config after a checkout.
_CONFIG_LEAKS = re.compile(
    r"extraheader|authorization|x-access-token|[a-z][a-z0-9+.-]*://[^/\s]*@|\[remote ",
    re.IGNORECASE,
)


@dataclass
class CheckoutSpec:
    repo_dir: str
    clone_url: str
    shas: list[str]  # head, merge_base (required)
    head_sha: str
    # Fetched after the required ones; a failure is tolerated (the prior head of an
    # incremental review may have been force-pushed away). Check with has_commit().
    optional_shas: list[str] = field(default_factory=list)


@dataclass
class GitAuth:
    token: str = field(repr=False)  # never logged


def _check_sha(sha: str) -> str:
    if not _SHA.match(sha):
        raise ValueError(f"not a full commit sha: {sha[:80]!r}")
    return sha


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "file"):
        raise ValueError("clone URL must use https (or file for tests)")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("clone URL must not carry credentials")


def _with_heartbeat(fn: Callable[[], None], heartbeat: Callable[[], None]) -> None:
    """Run ``fn`` in a thread and call ``heartbeat`` every few seconds until it returns."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # re-raised in the caller's thread
            errors.append(exc)

    worker = threading.Thread(target=target, name="msreview-git-fetch", daemon=True)
    worker.start()
    while True:
        worker.join(HEARTBEAT_EVERY)
        if not worker.is_alive():
            break
        heartbeat()
    if errors:
        raise errors[0]


def _fetch(spec: CheckoutSpec, shas: Sequence[str], env: dict[str, str] | None) -> None:
    run_git(
        spec.repo_dir,
        [
            "fetch",
            "--no-tags",
            "--depth=1",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--quiet",
            spec.clone_url,
            *shas,
        ],
        env_extra=env,
        timeout=FETCH_TIMEOUT,
        check=True,
    )


def _checkout_head(repo_dir: str, head: str) -> None:
    run_git(
        repo_dir,
        ["-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", head],
        timeout=FETCH_TIMEOUT,
        check=True,
    )


# Written to .git/info/attributes, which takes precedence over every .gitattributes
# file in the tree. The PR head's .gitattributes must not change what is reviewed:
# "auth.py -diff" would make the local diff and `git grep -I` treat a text file as
# binary (excluded, unsearchable), and "working-tree-encoding=UTF-16LE" would check
# out files the agent's tools then refuse to read. "!attr" means unspecified: git
# decides binary-ness from the content and converts nothing.
NEUTRAL_ATTRIBUTES = (
    "* !diff !text !eol !working-tree-encoding !filter !ident !merge"
    " !whitespace !conflict-marker-size !export-subst !export-ignore\n"
)


def _neutralise_attributes(repo_dir: str) -> None:
    info = Path(repo_dir) / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    target = info / "attributes"
    if target.is_symlink():
        target.unlink()
    target.write_text(NEUTRAL_ATTRIBUTES, encoding="utf-8")


def _prepare_dir(repo_dir: str) -> None:
    path = Path(repo_dir)
    if path.exists() and not path.is_dir():
        raise MsreviewGitError("checkout path exists and is not a directory")
    if path.is_dir() and any(path.iterdir()) and not (path / ".git").is_dir():
        raise MsreviewGitError("checkout directory is not empty and is not a git repository")
    path.mkdir(parents=True, exist_ok=True)
    # --template= : no sample hooks or other template files are copied in.
    run_git(None, ["init", "-q", "--template=", str(path)], timeout=LOCAL_TIMEOUT, check=True)
    _neutralise_attributes(repo_dir)


def _verify(spec: CheckoutSpec, token: str | None) -> None:
    head = run_git(spec.repo_dir, ["rev-parse", "HEAD"], timeout=LOCAL_TIMEOUT, check=True)
    if head.stdout.strip() != spec.head_sha:
        raise MsreviewGitError("checkout HEAD does not match the requested head sha")
    git_dir = Path(spec.repo_dir) / ".git"
    for name in ("config", "FETCH_HEAD"):
        f = git_dir / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if _CONFIG_LEAKS.search(text) or (token and token in text):
            raise MsreviewGitError(f".git/{name} holds credentials or a remote after checkout")


def tree_size(repo_dir: str, sha: str) -> tuple[int, int]:
    """(entries, total blob bytes) of the tree at commit ``sha``."""
    res = run_git_bytes(
        repo_dir, ["ls-tree", "-r", "-l", "-z", _check_sha(sha)], timeout=FETCH_TIMEOUT, check=True
    )
    entries = 0
    total = 0
    for record in res.stdout.split(b"\0"):
        if not record:
            continue
        entries += 1
        meta = record.split(b"\t", 1)[0].split()
        if len(meta) == 4 and meta[3].isdigit():
            total += int(meta[3])
    return entries, total


def _check_tree_size(repo_dir: str, head: str) -> None:
    """Refuse a head tree whose checkout would be enormous.

    Many tree entries can point to one large, well-compressed blob: a pack of a few
    kilobytes then writes gigabytes to the disk of the CLI user or the runner
    (security review GIT-2). The sizes come from the tree itself, before checkout.
    """
    entries, total = tree_size(repo_dir, head)
    if entries > MAX_TREE_ENTRIES or total > MAX_TREE_BYTES:
        raise MsreviewTooLarge(
            f"the head commit's tree is too large to check out ({entries} files, "
            f"{total / 2**30:.1f} GiB; limits {MAX_TREE_ENTRIES} files, "
            f"{MAX_TREE_BYTES / 2**30:.0f} GiB)"
        )


def has_commit(repo_dir: str, sha: str) -> bool:
    """True when the commit object ``sha`` is present in the local repository."""
    res = run_git(
        repo_dir, ["cat-file", "-e", f"{_check_sha(sha)}^{{commit}}"], timeout=LOCAL_TIMEOUT
    )
    return res.returncode == 0


def checkout(spec: CheckoutSpec, auth: GitAuth | None, heartbeat: Callable[[], None]) -> None:
    """Fetch the PR commits (depth 1, by sha) and check out ``head_sha`` detached.

    Safe to repeat in the same directory (a retried ``prepare``): commits already
    present are not fetched again. Raises ``MsreviewGitError`` when git fails, HEAD
    does not match, or ``.git/config`` ends up holding a credential or a remote.
    """
    _check_url(spec.clone_url)
    head = _check_sha(spec.head_sha)
    required = list(dict.fromkeys([head, *(_check_sha(s) for s in spec.shas)]))
    optional = [
        s for s in dict.fromkeys(_check_sha(s) for s in spec.optional_shas) if s not in required
    ]
    env = git_auth_env(auth.token) if auth is not None else None
    _prepare_dir(spec.repo_dir)
    heartbeat()
    missing = [s for s in required if not has_commit(spec.repo_dir, s)]
    if missing:
        _with_heartbeat(partial(_fetch, spec, missing, env), heartbeat)
    for sha in optional:
        if has_commit(spec.repo_dir, sha):
            continue
        try:
            _with_heartbeat(partial(_fetch, spec, [sha], env), heartbeat)
        except MsreviewGitError:
            continue  # the caller checks has_commit() and falls back to a full review
    heartbeat()
    _check_tree_size(spec.repo_dir, head)
    # Writing a large working tree can outlast the activity's heartbeat timeout too.
    _with_heartbeat(partial(_checkout_head, spec.repo_dir, head), heartbeat)
    _verify(spec, auth.token if auth is not None else None)


def diff_names(repo_dir: str, a: str, b: str) -> list[str]:
    """Paths that differ between commits ``a`` and ``b`` (renames as delete + add)."""
    res = run_git(
        repo_dir,
        [
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            _check_sha(a),
            _check_sha(b),
        ],
        timeout=LOCAL_TIMEOUT,
        check=True,
    )
    return [p for p in res.stdout.split("\0") if p]


def _check_path(path: str) -> str:
    if not path or path.startswith("/") or "\0" in path or "\n" in path:
        raise ValueError(f"invalid repository path: {path[:200]!r}")
    return path


def show_file(repo_dir: str, sha: str, path: str) -> str | None:
    """Content of ``path`` at commit ``sha`` (UTF-8, invalid bytes replaced), or ``None``."""
    res = run_git_bytes(
        repo_dir,
        ["cat-file", "blob", f"{_check_sha(sha)}:{_check_path(path)}"],
        timeout=LOCAL_TIMEOUT,
    )
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", "replace")


def head_first_lines(repo_dir: str, paths: Sequence[str], n_lines: int = 3) -> dict[str, str]:
    """First lines of each checked-out file (for generated-file markers).

    Reads at most 1 KB per file, never follows a symlink and never leaves
    ``repo_dir``; unreadable or missing files are left out.
    """
    root = Path(repo_dir).resolve()
    out: dict[str, str] = {}
    for rel in paths:
        try:
            _check_path(rel)
        except ValueError:
            continue
        full = root / rel
        real = full.resolve()
        # A symlink anywhere on the path changes the resolved path: skip it.
        if real != full or root not in real.parents or ".git" in Path(rel).parts:
            continue
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            continue
        try:
            if not real.is_file():
                continue
            data = os.read(fd, _FIRST_LINES_BYTES)
        except OSError:
            continue
        finally:
            os.close(fd)
        out[rel] = "\n".join(data.decode("utf-8", "replace").splitlines()[:n_lines])
    return out
