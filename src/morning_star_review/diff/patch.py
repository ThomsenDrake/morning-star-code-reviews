"""Unified diff parsing into per-file patches.

Two sources feed this module: the per-file ``patch`` strings GitHub returns for a
pull request (authoritative for anchors) and a local ``git diff`` that fills in
files GitHub left without a patch. Both are parsed into the same ``FilePatch``
shape, numbered with head (RIGHT-side) line numbers, so they agree on coordinates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from morning_star_review.core.proc import run_git

__all__ = [
    "FilePatch",
    "Hunk",
    "changed_line_count",
    "local_patches",
    "parse_patch",
]

_HUNK_HEADER = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+ ?(.*)$")
_GIT_DIFF_TIMEOUT = 120.0

# `git diff --raw` status letters -> GitHub file status names.
_RAW_STATUS = {
    "A": "added",
    "D": "removed",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "T": "changed",
}


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str  # the function context after the second "@@", stripped
    # (kind, head line number or None for removed lines, text without the prefix)
    lines: list[tuple[str, int | None, str]] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str
    previous_path: str | None
    status: str
    hunks: list[Hunk]
    binary: bool
    no_newline_flags: bool

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for kind, _, _ in h.lines if kind == "+")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for kind, _, _ in h.lines if kind == "-")


def changed_line_count(fp: FilePatch) -> int:
    """Added plus removed lines in ``fp``."""
    return fp.added + fp.removed


def _strip_cr(text: str) -> str:
    # CRLF files produce patch lines ending in "\r"; the gutter must not carry it.
    return text[:-1] if text.endswith("\r") else text


def _parse_hunks(lines: Iterable[str]) -> tuple[list[Hunk], bool]:
    """Hunks in a patch body. Lines before the first ``@@`` (git headers) are skipped."""
    hunks: list[Hunk] = []
    no_newline = False
    cur: Hunk | None = None
    old_left = new_left = 0
    new_no = 0
    for raw in lines:
        line = _strip_cr(raw)
        m = _HUNK_HEADER.match(line)
        if m:
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            cur = Hunk(
                old_start=int(m.group(1)),
                old_len=old_len,
                new_start=int(m.group(3)),
                new_len=new_len,
                header=m.group(5).strip(),
            )
            hunks.append(cur)
            old_left, new_left, new_no = old_len, new_len, int(m.group(3))
            continue
        if cur is None:
            continue  # git headers before the first hunk
        if line.startswith("\\"):
            no_newline = True
            continue
        if old_left <= 0 and new_left <= 0:
            continue  # trailing text after a complete hunk (e.g. a final empty line)
        kind = line[:1] or " "  # some tools strip the space of an empty context line
        text = line[1:]
        if kind == "+" and new_left > 0:
            cur.lines.append(("+", new_no, text))
            new_no += 1
            new_left -= 1
        elif kind == "-" and old_left > 0:
            cur.lines.append(("-", None, text))
            old_left -= 1
        elif kind == " " and old_left > 0 and new_left > 0:
            cur.lines.append((" ", new_no, text))
            new_no += 1
            old_left -= 1
            new_left -= 1
        else:
            # A line that does not fit the hunk counts: the patch is truncated or
            # malformed. Stop this hunk instead of inventing line numbers.
            old_left = new_left = 0
    return hunks, no_newline


def parse_patch(path: str, patch: str | None, status: str, previous_path: str | None) -> FilePatch:
    """Parse one file's unified diff (GitHub ``patch`` field or a ``git diff`` section).

    A ``None`` patch (GitHub omits it for binary and very large files) gives a
    ``FilePatch`` without hunks; the caller knows from ``PrFile.binary`` which case
    it is. A ``Binary files ... differ`` line marks the patch binary.
    """
    if patch is None:
        return FilePatch(path, previous_path, status, [], binary=False, no_newline_flags=False)
    lines = patch.split("\n")
    hunks, no_newline = _parse_hunks(lines)
    binary = not hunks and any(
        _strip_cr(ln).startswith(("Binary files ", "GIT binary patch")) for ln in lines
    )
    return FilePatch(path, previous_path, status, hunks, binary=binary, no_newline_flags=no_newline)


_DIFF_OPTS = (
    "-M",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--ignore-submodules=none",
)


def _raw_entries(text: str) -> list[tuple[str, str | None, str]]:
    """Parse ``git diff --raw -z``: ``(status, previous_path or None, path)`` in output order."""
    parts = text.split("\0")
    out: list[tuple[str, str | None, str]] = []
    i = 0
    while i < len(parts):
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        letter = meta.split()[-1][:1]
        status = _RAW_STATUS.get(letter, "modified")
        if letter in ("R", "C") and i + 2 < len(parts):
            out.append((status, parts[i + 1], parts[i + 2]))
            i += 3
        elif i + 1 < len(parts):
            out.append((status, None, parts[i + 1]))
            i += 2
        else:
            break
    return out


def _split_sections(text: str) -> list[str]:
    """Split ``git diff -p`` output into per-file sections (each starts with ``diff --git``)."""
    sections: list[list[str]] = []
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return ["\n".join(s) for s in sections]


def _git_diff(repo_dir: str, args: list[str]) -> str:
    return run_git(repo_dir, ["diff", *args], timeout=_GIT_DIFF_TIMEOUT, check=True).stdout


def local_patches(repo_dir: str, base: str, head: str, paths: list[str]) -> dict[str, FilePatch]:
    """Per-file patches from ``git diff -U3 -M base head`` for the files in ``paths``.

    Keys are head paths (the path itself for removed files). The whole tree diff is
    computed once and filtered, so rename detection also sees old names that are not
    in ``paths``. Files with no change between ``base`` and ``head`` are absent.
    """
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise ValueError(f"invalid revision: {rev!r}")
    wanted = set(paths)
    if not wanted:
        return {}
    raw = _raw_entries(_git_diff(repo_dir, ["--raw", "-z", *_DIFF_OPTS, base, head]))
    if not raw:
        return {}
    body = _git_diff(
        repo_dir, ["-U3", "--src-prefix=a/", "--dst-prefix=b/", *_DIFF_OPTS, base, head]
    )
    sections = _split_sections(body)
    # Both commands walk the same diff queue, but `-p` prints a type change (file <->
    # symlink or submodule) as two sections, a deletion then a creation.
    widths = [2 if status == "changed" else 1 for status, _, _ in raw]
    out: dict[str, FilePatch] = {}
    if len(sections) == sum(widths):
        i = 0
        for (status, prev, path), width in zip(raw, widths, strict=True):
            section = "\n".join(sections[i : i + width])
            i += width
            if path in wanted:
                out[path] = parse_patch(path, section, status, prev)
        return out
    # Should not happen; fall back to one diff per wanted file so a parsing surprise
    # costs time, not correctness.
    for status, prev, path in raw:  # pragma: no cover - defensive
        if path not in wanted:
            continue
        spec = [path] if prev is None else [prev, path]
        section = _git_diff(
            repo_dir,
            ["-U3", "--src-prefix=a/", "--dst-prefix=b/", *_DIFF_OPTS, base, head, "--", *spec],
        )
        out[path] = parse_patch(path, section, status, prev)
    return out
