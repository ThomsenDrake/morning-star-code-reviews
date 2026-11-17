"""Pre-pass that plans which files and hunks go into the prompt (DESIGN 7.3).

Steps: drop non-reviewable artifacts (lock files, minified bundles, source maps,
snapshots, binaries, generated files, repository ``exclude`` globs); rank the rest
by kind, size and risk; inline diffs in rank order within the character budget;
everything else that is reviewable stays in scope "on demand" (the explorer reads
it with ``show_file_diff``). A hard ceiling keeps very large PRs bounded and marks
the review partial.

Generated-file markers are author data: a PR author could add ``@generated`` to a
hand-written file to hide it from review. A marker is honoured only when the
merge-base version of the same file (``previous_path`` for a rename) carried one
too; a newly added, copied or newly marked file stays reviewable with a low rank.
The head scan covers the first line only, unless the base version had a marker on
lines 1-3. Lines 2-3 count only a clear generator banner (``@generated``, or
``DO NOT EDIT`` on a line that also says "generated"), so a hand-written
"DO NOT EDIT" comment below the first line never excludes a file.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal

from morning_star_review.core.contract import MAX_FILES, ExcludedFile, PrFile, ResolvedConfig
from morning_star_review.core.errors import MsreviewGitError
from morning_star_review.diff.patch import FilePatch
from morning_star_review.diff.render import missing_diff_text, render_file
from morning_star_review.gitops import show_file

__all__ = [
    "MAX_REVIEWABLE_LINES",
    "PromptPlan",
    "base_first_lines",
    "default_exclusion_reason",
    "generated_marker_line",
    "glob_match",
    "incremental_files",
    "is_generated",
    "kind_weight",
    "nothing_to_review",
    "plan",
    "rank_score",
    "unreviewed_code",
    "write_diff_files",
]

MAX_REVIEWABLE_LINES = 20_000  # changed lines across reviewable files
OVER_CEILING = "over_ceiling"

_LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "go.sum",
        "composer.lock",
        "gemfile.lock",
        "pipfile.lock",
    }
)
_MEDIA_EXT = frozenset(
    [
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tif",
        ".tiff",
        ".psd",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".war",
        ".whl",
        ".egg",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".class",
        ".pyc",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".m4a",
        ".wav",
        ".ogg",
        ".flac",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".sqlite",
        ".db",
        ".parquet",
    ]
)
# Files in a __snapshots__ directory are excluded only with these extensions (snapshot
# data). The PR author picks the names: "tests/__snapshots__/conftest.py" is code that
# pytest imports, and must stay reviewable.
_SNAPSHOT_EXT = frozenset(
    {".snap", ".ambr", ".json", ".txt", ".html", ".htm", ".svg", ".xml", ".yaml", ".yml"}
    | {".md", ".csv", ".raw", ".png", ".jpg", ".jpeg", ".gif", ".webp", ""}
)
# Excluded content that can change what runs: when nothing else changed, the review
# is not "clean", because nothing was reviewed (security review EXCL-1).
_UNREVIEWED_CODE_REASONS = frozenset({"lock_file", "minified", "generated"})
_UNREVIEWED_WORDS = {
    "lock_file": "lock file",
    "minified": "minified file",
    "generated": "generated file",
}
_INERT_BINARY_EXT = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd", ".pdf"}
    | {".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".m4a", ".wav", ".ogg"}
    | {".flac", ".mov", ".avi", ".mkv", ".webm"}
)
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")
# Lines the scan may cover when the base version corroborates the marker.
_GENERATED_SCAN_LINES = 3
_BASE_SCAN_CHARS = 1024
# Statuses whose path has no merge-base version of the same file.
_NO_BASE_STATUSES = frozenset({"added", "copied", "removed", "unchanged"})

_VENDOR_DIRS = frozenset({"vendor", "vendors", "third_party", "third-party", "node_modules"})
_BUILD_DIRS = frozenset({"dist", "build"})
_DOC_EXT = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
_DOC_DIRS = frozenset({"docs", "doc"})
_DOC_NAMES = frozenset({"license", "licence", "changelog", "authors", "notice", "contributing"})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing", "testdata"})
_TEST_NAME = re.compile(
    r"(^test_.*\.py$|_test\.(py|go|rb|rs|exs?)$|\.(test|spec)\.[a-z0-9]+$|Tests?\.(java|kt|cs|scala)$)"
)
_INFRA_EXT = frozenset(
    [
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".properties",
        ".gradle",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".bat",
        ".tf",
        ".tfvars",
        ".hcl",
        ".nix",
        ".mk",
        ".cmake",
        ".env",
    ]
)
_INFRA_NAMES = frozenset(
    {"dockerfile", "containerfile", "makefile", "jenkinsfile", "procfile", "justfile"}
)
_RISK_WORDS = (
    "auth",
    "permission",
    "acl",
    "crypto",
    "token",
    "secret",
    "session",
    "password",
    "sql",
    "query",
    "payment",
    "billing",
    "admin",
)

WEIGHT_SOURCE = 1.0
WEIGHT_INFRA = 0.8
WEIGHT_TEST = 0.6
WEIGHT_VENDOR = 0.2
WEIGHT_DOCS = 0.2
WEIGHT_DELETION_ONLY = 0.3
WEIGHT_UNVERIFIED_GENERATED = 0.2  # head marker the base version does not corroborate
RISK_BOOST = 1.5


@dataclass
class PromptPlan:
    inlined: list[str]
    on_demand: list[str]
    excluded: list[ExcludedFile]
    rendered_diff: str
    # path -> run-dir relative file name of its rendered diff ("diffs/0001.txt"); covers
    # every file with a diff, excluded ones included (show_file_diff still serves them)
    per_file_diff_files: dict[str, str]
    partial_reason: str | None
    # path -> rendered diff text (the content of per_file_diff_files)
    diff_texts: dict[str, str] = field(default_factory=dict)
    ranked: list[str] = field(default_factory=list)  # reviewable paths, best first
    # path -> (additions, deletions) of the files given to plan(); for an incremental
    # plan these are the counts since the prior head, not the whole PR's
    line_counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def reviewable(self) -> list[str]:
        return [*self.inlined, *self.on_demand]


# --------------------------------------------------------------------------------------
# Globs
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip()
    # Gitignore rule: a pattern with no "/" except a trailing one matches at any depth.
    anchored = "/" in pat.rstrip("/")
    if pat.endswith("/"):
        pat += "**"
    pat = pat.lstrip("/")
    out: list[str] = [] if anchored else ["(?:.*/)?"]
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and "]" in pat[i + 2 :]:
            end = pat.index("]", i + 2)
            body = pat[i + 1 : end]
            neg = body.startswith(("!", "^"))
            body = body[1:] if neg else body
            body = body.replace("\\", "\\\\").replace("^", "\\^")
            out.append(f"[{'^' if neg else ''}{body}]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    """Gitignore-like glob match on a repo-relative POSIX path.

    ``*`` and ``?`` stay within one path segment, ``**`` crosses segments, a
    trailing ``/`` matches everything below a directory, and a pattern without
    any other ``/`` matches at any depth (``*.snap``, ``CHANGELOG.md``, ``vendor/``).
    """
    return _glob_regex(pattern).match(path) is not None


# --------------------------------------------------------------------------------------
# Exclusions
# --------------------------------------------------------------------------------------


def default_exclusion_reason(path: str) -> str | None:
    """Reason a path is a non-reviewable artifact by name alone, else ``None``."""
    p = PurePosixPath(path)
    name = p.name.lower()
    if name in _LOCK_FILES or name.endswith(".lock"):
        return "lock_file"
    if name.endswith((".min.js", ".min.css", ".min.mjs")):
        return "minified"
    if name.endswith(".map"):
        return "source_map"
    if name.endswith(".snap") or (
        "__snapshots__" in p.parts[:-1] and p.suffix.lower() in _SNAPSHOT_EXT
    ):
        return "snapshot"
    return None


def generated_marker_line(text: str, max_lines: int = 1) -> int | None:
    """1-based line (at most ``max_lines``) carrying a generated-code marker, else ``None``.

    Line 1 counts either marker (``@generated``, ``DO NOT EDIT``), as DESIGN 7.3
    says. Later lines count only a generator banner: ``@generated``, or
    ``DO NOT EDIT`` on a line that also says "generated" ("Code generated by
    protoc-gen-go. DO NOT EDIT."). A hand-written "DO NOT EDIT" below line 1 is
    not a marker.
    """
    limit = max(1, min(max_lines, _GENERATED_SCAN_LINES))
    for i, line in enumerate(text.splitlines()[:limit], start=1):
        if "@generated" in line:
            return i
        if "DO NOT EDIT" in line and (i == 1 or "generated" in line.lower()):
            return i
    return None


def is_generated(first_lines: str, *, max_lines: int = 1) -> bool:
    """True when a generated-code marker is on line 1 (or lines 1..``max_lines``)."""
    return generated_marker_line(first_lines, max_lines) is not None


def _first_lines_from_patch(fp: FilePatch | None) -> str | None:
    """Head lines 1..3 when the patch happens to contain them."""
    if fp is None:
        return None
    got = {n: t for h in fp.hunks for _, n, t in h.lines if n is not None and n <= 3}
    if 1 not in got:
        return None
    return "\n".join(got[n] for n in sorted(got))


def _base_path(f: PrFile) -> str | None:
    """Path of the file's merge-base version, or ``None`` when it has none."""
    if f.status in _NO_BASE_STATUSES:
        return None
    if f.status == "renamed":
        return f.previous_path or None
    return f.path


def base_first_lines(
    repo_dir: str,
    base_sha: str,
    files: Sequence[PrFile],
    head_first_lines: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """First lines of each file's merge-base version, keyed by HEAD path.

    Feeds ``plan(base_first_lines=...)``: a generated marker is honoured only when
    this mapping shows the base version carried one too. Renamed files are read
    at ``previous_path``; added and copied files have no base version. With
    ``head_first_lines``, only files whose head carries a marker on lines 1-3 (or
    whose head lines are unknown) are read, so a normal PR costs no git calls.
    Unreadable files are left out, which keeps them reviewable.
    """
    out: dict[str, str] = {}
    for f in files:
        base_path = _base_path(f)
        if base_path is None:
            continue
        if head_first_lines is not None:
            head = head_first_lines.get(f.path)
            if head is not None and generated_marker_line(head, _GENERATED_SCAN_LINES) is None:
                continue
        try:
            text = show_file(repo_dir, base_sha, base_path)
        except (MsreviewGitError, ValueError, OSError):
            continue
        if text is not None:
            # Same 1 KB window as gitops.head_first_lines reads from the head.
            lines = text[:_BASE_SCAN_CHARS].splitlines()[:_GENERATED_SCAN_LINES]
            out[f.path] = "\n".join(lines)
    return out


_Generated = Literal["generated", "unverified"]


def _generated(
    f: PrFile,
    fp: FilePatch | None,
    first_lines: Mapping[str, str],
    base_lines: Mapping[str, str],
) -> _Generated | None:
    """``"generated"`` (exclude), ``"unverified"`` (head-only marker: rank low) or ``None``."""
    if f.status == "removed":
        return None
    head = first_lines.get(f.path)
    if head is None:
        head = _first_lines_from_patch(fp)
    if head is None:
        return None
    base = base_lines.get(f.path) if _base_path(f) is not None else None
    # A rename moves the base's marker to a path the PR author chose (a generated file
    # renamed to tests/conftest.py, with code added): the marker is unverified there.
    if f.status == "renamed" and f.previous_path != f.path:
        base = None
    if base is not None and generated_marker_line(base, _GENERATED_SCAN_LINES) is not None:
        if is_generated(head, max_lines=_GENERATED_SCAN_LINES):
            return "generated"
        return None
    return "unverified" if is_generated(head) else None


def _exclusion(
    f: PrFile, fp: FilePatch | None, cfg: ResolvedConfig, generated: _Generated | None
) -> str | None:
    if f.status == "unchanged":
        return "no_changes"
    reason = default_exclusion_reason(f.path)
    if reason:
        return reason
    has_text = fp is not None and bool(fp.hunks)
    media = PurePosixPath(f.path).suffix.lower() in _MEDIA_EXT
    if (f.binary or (fp is not None and fp.binary) or media) and not has_text:
        return "binary"
    for glob in cfg.exclude:
        if glob_match(glob, f.path):
            return f"repository_config: {glob}"[:200]
    if generated == "generated":
        return "generated"
    if f.additions + f.deletions == 0 and not has_text:
        return "no_line_changes"  # pure rename, mode change
    return None


# --------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------


def kind_weight(path: str, *, deletion_only: bool = False) -> float:
    """Weight of a file's kind: source > infra/config > tests > vendored/build/docs."""
    if deletion_only:
        return WEIGHT_DELETION_ONLY
    p = PurePosixPath(path)
    dirs = {d.lower() for d in p.parts[:-1]}
    name = p.name
    low = name.lower()
    suffix = p.suffix.lower()
    if dirs & _VENDOR_DIRS or dirs & _BUILD_DIRS:
        return WEIGHT_VENDOR
    if suffix in _DOC_EXT or dirs & _DOC_DIRS or p.stem.lower() in _DOC_NAMES:
        return WEIGHT_DOCS
    if dirs & _TEST_DIRS or _TEST_NAME.search(name):
        return WEIGHT_TEST
    if (
        suffix in _INFRA_EXT
        or low in _INFRA_NAMES
        or low.startswith(("dockerfile", "containerfile"))
        or ".github" in dirs
    ):
        return WEIGHT_INFRA
    return WEIGHT_SOURCE


def rank_score(f: PrFile, *, unverified_generated: bool = False) -> float:
    """``kind_weight * log2(2 + changed_lines) * risk_boost`` (DESIGN 7.3).

    ``unverified_generated``: the head carries a generated marker the base version
    does not; the file stays reviewable but ranks like vendored code.
    """
    changed = f.additions + f.deletions
    deletion_only = f.status == "removed" or (f.additions == 0 and f.deletions > 0)
    weight = kind_weight(f.path, deletion_only=deletion_only)
    if unverified_generated:
        weight = min(weight, WEIGHT_UNVERIFIED_GENERATED)
    low = f.path.lower()
    boost = RISK_BOOST if any(w in low for w in _RISK_WORDS) else 1.0
    return weight * math.log2(2 + changed) * boost


# --------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------


def _diff_text(f: PrFile, fp: FilePatch | None) -> str:
    if fp is None or (not fp.hunks and not fp.binary and f.additions + f.deletions > 0):
        return missing_diff_text(f.path, f.status)
    return render_file(fp)


def plan(
    files: list[PrFile],
    patches: Mapping[str, FilePatch],
    cfg: ResolvedConfig,
    *,
    first_lines: Mapping[str, str] | None = None,
    base_first_lines: Mapping[str, str] | None = None,
    total_files: int | None = None,
) -> PromptPlan:
    """Split the PR's files into inlined, on-demand and excluded (DESIGN 7.3).

    ``patches`` maps head paths to the best available patch (GitHub's, else the
    local diff). ``first_lines`` maps paths to the first lines of their head version
    (for generated-file markers); without it only patches that start at line 1 are
    checked. ``base_first_lines`` maps head paths to the first lines of their
    merge-base version (see ``base_first_lines()``); a generated marker excludes a
    file only when that base version carried one too, so without it no file is
    excluded as generated. ``total_files`` is the PR's real file count when GitHub
    listed fewer.
    """
    budgets = cfg.budgets
    first = first_lines or {}
    base = base_first_lines or {}
    excluded: list[ExcludedFile] = []
    candidates: list[PrFile] = []
    unverified: set[str] = set()
    for f in files:
        fp = patches.get(f.path)
        generated = _generated(f, fp, first, base)
        reason = _exclusion(f, fp, cfg, generated)
        if reason:
            excluded.append(ExcludedFile(path=f.path, reason=reason, changed_lines=_lines(f)))
        else:
            candidates.append(f)
            if generated == "unverified":
                unverified.add(f.path)

    ranked = sorted(
        candidates,
        key=lambda f: (-rank_score(f, unverified_generated=f.path in unverified), f.path),
    )
    partial: list[str] = []
    if total_files is not None and total_files > len(files):
        partial.append(
            f"the pull request changes {total_files} files and GitHub lists only {len(files)}"
        )
    kept: list[PrFile] = []
    total = 0
    over = 0
    for f in ranked:
        n = _lines(f)
        if len(kept) < MAX_FILES and total + n <= MAX_REVIEWABLE_LINES:
            kept.append(f)
            total += n
        else:
            over += 1
            excluded.append(ExcludedFile(path=f.path, reason=OVER_CEILING, changed_lines=n))
    if over:
        partial.append(
            f"{over} reviewable files were left out to stay under "
            f"{MAX_REVIEWABLE_LINES} changed lines"
        )

    texts: dict[str, str] = {}
    kept_paths = {f.path for f in kept}
    for f in files:
        fp = patches.get(f.path)
        if fp is not None or f.path in kept_paths:
            texts[f.path] = _diff_text(f, fp)

    budget = budgets.inline_diff_chars
    share_cap = budgets.max_file_share * budget
    inlined: list[str] = []
    on_demand: list[str] = []
    used = 0
    for f in kept:
        size = len(texts[f.path]) + 1  # +1: blank line between files
        if size <= share_cap and used + size <= budget:
            inlined.append(f.path)
            used += size
        else:
            on_demand.append(f.path)

    names = {path: f"diffs/{i:04d}.txt" for i, path in enumerate(texts)}
    return PromptPlan(
        inlined=inlined,
        on_demand=on_demand,
        excluded=excluded,
        rendered_diff="\n".join(texts[p] for p in inlined),
        per_file_diff_files=names,
        partial_reason="; ".join(partial)[:500] or None,
        diff_texts=texts,
        ranked=[f.path for f in kept],
        line_counts={f.path: (f.additions, f.deletions) for f in files},
    )


def _lines(f: PrFile) -> int:
    return f.additions + f.deletions


DIFF_INDEX = "diffs/index.json"


def write_diff_files(p: PromptPlan, run_dir: str | Path) -> Path:
    """Write ``diffs/NNNN.txt`` for every rendered file and ``diffs/index.json``.

    The index maps head paths to the run-dir relative file names; ``show_file_diff``
    reads it. Returns the index path.
    """
    root = Path(run_dir)
    (root / "diffs").mkdir(parents=True, exist_ok=True)
    for path, rel in p.per_file_diff_files.items():
        (root / rel).write_text(p.diff_texts[path], encoding="utf-8")
    index = root / DIFF_INDEX
    index.write_text(
        json.dumps(p.per_file_diff_files, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return index


def incremental_files(
    files: list[PrFile], patches: Mapping[str, FilePatch], paths: list[str]
) -> list[PrFile]:
    """PR files restricted to ``paths``, with line counts taken from ``patches``.

    For an incremental review the prompt diff is ``git diff <prior head> <head>``
    (``patch.local_patches``) over ``ScopeDecision.diff_paths``; ranking and the
    ceiling must count those lines, not the whole PR's. Use as
    ``plan(incremental_files(files, inc, scope.diff_paths), inc, cfg)``.
    """
    wanted = set(paths)
    out: list[PrFile] = []
    for f in files:
        if f.path not in wanted:
            continue
        fp = patches.get(f.path)
        if fp is None:
            out.append(f)
            continue
        out.append(f.model_copy(update={"additions": fp.added, "deletions": fp.removed}))
    return out


# Exclusion reasons in words: (one file, several files).
_EXCLUDED_WORDS: dict[str, tuple[str, str]] = {
    "lock_file": ("lock file", "lock files"),
    "generated": ("generated", "generated"),
    "binary": ("binary", "binary"),
    "minified": ("minified", "minified"),
    "source_map": ("source map", "source maps"),
    "snapshot": ("snapshot", "snapshots"),
    "no_changes": ("without line changes", "without line changes"),
    "no_line_changes": ("without line changes", "without line changes"),
    OVER_CEILING: ("over the review size limit", "over the review size limit"),
}
_REPO_CONFIG = ("excluded by the repository config", "excluded by the repository config")


def _excluded_kind(reason: str) -> tuple[str, str]:
    if reason.startswith("repository_config"):
        return _REPO_CONFIG
    return _EXCLUDED_WORDS.get(reason, ("excluded", "excluded"))


def unreviewed_code(excluded: Sequence[ExcludedFile]) -> str | None:
    """The excluded files that can change what runs (lock files, minified bundles,
    generated code, non-media binaries) in words, e.g. ``2 lock files``; else ``None``.

    Used when nothing was reviewable: such a pull request is not called ``clean``.
    """
    risky = [
        e
        for e in excluded
        if e.reason in _UNREVIEWED_CODE_REASONS
        or (e.reason == "binary" and PurePosixPath(e.path).suffix.lower() not in _INERT_BINARY_EXT)
    ]
    if not risky:
        return None
    counts = Counter(_UNREVIEWED_WORDS.get(e.reason, "binary file") for e in risky)
    parts = [
        f"{n} {kind if n == 1 else kind + 's'}"
        for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ", ".join(parts)


def nothing_to_review(excluded: Sequence[ExcludedFile]) -> str:
    """Why a pull request has no reviewable file, e.g. ``all 5 changed files are ...``."""
    if not excluded:
        return "the pull request changes no files"
    counts = Counter(_excluded_kind(e.reason) for e in excluded)
    if len(excluded) == 1:
        return f"the only changed file is excluded from review ({next(iter(counts))[0]})"
    parts = [
        f"{n} {one if n == 1 else many}"
        for (one, many), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
    ]
    if len(parts) == 1:
        return f"all {len(excluded)} changed files are {next(iter(counts))[1]}"
    return f"all {len(excluded)} changed files are excluded from review ({', '.join(parts)})"
