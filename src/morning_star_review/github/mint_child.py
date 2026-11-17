"""Child process that mints installation tokens without exposing the App key.

The driver runs ``python -m morning_star_review.github.mint_child --repo o/r`` (the
same entry point as the hidden ``msreview _mint``) with an allowlisted environment
and reads exactly one JSON line from its stdout. The App private key is read here
(from a file, or from the output of ``private_key_command``) and never leaves this
process: it is not printed, logged, passed on or written anywhere.

Settings, highest precedence first: ``MSREVIEW_APP_CLIENT_ID``,
``MSREVIEW_APP_PRIVATE_KEY_COMMAND``, ``MSREVIEW_APP_PRIVATE_KEY_PATH``; then the
``[app]`` table of ``~/.config/morning-star-review/config.toml`` (``client_id``,
``private_key_command``, ``private_key_path``). A command wins over a path from the
same source. Key files must not be accessible to group or others.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.proc import child_env, run_secret_command
from morning_star_review.core.redact import redact
from morning_star_review.github.app_auth import (
    find_installation,
    get_app,
    has_level,
    make_app_jwt,
    mint_installation_token,
    missing_permissions,
    requested_permissions,
)
from morning_star_review.github.http import API_BASE, GitHubHttp

__all__ = [
    "ENV_CLIENT_ID",
    "ENV_KEY_COMMAND",
    "ENV_KEY_PATH",
    "AppSettings",
    "MintResult",
    "MintStatus",
    "default_config_path",
    "load_app_settings",
    "main",
    "mint",
    "mint_via_child",
    "read_private_key",
]

MintStatus = Literal["ok", "not_installed", "no_credentials", "insufficient", "error"]
_STATUSES: frozenset[str] = frozenset(
    {"ok", "not_installed", "no_credentials", "insufficient", "error"}
)

ENV_CLIENT_ID = "MSREVIEW_APP_CLIENT_ID"
ENV_KEY_PATH = "MSREVIEW_APP_PRIVATE_KEY_PATH"
ENV_KEY_COMMAND = "MSREVIEW_APP_PRIVATE_KEY_COMMAND"
_MAX_KEY_BYTES = 64 * 1024
_MAX_CONFIG_BYTES = 256 * 1024
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9._-]{1,100}$")
_REASON_LIMIT = 500
_CHILD_MODULE = "morning_star_review.github.mint_child"


class MintError(MsreviewError):
    """A problem that ends minting with ``status`` (the message is safe to show)."""

    def __init__(self, status: MintStatus, reason: str) -> None:
        super().__init__(reason)
        self.status: MintStatus = status
        self.reason = reason


@dataclass(frozen=True)
class MintResult:
    status: MintStatus
    token: str | None = field(default=None, repr=False)
    slug: str | None = None
    reason: str | None = None
    expires_at: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)

    @property
    def bot_login(self) -> str | None:
        return f"{self.slug}[bot]" if self.slug else None

    @property
    def can_label(self) -> bool:
        """True when the token may manage labels (Issues: write)."""
        return has_level(self.permissions, "issues", "write")

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.status}
        for name in ("token", "slug", "reason", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.permissions:
            data["permissions"] = dict(self.permissions)
        return data

    @classmethod
    def from_json(cls, data: Any) -> MintResult:
        if not isinstance(data, dict) or data.get("status") not in _STATUSES:
            return cls(status="error", reason="the mint child returned an unexpected result")
        perms = data.get("permissions")

        def opt(name: str) -> str | None:
            value = data.get(name)
            return str(value) if isinstance(value, str) and value else None

        status: MintStatus = data["status"]
        token = opt("token")
        if status == "ok" and token is None and not data.get("check"):
            return cls(status="error", reason="the mint child returned no token")
        return cls(
            status=status,
            token=token,
            slug=opt("slug"),
            reason=opt("reason"),
            expires_at=opt("expires_at"),
            permissions={str(k): str(v) for k, v in perms.items()}
            if isinstance(perms, dict)
            else {},
        )


@dataclass(frozen=True)
class AppSettings:
    client_id: str | None
    private_key_path: str | None
    private_key_command: str | None = field(default=None, repr=False)

    @property
    def complete(self) -> bool:
        return bool(self.client_id and (self.private_key_path or self.private_key_command))


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``$MSREVIEW_CONFIG`` or ``~/.config/morning-star-review/config.toml``: the same file
    as ``usersettings.config_path``, so ``[app]`` comes from the file the rest does."""
    source = os.environ if env is None else env
    explicit = source.get("MSREVIEW_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = source.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "morning-star-review" / "config.toml"


def _config_app_table(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()[: _MAX_CONFIG_BYTES + 1]
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MintError("error", f"cannot read the config file {path}: {exc.strerror}") from None
    if len(raw) > _MAX_CONFIG_BYTES:
        raise MintError("error", f"the config file {path} is too large")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise MintError("error", f"the config file {path} is not valid TOML") from None
    table = data.get("app")
    return table if isinstance(table, dict) else {}


def load_app_settings(env: Mapping[str, str], config_path: Path | None) -> AppSettings:
    """App settings from the environment, then the config file's ``[app]`` table."""

    def clean(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    table = _config_app_table(config_path) if config_path is not None else {}
    client_id = clean(env.get(ENV_CLIENT_ID)) or clean(table.get("client_id"))
    env_cmd, env_path = clean(env.get(ENV_KEY_COMMAND)), clean(env.get(ENV_KEY_PATH))
    if env_cmd or env_path:
        return AppSettings(client_id, None if env_cmd else env_path, env_cmd)
    file_cmd = clean(table.get("private_key_command"))
    file_path = clean(table.get("private_key_path"))
    return AppSettings(client_id, None if file_cmd else file_path, file_cmd)


def read_private_key(settings: AppSettings, *, env: Mapping[str, str] | None = None) -> bytes:
    """The PEM bytes, from the key command or the key file (mode checked)."""
    if settings.private_key_command:
        try:
            value = run_secret_command(settings.private_key_command, env=env)
        except MsreviewError as exc:
            raise MintError("error", f"private_key_command: {exc}") from None
        return value.encode("utf-8") + b"\n"
    if not settings.private_key_path:
        raise MintError("no_credentials", "no private key is configured")
    path = Path(settings.private_key_path).expanduser()
    try:
        # Open first and check the opened file, so the checked file is the read one.
        # O_NONBLOCK: opening a FIFO must not hang (it is refused below).
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise MintError("error", f"the private key file {path} does not exist") from None
    except OSError as exc:
        raise MintError(
            "error", f"cannot read the private key file {path}: {exc.strerror}"
        ) from None
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise MintError("error", f"the private key path {path} is not a regular file")
        if info.st_mode & 0o077:
            raise MintError(
                "error",
                f"the private key file {path} is accessible to group or others "
                f"(mode {stat.S_IMODE(info.st_mode):04o}); run: chmod 600 {path}",
            )
        try:
            data = fh.read(_MAX_KEY_BYTES + 1)
        except OSError as exc:
            raise MintError(
                "error", f"cannot read the private key file {path}: {exc.strerror}"
            ) from None
    if len(data) > _MAX_KEY_BYTES:
        raise MintError("error", f"the private key file {path} is too large")
    return data


async def mint(
    repo: str,
    settings: AppSettings,
    *,
    check_only: bool = False,
    base_url: str = API_BASE,
    now: int | None = None,
    env: Mapping[str, str] | None = None,
) -> MintResult:
    """Mint an installation token for ``owner/repo`` (or only check, ``check_only``)."""
    if not _REPO.match(repo):
        return MintResult(status="error", reason="--repo must look like owner/repo")
    owner, name = repo.split("/", 1)
    if not settings.complete:
        return MintResult(
            status="no_credentials",
            reason="no GitHub App credentials were configured (client id and private key)",
        )
    secrets: list[str] = []
    try:
        pem = read_private_key(settings, env=env)
        secrets.append(pem.decode("utf-8", "replace"))
        try:
            app_jwt = make_app_jwt(settings.client_id or "", pem, int(now or time.time()))
        except Exception:
            raise MintError("error", "the private key is not a valid PEM RSA key") from None
        secrets.append(app_jwt)
        async with GitHubHttp(app_jwt, base_url=base_url) as app_client:
            app = await get_app(app_client)
            inst = await find_installation(app_client, owner, name)
            if inst is None:
                return MintResult(
                    status="not_installed",
                    slug=app.slug,
                    reason=f"the GitHub App '{app.slug}' is not installed on {repo} "
                    f"(GET /repos/{repo}/installation returned 404)",
                )
            missing = missing_permissions(inst.permissions)
            if missing:
                return MintResult(
                    status="insufficient",
                    slug=app.slug,
                    permissions=inst.permissions,
                    reason=f"the App installation on {repo} lacks {', '.join(missing)}",
                )
            if check_only:
                return MintResult(status="ok", slug=app.slug, permissions=inst.permissions)
            token = await mint_installation_token(
                app_client, inst.id, name, requested_permissions(inst.permissions)
            )
        return MintResult(
            status="ok",
            token=token.token,
            slug=app.slug,
            expires_at=token.expires_at,
            permissions=token.permissions,
        )
    except MintError as exc:
        return MintResult(status=exc.status, reason=_safe(exc.reason, secrets))
    except MsreviewError as exc:
        return MintResult(status="error", reason=_safe(str(exc), secrets))
    except Exception as exc:
        return MintResult(status="error", reason=_safe(type(exc).__name__, secrets))


def _safe(text: str, secrets: list[str]) -> str:
    out = redact(" ".join(text.split()), secrets)
    return out if len(out) <= _REASON_LIMIT else out[: _REASON_LIMIT - 1] + "…"


def main(argv: list[str]) -> int:
    """Entry point of ``msreview _mint``: prints one JSON line; exit 0 unless misused."""
    parser = argparse.ArgumentParser(prog="msreview _mint", add_help=True)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--check", action="store_true", help="check the installation only")
    parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    config_path = Path(args.config) if args.config else default_config_path()
    try:
        settings = load_app_settings(os.environ, config_path)
    except MintError as exc:
        result = MintResult(status=exc.status, reason=_safe(exc.reason, []))
    else:
        result = asyncio.run(mint(args.repo, settings, check_only=args.check))
    payload = result.to_json()
    if args.check:
        payload["check"] = True
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


async def mint_via_child(
    repo: str,
    *,
    check_only: bool = False,
    python: str | None = None,
    timeout_s: float = 90.0,
    env: Mapping[str, str] | None = None,
) -> MintResult:
    """Run the mint child for ``repo`` and return its result (never raises).

    ``env`` replaces the allowlisted environment (tests); the default is
    ``child_env("mint")``.
    """
    # -I (isolated mode): the cwd (perhaps a cloned repository with a json.py or an
    # httpx/ package) is not put on sys.path, and PYTHON* variables are ignored. The
    # child holds the App key command and password-manager tokens.
    argv = [python or sys.executable, "-I", "-m", _CHILD_MODULE, "--repo", repo]
    if check_only:
        argv.append("--check")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else child_env("mint"),
            start_new_session=True,
        )
    except OSError as exc:
        return MintResult(status="error", reason=f"cannot start the mint child: {exc.strerror}")
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        await _kill_group(proc)
        return MintResult(status="error", reason=f"the mint child timed out after {timeout_s:g}s")
    except BaseException:  # cancelled: do not leave the child (or its key command) running
        await _kill_group(proc)
        raise
    # stderr is dropped unread: a key command may have echoed something sensitive.
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    if not lines:
        return MintResult(
            status="error", reason=f"the mint child printed nothing (exit code {proc.returncode})"
        )
    try:
        data = json.loads(lines[-1])
    except ValueError:
        return MintResult(status="error", reason="the mint child printed invalid JSON")
    result = MintResult.from_json(data)
    if result.status == "ok" and result.token is None and not check_only:
        return MintResult(status="error", reason="the mint child returned no token")
    return result


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the child's whole session (it may have started a key command)."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    await proc.wait()


if __name__ == "__main__":  # pragma: no cover - exercised through mint_via_child
    sys.exit(main(sys.argv[1:]))
