"""Helpers for in-process workflow runs (DESIGN 15.4): no server, no key, no network.

- GitHub is the stateful fake from ``tests/github/fakegh.py`` (via ``set_test_transport``).
- The clone source is a local git repository fetched over ``file://``.
- ``ctx.agent_python`` is a wrapper script that runs ``tests/stubs/stub_agent.py`` with a
  scenario file, so every agent phase writes a canned ``result.json``.

Repository contents, planted problems and notes builders live here; ``harness.py`` runs
the workflow and ``conftest.py`` wires the fixtures.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakegh import FakeGitHub

TESTS = Path(__file__).resolve().parent.parent
STUB = TESTS / "stubs" / "stub_agent.py"
PR_URL = "https://github.com/octo-org/widgets/pull/7"
TOKEN = "ghs_workflowtesttoken0123456789"  # synthetic
MISTRAL_KEY = "mk-test-not-a-real-key-0123456789"  # synthetic
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

BASE_FILES = {
    "app/money.py": (
        '"""Money helpers."""\n'
        "\n"
        "\n"
        "def prorate(amount, days, total_days):\n"
        '    """Return the part of amount that covers days out of total_days."""\n'
        "    if total_days <= 0:\n"
        '        raise ValueError("total_days must be positive")\n'
        "    return amount * days / total_days\n"
    ),
    "app/service.py": (
        '"""Billing service."""\n'
        "\n"
        "from app.money import prorate\n"
        "\n"
        "\n"
        "def monthly_charge(amount, days):\n"
        '    """Charge for a partial month."""\n'
        "    return round(amount, 2)\n"
    ),
    "app/handlers.py": (
        '"""Request handlers."""\n'
        "\n"
        "\n"
        "def can_delete(user, item):\n"
        '    """Only admins and owners may delete."""\n'
        "    return user.is_admin or item.owner == user.name\n"
    ),
    "app/db.py": (
        '"""Database access."""\n'
        "\n"
        "\n"
        "def find_user(conn, name):\n"
        '    """Look a user up by name."""\n'
        '    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()\n'
    ),
    "uv.lock": "version = 1\n",
}

HEAD_FILES = {
    "app/service.py": (
        '"""Billing service."""\n'
        "\n"
        "from app.money import prorate\n"
        "\n"
        "\n"
        "def monthly_charge(amount, days):\n"
        '    """Charge for a partial month."""\n'
        "    share = prorate(days, amount, 30)\n"
        "    return round(share, 2)\n"
    ),
    "app/handlers.py": (
        '"""Request handlers."""\n'
        "\n"
        "\n"
        "def can_delete(user, item):\n"
        '    """Only admins and owners may delete."""\n'
        "    if not user.is_admin:\n"
        "        return True\n"
        "    return item.owner == user.name\n"
    ),
    "app/db.py": (
        '"""Database access."""\n'
        "\n"
        "\n"
        "def find_user(conn, name):\n"
        '    """Look a user up by name."""\n'
        "    query = f\"SELECT * FROM users WHERE name = '{name}'\"\n"
        "    return conn.execute(query).fetchone()\n"
    ),
}

# The planted problems: (path, evidence line, title, severity)
SQLI = (
    "app/db.py",
    "    query = f\"SELECT * FROM users WHERE name = '{name}'\"",
    "SQL injection through the name",
    "high",
)
AUTHZ = ("app/handlers.py", "        return True", "Inverted admin check allows any delete", "high")
SWAP = (
    "app/service.py",
    "    share = prorate(days, amount, 30)",
    "prorate called with swapped arguments",
    "high",
)
ROUNDING = ("app/service.py", "    return round(share, 2)", "Rounding may lose a cent", "medium")


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **_GIT_ENV}, capture_output=True, check=True
    )
    return res.stdout.decode().strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def line_of(text: str, evidence: str) -> int:
    """1-based line number of ``evidence`` in ``text``."""
    return text.splitlines().index(evidence) + 1


@dataclass
class Repo:
    """A local repository with ``main`` (base) and ``feature`` (PR head) branches."""

    path: Path
    base: str
    head: str
    contents: dict[str, str]  # head contents of every file

    @classmethod
    def create(cls, root: Path) -> Repo:
        path = root / "origin"
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        write_files(path, BASE_FILES)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "base")
        base = git(path, "rev-parse", "HEAD")
        git(path, "checkout", "-q", "-b", "feature")
        repo = cls(path=path, base=base, head=base, contents=dict(BASE_FILES))
        repo.commit(HEAD_FILES, "feature")
        return repo

    def commit(self, files: dict[str, str], message: str) -> str:
        write_files(self.path, files)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)
        self.head = git(self.path, "rev-parse", "HEAD")
        self.contents.update(files)
        return self.head

    def line(self, path: str, evidence: str) -> int:
        return line_of(self.contents[path], evidence)

    def pr_files(self, a: str | None = None, b: str | None = None) -> list[dict[str, Any]]:
        """GitHub's files list for ``a...b`` (default base...head): hunks only in ``patch``."""
        a, b = a or self.base, b or self.head
        names = git(self.path, "diff", "--name-status", "--no-renames", a, b).splitlines()
        out = []
        for row in names:
            status_code, name = row.split("\t", 1)
            diff = git(self.path, "diff", "--no-color", "-U3", a, b, "--", name)
            lines = diff.splitlines()
            start = next(i for i, ln in enumerate(lines) if ln.startswith("@@"))
            patch = "\n".join(lines[start:])
            adds = sum(1 for ln in lines[start:] if ln.startswith("+"))
            dels = sum(1 for ln in lines[start:] if ln.startswith("-"))
            status = {"A": "added", "D": "removed"}.get(status_code, "modified")
            out.append(
                {
                    "sha": "0" * 40,
                    "filename": name,
                    "status": status,
                    "additions": adds,
                    "deletions": dels,
                    "changes": adds + dels,
                    "patch": patch,
                }
            )
        return out


def compare_body(merge_base: str, *, commits: int = 1) -> dict[str, Any]:
    return {
        "status": "ahead",
        "merge_base_commit": {"sha": merge_base},
        "commits": [{"sha": "e" * 40, "parents": [{"sha": "f" * 40}]} for _ in range(commits)],
        "total_commits": commits,
    }


def point_fake_at(fake: FakeGitHub, repo: Repo) -> None:
    """Make the fake PR match the repository's current head."""
    fake.pr["head"]["sha"] = repo.head
    fake.pr["base"]["sha"] = repo.base
    fake.files = repo.pr_files()
    fake.pr["changed_files"] = len(fake.files)
    fake.pr["additions"] = sum(f["additions"] for f in fake.files)
    fake.pr["deletions"] = sum(f["deletions"] for f in fake.files)
    fake.compare[f"{repo.base}...{repo.head}"] = compare_body(repo.base)


def finding(path: str, line: int, evidence: str, title: str, severity: str) -> str:
    return (
        "FINDING\n"
        f"file: {path}\n"
        f"line: {line}\n"
        f"severity: {severity}\n"
        "category: correctness\n"
        f"title: {title}\n"
        f"evidence: {evidence.strip()}\n"
        f"why: {title} for some realistic input.\n"
        "fix: Correct the code.\n"
        "END FINDING\n"
    )


def notes_for(repo: Repo, *problems: tuple[str, str, str, str], priors: str = "") -> str:
    blocks = [finding(p, repo.line(p, ev), ev, t, s) for p, ev, t, s in problems]
    if not blocks:
        blocks = ["NO FINDINGS\n"]
    return (
        "\n".join(blocks)
        + priors
        + "SUMMARY: Read the changed files and their callers.\nREVIEW COMPLETE\n"
    )
