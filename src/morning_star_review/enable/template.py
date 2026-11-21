"""The workflow file that ``msreview enable`` writes.

One template, packaged as ``_data/morning-star-code-review.yml.tmpl``, is the single
source of truth: ``examples/workflows/morning-star-code-review.yml`` is the template
rendered for the current release without a commit SHA (a test keeps them equal).
"""

from __future__ import annotations

import difflib
import importlib.resources
import re
from dataclasses import dataclass

from morning_star_review.core.sanitize import without_controls

__all__ = [
    "ACTION_REPO",
    "PRESETS",
    "WORKFLOW_FILE",
    "WORKFLOW_PATH",
    "Pin",
    "diff_summary",
    "find_pin",
    "find_preset",
    "normalize_version",
    "render",
    "repin",
    "template_text",
    "uses_legacy_secret",
]

ACTION_REPO = "ThomsenDrake/morning-star-code-reviews"
WORKFLOW_FILE = "morning-star-code-review.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
TEMPLATE_NAME = "morning-star-code-review.yml.tmpl"
PRESETS = ("best", "balanced", "single", "fast", "mistral")

_REF = "__MSREVIEW_REF__"
_PRESET = "__MSREVIEW_PRESET__"
_PRESET_HELP = "# " + " | ".join(PRESETS)
_PRESET_WIDTH = 26
_VERSION_RE = re.compile(r"^v?(\d{1,4}\.\d{1,4}\.\d{1,4})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_RE = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)?uses:[ \t]*)"
    + re.escape(ACTION_REPO)
    + r"@(?P<ref>[^\s#]+)(?:[ \t]+#[ \t]*(?P<comment>[^\n]*?))?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# secrets.MISTRAL_API_KEY (or secrets['MISTRAL_API_KEY']): the name 0.1.x workflows used.
_LEGACY_SECRET_RE = re.compile(
    r"\bsecrets\s*(?:\.\s*MISTRAL_API_KEY\b|\[\s*['\"]MISTRAL_API_KEY['\"]\s*\])",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"(?:^|[ \t])#.*$", re.MULTILINE)
_PRESET_RE = re.compile(
    r"^[ \t]+preset:[ \t]*[\"']?(?P<name>[A-Za-z0-9_-]+)[\"']?[ \t]*(?:#[^\n]*)?$", re.MULTILINE
)


def normalize_version(text: str) -> str:
    """``vX.Y.Z`` from ``vX.Y.Z`` or ``X.Y.Z``; ``ValueError`` otherwise."""
    m = _VERSION_RE.match(text.strip())
    if m is None:
        raise ValueError(f"expected a release like v1.2.3, not {text.strip()[:40]!r}")
    return f"v{m.group(1)}"


def is_sha(text: str) -> bool:
    return _SHA_RE.match(text) is not None


def template_text() -> str:
    ref = importlib.resources.files("morning_star_review") / "_data" / TEMPLATE_NAME
    return ref.read_text(encoding="utf-8")


def _action_ref(version: str, sha: str | None) -> str:
    tag = normalize_version(version)
    if sha is None:
        return tag
    if not is_sha(sha):
        raise ValueError("expected a full 40-character commit SHA")
    return f"{sha} # {tag}"


def _preset_line(preset: str | None) -> str:
    if preset is None:
        return "# " + "preset: best".ljust(_PRESET_WIDTH) + _PRESET_HELP
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    return f"preset: {preset}".ljust(_PRESET_WIDTH) + _PRESET_HELP


def render(version: str, sha: str | None = None, preset: str | None = None) -> str:
    """The workflow file pinned to ``sha`` (with a ``# vX.Y.Z`` comment), or to the tag
    when ``sha`` is None (the example file); ``preset`` sets the Action's preset input."""
    text = template_text()
    return text.replace(_REF, _action_ref(version, sha)).replace(_PRESET, _preset_line(preset))


@dataclass(frozen=True)
class Pin:
    """The release a workflow file uses: a commit SHA (with its version comment) or a tag."""

    ref: str
    version: str | None

    @property
    def sha(self) -> str | None:
        return self.ref if is_sha(self.ref) else None

    def describe(self) -> str:
        if self.sha is not None:
            return f"{self.version or 'unknown release'} ({self.sha[:7]})"
        return f"{without_controls(self.ref)[:60]} (not pinned to a commit)"


def find_pin(text: str) -> Pin | None:
    """The first ``uses: ThomsenDrake/morning-star-code-reviews@REF`` of ``text``."""
    m = _USES_RE.search(text)
    if m is None:
        return None
    ref = m.group("ref")
    version: str | None = None
    for candidate in (m.group("comment") or "", ref):
        try:
            version = normalize_version(candidate)
            break
        except ValueError:
            continue
    return Pin(ref=ref, version=version)


def find_preset(text: str) -> str | None:
    """The ``preset:`` input set in ``text`` (a commented-out line does not count)."""
    m = _PRESET_RE.search(text)
    if m is None or m.group("name") not in PRESETS:
        return None
    return m.group("name")


def uses_legacy_secret(text: str) -> bool:
    """Whether ``text`` maps the secret ``MISTRAL_API_KEY`` (0.1.x workflows did; msreview
    now sets ``MSREVIEW_MISTRAL_API_KEY``). Comments do not count."""
    return _LEGACY_SECRET_RE.search(_COMMENT_RE.sub("", text)) is not None


def repin(text: str, version: str, sha: str) -> str | None:
    """``text`` with every use of the Action pinned to ``sha`` (``None`` if it has none).
    Everything else (edits, comments, other steps) is kept as it is."""
    ref = _action_ref(version, sha)
    new, count = _USES_RE.subn(lambda m: f"{m.group('lead')}{ACTION_REPO}@{ref}", text)
    return new if count else None


def diff_summary(old: str, new: str, limit: int = 6) -> list[str]:
    """A few lines that say how ``old`` differs from ``new`` (safe to print)."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    changed = [line for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    out = ["differences (- the existing file, + the file msreview would write):"]
    out += ["    " + without_controls(line)[:100] for line in changed[:limit]]
    if len(changed) > limit:
        out.append(f"    ... and {len(changed) - limit} more")
    return out
