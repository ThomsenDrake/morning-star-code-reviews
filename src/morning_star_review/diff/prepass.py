"""Pre-pass that plans which files and hunks go into the prompt (DESIGN 7.3).

Steps: drop non-reviewable artifacts (lock files, minified bundles, source maps,
snapshots, binaries, generated files, repository ``exclude`` globs); rank the rest
by kind, size and risk; inline diffs in rank order within the character budget;
everything else that is reviewable stays in scope "on demand" (the explorer reads
it with ``show_file_diff``). A hard ceiling keeps very large PRs bounded and marks
the review partial.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath

from morning_star_review.core.contract import MAX_FILES, ExcludedFile, PrFile, ResolvedConfig
from morning_star_review.diff.patch import FilePatch
from morning_star_review.diff.render import missing_diff_text, render_file

__all__ = [
    "MAX_REVIEWABLE_LINES",
    "PromptPlan",
    "default_exclusion_reason",
    "glob_match",
    "incremental_files",
    "is_generated",
    "kind_weight",
    "plan",
    "rank_score",
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
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")
_GENERATED_SCAN_LINES = 3

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
    if "__snapshots__" in p.parts[:-1] or name.endswith(".snap"):
        return "snapshot"
    return None


def is_generated(first_lines: str) -> bool:
    """True when the first lines of a file carry a generated-code marker."""
    head = "\n".join(first_lines.splitlines()[:_GENERATED_SCAN_LINES])
    return any(marker in head for marker in _GENERATED_MARKERS)


def _first_lines_from_patch(fp: FilePatch | None) -> str | None:
    """Head lines 1..3 when the patch happens to contain them."""
    if fp is None:
        return None
    got = {n: t for h in fp.hunks for _, n, t in h.lines if n is not None and n <= 3}
    if 1 not in got:
        return None
    return "\n".join(got[n] for n in sorted(got))


def _exclusion(
    f: PrFile, fp: FilePatch | None, cfg: ResolvedConfig, first_lines: Mapping[str, str]
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
    if f.status != "removed":
        head = first_lines.get(f.path)
        if head is None:
            head = _first_lines_from_patch(fp)
        if head is not None and is_generated(head):
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


def rank_score(f: PrFile) -> float:
    """``kind_weight * log2(2 + changed_lines) * risk_boost`` (DESIGN 7.3)."""
    changed = f.additions + f.deletions
    deletion_only = f.status == "removed" or (f.additions == 0 and f.deletions > 0)
    weight = kind_weight(f.path, deletion_only=deletion_only)
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
    total_files: int | None = None,
) -> PromptPlan:
    """Split the PR's files into inlined, on-demand and excluded (DESIGN 7.3).

    ``patches`` maps head paths to the best available patch (GitHub's, else the
    local diff). ``first_lines`` maps paths to the first lines of their head version
    (for generated-file markers); without it only patches that start at line 1 are
    checked. ``total_files`` is the PR's real file count when GitHub listed fewer.
    """
    budgets = cfg.budgets
    first = first_lines or {}
    excluded: list[ExcludedFile] = []
    candidates: list[PrFile] = []
    for f in files:
        reason = _exclusion(f, patches.get(f.path), cfg, first)
        if reason:
            excluded.append(ExcludedFile(path=f.path, reason=reason, changed_lines=_lines(f)))
        else:
            candidates.append(f)

    ranked = sorted(candidates, key=lambda f: (-rank_score(f), f.path))
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
