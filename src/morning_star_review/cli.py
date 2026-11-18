"""Command-line interface ``msreview`` (argparse; heavy modules are imported lazily).

Commands: ``review <PR URL>``, ``ci`` (used by the Action), ``doctor``, ``setup``,
``app-check <owner>/<repo>`` and the hidden ``_mint`` (the token-minting child).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from morning_star_review._version import __version__

if TYPE_CHECKING:
    from morning_star_review.core.contract import OperatorPolicy, RunOverrides
    from morning_star_review.driver import DriverSecrets
    from morning_star_review.github.mint_child import MintResult

__all__ = ["build_parser", "main"]

PRESETS = ("best", "balanced", "single", "fast", "mistral")
EFFORTS = ("default", "low", "medium", "high", "max")
FAIL_ON = ("never", "findings", "high", "critical")
CLI_DEADLINE_MINUTES = 45
CI_DEADLINE_MINUTES = 40
# A GitHub App installation token is minted once, before the run, and expires after 60
# minutes; publish (and report_failure) must still be able to use it (SEC-CI-7).
MAX_DEADLINE_MINUTES = 55
DEFAULT_OUT = "msreview-reports"


class UsageError(Exception):
    """Bad flags, environment or configuration (exit code 2)."""


def _err(message: str) -> None:
    print(f"msreview: error: {message}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- parser


def _add_review_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("pr_url", help="pull request URL (https://github.com/o/r/pull/N or o/r#N)")
    p.add_argument("--post", choices=("auto", "never"), default="auto")
    p.add_argument("--preset", choices=PRESETS, default=None)
    p.add_argument(
        "--explorers", default=None, help="comma-separated explorer models (an ensemble)"
    )
    p.add_argument("--explorer-model", default=None, help="a single explorer model")
    p.add_argument("--formatter-model", default=None)
    p.add_argument("--effort", choices=EFFORTS, default=None, help="explorer reasoning effort")
    verify = p.add_mutually_exclusive_group()
    verify.add_argument("--verify", dest="verify", action="store_true", default=None)
    verify.add_argument("--no-verify", dest="verify", action="store_false")
    p.add_argument("--verifier-model", default=None)
    p.add_argument("--verifier-effort", choices=EFFORTS, default=None)
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--max-tool-calls", type=int, default=None)
    p.add_argument("--explorer-minutes", type=int, default=None)
    p.add_argument("--verifier-max-tool-calls", type=int, default=None)
    p.add_argument("--verifier-minutes", type=int, default=None)
    p.add_argument("--deadline-minutes", type=int, default=CLI_DEADLINE_MINUTES)
    p.add_argument("--expected-head-sha", default=None, help="skip if the PR head moved")
    p.add_argument("--out", default=DEFAULT_OUT, help="report directory")
    p.add_argument("--json", action="store_true", help="print the outcome as JSON")
    p.add_argument("--force", action="store_true", help="review drafts, closed or reviewed PRs")
    p.add_argument(
        "--trust-repo-config",
        action="store_true",
        help="let the repository's config choose models, budgets, exclusions and instructions",
    )
    p.add_argument("--read-token", choices=("env", "gh", "none"), default=None)
    p.add_argument("--local", action="store_true", help="run in-process (no Workflows worker)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--telemetry", action="store_true")
    p.add_argument("--fail-on", choices=FAIL_ON, default=None)
    p.add_argument("--no-labels", action="store_true", help="do not set verdict labels")
    p.add_argument("--no-suggestions", action="store_true", help="no suggestion blocks")
    p.add_argument("--keep-run-dir", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--agent-python", default=None, help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msreview",
        description="Review GitHub pull requests with Mistral-hosted models as a Mistral Workflow.",
    )
    parser.add_argument("--version", action="version", version=f"msreview {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    review = sub.add_parser("review", help="review one pull request")
    _add_review_options(review)
    sub.add_parser("ci", help="run inside the GitHub Action (reads MSREVIEW_* variables)")
    doctor = sub.add_parser("doctor", help="check the key, Workflows access and the setup")
    doctor.add_argument("--models", action="store_true", help="one tiny call per model")
    doctor.add_argument("--deployments", action="store_true", help="list msreview deployments")
    setup = sub.add_parser("setup", help="install the agent environment")
    setup.add_argument("--source", default=None, help="pip spec or path of this package")
    setup.add_argument("--force", action="store_true", help="reinstall")
    setup.add_argument("--check", action="store_true", help="only check the installation")
    app = sub.add_parser("app-check", help="is the GitHub App installed on a repository?")
    app.add_argument("repo", help="owner/repo")
    return parser


# ----------------------------------------------------------------------------- helpers


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _bool_text(value: str, name: str) -> bool:
    text = value.strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise UsageError(f"{name} must be true or false, not {value!r}")


def _opt_bool(value: str, name: str) -> bool | None:
    return None if not value.strip() else _bool_text(value, name)


def _opt_int(value: str, name: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise UsageError(f"{name} must be a whole number, not {value!r}") from None


def _opt_float(value: str, name: str) -> float | None:
    if not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        raise UsageError(f"{name} must be a number, not {value!r}") from None


def _opt_str(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def build_overrides(**values: Any) -> RunOverrides:
    """``RunOverrides`` from flag/input values (``None`` = not set)."""
    from pydantic import ValidationError

    from morning_star_review.core.contract import RunOverrides

    data = {k: v for k, v in values.items() if v is not None}
    try:
        return RunOverrides.model_validate(data)
    except ValidationError as exc:
        raise UsageError(_first_error(exc)) from None


def build_policy(
    *,
    trust_repo_config: bool,
    overrides: RunOverrides,
    allowed_models: list[str] | None = None,
    review_drafts: bool = False,
    labels: bool = True,
    suggestions: bool = True,
    default_preset: str | None = None,
) -> OperatorPolicy:
    """The operator policy. An explicit budget above a default cap raises that cap.

    ``default_preset`` is the operator's default (user config ``[defaults] preset``):
    it only replaces the built-in default, so a trusted repository file still wins
    over it. Only ``--preset`` / the Action ``preset`` input is an explicit run preset.
    """
    from pydantic import ValidationError

    from morning_star_review.core.contract import OperatorPolicy

    base = OperatorPolicy(trust_repo_config_models=trust_repo_config)
    data: dict[str, Any] = {
        "trust_repo_config_models": trust_repo_config,
        "review_drafts": review_drafts,
        "labels": labels,
        "suggestions": suggestions,
    }
    if default_preset is not None:
        if default_preset not in PRESETS:
            raise UsageError(f"config file: defaults.preset must be one of {', '.join(PRESETS)}")
        data["default_preset"] = default_preset
    if allowed_models:
        data["allowed_models"] = allowed_models
    raises = {
        "max_cost_usd_cap": overrides.max_cost_usd,
        "max_tool_calls_cap": overrides.max_tool_calls,
        "explorer_minutes_cap": overrides.explorer_minutes,
        "verifier_tool_calls_cap": overrides.verifier_max_tool_calls,
        "verifier_minutes_cap": overrides.verifier_minutes,
    }
    for name, value in raises.items():
        if value is not None and value > getattr(base, name):
            data[name] = value
    try:
        return OperatorPolicy.model_validate(data)
    except ValidationError as exc:
        raise UsageError(_first_error(exc)) from None


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        items = errors()
        if items:
            first = items[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))
            return f"invalid setting {loc}: {first.get('msg', 'invalid value')}"
    return "invalid settings"


def mint_reason(res: MintResult, owner: str, repo: str) -> str | None:
    """The report-mode reason for a mint result that is not ``ok`` (WP4's texts)."""
    from morning_star_review.github.publish import report_reason

    if res.status == "ok":
        return None
    if res.status == "not_installed":
        return report_reason("not_installed", slug=res.slug or "?", owner=owner, repo=repo)
    if res.status == "no_credentials":
        return report_reason("no_credentials")
    if res.status == "insufficient":
        return report_reason("insufficient", owner=owner, repo=repo)
    return f"Not posted: minting a GitHub App token failed ({res.reason or 'unknown error'})."


def read_token_from(mode: str, env: Mapping[str, str]) -> str | None:
    """Optional read-only token: ``env`` (GITHUB_TOKEN / GH_TOKEN), ``gh`` or ``none``."""
    if mode == "none":
        return None
    if mode == "env":
        return _opt_str(env.get("GITHUB_TOKEN")) or _opt_str(env.get("GH_TOKEN"))
    from morning_star_review.core.proc import absolute_path_entries

    # Absolute PATH entries only: a relative one would find a "gh" in the cwd.
    gh = shutil.which("gh", path=absolute_path_entries(env.get("PATH")))
    if gh is None:
        raise UsageError("--read-token gh needs the GitHub CLI (gh) on PATH")
    try:
        res = subprocess.run(
            [gh, "auth", "token"], capture_output=True, timeout=30, check=False, text=True
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UsageError("`gh auth token` did not run") from None
    token = res.stdout.strip()
    if res.returncode != 0 or not token:
        raise UsageError("`gh auth token` returned no token; run `gh auth login`")
    return token


def resolve_agent_python(
    explicit: str | None, env: Mapping[str, str], configured: str | None
) -> str:
    """The agent environment's python: flag, env, config file, then the setup default."""
    from morning_star_review.usersettings import ENV_AGENT_PYTHON, default_agent_python

    for candidate in (explicit, env.get(ENV_AGENT_PYTHON), configured):
        if candidate and candidate.strip():
            path = Path(candidate.strip()).expanduser()
            if not path.exists():
                raise UsageError(f"the agent python {path} does not exist")
            return str(path)
    default = default_agent_python(env)
    if not default.exists():
        raise UsageError(
            f"the agent environment is not installed; run `msreview setup` (expected {default})"
        )
    return str(default)


@contextlib.contextmanager
def stdout_to_stderr() -> Iterator[None]:
    """Send fd 1 to stderr while the run is in progress, so stdout carries only results."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


# ----------------------------------------------------------------------------- review


async def _mint_secrets(
    key: str, repo_full: str, post: str, read_token: str | None
) -> DriverSecrets:
    from morning_star_review.driver import DriverSecrets
    from morning_star_review.github.mint_child import mint_via_child

    owner, repo = repo_full.split("/", 1)
    if post == "never":
        return DriverSecrets(mistral_api_key=key, read_token=read_token)
    res = await mint_via_child(repo_full)
    reason = mint_reason(res, owner, repo)
    if reason is not None:
        print(f"msreview: report mode: {reason}", file=sys.stderr, flush=True)
        return DriverSecrets(mistral_api_key=key, read_token=read_token, cannot_post_reason=reason)
    print(f"msreview: posting as {res.bot_login}", file=sys.stderr, flush=True)
    return DriverSecrets(
        mistral_api_key=key, github_token=res.token, read_token=read_token, bot_login=res.bot_login
    )


def _absolutize_env_paths() -> None:
    """Make relative ``MSREVIEW_CONFIG`` / key paths absolute in ``os.environ`` (the mint
    child reads them), so they keep naming the same files after the move to an empty cwd."""
    for name in ("MSREVIEW_CONFIG", "MSREVIEW_APP_PRIVATE_KEY_PATH"):
        value = os.environ.get(name, "").strip()
        if value and not Path(value).expanduser().is_absolute():
            os.environ[name] = str(Path(value).expanduser().resolve())


def cmd_review(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    from morning_star_review import driver
    from morning_star_review.core.naming import parse_pr_url
    from morning_star_review.usersettings import load_mistral_key, load_user_settings

    src = os.environ if env is None else env
    settings = load_user_settings(src)
    # Resolve relative paths before the driver moves the process to an empty cwd.
    if args.out and Path(args.out).expanduser().is_symlink():
        # A checked-out PR could ship ./msreview-reports as a link to any directory.
        raise UsageError(f"--out {args.out} is a symbolic link; pass the directory it points to")
    out_dir = Path(args.out).expanduser().resolve() if args.out else None
    _absolutize_env_paths()  # the mint child starts after the move
    driver.prepare_process_env(telemetry=args.telemetry, verbose=args.verbose)
    try:
        ref = parse_pr_url(args.pr_url)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    overrides = build_overrides(
        preset=args.preset,  # explicit run preset; the user config's preset is a default
        explorers=_csv(args.explorers),
        explorer_model=_opt_str(args.explorer_model),
        formatter_model=_opt_str(args.formatter_model),
        explorer_effort=args.effort,
        verify=args.verify,
        verifier_model=_opt_str(args.verifier_model),
        verifier_effort=args.verifier_effort,
        max_cost_usd=args.max_cost_usd if args.max_cost_usd is not None else settings.max_cost_usd,
        max_tool_calls=args.max_tool_calls,
        explorer_minutes=args.explorer_minutes,
        verifier_max_tool_calls=args.verifier_max_tool_calls,
        verifier_minutes=args.verifier_minutes,
    )
    policy = build_policy(
        trust_repo_config=args.trust_repo_config,
        overrides=overrides,
        allowed_models=settings.allowed_models,
        labels=not args.no_labels,
        suggestions=not args.no_suggestions,
        default_preset=settings.preset,
    )
    agent_python = resolve_agent_python(args.agent_python, src, settings.agent_python)
    if not 0 < args.deadline_minutes <= MAX_DEADLINE_MINUTES:
        raise UsageError(
            f"--deadline-minutes must be between 1 and {MAX_DEADLINE_MINUTES} (the GitHub "
            "App token the run posts with expires after 60 minutes)"
        )
    key = load_mistral_key(src, settings)
    read_token = read_token_from(args.read_token or settings.read_token, src)
    fail_on = args.fail_on or settings.fail_on or "never"
    if fail_on not in FAIL_ON:
        raise UsageError(f"fail-on must be one of {', '.join(FAIL_ON)}")
    cfg = driver.DriverConfig(
        mode="local" if args.local else "cli",
        pr_url=ref.canonical_url,
        expected_head_sha=_opt_str(args.expected_head_sha),
        post=args.post,
        force=args.force,
        overrides=overrides,
        policy=policy,
        run_label=f"cli:{secrets.token_hex(4)}",
        deadline_s=args.deadline_minutes * 60,
        out_dir=out_dir,
        fail_on=fail_on,  # type: ignore[arg-type]
        verbose=args.verbose,
        telemetry=args.telemetry,
        agent_python=agent_python,
        keep_run_dir=args.keep_run_dir,
    )

    async def go() -> tuple[Any, int]:
        sec = await _mint_secrets(key, ref.full_name, args.post, read_token)
        return await driver.run_review(cfg, sec)

    with stdout_to_stderr():
        outcome, code = asyncio.run(go())
    if args.json:
        sys.stdout.write(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False) + "\n")
    else:
        from morning_star_review.core.sanitize import without_controls

        text = outcome.report_markdown or f"{outcome.status}: {outcome.reason or ''}".strip()
        text = without_controls(text)  # PR paths and code: no terminal escape sequences
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()
    return int(code)


# ----------------------------------------------------------------------------- ci


def _ci_settings(env: Mapping[str, str]) -> dict[str, Any]:
    """Parse the ``MSREVIEW_*`` contract of the Action (DESIGN 10.2 + ADDENDUM)."""

    def get(name: str) -> str:
        return env.get(f"MSREVIEW_{name}", "")

    post = get("POST").strip() or "auto"
    if post not in ("auto", "never"):
        raise UsageError(f"post must be auto or never, not {post!r}")
    fail_on = get("FAIL_ON").strip() or "never"
    if fail_on not in FAIL_ON:
        raise UsageError(f"fail-on must be one of {', '.join(FAIL_ON)}")
    pr_url = get("PR_URL").strip()
    if not pr_url:
        raise UsageError("MSREVIEW_PR_URL is empty; run the Action on pull_request events")
    deadline = _opt_int(get("DEADLINE_MINUTES"), "deadline-minutes") or CI_DEADLINE_MINUTES
    if not 0 < deadline <= MAX_DEADLINE_MINUTES:
        raise UsageError(
            f"deadline-minutes must be between 1 and {MAX_DEADLINE_MINUTES} (the GitHub "
            "App token the run posts with expires after 60 minutes)"
        )
    return {
        "post": post,
        "fail_on": fail_on,
        "pr_url": pr_url,
        "head_sha": _opt_str(get("HEAD_SHA")),
        "run_label": _opt_str(get("RUN_LABEL")) or f"gha:{secrets.token_hex(4)}",
        "deadline_s": deadline * 60,
        "telemetry": bool(_opt_bool(get("TELEMETRY"), "telemetry")),
        "verbose": bool(_opt_bool(get("VERBOSE"), "verbose")),
        "labels": _opt_bool(get("LABELS"), "labels") is not False,
        "suggestions": _opt_bool(get("SUGGESTIONS"), "suggestions") is not False,
        "review_drafts": bool(_opt_bool(get("REVIEW_DRAFTS"), "review-drafts")),
        "allowed_models": _csv(get("ALLOWED_MODELS")),
        "remove_label": _opt_str(get("REMOVE_LABEL")),
        "report_dir": _opt_str(get("REPORT_DIR")),
        "run_root": _opt_str(get("RUN_ROOT")),
        "agent_python": _opt_str(get("AGENT_PYTHON")),
        "app_slug": _opt_str(get("APP_SLUG")),
        "token_step": _opt_str(get("TOKEN_STEP")) or "skipped",
        "overrides": {
            "preset": _opt_str(get("PRESET")),
            "explorers": _csv(get("EXPLORERS")),
            "explorer_model": _opt_str(get("EXPLORER_MODEL")),
            "formatter_model": _opt_str(get("FORMATTER_MODEL")),
            "explorer_effort": _opt_str(get("EXPLORER_EFFORT")),
            "verify": _opt_bool(get("VERIFY"), "verify"),
            "verifier_model": _opt_str(get("VERIFIER_MODEL")),
            "verifier_effort": _opt_str(get("VERIFIER_EFFORT")),
            "max_cost_usd": _opt_float(get("MAX_COST_USD"), "max-cost-usd"),
            "max_tool_calls": _opt_int(get("MAX_TOOL_CALLS"), "max-tool-calls"),
            "explorer_minutes": _opt_int(get("EXPLORER_MINUTES"), "explorer-minutes"),
            "verifier_max_tool_calls": _opt_int(
                get("VERIFIER_MAX_TOOL_CALLS"), "verifier-max-tool-calls"
            ),
            "verifier_minutes": _opt_int(get("VERIFIER_MINUTES"), "verifier-minutes"),
        },
    }


def ci_secrets(
    env: dict[str, str] | os._Environ[str], key: str, post: str, app_slug: str | None, step: str
) -> DriverSecrets:
    """Pop the tokens from the environment and decide whether posting is possible."""
    from morning_star_review.driver import DriverSecrets
    from morning_star_review.github.publish import report_reason

    token = _opt_str(env.pop("MSREVIEW_GITHUB_TOKEN", None))
    read_token = _opt_str(env.pop("MSREVIEW_READ_TOKEN", None))
    reason: str | None = None
    if post != "never" and step in ("failure", "cancelled"):
        reason = report_reason("token_step_failed", outcome=step)
        token = None
    bot = f"{app_slug}[bot]" if app_slug and token else None
    return DriverSecrets(
        mistral_api_key=key,
        github_token=token if post != "never" else None,
        read_token=read_token,
        bot_login=bot,
        cannot_post_reason=reason,
    )


async def remove_pr_label(token: str, pr_url: str, name: str) -> str:
    """Best effort: remove ``name`` (the fork approval label) from the PR."""
    from morning_star_review.core.naming import parse_pr_url
    from morning_star_review.github.http import GitHubHttp
    from morning_star_review.github.labels import remove_label

    ref = parse_pr_url(pr_url)
    try:
        async with GitHubHttp(token) as gh:
            removed = await remove_label(gh, ref.owner, ref.repo, ref.number, name)
    except Exception as exc:
        return f"could not remove the label {name!r} ({type(exc).__name__})"
    return f"removed the label {name!r}" if removed else f"the label {name!r} was not set"


def cmd_ci(env: dict[str, str] | os._Environ[str] | None = None) -> int:
    """``msreview ci``: the Action's entry point (environment contract of DESIGN 10.2)."""
    from morning_star_review import driver, outputs
    from morning_star_review.core.errors import MsreviewError
    from morning_star_review.usersettings import (
        SettingsError,
        load_mistral_key,
        load_user_settings,
    )

    src = os.environ if env is None else env
    stop = outputs.StopCommands()
    stop.start()
    key = ""
    try:
        s = _ci_settings(src)
        settings = load_user_settings(src)
        try:
            key = load_mistral_key(src, settings)
        except SettingsError:
            # No key at all: the CLI's remedies (variables, config file) do not apply to
            # the Action, whose key comes from its `mistral-api-key` input.
            raise SettingsError(driver.CI_NO_KEY) from None
        sec = ci_secrets(src, key, s["post"], s["app_slug"], s["token_step"])
        # The token is needed again after the run (label removal); keep it in memory only.
        write_token = sec.github_token
        runner_temp = src.get("RUNNER_TEMP", "").strip() or None
        report_dir = Path(
            s["report_dir"]
            or (str(Path(runner_temp) / "msreview" / "report") if runner_temp else DEFAULT_OUT)
        ).resolve()
        run_root = Path(s["run_root"]).resolve() if s["run_root"] else None
        driver.prepare_process_env(telemetry=s["telemetry"], verbose=s["verbose"], env=src)
        overrides = build_overrides(**s["overrides"])
        policy = build_policy(
            trust_repo_config=True,
            overrides=overrides,
            allowed_models=s["allowed_models"],
            review_drafts=s["review_drafts"],
            labels=s["labels"],
            suggestions=s["suggestions"],
        )
        agent_python = resolve_agent_python(s["agent_python"], src, settings.agent_python)
        cfg = driver.DriverConfig(
            mode="ci",
            pr_url=s["pr_url"],
            expected_head_sha=s["head_sha"],
            post=s["post"],
            force=False,
            overrides=overrides,
            policy=policy,
            run_label=s["run_label"],
            deadline_s=s["deadline_s"],
            out_dir=report_dir,
            fail_on=s["fail_on"],
            verbose=s["verbose"],
            telemetry=s["telemetry"],
            agent_python=agent_python,
            run_root=run_root,
            stop_commands=stop,
        )
        try:
            _outcome, code = asyncio.run(driver.run_review(cfg, sec))
        finally:
            # The fork template's approval label goes away after every attempt.
            if s["remove_label"] and write_token:
                note = asyncio.run(remove_pr_label(write_token, s["pr_url"], s["remove_label"]))
                print(f"msreview: {note}", file=sys.stderr, flush=True)
        return int(code)
    except (UsageError, MsreviewError) as exc:
        # Exit 2 before or around the run: tell Actions why (once; run_review may have).
        driver.report_ci_usage_failure(
            exc,
            secrets=[key] if key else [],
            stop_commands=stop,
            pr_url=src.get("MSREVIEW_PR_URL", ""),
            env=src,
        )
        raise
    finally:
        stop.resume()


# ----------------------------------------------------------------------------- others


def cmd_app_check(repo: str) -> int:
    from morning_star_review.github.app_auth import missing_permissions
    from morning_star_review.github.mint_child import mint_via_child

    if repo.count("/") != 1:
        raise UsageError("expected owner/repo")
    res = asyncio.run(mint_via_child(repo, check_only=True))
    owner, name = repo.split("/", 1)
    if res.status != "ok":
        print(mint_reason(res, owner, name) or res.status)
        return 1
    print(f"The GitHub App '{res.slug}' is installed on {repo}.")
    for perm, level in sorted(res.permissions.items()):
        print(f"  {perm}: {level}")
    missing = missing_permissions(res.permissions)
    if missing:
        print("Missing: " + ", ".join(missing))
        return 1
    if not res.can_label:
        print("Note: without Issues: write the verdict labels cannot be set.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "_mint":
        from morning_star_review.github.mint_child import main as mint_main

        return mint_main(args_list[1:])
    parser = build_parser()
    args = parser.parse_args(args_list)
    from morning_star_review.core.errors import MsreviewError
    from morning_star_review.driver import DriverError

    try:
        if args.command == "review":
            return cmd_review(args)
        if args.command == "ci":
            return cmd_ci()
        if args.command == "doctor":
            from morning_star_review.doctor import run_doctor

            return run_doctor(models=args.models, deployments=args.deployments)
        if args.command == "setup":
            from morning_star_review.setup_agent import run_setup

            return run_setup(source=args.source, force=args.force, check=args.check)
        if args.command == "app-check":
            return cmd_app_check(args.repo)
    except (UsageError, DriverError, MsreviewError) as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        return 130
    parser.print_help(sys.stderr)
    return 2
