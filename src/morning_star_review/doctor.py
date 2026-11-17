"""Environment and configuration checks behind ``msreview doctor`` (DESIGN 10.4).

Read-only: the key is checked with ``whoami`` (and, with ``--models``, one 1-token
chat completion per model); deployments are only listed. Secrets are never printed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from morning_star_review._version import __version__
from morning_star_review.core.contract import DEFAULT_ALLOWED_MODELS
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.naming import NAME_PREFIX
from morning_star_review.platform import MistralPlatform, Platform, PlatformError

__all__ = ["Check", "Report", "check_app_settings", "run_doctor"]

CHAT_URL = "https://api.mistral.ai/v1/chat/completions"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        self.checks.append(Check(name, ok, detail, required))
        mark = "ok  " if ok else ("FAIL" if required else "warn")
        print(f"[{mark}] {name}: {detail}", flush=True)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


def check_app_settings(env: Mapping[str, str]) -> tuple[bool, str]:
    """The GitHub App settings for CLI posting (optional: report mode works without)."""
    from morning_star_review.github.mint_child import (
        MintError,
        default_config_path,
        load_app_settings,
    )

    try:
        settings = load_app_settings(env, default_config_path(env))
    except MintError as exc:
        return False, exc.reason
    if not settings.client_id:
        return False, "no client id (MSREVIEW_APP_CLIENT_ID or [app] client_id); report mode only"
    if settings.private_key_command:
        return True, "client id set; private key from a command (not run by doctor)"
    if not settings.private_key_path:
        return False, "no private key (private_key_command or private_key_path)"
    path = Path(settings.private_key_path).expanduser()
    try:
        mode = path.stat().st_mode
    except OSError:
        return False, f"private key file {path} does not exist"
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return False, f"private key file {path} is readable by others; chmod 600 it"
    return True, f"client id set; private key file {path} (mode 0600)"


async def _ping_model(client: httpx.AsyncClient, key: str, model: str) -> tuple[bool, str]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
    }
    try:
        resp = await client.post(CHAT_URL, json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    if resp.status_code == 200:
        return True, "reachable"
    return False, f"HTTP {resp.status_code}"


def _age(when: datetime | None) -> str:
    if when is None:
        return "never"
    ref = when if when.tzinfo else when.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - ref).total_seconds())
    return f"{seconds}s ago" if seconds < 3600 else f"{seconds // 3600}h ago"


async def _platform_checks(
    report: Report,
    key: str,
    models: list[str],
    deployments: bool,
    platform_factory: Callable[[str], Platform],
) -> None:
    platform = platform_factory(key)
    try:
        who = await platform.whoami()
    except PlatformError as exc:
        report.add("workflows access", False, str(exc))
        return
    report.add("workflows access", True, f"workspace namespace {who.namespace[:8]}...")
    if deployments:
        try:
            items = await platform.deployments(f"{NAME_PREFIX}-", 100)
        except PlatformError as exc:
            report.add("deployments", False, str(exc), required=False)
        else:
            report.add("deployments", True, f"{len(items)} {NAME_PREFIX}-* deployment(s)")
            for d in items:
                state = "active" if d.is_active else "inactive"
                print(
                    f"       {d.name}  {state}  workers={d.active_worker_count}  "
                    f"heartbeat={_age(d.last_heartbeat)}"
                )
    if models:
        async with httpx.AsyncClient(timeout=60.0) as client:
            for model in models:
                ok, detail = await _ping_model(client, key, model)
                report.add(f"model {model}", ok, detail)


def _default_platform(key: str) -> Platform:
    return MistralPlatform(key)


def run_doctor(
    *,
    models: bool = False,
    deployments: bool = False,
    env: Mapping[str, str] | None = None,
    platform_factory: Callable[[str], Platform] = _default_platform,
) -> int:
    """Run every check; exit code 0 when all required checks pass."""
    from morning_star_review.setup_agent import SetupError, check_agent_python
    from morning_star_review.usersettings import (
        ENV_AGENT_PYTHON,
        default_agent_python,
        load_mistral_key,
        load_user_settings,
    )

    src = os.environ if env is None else env
    report = Report()
    report.add("version", True, f"msreview {__version__} (python {sys.version.split()[0]})")
    git = shutil.which("git", path=src.get("PATH"))
    if git is None:
        report.add("git", False, "git is not on PATH")
    else:
        out = subprocess.run([git, "--version"], capture_output=True, text=True, check=False)
        report.add("git", out.returncode == 0, out.stdout.strip() or "git did not run")
    try:
        settings = load_user_settings(src)
    except MsreviewError as exc:
        report.add("config file", False, str(exc))
        return 1
    report.add("config file", True, "loaded (or absent: defaults)")
    agent = src.get(ENV_AGENT_PYTHON, "").strip() or settings.agent_python
    agent_python = Path(agent).expanduser() if agent else default_agent_python(src)
    try:
        versions = check_agent_python(agent_python)
    except SetupError as exc:
        report.add("agent environment", False, str(exc))
    else:
        same = versions.get("version") == __version__
        report.add(
            "agent environment",
            same,
            f"{agent_python} (msreview {versions.get('version')}, "
            f"mistralai-agents {versions.get('agents')})",
        )
    ok, detail = check_app_settings(src)
    report.add("github app (cli)", ok, detail, required=False)
    try:
        key = load_mistral_key(src, settings)
    except MsreviewError as exc:
        report.add("mistral key", False, str(exc))
        return 1
    report.add("mistral key", True, "found (not shown)")
    model_list = (settings.allowed_models or list(DEFAULT_ALLOWED_MODELS)) if models else []
    asyncio.run(_platform_checks(report, key, model_list, deployments, platform_factory))
    return 0 if report.ok else 1
