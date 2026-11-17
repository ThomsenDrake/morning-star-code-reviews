"""Read-only repository tools exposed to the agents (DESIGN 6.3).

Five direct tools: ``list_repo_files``, ``read_repo_file``, ``read_base_file``,
``search_repo`` and ``show_file_diff``. Their docstrings are what the model reads.

Safety rules shared by all of them:
- Paths are repository-relative POSIX strings. Absolute paths, ``..`` parts, ``.git``
  and anything that resolves outside the checkout (symlinks included) are refused
  with ``agents.ToolError``, which the model reads.
- ``git`` runs with an argv list through ``core.proc.run_git`` (no shell, safe config).
- Every result is capped at ``max_tool_output_chars``, and the meter may withhold it
  when the context budget would be passed (``hooks.CONTEXT_WITHHELD``).
- ``read_repo_file`` and ``show_file_diff`` record the path in ``coverage.json``.

The tools read their settings from the module-level ``ToolContext`` set by
``configure``; the agent process runs one job.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import mistralai.agents as agents

from morning_star_review.agent import hooks
from morning_star_review.agent.jobio import add_coverage, read_coverage
from morning_star_review.core.contract import AgentJob, ChangedFileRef
from morning_star_review.core.errors import MsreviewGitError
from morning_star_review.core.proc import run_git, run_git_bytes

__all__ = [
    "ALL_TOOLS",
    "ToolContext",
    "configure",
    "list_repo_files",
    "read_base_file",
    "read_repo_file",
    "search_repo",
    "show_file_diff",
]

MAX_LINES_PER_READ = 400
MAX_LISTED_FILES = 500
MAX_SEARCH_HITS = 200
MAX_HIT_CHARS = 300
MAX_SEARCH_TEXT = 200
MAX_FILE_BYTES = 20 * 1024 * 1024
GIT_TIMEOUT = 10.0
_BINARY_SNIFF = 8000
_GLOB_CHARS = frozenset("*?[")


@dataclass
class ToolContext:
    repo_dir: Path  # real path of the head checkout
    merge_base_sha: str
    job_dir: Path
    changed: dict[str, ChangedFileRef]
    max_output_chars: int
    coverage_path: Path | None
    opened: list[str] = field(default_factory=list)


_CTX: ToolContext | None = None


def configure(job: AgentJob, job_dir: Path, coverage_path: Path | None) -> ToolContext:
    """Point the tools at the job's checkout and diff files."""
    global _CTX
    changed: dict[str, ChangedFileRef] = {}
    for ref in job.changed_files:
        changed[ref.path] = ref
    _CTX = ToolContext(
        repo_dir=Path(os.path.realpath(job.repo_dir)),
        merge_base_sha=job.merge_base_sha,
        job_dir=job_dir,
        changed=changed,
        max_output_chars=job.budgets.max_tool_output_chars,
        coverage_path=coverage_path,
        opened=read_coverage(coverage_path) if coverage_path is not None else [],
    )
    return _CTX


def _ctx() -> ToolContext:
    if _CTX is None:
        raise agents.ToolError("tools are not available in this run")
    return _CTX


# ------------------------------------------------------------------------ paths


def _clean_rel(path: str) -> str:
    """A normalised repository-relative path, or ``ToolError``."""
    raw = (path or "").strip()
    if not raw:
        raise agents.ToolError("path: a file path is required")
    if "\x00" in raw or "\n" in raw:
        raise agents.ToolError("path: invalid characters")
    shown = raw[:200]
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        raise agents.ToolError(f"{shown}: use a path relative to the repository root")
    parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in ("", ".")]
    if not parts:
        raise agents.ToolError(f"{shown}: not a file path")
    if ".." in parts:
        raise agents.ToolError(f"{shown}: '..' is not allowed")
    if any(p.lower() == ".git" for p in parts):
        raise agents.ToolError(f"{shown}: the .git directory is not readable")
    return "/".join(parts)


def _confined(rel: str) -> Path:
    """The real path of ``rel`` inside the checkout; refuses escapes through symlinks."""
    ctx = _ctx()
    real = Path(os.path.realpath(ctx.repo_dir / rel))
    root = ctx.repo_dir
    if real != root and root not in real.parents:
        raise agents.ToolError(f"{rel}: resolves outside the repository")
    inner = real.relative_to(root).parts
    if any(p.lower() == ".git" for p in inner):
        raise agents.ToolError(f"{rel}: the .git directory is not readable")
    return real


# ------------------------------------------------------------------------ output


def _split_lines(text: str) -> list[str]:
    """Lines as git and editors number them (only ``\\n`` breaks a line)."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _finish(text: str, *, coverage: str | None = None) -> str:
    """Charge the output to the meter (or withhold it) and record coverage."""
    m = hooks.METER
    if m is not None and not m.admit_tool_output(len(text)):
        return hooks.CONTEXT_WITHHELD
    if coverage is not None:
        ctx = _ctx()
        if ctx.coverage_path is not None:
            add_coverage(ctx.coverage_path, coverage, ctx.opened)
        elif coverage not in ctx.opened:
            ctx.opened.append(coverage)
    return text


def _numbered(
    path: str, lines: list[str], start: int, end: int | None, tool: str, label: str | None = None
) -> str:
    """Lines ``start..end`` with right-aligned numbers, capped to the output budget."""
    total = len(lines)
    label = label or path
    if total == 0:
        return f"{label} is empty (0 lines)."
    first = max(1, start)
    if first > total:
        raise agents.ToolError(f"{path}: has only {total} lines")
    last = total if end is None else min(end, total)
    last = min(last, first + MAX_LINES_PER_READ - 1)
    if last < first:
        raise agents.ToolError(f"{path}: end_line must not be before start_line")
    budget = _ctx().max_output_chars - 200  # room for header and footer
    out: list[str] = []
    used = 0
    shown_last = first - 1
    for n in range(first, last + 1):
        row = f"{n:>5}| {lines[n - 1]}"
        if used + len(row) + 1 > budget and out:
            break
        out.append(row[:budget])
        used += len(row) + 1
        shown_last = n
    header = f"{label} lines {first}-{shown_last} of {total}"
    body = "\n".join([header, *out])
    if shown_last < total:
        body += (
            f"\n[more: call {tool}(path={path!r}, start_line={shown_last + 1}) for the next lines]"
        )
    return body


def _cap(text: str, hint: str) -> str:
    """Cut ``text`` to the output budget at a line boundary, naming how to see the rest."""
    limit = _ctx().max_output_chars
    if len(text) <= limit:
        return text
    room = max(0, limit - len(hint) - 2)
    cut = text.rfind("\n", 0, room)
    cut = cut if cut > 0 else room
    return text[:cut] + "\n" + hint


_READ_ONLY = agents.ToolAnnotations(read_only_hint=True, destructive_hint=False)


# ------------------------------------------------------------------------ tools


@agents.tool(model_access="direct", annotations=_READ_ONLY)
def list_repo_files(pattern: str = "*") -> str:
    """List files of the repository at the pull request's head commit.

    Args:
        pattern: A glob matched against the whole repository-relative path, where `*`
            also matches `/` (for example `*.py`, `src/*`, `*/models.py`). A plain
            directory name such as `src/api` lists the files under it. Default: all files.

    Returns at most 500 paths and the total number of matches.
    """
    ctx = _ctx()
    try:
        res = run_git_bytes(ctx.repo_dir, ["ls-files", "-z"], timeout=GIT_TIMEOUT, check=True)
    except MsreviewGitError as exc:
        raise agents.ToolError(f"listing files failed: {exc}") from None
    paths = [p for p in res.stdout.decode("utf-8", "replace").split("\x00") if p]
    pat = (pattern or "*").strip() or "*"
    if any(c in _GLOB_CHARS for c in pat):
        hits = [p for p in paths if fnmatch.fnmatchcase(p, pat)]
    else:
        prefix = pat.strip("/")
        hits = [p for p in paths if p == prefix or p.startswith(prefix + "/")]
    shown = hits[:MAX_LISTED_FILES]
    head = f"{len(hits)} file(s) match {pat!r}"
    if len(hits) > len(shown):
        head += f" (showing the first {len(shown)}; use a narrower pattern for the rest)"
    body = "\n".join([head + ":", *shown]) if shown else head + "."
    return _finish(_cap(body, "[list cut; use a narrower pattern]"))


@agents.tool(model_access="direct", annotations=_READ_ONLY)
def read_repo_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a file as it is after the change (the pull request's head commit).

    Args:
        path: Repository-relative path, for example `src/app/models.py`.
        start_line: First line to show (1-based). Default 1.
        end_line: Last line to show. Default: up to 400 lines from start_line.

    Every line is prefixed with its line number. At most 400 lines per call; the
    result says how to read the next part.
    """
    rel = _clean_rel(path)
    real = _confined(rel)
    if not real.exists():
        raise agents.ToolError(f"{rel}: no such file at the head commit")
    if not real.is_file():
        raise agents.ToolError(f"{rel}: not a regular file (use list_repo_files for directories)")
    try:
        with real.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise agents.ToolError(f"{rel}: cannot be read") from None
    if len(data) > MAX_FILE_BYTES:
        raise agents.ToolError(f"{rel}: file is too large to read")
    if _is_binary(data):
        raise agents.ToolError(f"{rel}: binary file")
    lines = _split_lines(data.decode("utf-8", "replace"))
    text = _numbered(rel, lines, start_line, end_line, "read_repo_file")
    return _finish(text, coverage=rel)


def _git_blob(sha: str, rel: str) -> bytes | None:
    """The blob ``sha:rel`` or ``None`` when that path is no file at ``sha``."""
    ctx = _ctx()
    spec = f"{sha}:{rel}"
    kind = run_git(ctx.repo_dir, ["cat-file", "-t", spec], timeout=GIT_TIMEOUT)
    if kind.returncode != 0 or kind.stdout.strip() != "blob":
        return None
    res = run_git_bytes(ctx.repo_dir, ["cat-file", "blob", spec], timeout=GIT_TIMEOUT)
    if res.returncode != 0:
        return None
    return res.stdout


@agents.tool(model_access="direct", annotations=_READ_ONLY)
def read_base_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a file as it was before the change (the version the diff is compared against).

    Args:
        path: Repository-relative path. For a renamed file, the new or the old path.
        start_line: First line to show (1-based). Default 1.
        end_line: Last line to show. Default: up to 400 lines from start_line.

    Use it to compare old and new behaviour. Fails for files the change adds.
    """
    ctx = _ctx()
    rel = _clean_rel(path)
    try:
        data = _git_blob(ctx.merge_base_sha, rel)
        shown = rel
        ref = ctx.changed.get(rel)
        if data is None and ref is not None and ref.previous_path:
            data = _git_blob(ctx.merge_base_sha, ref.previous_path)
            shown = f"{ref.previous_path} (old path of {rel})"
    except MsreviewGitError as exc:
        raise agents.ToolError(f"{rel}: {exc}") from None
    if data is None:
        raise agents.ToolError(f"{rel}: not present before this change")
    if _is_binary(data):
        raise agents.ToolError(f"{rel}: binary file")
    lines = _split_lines(data.decode("utf-8", "replace"))
    text = _numbered(rel, lines, start_line, end_line, "read_base_file", label=shown)
    return _finish(text)


@agents.tool(model_access="direct", annotations=_READ_ONLY)
def search_repo(text: str, path_glob: str = "", regex: bool = False) -> str:
    """Search the files at the head commit for a text (like `git grep`).

    Args:
        text: What to look for, 1 to 200 characters. Plain text unless `regex` is true.
        path_glob: Optional pathspec that limits the search, for example `*.py` or `src/`.
        regex: Treat `text` as an extended regular expression.

    Returns up to 200 hits as `path:line: text`. Use it to find callers, definitions
    and other uses of a name.
    """
    ctx = _ctx()
    if not text or len(text) > MAX_SEARCH_TEXT:
        raise agents.ToolError(f"text: give 1 to {MAX_SEARCH_TEXT} characters")
    if "\x00" in text or "\n" in text:
        raise agents.ToolError("text: must be a single line")
    args = ["grep", "-n", "-I", "--no-color", "--null", "-E" if regex else "-F", "-e", text]
    glob = (path_glob or "").strip()
    if glob:
        if glob.startswith(":"):
            raise agents.ToolError(f"{glob[:100]}: pathspec magic is not allowed")
        if glob.startswith(("/", "\\")) or ".." in PurePosixPath(glob).parts:
            raise agents.ToolError(f"{glob[:100]}: use a pattern relative to the repository root")
        args += ["--", glob]
    try:
        res = run_git_bytes(ctx.repo_dir, args, timeout=GIT_TIMEOUT)
    except MsreviewGitError as exc:
        raise agents.ToolError(f"search failed: {exc}") from None
    if res.returncode == 1:
        return _finish(f"No matches for {text!r}" + (f" in {glob!r}." if glob else "."))
    if res.returncode != 0:
        detail = res.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[0][:200] if detail else f"exit code {res.returncode}"
        raise agents.ToolError(f"search failed: {reason}")
    hits: list[str] = []
    total = 0
    for raw in res.stdout.decode("utf-8", "replace").split("\n"):
        if not raw:
            continue
        # With --null, git separates path, line number and text with NUL bytes.
        fields = raw.split("\x00", 2)
        if len(fields) != 3:
            continue
        name, line_no, content = fields
        total += 1
        if len(hits) < MAX_SEARCH_HITS:
            hits.append(f"{name}:{line_no}: {content.rstrip()}"[:MAX_HIT_CHARS])
    head = f"{total} hit(s) for {text!r}"
    if total > len(hits):
        head += f" (showing the first {len(hits)}; narrow the search with path_glob)"
    body = "\n".join([head + ":", *hits])
    return _finish(_cap(body, "[hits cut; narrow the search with path_glob]"))


def _changed_ref(rel: str) -> ChangedFileRef | None:
    ctx = _ctx()
    ref = ctx.changed.get(rel)
    if ref is not None:
        return ref
    for candidate in ctx.changed.values():
        if candidate.previous_path == rel:
            return candidate
    return None


def _parts(text: str, size: int) -> list[str]:
    """Split ``text`` into chunks of at most ``size`` chars at line boundaries."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for line in text.split("\n"):
        while len(line) > size:  # a single huge line: hard split
            if current:
                chunks.append("\n".join(current))
                current, used = [], 0
            chunks.append(line[:size])
            line = line[size:]
        if used + len(line) + 1 > size and current:
            chunks.append("\n".join(current))
            current, used = [], 0
        current.append(line)
        used += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


@agents.tool(model_access="direct", annotations=_READ_ONLY)
def show_file_diff(path: str, part: int = 1) -> str:
    """Show the changed lines (hunks) of one file of this pull request.

    Args:
        path: Repository-relative path of a changed file.
        part: For large diffs, which part to show (1-based). The result says how
            many parts there are.

    Lines marked `+` were added, lines marked `-` were removed; the numbers are
    line numbers in the new version of the file.
    """
    ctx = _ctx()
    rel = _clean_rel(path)
    ref = _changed_ref(rel)
    if ref is None:
        listed = ", ".join(sorted(ctx.changed)[:20])
        raise agents.ToolError(f"{rel}: not changed in this pull request (changed: {listed})")
    if not ref.diff_file:
        raise agents.ToolError(f"{rel}: no text diff is available (binary or too large)")
    diff_path = Path(os.path.realpath(ctx.job_dir / ref.diff_file))
    job_root = Path(os.path.realpath(ctx.job_dir))
    if job_root not in diff_path.parents:
        raise agents.ToolError(f"{rel}: no text diff is available")
    try:
        text = diff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise agents.ToolError(f"{rel}: no text diff is available") from None
    size = max(1000, ctx.max_output_chars - 300)
    chunks = _parts(text.rstrip("\n"), size)
    if part < 1 or part > len(chunks):
        raise agents.ToolError(f"{rel}: part must be between 1 and {len(chunks)}")
    header = f"{ref.path} ({ref.status}) diff part {part} of {len(chunks)}"
    body = f"{header}\n{chunks[part - 1]}"
    if part < len(chunks):
        body += f"\n[more: call show_file_diff(path={ref.path!r}, part={part + 1})]"
    return _finish(body, coverage=ref.path)


ALL_TOOLS: tuple[Any, ...] = (
    list_repo_files,
    read_repo_file,
    read_base_file,
    search_repo,
    show_file_diff,
)
