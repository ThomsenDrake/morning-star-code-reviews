"""``msreview enable | status | update | disable``: argument handling and output."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TextIO

from morning_star_review._version import __version__
from morning_star_review.enable import template as tpl
from morning_star_review.enable.actions import (
    Result,
    commands_hint,
    disable_one,
    legacy_hint,
    moved_warning,
    publish,
    status_one,
    untouched_note,
    update_one,
    write_values,
)
from morning_star_review.enable.appapi import AppApi, HttpAppApi
from morning_star_review.enable.collect import (
    check_login,
    client_id_from,
    collect_values,
    listing_key,
    verify_values,
)
from morning_star_review.enable.gh import Gh, GhError, find_gh
from morning_star_review.enable.installs import Installations, InstallCheck, resolve_release
from morning_star_review.enable.ops import Repo, parse_repo
from morning_star_review.enable.values import EnableUsageError, Prompts, Values
from morning_star_review.enable.verify import HttpVerifier, Verifier

__all__ = ["Deps", "run"]


@dataclass
class Deps:
    """Everything a command needs from the outside (tests replace parts of it)."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    prompts: Prompts = field(default_factory=Prompts.terminal)
    verifier: Verifier = field(default_factory=HttpVerifier)
    app_api: Callable[[str, bytes], AppApi] = HttpAppApi
    gh: Gh | None = None

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def note(self, text: str) -> None:
        print(f"msreview: {text}", file=self.err, flush=True)

    def warn(self, text: str) -> None:
        print(f"msreview: warning: {text}", file=self.err, flush=True)


def run(args: argparse.Namespace, deps: Deps | None = None) -> int:
    d = deps or Deps()
    gh = d.gh or Gh(find_gh(d.env), d.env)
    commands: dict[str, Callable[[argparse.Namespace, Gh, Deps], int]] = {
        "enable": cmd_enable,
        "status": cmd_status,
        "update": cmd_update,
        "disable": cmd_disable,
    }
    try:
        return commands[args.command](args, gh, d)
    except EOFError:  # Ctrl-D at a prompt
        raise EnableUsageError("no input") from None


def _repos(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        full = parse_repo(name)
        if full.lower() not in (o.lower() for o in out):
            out.append(full)
    return out


def _finish(results: list[Result], deps: Deps, verb: str) -> int:
    for r in results:
        deps.say(f"{r.repo}: {r.text}")
        for line in r.details:
            deps.say(f"  {line}")
    good = sum(1 for r in results if r.ok)
    if len(results) > 1:
        deps.note(f"{verb} {good} of {len(results)} repositories")
    return 0 if good == len(results) else 1


def _attempt(results: list[Result], full: str, step: Callable[[], Result | None]) -> None:
    """Run one repository's step; a GitHub failure is that repository's result."""
    try:
        res = step()
    except GhError as exc:
        res = Result(full, False, f"failed: {exc}")
    if res is not None:
        results.append(res)


# ----------------------------------------------------------------------------- enable


def cmd_enable(args: argparse.Namespace, gh: Gh, deps: Deps) -> int:
    repos = _repos(args.repos)
    if args.secrets_only and (args.preset or args.pr or args.release):
        deps.warn("--preset, --pr and --version have no effect with --secrets-only")
    workflow = not args.secrets_only
    secrets = not args.no_secrets
    check_login(gh, workflow=workflow, warn=deps.warn)
    tag, sha = resolve_release(gh, args.release or __version__) if workflow else ("", "")
    values = collect_values(
        args, deps.env, deps.prompts, secrets=secrets, warn=deps.warn, note=deps.note
    )
    api: AppApi | None = None
    try:
        if values.private_key is not None:
            api = deps.app_api(values.client_id, bytes(values.private_key))
        if args.verify:
            verify_values(values, deps.verifier, api, note=deps.note)
        installs = Installations(api)
        results: list[Result] = []
        for full in repos:
            _attempt(
                results,
                full,
                partial(_enable_one, Repo(gh, full), args, installs, values, tag, sha),
            )
    finally:
        values.clear()
        if api is not None:
            api.close()
    return _finish(results, deps, "enabled")


def _enable_one(
    repo: Repo,
    args: argparse.Namespace,
    installs: Installations,
    values: Values,
    tag: str,
    sha: str,
) -> Result:
    notes: list[str] = []
    repo.info()  # not found / no access: fail before anything else
    try:
        check = installs.check(repo.full)
    except GhError as exc:
        if not args.force:
            note = f"could not check the App installation ({exc}); --force skips the check"
            raise GhError(note) from None
        check = InstallCheck(None)
    if check.installed is None:
        notes.append("the App installation was not checked")
    elif not check.installed:
        if not args.force:
            return Result(repo.full, False, f"skipped: {check.reason} (--force enables it anyway)")
        notes.append("the App is not installed here yet")
    new: str | None = None
    existing = None
    if not args.secrets_only:
        new = tpl.render(tag, sha, args.preset)
        existing = repo.get_file(tpl.WORKFLOW_PATH, repo.default_branch())
        if existing is not None and existing.text == new:
            new = None
            notes.append(f"workflow already at {tag}")
        elif existing is not None and not args.force:
            pin = tpl.find_pin(existing.text)
            hint = ""
            if pin is not None and pin.version != tag:
                hint = f" (it uses {pin.describe()}; `msreview update` re-pins it)"
            return Result(
                repo.full,
                False,
                f"failed: a different {tpl.WORKFLOW_PATH} exists{hint}; --force replaces it",
                tpl.diff_summary(existing.text, new),
            )
    # msreview's names start with MSREVIEW_; a repository's own MISTRAL_API_KEY (often
    # used by other workflows) is never set, overwritten or deleted.
    untouched = untouched_note(repo)
    details = [untouched] if untouched else []
    # Secrets first: the workflow never exists without them.
    if not args.no_secrets:
        write_values(repo, values)
        notes.append("secrets and variable set")
    elif new is not None and existing is not None:
        # --force --no-secrets on a 0.1.x workflow: the new file needs SECRET_MISTRAL.
        warning = moved_warning(repo, existing.text, new)
        if warning is not None:
            details.append(warning)
    if new is not None:
        done = publish(
            repo,
            text=new,
            existing=existing,
            message="Enable Morning Star Code Review",
            pr_branch="msreview/enable" if args.pr else None,
            command="enable",
        )
        notes.append(f"workflow {tag}: {done}")
    return Result(repo.full, True, "enabled: " + "; ".join(notes), details)


# ----------------------------------------------------------------------------- others


def _app_repositories(args: argparse.Namespace, deps: Deps) -> list[str]:
    """Every repository the App's installations can see (asked as the App)."""
    client_id = client_id_from(args.client_id, deps.env, deps.note)
    key = listing_key(args, deps.env, warn=deps.warn, note=deps.note) if client_id else None
    if client_id is None or key is None:
        raise EnableUsageError(
            "name the repositories, or give the App's client id (--client-id) and private "
            "key (--app-key-file) so msreview can list the repositories it is installed on"
        )
    api = deps.app_api(client_id, bytes(key))
    key[:] = b"\0" * len(key)
    try:
        found = Installations(api).repositories()
    finally:
        api.close()
    if not found:
        deps.note(f"the App {client_id} is not installed on any repository")
    return found


def cmd_status(args: argparse.Namespace, gh: Gh, deps: Deps) -> int:
    check_login(gh, workflow=False, warn=deps.warn)
    repos = _repos(args.repos) if args.repos else _app_repositories(args, deps)
    rows: list[dict[str, object]] = []
    failed = False
    for full in repos:
        try:
            rows.append(status_one(Repo(gh, full)))
        except GhError as exc:
            failed = True
            rows.append({"repo": full, "error": str(exc)})
    if args.json:
        deps.say(json.dumps(rows, indent=2))
    else:
        for row in rows:
            for line in _status_lines(row):
                deps.say(line)
    return 1 if failed else 0


def _status_lines(row: dict[str, object]) -> list[str]:
    if "error" in row:
        return [f"{row['repo']}: {row['error']}"]
    wf = row.get("workflow")
    if not isinstance(wf, dict):
        return [f"{row['repo']}: no status"]
    if wf["present"]:
        text = str(wf["pinned"] or "does not use the Action")
        if wf["preset"]:
            text += f", preset {wf['preset']}"
    else:
        text = "not enabled"

    legacy = row.get("legacy_secret")

    def names(value: object) -> str:
        if not isinstance(value, dict):
            return "unknown"
        word = {True: "set", False: "missing", None: "unknown"}
        return ", ".join(
            f"{'legacy secret name ' if k == legacy else ''}{k} {word[v]}" for k, v in value.items()
        )

    run = row["last_run"]
    last = "none"
    if isinstance(run, dict):
        last = f"{run.get('conclusion') or '?'} ({str(run.get('created_at') or '')[:10]})"
    lines = [
        f"{row['repo']}",
        f"  workflow  {text}",
        f"  secrets   {names(row['secrets'])}",
        f"  variable  {names(row['variables'])}",
        f"  last run  {last}",
    ]
    if legacy:
        lines.append(f"  hint      {legacy_hint(str(row['repo']))}")
    if wf["present"] and wf.get("commands") is False:
        lines.append(f"  hint      {commands_hint(str(row['repo']))}")
    return lines


def cmd_update(args: argparse.Namespace, gh: Gh, deps: Deps) -> int:
    if bool(args.repos) == bool(args.all):
        raise EnableUsageError("name the repositories to update, or pass --all")
    check_login(gh, workflow=True, warn=deps.warn)
    tag, sha = resolve_release(gh, args.release or __version__)
    repos = _repos(args.repos) if args.repos else _app_repositories(args, deps)
    results: list[Result] = []
    for full in repos:
        _attempt(
            results,
            full,
            partial(
                update_one,
                Repo(gh, full),
                tag,
                sha,
                force=args.force,
                pr=args.pr,
                missing_ok=args.all,
            ),
        )
    if not results:
        deps.note("no repository has the workflow")
    return _finish(results, deps, "updated")


def cmd_disable(args: argparse.Namespace, gh: Gh, deps: Deps) -> int:
    check_login(gh, workflow=True, warn=deps.warn)
    results: list[Result] = []
    for full in _repos(args.repos):
        _attempt(
            results,
            full,
            partial(disable_one, Repo(gh, full), keep_secrets=args.keep_secrets, pr=args.pr),
        )
    return _finish(results, deps, "disabled")
