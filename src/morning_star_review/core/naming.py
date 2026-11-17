"""Pull request URL parsing, execution ids and deployment names."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from morning_star_review.core.contract import Mode

__all__ = [
    "NAME_PREFIX",
    "WORKFLOW_NAME",
    "PrRef",
    "deployment_name",
    "execution_id",
    "execution_id_prefix",
    "parse_pr_url",
    "slug",
]

WORKFLOW_NAME = "morning-star-code-review"
NAME_PREFIX = "msreview"

_MODES: frozenset[str] = frozenset({"ci", "cli", "local"})
# Lenient on purpose: managed-user logins contain "_"; the value only builds API paths.
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_TOKEN = re.compile(r"^[a-z0-9]{1,32}$")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_BARE_HOSTS = ("github.com/", "www.github.com/", "api.github.com/")
# "owner/repo#12" shorthand.
_SHORTHAND = re.compile(r"^(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)#(?P<number>[0-9]+)$")


@dataclass(frozen=True, slots=True)
class PrRef:
    owner: str
    repo: str
    number: int
    canonical_url: str  # https://github.com/<owner>/<repo>/pull/<n>

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def _make_ref(owner: str, repo: str, number: str, original: str) -> PrRef:
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER.match(owner):
        raise ValueError(f"not a valid GitHub owner in {original!r}")
    if not _REPO.match(repo) or repo in {".", ".."}:
        raise ValueError(f"not a valid GitHub repository name in {original!r}")
    if not _NUMBER.match(number):
        raise ValueError(f"not a valid pull request number in {original!r}")
    n = int(number)
    return PrRef(owner, repo, n, f"https://github.com/{owner}/{repo}/pull/{n}")


def parse_pr_url(s: str) -> PrRef:
    """Parse a pull request reference; raise ``ValueError`` if it is not one.

    Accepted: ``https://github.com/o/r/pull/12`` (optionally with a trailing path such
    as ``/files``, a query or a fragment, or without the scheme), the REST URL
    ``https://api.github.com/repos/o/r/pulls/12``, ``o/r/pull/12`` and ``o/r#12``.
    """
    text = s.strip()
    if not text or len(text) > 500:
        raise ValueError("expected a GitHub pull request URL")
    m = _SHORTHAND.match(text)
    if m:
        return _make_ref(m["owner"], m["repo"], m["number"], text)

    if "://" not in text and text.lower().startswith(_BARE_HOSTS):
        text = "https://" + text
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"not a GitHub pull request URL: {s!r}")
        host = (parts.hostname or "").lower()
        if parts.port is not None or parts.username is not None or parts.password is not None:
            raise ValueError(f"not a GitHub pull request URL: {s!r}")
        segments = [p for p in parts.path.split("/") if p]
        if host == "api.github.com":
            if len(segments) >= 5 and segments[0] == "repos" and segments[3] == "pulls":
                return _make_ref(segments[1], segments[2], segments[4], s)
            raise ValueError(f"not a GitHub pull request URL: {s!r}")
        if host not in _GITHUB_HOSTS:
            raise ValueError(f"not a github.com URL: {s!r}")
    else:
        segments = [p for p in text.split("/") if p]
    if len(segments) >= 4 and segments[2] == "pull":
        return _make_ref(segments[0], segments[1], segments[3], s)
    raise ValueError(f"not a GitHub pull request URL: {s!r}")


def slug(text: str) -> str:
    """Lowercase ``[a-z0-9-]`` form of ``text`` (runs of other characters become ``-``)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _check_mode(mode: str) -> None:
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r}")


def execution_id_prefix(mode: Mode, ref: PrRef) -> str:
    """Common prefix of every execution id for this PR and mode (orphan sweep)."""
    _check_mode(mode)
    repo_slug = slug(f"{ref.owner}-{ref.repo}")[:40].strip("-")
    return f"{NAME_PREFIX}-{mode}-{repo_slug}-pr{ref.number}-"


def execution_id(mode: Mode, ref: PrRef, head_sha: str | None, rand: str) -> str:
    """Unguessable execution id: prefix + short head sha (or ``latest``) + ``rand``.

    ``rand`` must be fresh randomness, e.g. ``secrets.token_hex(4)``. Charset ``[a-z0-9-]``.
    """
    if not _TOKEN.match(rand):
        raise ValueError("rand must be 1-32 characters of [a-z0-9]")
    if head_sha:
        sha = head_sha.lower()
        if not _SHA.match(sha):
            raise ValueError("head_sha must be a hex object id")
        short = sha[:7]
    else:
        short = "latest"
    return f"{execution_id_prefix(mode, ref)}{short}-{rand}"


def deployment_name(mode: Mode, ref: PrRef, suffix: str | None = None) -> str:
    """Stable per-PR deployment name: about one deployment record per PR.

    GitHub names are case-insensitive, so the hash uses the lowercase full name; the
    hash keeps repositories whose slugs collide (``a-b/c`` and ``a/b-c``) apart.
    ``suffix`` (``[a-z0-9]``) is appended as ``-<suffix>`` when the stable name is
    busy; the driver passes ``"x" + secrets.token_hex(2)``.
    """
    _check_mode(mode)
    full = f"{ref.owner}/{ref.repo}".lower()
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:6]
    repo_slug = slug(f"{ref.owner}-{ref.repo}")[:48].strip("-")
    name = f"{NAME_PREFIX}-{mode}-{repo_slug}-{digest}-pr{ref.number}"
    if suffix is not None:
        if not _TOKEN.match(suffix):
            raise ValueError("suffix must be 1-32 characters of [a-z0-9]")
        name = f"{name}-{suffix}"
    return name
