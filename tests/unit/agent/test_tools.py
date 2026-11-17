"""Unit tests of the read-only agent tools (DESIGN 6.3, 15.1)."""

from __future__ import annotations

import json

import pytest
from mistralai import agents

from morning_star_review.agent import hooks, tools

from .conftest import Repo, budgets


def _err(fn: object, *args: object, **kwargs: object) -> str:
    with pytest.raises(agents.ToolError) as info:
        fn(*args, **kwargs)  # type: ignore[operator]
    return info.value.message


# ------------------------------------------------------------------ confinement


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../etc/passwd", "'..' is not allowed"),
        ("pkg/../../x", "'..' is not allowed"),
        ("/etc/passwd", "relative to the repository root"),
        ("C:/x", "relative to the repository root"),
        (".git/config", ".git directory"),
        ("pkg/.GIT/config", ".git directory"),
        ("link_etc/hosts", "outside the repository"),
        ("link_hosts", "outside the repository"),
        ("link_up", "outside the repository"),
        ("link_git", ".git directory"),
        ("", "path is required"),
    ],
)
def test_read_repo_file_confinement(repo: Repo, path: str, reason: str) -> None:
    repo.configure()
    assert reason in _err(tools.read_repo_file, path)


def test_read_repo_file_numbering_and_header(repo: Repo) -> None:
    repo.configure()
    out = tools.read_repo_file("pkg/big.py", start_line=10, end_line=12)
    lines = out.splitlines()
    assert lines[0] == "pkg/big.py lines 10-12 of 1000"
    assert lines[1] == "   10| line_10 = 10"
    assert lines[3] == "   12| line_12 = 12"
    assert "start_line=13" in lines[-1]


def test_read_repo_file_caps_at_400_lines(repo: Repo) -> None:
    repo.configure()
    out = tools.read_repo_file("pkg/big.py")
    assert out.splitlines()[0] == "pkg/big.py lines 1-400 of 1000"
    assert "start_line=401" in out


def test_read_repo_file_char_cap(repo: Repo) -> None:
    repo.configure(max_tool_output_chars=1_000)
    out = tools.read_repo_file("pkg/big.py")
    assert len(out) <= 1_000
    first = out.splitlines()[0]
    shown_last = int(first.split()[2].split("-")[1])
    assert shown_last < 400
    assert f"start_line={shown_last + 1}" in out


def test_read_repo_file_special_files(repo: Repo) -> None:
    repo.configure()
    assert "binary file" in _err(tools.read_repo_file, "img.bin")
    assert tools.read_repo_file("empty.txt") == "empty.txt is empty (0 lines)."
    assert "has only 2 lines" in _err(tools.read_repo_file, "crlf.txt", start_line=5)
    crlf = tools.read_repo_file("crlf.txt")
    assert "    1| one\n    2| two" in crlf and "\r" not in crlf
    assert "no such file" in _err(tools.read_repo_file, "missing.py")
    assert "not a regular file" in _err(tools.read_repo_file, "pkg")


def test_coverage_is_recorded(repo: Repo) -> None:
    ctx = repo.configure()
    tools.read_repo_file("pkg/big.py", end_line=2)
    tools.read_repo_file("pkg/big.py", start_line=3, end_line=4)
    tools.show_file_diff("pkg/new_name.py")
    tools.read_base_file("pkg/big.py", end_line=2)  # base reads are not coverage
    data = json.loads((repo.job_dir / "coverage.json").read_text())
    assert data == ["pkg/big.py", "pkg/new_name.py"]
    assert ctx.opened == data


# ------------------------------------------------------------------ base file


def test_read_base_file(repo: Repo) -> None:
    repo.configure()
    out = tools.read_base_file("pkg/big.py", start_line=1, end_line=1)
    assert out.splitlines() == [
        "pkg/big.py lines 1-1 of 1000",
        "    1| line_1 = 1",
        out.splitlines()[2],
    ]
    renamed = tools.read_base_file("pkg/new_name.py")
    assert "old path of pkg/new_name.py" in renamed
    assert "return 1" in renamed
    assert "not present before this change" in _err(tools.read_base_file, "empty.txt")
    assert "not present before this change" in _err(tools.read_base_file, "pkg")
    assert "'..' is not allowed" in _err(tools.read_base_file, "../x")


# ------------------------------------------------------------------ search


def test_search_repo_basic_and_dash_text(repo: Repo) -> None:
    repo.configure()
    out = tools.search_repo("line_99 =")
    assert out.splitlines()[0].startswith("1 hit(s)")
    assert "pkg/big.py:99: line_99 = 99" in out
    dash = tools.search_repo("-v")  # must be a pattern, not a git option
    assert "README.md:1: -v flag" in dash
    assert tools.search_repo("nothing-like-this-anywhere").startswith("No matches")


def test_search_repo_regex_glob_and_caps(repo: Repo) -> None:
    repo.configure()
    out = tools.search_repo("^line_[0-9]+ = ", regex=True, path_glob="*.py")
    assert out.splitlines()[0].startswith("1000 hit(s)")
    assert len(out.splitlines()) == 1 + tools.MAX_SEARCH_HITS
    assert tools.search_repo("hello", path_glob="pkg/").startswith("No matches")
    assert "search failed" in _err(tools.search_repo, "([", regex=True)
    assert "1 to 200" in _err(tools.search_repo, "")
    assert "1 to 200" in _err(tools.search_repo, "x" * 201)
    assert "pathspec magic" in _err(tools.search_repo, "x", path_glob=":(exclude)*")
    assert "relative" in _err(tools.search_repo, "x", path_glob="../*")


# ------------------------------------------------------------------ list / diff


def test_list_repo_files(repo: Repo) -> None:
    repo.configure()
    all_files = tools.list_repo_files()
    assert all_files.splitlines()[0] == "6 file(s) match '*':"
    assert "pkg/big.py" in tools.list_repo_files("*.py")
    assert tools.list_repo_files("pkg").splitlines()[1:] == ["pkg/big.py", "pkg/new_name.py"]
    assert tools.list_repo_files("*/new_*").splitlines()[1:] == ["pkg/new_name.py"]
    assert tools.list_repo_files("nothing*") == "0 file(s) match 'nothing*'."


def test_show_file_diff_parts(repo: Repo) -> None:
    repo.configure(max_tool_output_chars=10_000)
    first = tools.show_file_diff("pkg/big.py")
    assert first.splitlines()[0].startswith("pkg/big.py (modified) diff part 1 of ")
    assert len(first) <= 10_000
    assert "part=2" in first
    total = int(first.splitlines()[0].rsplit(" ", 1)[1])
    last = tools.show_file_diff("pkg/big.py", part=total)
    assert "line_3000" in last and "[more:" not in last
    assert "between 1 and" in _err(tools.show_file_diff, "pkg/big.py", part=total + 1)
    old = tools.show_file_diff("pkg/old_name.py")  # the old path of a rename works too
    assert old.startswith("pkg/new_name.py (renamed)")
    assert "not changed in this pull request" in _err(tools.show_file_diff, "README.md")
    assert "no text diff" in _err(tools.show_file_diff, "img.bin")


# ------------------------------------------------------------------ context budget


def test_context_budget_withholds_output_and_closes_tools(repo: Repo) -> None:
    repo.configure()
    meter = hooks.Meter.start(
        phase="explore",
        model="mistral-large-4",
        budgets=budgets(max_context_tokens=2_000),
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        path=None,
    )
    hooks.install_meter(meter)
    small = tools.read_repo_file("pkg/big.py", end_line=5)
    assert small.startswith("pkg/big.py lines 1-5")
    big = tools.read_repo_file("pkg/big.py")  # ~6k chars > 2k tokens * 3
    assert big == hooks.CONTEXT_WITHHELD
    assert meter.state.tools_closed is True
    assert meter.state.exhausted_by == ["context"]
    # Once closed, even small outputs are withheld (parallel calls stay deterministic).
    assert tools.read_repo_file("pkg/big.py", end_line=1) == hooks.CONTEXT_WITHHELD


def test_tools_without_configuration_fail_cleanly() -> None:
    tools._CTX = None
    assert "not available" in _err(tools.list_repo_files)
