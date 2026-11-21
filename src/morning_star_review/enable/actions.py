"""What each command does to one repository (the results are printed by ``commands``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from morning_star_review.enable import template as tpl
from morning_star_review.enable.gh import GhError
from morning_star_review.enable.ops import (
    LEGACY_SECRET_MISTRAL,
    SECRET_APP_KEY,
    SECRET_MISTRAL,
    VAR_CLIENT_ID,
    RemoteFile,
    Repo,
)
from morning_star_review.enable.values import Values

__all__ = [
    "Result",
    "disable_one",
    "legacy_hint",
    "moved_warning",
    "publish",
    "status_one",
    "untouched_note",
    "update_one",
    "write_values",
]

PR_BODY = (
    "This pull request was opened by `msreview {command}` "
    "(https://github.com/ThomsenDrake/morning-star-code-reviews)."
)


@dataclass
class Result:
    repo: str
    ok: bool
    text: str
    details: list[str] = field(default_factory=list)


def publish(
    repo: Repo,
    *,
    text: str | None,
    existing: RemoteFile | None,
    message: str,
    pr_branch: str | None,
    command: str,
) -> str:
    """Write (``text``) or delete (``None``) the workflow file on the default branch, or
    on ``pr_branch`` with a pull request; a short description of what happened."""
    base = repo.default_branch()
    branch = base
    if pr_branch is not None:
        url = repo.find_pr(pr_branch)
        if url is not None:  # an open PR that already makes this change: no new commit
            proposed = repo.get_file(tpl.WORKFLOW_PATH, pr_branch)
            if (proposed.text if proposed else None) == text:
                return f"already proposed in {url}"
        repo.reset_branch(pr_branch, repo.branch_sha(base))
        branch = pr_branch
    try:
        if text is None:
            if existing is None:
                return "no workflow file"
            commit = repo.delete_file(
                tpl.WORKFLOW_PATH, branch=branch, sha=existing.sha, message=message
            )
        else:
            commit = repo.put_file(
                tpl.WORKFLOW_PATH,
                text,
                branch=branch,
                sha=existing.sha if existing else None,
                message=message,
            )
    except GhError as exc:
        if pr_branch is None and exc.status in (403, 409, 422):
            raise GhError(f"{exc} (a protected branch? --pr opens a pull request)") from None
        raise
    if pr_branch is None:
        return f"committed {commit[:7]} to {base}"
    url, new = repo.open_pr(pr_branch, base, message, PR_BODY.format(command=command))
    return f"{'opened' if new else 'updated'} {url}"


def legacy_hint(full: str) -> str:
    """How to move a workflow from the secret ``MISTRAL_API_KEY`` to msreview's own name."""
    return (
        f"the workflow uses the secret {LEGACY_SECRET_MISTRAL} (the 0.1.x name); to move to "
        f"{SECRET_MISTRAL}, run `msreview enable {full} --secrets-only`, then "
        f"`msreview update {full} --force` (it re-renders the file)"
    )


def moved_warning(repo: Repo, old: str, new: str) -> str | None:
    """A warning when ``new`` moves a 0.1.x workflow (``old``) from the secret
    ``MISTRAL_API_KEY`` to ``SECRET_MISTRAL`` while that secret is not set."""
    if tpl.uses_legacy_secret(new) or not tpl.uses_legacy_secret(old):
        return None
    if _names(repo.secret_names, (SECRET_MISTRAL,))[SECRET_MISTRAL] is not False:
        return None  # set, or unknown (the secrets cannot be listed)
    return (
        f"warning: {SECRET_MISTRAL} is not set here, so reviews fail until you run "
        f"`msreview enable {repo.full} --secrets-only`"
    )


def untouched_note(repo: Repo) -> str | None:
    """A note when ``repo`` has a secret ``MISTRAL_API_KEY`` of its own (msreview leaves
    it alone); ``None`` when it has none or the secrets cannot be listed."""
    try:
        names = {n.upper() for n in repo.secret_names()}
    except GhError:
        return None
    if LEGACY_SECRET_MISTRAL not in names:
        return None
    return (
        f"the existing secret {LEGACY_SECRET_MISTRAL} is left untouched "
        f"(msreview uses {SECRET_MISTRAL})"
    )


def write_values(repo: Repo, values: Values) -> None:
    """Set both secrets and the variable (values on gh's standard input). Only the
    ``MSREVIEW_`` names: a repository's own ``MISTRAL_API_KEY`` is never touched."""
    if values.mistral_key is None or values.private_key is None:
        raise GhError("internal error: the secret values were not collected")
    repo.set_secret(SECRET_MISTRAL, bytes(values.mistral_key))
    repo.set_secret(SECRET_APP_KEY, bytes(values.private_key))
    repo.set_variable(VAR_CLIENT_ID, values.client_id)


def update_one(
    repo: Repo, tag: str, sha: str, *, force: bool, pr: bool, missing_ok: bool = False
) -> Result | None:
    """Pin the workflow to ``tag``/``sha``; ``None`` when it has no workflow and
    ``missing_ok`` (``--all``)."""
    existing = repo.get_file(tpl.WORKFLOW_PATH, repo.default_branch())
    if existing is None:
        if missing_ok:
            return None
        return Result(repo.full, False, f"no {tpl.WORKFLOW_PATH}; run `msreview enable`")
    preset = tpl.find_preset(existing.text)
    pin = tpl.find_pin(existing.text)
    fresh = tpl.render(tag, sha, preset)
    # Only the pinned release changes (edits are kept); --force re-renders the file.
    new = fresh if force else tpl.repin(existing.text, tag, sha)
    if new is None:
        return Result(repo.full, False, f"the workflow does not use {tpl.ACTION_REPO}")
    details: list[str] = []
    if tpl.uses_legacy_secret(new):  # kept as it is: only --force changes the name
        details.append(legacy_hint(repo.full))
    warning = moved_warning(repo, existing.text, new)  # --force: is the new name set?
    if warning is not None:
        details.append(warning)
    if new == existing.text:
        return Result(repo.full, True, f"already at {tag}", details)
    how = "" if new == fresh else " (kept the file's other lines; --force re-renders it)"
    done = publish(
        repo,
        text=new,
        existing=existing,
        message=f"Update Morning Star Code Review to {tag}",
        pr_branch="msreview/update" if pr else None,
        command="update",
    )
    old = pin.describe() if pin else "?"
    return Result(repo.full, True, f"updated {old} -> {tag}: {done}{how}", details)


def disable_one(repo: Repo, *, keep_secrets: bool, pr: bool) -> Result:
    existing = repo.get_file(tpl.WORKFLOW_PATH, repo.default_branch())
    notes: list[str] = []
    if existing is not None:
        done = publish(
            repo,
            text=None,
            existing=existing,
            message="Disable Morning Star Code Review",
            pr_branch="msreview/disable" if pr else None,
            command="disable",
        )
        # With --pr nothing is removed until the pull request is merged.
        notes.append(f"workflow {'removal' if pr else 'removed'}: {done}")
    else:
        notes.append("no workflow file")
    details: list[str] = []
    if not keep_secrets:
        # Only msreview's own names; MISTRAL_API_KEY may belong to other workflows.
        gone = [n for n in (SECRET_MISTRAL, SECRET_APP_KEY) if repo.delete_secret(n)]
        if repo.delete_variable(VAR_CLIENT_ID):
            gone.append(VAR_CLIENT_ID)
        notes.append(f"deleted {', '.join(gone)}" if gone else "no secrets or variable to delete")
        if pr and existing is not None:
            notes.append("until the PR is merged, reviews fail for lack of the secrets")
        if existing is not None and tpl.uses_legacy_secret(existing.text):
            details.append(
                f"the workflow used the secret {LEGACY_SECRET_MISTRAL}: msreview leaves it in "
                "place (other workflows may use it); delete it yourself if nothing else does"
            )
    return Result(repo.full, True, "; ".join(notes), details)


def status_one(repo: Repo) -> dict[str, Any]:
    """What is set up on ``repo`` (names only: secret values cannot be read)."""
    out: dict[str, Any] = {"repo": repo.full}
    existing = repo.get_file(tpl.WORKFLOW_PATH, repo.default_branch())
    pin = tpl.find_pin(existing.text) if existing else None
    out["workflow"] = {
        "present": existing is not None,
        "version": pin.version if pin else None,
        "sha": pin.sha if pin else None,
        "pinned": pin.describe() if pin else None,
        "preset": tpl.find_preset(existing.text) if existing else None,
    }
    # A 0.1.x workflow maps secrets.MISTRAL_API_KEY: report the name it really uses.
    legacy = existing is not None and tpl.uses_legacy_secret(existing.text)
    out["legacy_secret"] = LEGACY_SECRET_MISTRAL if legacy else None
    mistral = LEGACY_SECRET_MISTRAL if legacy else SECRET_MISTRAL
    out["secrets"] = _names(repo.secret_names, (mistral, SECRET_APP_KEY))
    out["variables"] = _names(repo.variable_names, (VAR_CLIENT_ID,))
    run = repo.last_run(tpl.WORKFLOW_FILE) if existing else None
    out["last_run"] = (
        {
            "conclusion": run.get("conclusion") or run.get("status"),
            "created_at": run.get("created_at"),
            "url": run.get("html_url"),
        }
        if run
        else None
    )
    return out


def _names(lister: Any, wanted: tuple[str, ...]) -> dict[str, bool | None]:
    try:
        names = lister()
    except GhError:
        return dict.fromkeys(wanted)  # None: unknown (no access)
    return {name: name in names for name in wanted}
