"""Public-repo hygiene: no forbidden identifiers in any file that git would publish.

The forbidden strings live in a git-ignored local file so that the list itself is
never committed. Without that file (for example in a fresh public clone) the test
is skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_FILE = REPO_ROOT / ".internal" / "forbidden-strings.txt"


def load_forbidden(path: Path) -> list[str]:
    """One string per line, lower-cased; blank lines and `#` comments are ignored."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line.lower())
    return out


def publishable_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored (what a commit could include)."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted({p for p in proc.stdout.decode("utf-8").split("\0") if p})


def find_hits(root: Path, rel_paths: list[str], forbidden: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in rel_paths:
        for word in forbidden:
            if word in rel.lower():
                hits.append(f"{rel}: path contains a forbidden string")
        path = root / rel
        if not path.is_file():
            continue  # deleted in the work tree but still tracked
        text = path.read_bytes().decode("utf-8", errors="ignore").lower()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for word in forbidden:
                if word in line:
                    # Report the location only, never the matched string itself.
                    hits.append(f"{rel}:{lineno}: forbidden string #{forbidden.index(word) + 1}")
    return hits


def test_find_hits_reports_location_without_echoing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("fine\nhas SECRET-Thing inside\n")
    (tmp_path / "b.txt").write_bytes(b"\xff\xfe binary secret-thing")
    (tmp_path / "secret-thing.md").write_text("ok\n")
    hits = find_hits(
        tmp_path, ["a.txt", "b.txt", "secret-thing.md", "gone.txt"], ["other", "secret-thing"]
    )
    assert hits == [
        "a.txt:2: forbidden string #2",
        "b.txt:1: forbidden string #2",
        "secret-thing.md: path contains a forbidden string",
    ]


def test_load_forbidden_skips_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / "list.txt"
    f.write_text("# comment\n\n  Alpha  \nbeta\n")
    assert load_forbidden(f) == ["alpha", "beta"]


def test_no_forbidden_strings_in_publishable_files() -> None:
    if not FORBIDDEN_FILE.is_file():
        pytest.skip(f"no local forbidden-strings list at {FORBIDDEN_FILE.relative_to(REPO_ROOT)}")
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    forbidden = load_forbidden(FORBIDDEN_FILE)
    assert forbidden, "forbidden-strings list is empty"
    files = publishable_files(REPO_ROOT)
    assert files, "git reported no publishable files"
    assert not any(f.startswith(".internal/") for f in files), ".internal/ must stay git-ignored"
    hits = find_hits(REPO_ROOT, files, forbidden)
    assert not hits, "forbidden strings found:\n" + "\n".join(hits)
