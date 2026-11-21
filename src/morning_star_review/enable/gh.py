"""A small wrapper around the GitHub CLI (``gh``): the user's own login talks to GitHub.

Request bodies and secret values go to ``gh`` on standard input; the argument list
only ever holds names, paths and flags.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.proc import absolute_path_entries
from morning_star_review.core.sanitize import without_controls

__all__ = ["AuthInfo", "Gh", "GhError", "GhResult", "find_gh"]

HOST = "github.com"
_TIMEOUT_S = 90.0
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
_MAX_PAGES = 50
_KEY_VARIABLES = frozenset({"MISTRAL_API_KEY"})


class GhError(MsreviewError):
    """A ``gh`` command failed (the message is safe to show)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def http_status(self) -> int | None:
        m = _HTTP_STATUS.search(self.stderr)
        return int(m.group(1)) if m else None

    def brief(self) -> str:
        """The first line of the error output, without control characters."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        text = lines[0] if lines else f"exit code {self.returncode}"
        return without_controls(text)[:200]


@dataclass(frozen=True)
class AuthInfo:
    login: str
    scopes: frozenset[str] | None  # None: the token does not report scopes
    source: str = ""  # keyring, GH_TOKEN, ...


def find_gh(env: Mapping[str, str]) -> str:
    """The absolute path of ``gh`` (absolute PATH entries only), or ``GhError``."""
    found = shutil.which("gh", path=absolute_path_entries(env.get("PATH")))
    if found is None:
        raise GhError(
            "the GitHub CLI (gh) is not on PATH; install it (https://cli.github.com) "
            "and run `gh auth login`"
        )
    return found


class Gh:
    """Runs ``gh``; ``api`` returns parsed JSON and raises ``GhError`` on failures."""

    def __init__(self, exe: str, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        # gh needs none of msreview's variables (some hold keys), nor a Mistral key the
        # shell may export: they stay out of its environment.
        base = {
            k: v
            for k, v in source.items()
            if not k.startswith("MSREVIEW_") and k not in _KEY_VARIABLES
        }
        # No prompts, no update notices, no colours: gh's output is parsed here.
        base.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"})
        base.pop("CLICOLOR_FORCE", None)
        self.exe = exe
        self._env = base

    def run(self, args: Sequence[str], *, stdin: bytes | None = None) -> GhResult:
        try:
            res = subprocess.run(
                [self.exe, *args],
                input=stdin if stdin is not None else b"",
                capture_output=True,
                timeout=_TIMEOUT_S,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GhError(f"`gh {args[0]}` did not finish in {int(_TIMEOUT_S)} s") from None
        except OSError as exc:
            raise GhError(f"`gh` did not run ({exc.strerror or type(exc).__name__})") from None
        return GhResult(
            res.returncode,
            res.stdout.decode("utf-8", errors="replace"),
            res.stderr.decode("utf-8", errors="replace"),
        )

    def api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        """``gh api``: the JSON response; ``None`` for 404 when ``allow_404``."""
        args = ["api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body).encode("utf-8")
        res = self.run(args, stdin=stdin)
        if res.returncode != 0:
            status = res.http_status
            if status == 404 and allow_404:
                return None
            raise GhError(f"{method} {path.split('?')[0]}: {res.brief()}", status)
        text = res.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise GhError(f"{method} {path.split('?')[0]}: the response is not JSON") from None

    def paged(self, path: str, key: str, *, per_page: int = 100) -> list[Any]:
        """Every item of a paginated list response (``{key: [...]}`` pages)."""
        sep = "&" if "?" in path else "?"
        out: list[Any] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self.api(f"{path}{sep}per_page={per_page}&page={page}")
            items = data.get(key) if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise GhError(f"GET {path.split('?')[0]}: unexpected response")
            out.extend(items)
            if len(items) < per_page:
                break
        return out

    def auth(self, host: str = HOST) -> AuthInfo:
        """The active login on ``host`` and its scopes (``GhError`` when not logged in)."""
        res = self.run(["auth", "status", "--json", "hosts", "--hostname", host])
        if res.returncode == 0 and res.stdout.lstrip().startswith("{"):
            return _auth_from_json(res.stdout, host)
        res = self.run(["auth", "status", "--hostname", host])  # gh before --json
        if res.returncode != 0:
            raise GhError(_NOT_LOGGED_IN)
        return _auth_from_text(res.stdout + "\n" + res.stderr)


_NOT_LOGGED_IN = "the GitHub CLI is not logged in; run `gh auth login`"


def _scopes(text: str) -> frozenset[str] | None:
    names = {part.strip().strip("'\"") for part in text.split(",")}
    names.discard("")
    return frozenset(names) or None


def _auth_from_json(text: str, host: str) -> AuthInfo:
    try:
        data = json.loads(text)
        accounts = data["hosts"].get(host) or []
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GhError("`gh auth status` printed unexpected output") from None
    active = next((a for a in accounts if isinstance(a, dict) and a.get("active")), None)
    if active is None or active.get("state") != "success":
        raise GhError(_NOT_LOGGED_IN)
    scopes = active.get("scopes")
    return AuthInfo(
        login=str(active.get("login") or "?"),
        scopes=_scopes(scopes) if isinstance(scopes, str) else None,
        source=str(active.get("tokenSource") or ""),
    )


def _auth_from_text(text: str) -> AuthInfo:
    login = re.search(r"(?:account|as) (\S+)", text)
    scopes = re.search(r"Token scopes:\s*([^\n]*)", text)
    return AuthInfo(
        login=login.group(1) if login else "?",
        scopes=_scopes(scopes.group(1)) if scopes else None,
    )
