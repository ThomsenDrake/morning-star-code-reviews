"""One repository through ``gh``: the workflow file, secrets, variable, runs, PRs."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from morning_star_review.enable.gh import Gh, GhError
from morning_star_review.enable.values import EnableUsageError

__all__ = [
    "LEGACY_SECRET_MISTRAL",
    "SECRET_APP_KEY",
    "SECRET_MISTRAL",
    "VAR_CLIENT_ID",
    "RemoteFile",
    "Repo",
    "parse_repo",
]

# Every name msreview sets or deletes starts with MSREVIEW_, so it never collides with
# a repository's own secrets. A repository's MISTRAL_API_KEY (the name 0.1.x workflows
# used) may belong to other workflows: msreview never sets, overwrites or deletes it.
SECRET_MISTRAL = "MSREVIEW_MISTRAL_API_KEY"  # noqa: S105 - a secret name, not a value
SECRET_APP_KEY = "MSREVIEW_APP_PRIVATE_KEY"  # noqa: S105 - a secret name
VAR_CLIENT_ID = "MSREVIEW_APP_CLIENT_ID"
LEGACY_SECRET_MISTRAL = "MISTRAL_API_KEY"  # noqa: S105 - read-only: status and hints
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_URL_PREFIX = re.compile(r"^(?:https?://)?github\.com/", re.IGNORECASE)


def parse_repo(text: str) -> str:
    """``owner/repo`` from ``owner/repo`` or ``https://github.com/owner/repo(.git)``."""
    value = _URL_PREFIX.sub("", text.strip()).rstrip("/")
    value = value.removesuffix(".git")
    if not _REPO_RE.match(value) or value.split("/")[1] in (".", ".."):
        raise EnableUsageError(f"expected OWNER/REPO, not {text.strip()[:80]!r}")
    return value


@dataclass(frozen=True)
class RemoteFile:
    text: str
    sha: str


class Repo:
    """GitHub calls for one repository (the user's ``gh`` login)."""

    def __init__(self, gh: Gh, full_name: str) -> None:
        self.gh = gh
        self.full = full_name
        self.owner, self.name = full_name.split("/", 1)
        self._base = f"repos/{quote(self.owner, safe='')}/{quote(self.name, safe='')}"
        self._info: dict[str, Any] | None = None

    def info(self) -> dict[str, Any]:
        if self._info is None:
            data = self.gh.api(self._base, allow_404=True)
            if not isinstance(data, dict):
                raise GhError("not found, or your GitHub login cannot see it", 404)
            self._info = data
        return self._info

    def default_branch(self) -> str:
        branch = self.info().get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GhError("the repository has no default branch (is it empty?)")
        return branch

    # ------------------------------------------------------------------ files

    def get_file(self, path: str, ref: str) -> RemoteFile | None:
        data = self.gh.api(
            f"{self._base}/contents/{quote(path)}?ref={quote(ref, safe='')}", allow_404=True
        )
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GhError(f"{path} is not a file")
        try:
            text = base64.b64decode(str(data.get("content", ""))).decode("utf-8")
        except ValueError:
            raise GhError(f"{path} is not a UTF-8 text file") from None
        return RemoteFile(text=text, sha=str(data.get("sha", "")))

    def put_file(self, path: str, text: str, *, branch: str, sha: str | None, message: str) -> str:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        data = self.gh.api(f"{self._base}/contents/{quote(path)}", method="PUT", body=body)
        return _commit_sha(data)

    def delete_file(self, path: str, *, branch: str, sha: str, message: str) -> str:
        body = {"message": message, "sha": sha, "branch": branch}
        data = self.gh.api(f"{self._base}/contents/{quote(path)}", method="DELETE", body=body)
        return _commit_sha(data)

    # ------------------------------------------------------------------ branches and PRs

    def branch_sha(self, branch: str) -> str:
        data = self.gh.api(f"{self._base}/git/ref/heads/{quote(branch)}")
        sha = (data or {}).get("object", {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str):
            raise GhError(f"cannot read the head of {branch}")
        return sha

    def reset_branch(self, branch: str, sha: str) -> None:
        """Create ``branch`` at ``sha``, or move it there when it exists (our own branch)."""
        try:
            self.gh.api(
                f"{self._base}/git/refs",
                method="POST",
                body={"ref": f"refs/heads/{branch}", "sha": sha},
            )
        except GhError as exc:
            if exc.status != 422:  # 422: the branch exists
                raise
            self.gh.api(
                f"{self._base}/git/refs/heads/{quote(branch)}",
                method="PATCH",
                body={"sha": sha, "force": True},
            )

    def find_pr(self, head: str) -> str | None:
        """The URL of the open pull request from the branch ``head``, if there is one."""
        owner_head = quote(f"{self.owner}:{head}", safe="")
        found = self.gh.api(f"{self._base}/pulls?state=open&head={owner_head}")
        if isinstance(found, list) and found and isinstance(found[0], dict):
            return str(found[0].get("html_url", ""))
        return None

    def open_pr(self, head: str, base: str, title: str, body: str) -> tuple[str, bool]:
        """The URL of the open PR from ``head`` (created if needed) and whether it is new."""
        url = self.find_pr(head)
        if url is not None:
            return url, False
        data = self.gh.api(
            f"{self._base}/pulls",
            method="POST",
            body={"title": title, "head": head, "base": base, "body": body},
        )
        return str((data or {}).get("html_url", "")), True

    # ------------------------------------------------------------------ secrets and variables

    def secret_names(self) -> set[str]:
        items = self.gh.paged(f"{self._base}/actions/secrets", "secrets")
        return {str(i.get("name")) for i in items if isinstance(i, dict)}

    def variable_names(self) -> set[str]:
        items = self.gh.paged(f"{self._base}/actions/variables", "variables", per_page=30)
        return {str(i.get("name")) for i in items if isinstance(i, dict)}

    def set_secret(self, name: str, value: bytes) -> None:
        """``gh secret set``; the value goes on standard input (never on the argv)."""
        _own(name)
        res = self.gh.run(
            ["secret", "set", name, "--repo", self.full, "--app", "actions"], stdin=value
        )
        if res.returncode != 0:
            raise GhError(f"could not set the secret {name}: {res.brief()}", res.http_status)

    def set_variable(self, name: str, value: str) -> None:
        res = self.gh.run(
            ["variable", "set", name, "--repo", self.full], stdin=value.encode("utf-8")
        )
        if res.returncode != 0:
            raise GhError(f"could not set the variable {name}: {res.brief()}", res.http_status)

    def delete_secret(self, name: str) -> bool:
        _own(name)
        res = self.gh.run(["secret", "delete", name, "--repo", self.full, "--app", "actions"])
        return _deleted(res.returncode, res.stderr, f"the secret {name}", res.brief())

    def delete_variable(self, name: str) -> bool:
        res = self.gh.run(["variable", "delete", name, "--repo", self.full])
        return _deleted(res.returncode, res.stderr, f"the variable {name}", res.brief())

    # ------------------------------------------------------------------ runs

    def last_run(self, workflow_file: str) -> dict[str, Any] | None:
        data = self.gh.api(
            f"{self._base}/actions/workflows/{quote(workflow_file)}/runs?per_page=1",
            allow_404=True,
        )
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if isinstance(runs, list) and runs and isinstance(runs[0], dict):
            return runs[0]
        return None


def _own(name: str) -> None:
    """msreview writes and deletes only its own (``MSREVIEW_``) secrets."""
    if not name.startswith("MSREVIEW_"):
        raise GhError(f"internal error: msreview does not change the secret {name}")


def _commit_sha(data: Any) -> str:
    sha = data.get("commit", {}).get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) else ""


def _deleted(code: int, stderr: str, what: str, brief: str) -> bool:
    if code == 0:
        return True
    if "404" in stderr or "Not Found" in stderr:
        return False
    raise GhError(f"could not delete {what}: {brief}")
