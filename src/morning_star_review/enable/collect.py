"""Before any repository is touched: the GitHub CLI login, then the values (once)."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping

from morning_star_review.enable.appapi import AppApi
from morning_star_review.enable.gh import HOST, AuthInfo, Gh
from morning_star_review.enable.values import (
    ENV_APP_PRIVATE_KEY,
    ENV_CLIENT_ID,
    ENV_MISTRAL_KEY,
    EnableUsageError,
    Prompts,
    Values,
    check_client_id,
    check_mistral_key,
    check_private_key,
    configured_client_id,
    read_key_file,
)
from morning_star_review.enable.verify import Verifier

__all__ = ["check_login", "client_id_from", "collect_values", "listing_key", "verify_values"]


def check_login(gh: Gh, *, workflow: bool, warn: Callable[[str], None]) -> AuthInfo:
    """The ``gh`` login must have the ``repo`` scope, and ``workflow`` to write workflows."""
    info = gh.auth()
    need = ["repo", "workflow"] if workflow else ["repo"]
    if info.scopes is None:
        warn(
            f"cannot see the scopes of the GitHub CLI token ({info.login}); "
            f"it needs {' and '.join(need)}"
        )
        return info
    missing = [s for s in need if s not in info.scopes]
    if not missing:
        return info
    names = ",".join(missing)
    if info.source.upper().endswith("TOKEN"):  # GH_TOKEN / GITHUB_TOKEN: no refresh
        raise EnableUsageError(
            f"the token in {info.source} lacks the scope(s) {names}; use a token with "
            f"{' and '.join(need)}, or unset {info.source} and run `gh auth login`"
        )
    raise EnableUsageError(
        f"your GitHub CLI login ({info.login}) lacks the scope(s) {names}; "
        f"add them with: gh auth refresh -h {HOST} -s {names}"
    )


def client_id_from(
    flag: str | None,
    env: Mapping[str, str],
    note: Callable[[str], None],
    prompts: Prompts | None = None,
) -> str | None:
    """The App client id: ``--client-id``, ``MSREVIEW_ENABLE_APP_CLIENT_ID``, the CLI's own
    App settings, then a prompt (when ``prompts`` is interactive)."""
    value = (flag or "").strip() or env.get(ENV_CLIENT_ID, "").strip()
    if not value:
        configured, where = configured_client_id(env)
        if configured:
            note(f"App client id {configured} (from {where})")
            value = configured
    if not value and prompts is not None and prompts.interactive:
        value = prompts.text("GitHub App client id: ").strip()
    return check_client_id(value) if value else None


def collect_values(
    args: argparse.Namespace,
    env: Mapping[str, str],
    prompts: Prompts,
    *,
    secrets: bool,
    warn: Callable[[str], None],
    note: Callable[[str], None],
) -> Values:
    """Ask for (or read) every value once, check its format; nothing is stored yet."""
    # Without secrets and without a key (--no-secrets), the client id has no use.
    key_given = bool(args.app_key_file or env.get(ENV_APP_PRIVATE_KEY, "").strip())
    needed = secrets or key_given
    client_id = client_id_from(args.client_id, env, note, prompts if needed else None)
    if client_id is None and needed:
        raise EnableUsageError(
            f"no App client id: pass --client-id or set {ENV_CLIENT_ID} "
            "(or run in a terminal to be asked)"
        )
    values = Values(client_id=client_id or "")
    try:
        if secrets:
            raw = env.get(ENV_MISTRAL_KEY, "")
            if not raw and prompts.interactive:
                raw = prompts.secret("Mistral API key (input hidden): ")
            if not raw:
                raise EnableUsageError(
                    f"no Mistral API key: set {ENV_MISTRAL_KEY} or run in a terminal to be asked"
                )
            values.mistral_key = check_mistral_key(raw.encode("utf-8"))
            del raw
        pem: bytearray | None = None
        if args.app_key_file:
            pem = read_key_file(args.app_key_file, warn)
        elif env.get(ENV_APP_PRIVATE_KEY, "").strip():
            pem = bytearray(env[ENV_APP_PRIVATE_KEY].encode("utf-8"))
        elif secrets:
            raise EnableUsageError(
                "no App private key: pass --app-key-file PATH (the .pem file GitHub gave "
                f"you) or set {ENV_APP_PRIVATE_KEY}"
            )
        if pem is not None:
            try:
                values.private_key = check_private_key(pem)
            finally:
                pem[:] = b"\0" * len(pem)
    except BaseException:
        values.clear()
        raise
    return values


def verify_values(
    values: Values, verifier: Verifier, api: AppApi | None, *, note: Callable[[str], None]
) -> None:
    """Check the key with Mistral and the App credentials with GitHub (stores nothing)."""
    if values.mistral_key is not None:
        verifier.mistral(bytes(values.mistral_key))
        note("Mistral API key: accepted")
    if api is not None:
        app = api.app()
        note(f"GitHub App: {app.name or app.slug} ({app.slug})")


def listing_key(
    args: argparse.Namespace,
    env: Mapping[str, str],
    *,
    warn: Callable[[str], None],
    note: Callable[[str], None],
) -> bytearray | None:
    """The App key to list the App's repositories with: ``--app-key-file``,
    ``MSREVIEW_ENABLE_APP_PRIVATE_KEY``, then the key the CLI posts with (if any)."""
    if args.app_key_file:
        raw = read_key_file(args.app_key_file, warn)
        try:
            return check_private_key(raw)
        finally:
            raw[:] = b"\0" * len(raw)
    if env.get(ENV_APP_PRIVATE_KEY, "").strip():
        return check_private_key(env[ENV_APP_PRIVATE_KEY].encode("utf-8"))
    from morning_star_review.github import mint_child

    try:
        settings = mint_child.load_app_settings(env, mint_child.default_config_path(env))
        if not (settings.private_key_path or settings.private_key_command):
            return None
        pem = mint_child.read_private_key(settings, env=env)
    except mint_child.MintError as exc:
        raise EnableUsageError(f"the App key configured for the CLI is unusable: {exc}") from None
    note("listing the App's repositories with the App key configured for the CLI")
    return check_private_key(pem)
