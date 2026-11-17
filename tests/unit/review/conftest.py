"""Builders shared by the review tests (exposed through the ``make`` fixture)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pytest

from morning_star_review.core.contract import (
    AnchorFile,
    AnchorMap,
    DraftFinding,
    ExploreResult,
    ModelRole,
    OperatorPolicy,
    Prepared,
    PrFile,
    PriorFinding,
    PriorState,
    PrSnapshot,
    ResolvedConfig,
    ResolvedRoles,
    ReviewDraft,
    Usage,
    VerifyEntry,
    VerifyResult,
)
from morning_star_review.review import RunInfo

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
MERGE_BASE = "c" * 40
PRIOR_SHA = "d" * 40

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"

# A small head file: lines 1..40, the PR changed lines 10..30 (one hunk).
SERVICE_PY = [
    "from decimal import Decimal",
    "",
    "from ledger.money import prorate",
    "",
    "",
    "class LedgerService:",
    '    """Posts entries and computes balances."""',
    "",
    "    def __init__(self, db):",
    "        self.db = db",
    "",
    "    def page(self, items, page, size):",
    "        start = page * size",
    "        end = start + size + 1",
    "        return items[start:end]",
    "",
    "    def can_edit(self, user, entry):",
    "        if user.id != entry.owner_id:",
    "            return True",
    "        return False",
    "",
    "    def find(self, name):",
    '        query = "SELECT * FROM entries WHERE name = \'" + name + "\'"',
    "        return self.db.execute(query)",
    "",
    "    def split(self, amount, days, total_days):",
    "        return prorate(days, amount, total_days)",
    "",
    "    def total(self, entries):",
    "        return sum((e.amount for e in entries), Decimal(0))",
    "",
    "    def describe(self):",
    "        return 'ledger'",
    "",
    "",
    "def helper(x):",
    "    return x * 2",
    "",
    "",
    "# end",
]

CONFIG_JSON = ["{", '  "name": "ledger",', '  "retries": 3,', '  "debug": false', "}"]
SETTINGS_YAML = ["service:", "  name: ledger", "  retries: 3", "  debug: false"]
PYPROJECT = ["[tool.ledger]", 'name = "ledger"', "retries = 3"]
MAIN_GO = ["package main", "", "func main() {", "\tx := compute(1, 2)", "\tprintln(x)", "}"]


def _lines(hunks: Iterable[tuple[int, int]]) -> list[int]:
    return sorted({n for a, b in hunks for n in range(a, b + 1)})


class Make:
    """Factory helpers. Defaults describe a one-file PR on ``ledger/service.py``."""

    head_sha = HEAD_SHA

    @staticmethod
    def anchor_file(path: str, hunks: Sequence[tuple[int, int]]) -> AnchorFile:
        return AnchorFile(path=path, right_lines=_lines(hunks), hunks=list(hunks))

    @staticmethod
    def anchors(
        files: dict[str, Sequence[tuple[int, int]]] | None = None,
        renamed_from: dict[str, str] | None = None,
    ) -> AnchorMap:
        files = files if files is not None else {"ledger/service.py": [(10, 30)]}
        return AnchorMap(
            files={p: Make.anchor_file(p, h) for p, h in files.items()},
            renamed_from=renamed_from or {},
        )

    @staticmethod
    def heads(extra: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
        files = {
            "ledger/service.py": SERVICE_PY,
            "conf/config.json": CONFIG_JSON,
            "conf/settings.yaml": SETTINGS_YAML,
            "pyproject.toml": PYPROJECT,
            "cmd/main.go": MAIN_GO,
        }
        files.update(extra or {})
        return files

    @staticmethod
    def head_text(files: dict[str, list[str]] | None = None) -> Callable[[str], list[str] | None]:
        table = files if files is not None else Make.heads()
        return lambda path: table.get(path)

    @staticmethod
    def draft(**kw: Any) -> DraftFinding:
        base: dict[str, Any] = {
            "path": "ledger/service.py",
            "line": 14,
            "severity": "high",
            "category": "correctness",
            "title": "Off-by-one in page end index",
            "evidence": "end = start + size + 1",
            "explanation": "The slice end includes one extra item, so pages overlap.",
            "fix": "Drop the + 1.",
        }
        base.update(kw)
        return DraftFinding(**base)

    @staticmethod
    def usage(model: str = GLM, **kw: Any) -> Usage:
        base: dict[str, Any] = {
            "model": model,
            "llm_calls": 5,
            "tool_calls": 7,
            "input_tokens": 12000,
            "cached_input_tokens": 3000,
            "output_tokens": 900,
            "reasoning_tokens": 400,
            "cost_usd": 0.05,
            "seconds": 42.0,
        }
        base.update(kw)
        return Usage(**base)

    @staticmethod
    def review_draft(
        findings: Sequence[DraftFinding], model: str | None = GLM, **kw: Any
    ) -> ReviewDraft:
        base: dict[str, Any] = {
            "findings": list(findings),
            "prior_status": [],
            "summary": "Reviewed the change.",
            "source": "parser",
            "marker_count": len(findings),
            "explorer_model": model,
        }
        base.update(kw)
        return ReviewDraft(**base)

    @staticmethod
    def explore(model: str = GLM, **kw: Any) -> ExploreResult:
        base: dict[str, Any] = {
            "notes": "notes",
            "state": "Completed",
            "stop_reason": None,
            "rescued": False,
            "complete": True,
            "budget_exhausted": False,
            "opened_paths": ["ledger/service.py"],
            "usage": Make.usage(model),
        }
        base.update(kw)
        return ExploreResult(**base)

    @staticmethod
    def verify(entries: Sequence[VerifyEntry], **kw: Any) -> VerifyResult:
        base: dict[str, Any] = {
            "entries": list(entries),
            "source": "parser",
            "state": "Completed",
            "stop_reason": None,
            "rescued": False,
            "complete": True,
            "budget_exhausted": False,
            "opened_paths": ["ledger/service.py"],
            "usage": Make.usage(GLM, cost_usd=0.02),
        }
        base.update(kw)
        return VerifyResult(**base)

    @staticmethod
    def roles(explorers: Sequence[str] = (GLM,), verifier: str | None = None) -> ResolvedRoles:
        return ResolvedRoles(
            explorer_model=explorers[0],
            explorer_effort="high",
            formatter_model=LARGE,
            formatter_effort="low",
            extra_explorers=[ModelRole(model=m, effort="high") for m in explorers[1:]],
            verifier=ModelRole(model=verifier, effort="high") if verifier else None,
        )

    @staticmethod
    def config(roles: ResolvedRoles | None = None, **kw: Any) -> ResolvedConfig:
        return ResolvedConfig(roles=roles or Make.roles(), **kw)

    @staticmethod
    def pr_files(paths: Sequence[str] = ("ledger/service.py",)) -> list[PrFile]:
        return [
            PrFile(path=p, status="modified", additions=5, deletions=2, has_patch=True)
            for p in paths
        ]

    @staticmethod
    def prior_finding(**kw: Any) -> PriorFinding:
        base: dict[str, Any] = {
            "id": "f000000000001",
            "path": "ledger/service.py",
            "line": 14,
            "severity": "high",
            "title": "Off-by-one in page end index",
            "evidence": "end = start + size + 1",
            "comment_id": 101,
            "thread_id": "PRRT_1",
            "state": "open",
        }
        base.update(kw)
        return PriorFinding(**base)

    @staticmethod
    def prior_state(findings: Sequence[PriorFinding] = (), **kw: Any) -> PriorState:
        base: dict[str, Any] = {
            "version": 1,
            "tool_version": "0.1.0",
            "head_sha": PRIOR_SHA,
            "merge_base_sha": MERGE_BASE,
            "complete": True,
            "verdict": "findings" if findings else "clean",
            "config_hash": "0" * 64,
            "reviewed_at": "2026-11-16T10:00:00Z",
            "findings": list(findings),
            "summary_comment_id": 55,
        }
        base.update(kw)
        return PriorState(**base)

    @staticmethod
    def snapshot(
        paths: Sequence[str] = ("ledger/service.py",),
        config: ResolvedConfig | None = None,
        prior: PriorState | None = None,
        **kw: Any,
    ) -> PrSnapshot:
        base: dict[str, Any] = {
            "owner": "octo",
            "repo": "ledger",
            "number": 7,
            "html_url": "https://github.com/octo/ledger/pull/7",
            "title": "Pagination and ledger fixes",
            "body_excerpt": "",
            "author": "dev",
            "author_association": "OWNER",
            "draft": False,
            "state": "open",
            "merged": False,
            "head_sha": HEAD_SHA,
            "head_repo": "octo/ledger",
            "base_ref": "main",
            "base_sha": BASE_SHA,
            "merge_base_sha": MERGE_BASE,
            "additions": 10,
            "deletions": 2,
            "changed_files": len(paths),
            "files": Make.pr_files(paths),
            "files_json_sha256": "1" * 64,
            "can_post": True,
            "bot_login": "morning-star-code-reviews[bot]",
            "prior": prior,
            "config": config or Make.config(),
            "config_source": "default",
            "run_dir": "/tmp/run",
        }
        base.update(kw)
        return PrSnapshot(**base)

    @staticmethod
    def prepared(
        paths: Sequence[str] = ("ledger/service.py",),
        roles: ResolvedRoles | None = None,
        **kw: Any,
    ) -> Prepared:
        base: dict[str, Any] = {
            "job_root": "/tmp/run",
            "repo_dir": "/tmp/run/repo",
            "scope": "full",
            "since_sha": None,
            "prompt_sha256": "2" * 64,
            "prompt_chars": 1000,
            "inlined_paths": list(paths),
            "on_demand_paths": [],
            "excluded": [],
            "reviewable_paths": list(paths),
            "anchors_sha256": "3" * 64,
            "prior_for_prompt": [],
            "roles": roles or Make.roles(),
            "budgets": {},
            "boundary": "b0undary",
        }
        base.update(kw)
        return Prepared(**base)

    @staticmethod
    def info(**kw: Any) -> RunInfo:
        base: dict[str, Any] = {
            "execution_id": "msreview-ci-octo-ledger-7-aaaaaaa-x1y2",
            "run_label": "gha:octo/ledger/123/1",
            "tool_version": "0.1.0",
            "reviewed_at": "2026-11-17T12:00:00Z",
            "duration_seconds": 312.0,
        }
        base.update(kw)
        return RunInfo(**base)

    @staticmethod
    def policy() -> OperatorPolicy:
        return OperatorPolicy(trust_repo_config_models=True)


@pytest.fixture
def make() -> type[Make]:
    return Make


# --------------------------------------------------------------------------------------
# Markdown safety checks (what GitHub would render as live prose)
# --------------------------------------------------------------------------------------


def drop_fenced_blocks(text: str) -> str:
    keep: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
            else:
                keep.append(line)
            continue
        c = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", line)
        if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
            fence = None
    return "\n".join(keep)


def prose_outside_code(text: str) -> str:
    """Rendered prose: fenced blocks and (per-line) code spans removed."""
    out_lines = []
    for line in drop_fenced_blocks(text).split("\n"):
        out: list[str] = []
        i, n = 0, len(line)
        while i < n:
            if line[i] == "\\" and i + 1 < n:
                out.append(line[i : i + 2])
                i += 2
                continue
            if line[i] != "`":
                out.append(line[i])
                i += 1
                continue
            j = i
            while j < n and line[j] == "`":
                j += 1
            run = j - i
            k, close = j, -1
            while k < n:
                if line[k] != "`":
                    k += 1
                    continue
                end = k
                while end < n and line[end] == "`":
                    end += 1
                if end - k == run:
                    close = end
                    break
                k = end
            if close < 0:
                out.append(line[i:j])
                i = j
            else:
                i = close
        out_lines.append("".join(out))
    return "\n".join(out_lines)


OUR_MARKER = re.compile(r"<!-- morning-star-review:(?:summary v1|finding [A-Za-z0-9_-]{1,40}) -->")


def assert_safe_markdown(text: str) -> None:
    """No foreign HTML comment, no live @mention, no link to a non-github host."""
    without_ours = OUR_MARKER.sub("", text)
    assert "<!--" not in without_ours and "-->" not in without_ours, text
    assert "morning-star-review:" not in without_ours.lower(), text
    prose = prose_outside_code(without_ours)
    github_links = re.sub(r"\]\(https://github\.com/[^)\s]*\)", "]()", prose)
    # Autolinks and bare URLs to github.com: "@" or "#1" inside them is part of the URL.
    github_links = re.sub(r"<?https?://github\.com(?:/\S*)?", "", github_links)
    assert not re.search(r"(?<![A-Za-z0-9])@[A-Za-z0-9]", github_links), text
    # Every link target was github (escaped brackets "\\]" are text, not links).
    assert not re.search(r"(?<!\\)\]\((?!\))", github_links), text
    # A scheme followed by a host would be autolinked ("https://" alone is not).
    assert not re.search(r"(?i)\b(?:https?|ftp)://(?!github\.com/)[A-Za-z0-9]", github_links), text
    assert not re.search(r"(?i)(?<![\w.])www\.[A-Za-z0-9]", github_links), text


# Test modules cannot import conftest (importlib import mode); reach these via ``make``.
Make.assert_safe = staticmethod(assert_safe_markdown)  # type: ignore[attr-defined]
Make.prose = staticmethod(prose_outside_code)  # type: ignore[attr-defined]
