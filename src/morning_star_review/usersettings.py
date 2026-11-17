"""The CLI's user settings (``~/.config/morning-star-review/config.toml``) and the Mistral key.

All settings are optional and environment variables win over the file. The ``[app]``
table is read only by the mint child (``github.mint_child``); this module reads
``[mistral]`` and ``[defaults]``. The Mistral key command runs here, in the driver,
once; its output is kept in memory and never logged.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from morning_star_review._version import __version__
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.proc import run_secret_command

__all__ = [
    "ENV_AGENT_PYTHON",
    "ENV_MISTRAL_KEY_COMMAND",
    "SettingsError",
    "UserSettings",
    "cache_dir",
    "config_path",
    "default_agent_python",
    "default_agent_venv",
    "load_mistral_key",
    "load_user_settings",
]

ENV_MISTRAL_KEY_COMMAND = "MSREVIEW_MISTRAL_API_KEY_COMMAND"
ENV_AGENT_PYTHON = "MSREVIEW_AGENT_PYTHON"
ENV_CONFIG = "MSREVIEW_CONFIG"  # path override (tests, unusual setups)
_MAX_CONFIG_BYTES = 256 * 1024
_READ_TOKEN_MODES = ("env", "gh", "none")


class SettingsError(MsreviewError):
    """The user settings are unusable (the message is safe to show)."""


@dataclass(frozen=True)
class UserSettings:
    mistral_api_key_command: str | None = field(default=None, repr=False)
    preset: str | None = None
    max_cost_usd: float | None = None
    read_token: str = "env"  # noqa: S105 - a mode name (env | gh | none), not a secret
    allowed_models: list[str] | None = None
    agent_python: str | None = None
    fail_on: str | None = None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """``$MSREVIEW_CONFIG`` or ``~/.config/morning-star-review/config.toml``."""
    src = os.environ if env is None else env
    explicit = src.get(ENV_CONFIG, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = src.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "morning-star-review" / "config.toml"


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """``~/.cache/morning-star-review`` (``$XDG_CACHE_HOME`` is honoured)."""
    src = os.environ if env is None else env
    xdg = src.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path(src.get("HOME") or str(Path.home())) / ".cache"
    return base / "morning-star-review"


def default_agent_venv(env: Mapping[str, str] | None = None) -> Path:
    """Where ``msreview setup`` puts the agent environment for this version."""
    return cache_dir(env) / f"agent-{__version__}"


def default_agent_python(env: Mapping[str, str] | None = None) -> Path:
    return default_agent_venv(env) / "bin" / "python"


def _read_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise SettingsError(f"config file: [{name}] must be a table")
    return table


def _opt_str(table: dict[str, Any], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"config file: {where}.{key} must be a string")
    return value.strip() or None


def load_user_settings(
    env: Mapping[str, str] | None = None, path: Path | None = None
) -> UserSettings:
    """Read the config file (missing file = defaults). Raises ``SettingsError``."""
    src = os.environ if env is None else env
    file = path or config_path(src)
    try:
        raw = file.read_bytes()[: _MAX_CONFIG_BYTES + 1]
    except FileNotFoundError:
        return UserSettings()
    except OSError as exc:
        raise SettingsError(f"cannot read the config file {file}: {exc.strerror}") from None
    if len(raw) > _MAX_CONFIG_BYTES:
        raise SettingsError(f"the config file {file} is too large")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise SettingsError(f"the config file {file} is not valid TOML") from None
    mistral = _read_table(data, "mistral")
    defaults = _read_table(data, "defaults")
    agent = _read_table(data, "agent")
    cost = defaults.get("max_cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float)):
        raise SettingsError("config file: defaults.max_cost_usd must be a number")
    read_token = _opt_str(defaults, "read_token", "defaults") or "env"
    if read_token not in _READ_TOKEN_MODES:
        raise SettingsError("config file: defaults.read_token must be env, gh or none")
    models = defaults.get("allowed_models")
    if models is not None and (
        not isinstance(models, list) or not all(isinstance(m, str) and m for m in models)
    ):
        raise SettingsError("config file: defaults.allowed_models must be a list of model ids")
    return UserSettings(
        mistral_api_key_command=_opt_str(mistral, "api_key_command", "mistral"),
        preset=_opt_str(defaults, "preset", "defaults"),
        max_cost_usd=float(cost) if cost is not None else None,
        read_token=read_token,
        allowed_models=list(models) if models is not None else None,
        agent_python=_opt_str(agent, "python", "agent"),
        fail_on=_opt_str(defaults, "fail_on", "defaults"),
    )


def load_mistral_key(env: Mapping[str, str] | None, settings: UserSettings) -> str:
    """The Mistral API key, read once into memory.

    Order: ``MSREVIEW_MISTRAL_API_KEY_COMMAND``, then ``MISTRAL_API_KEY``, then the
    config file's ``[mistral] api_key_command`` (environment variables win over the
    file). Raises ``SettingsError`` (no key) or ``MsreviewSecretCommandError``.
    """
    src = os.environ if env is None else env
    command = src.get(ENV_MISTRAL_KEY_COMMAND, "").strip()
    if command:
        return run_secret_command(command, env=dict(src))
    key = src.get("MISTRAL_API_KEY", "").strip()
    if key:
        return key
    if settings.mistral_api_key_command:
        return run_secret_command(settings.mistral_api_key_command, env=dict(src))
    raise SettingsError(
        "no Mistral API key: set MISTRAL_API_KEY, MSREVIEW_MISTRAL_API_KEY_COMMAND, or "
        "[mistral] api_key_command in the config file"
    )
