"""Installs the agent virtual environment behind ``msreview setup`` (DESIGN 10.4).

The CLI is installed with ``uv tool install git+https://github.com/<owner>/<repo>``
(the worker environment, without the Agents SDK). The agent runs in a second
environment at ``~/.cache/morning-star-review/agent-<version>/``:

1. ``uv venv`` with the same Python as the CLI;
2. ``uv pip install --require-hashes -r _data/agent-requirements.txt`` (exported from
   ``uv.lock``, so every dependency is pinned and hash-checked);
3. ``uv pip install --no-deps <this package>`` from the same source the CLI came from
   (the git commit, a local checkout or a wheel), so both environments run one version.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from morning_star_review._version import __version__
from morning_star_review.core.errors import MsreviewError
from morning_star_review.core.proc import absolute_path_entries
from morning_star_review.usersettings import default_agent_venv

__all__ = [
    "DIST_NAME",
    "REPO_URL",
    "InstallSource",
    "SetupError",
    "check_agent_python",
    "install_source",
    "plan_commands",
    "requirements_path",
    "run_setup",
]

DIST_NAME = "morning-star-code-reviews"
REPO_URL = "https://github.com/ThomsenDrake/morning-star-code-reviews"
_STEP_TIMEOUT_S = 900


class SetupError(MsreviewError):
    """Setup cannot continue (the message is safe to show)."""


@dataclass(frozen=True)
class InstallSource:
    spec: str  # what `uv pip install` receives
    editable: bool = False
    origin: str = "git"  # git | dir | archive | release


def requirements_path() -> Path:
    ref = importlib.resources.files("morning_star_review") / "_data" / "agent-requirements.txt"
    return Path(str(ref))


def _file_url_path(url: str) -> str:
    parts = urlsplit(url)
    return unquote(parts.path)


def install_source(direct_url_json: str | None = None) -> InstallSource:
    """Where this package was installed from (PEP 610 ``direct_url.json``).

    Falls back to the release tag ``v<version>`` of the public repository.
    """
    raw = direct_url_json
    if raw is None:
        try:
            raw = importlib.metadata.distribution(DIST_NAME).read_text("direct_url.json")
        except importlib.metadata.PackageNotFoundError:
            raw = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            url: str = data["url"]
            vcs = data.get("vcs_info")
            if isinstance(vcs, dict) and vcs.get("vcs") == "git":
                commit = vcs.get("commit_id") or vcs.get("requested_revision")
                spec = f"git+{url}@{commit}" if commit else f"git+{url}"
                return InstallSource(spec=spec, origin="git")
            info = data.get("dir_info")
            if isinstance(info, dict) and url.startswith("file:"):
                return InstallSource(
                    spec=_file_url_path(url), editable=bool(info.get("editable")), origin="dir"
                )
            if isinstance(data.get("archive_info"), dict):
                spec = _file_url_path(url) if url.startswith("file:") else url
                return InstallSource(spec=spec, origin="archive")
    return InstallSource(spec=f"git+{REPO_URL}@v{__version__}", origin="release")


def plan_commands(
    uv: str, venv: Path, requirements: Path, source: InstallSource, python: str
) -> list[list[str]]:
    """The three ``uv`` commands that build the agent environment.

    Each has ``--no-config``: ``uv`` would otherwise read ``uv.toml`` or ``[tool.uv]``
    from the cwd and its parents (perhaps an untrusted checkout), which can point the
    unhashed build requirements of the last step at another index.
    """
    vpy = str(venv / "bin" / "python")
    project = ["-e", source.spec] if source.editable else [source.spec]
    return [
        [uv, "venv", "--no-config", "--python", python, str(venv)],
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            vpy,
            "--require-hashes",
            "-r",
            str(requirements),
        ],
        [uv, "pip", "install", "--no-config", "--python", vpy, "--no-deps", *project],
    ]


_CHECK_CODE = (
    "import json, mistralai.agents, morning_star_review.agent; "
    "from morning_star_review._version import __version__ as v; "
    "import importlib.metadata as m; "
    "print(json.dumps({'version': v, 'agents': m.version('mistralai-agents')}))"
)


def check_agent_python(python: Path) -> dict[str, str]:
    """Import the agent runner in the agent environment; return the versions it reports."""
    if not python.exists():
        raise SetupError(f"{python} does not exist; run `msreview setup`")
    try:
        res = subprocess.run(
            [str(python), "-c", _CHECK_CODE],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(python.parent),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"the agent python did not run ({type(exc).__name__})") from None
    if res.returncode != 0:
        tail = res.stderr.strip().splitlines()[-1:] or ["no output"]
        raise SetupError(f"the agent environment is broken: {tail[0][:300]}")
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SetupError("the agent environment printed an unexpected answer") from None
    return {str(k): str(v) for k, v in data.items()}


def _run(cmd: Sequence[str], env: Mapping[str, str]) -> None:
    print("msreview setup: " + " ".join(cmd), file=sys.stderr, flush=True)
    step = " ".join(word for word in cmd[1:3] if not word.startswith("-"))
    try:
        # UV_NO_CONFIG as well as --no-config: also covers the uv the build backend runs.
        res = subprocess.run(
            list(cmd), env={**env, "UV_NO_CONFIG": "1"}, timeout=_STEP_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"`{step}` did not finish ({type(exc).__name__})") from None
    if res.returncode != 0:
        raise SetupError(f"`{step}` failed with exit code {res.returncode}")


def run_setup(
    *,
    source: str | None = None,
    force: bool = False,
    check: bool = False,
    venv: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """``msreview setup``: build (or check) the agent environment. Returns an exit code."""
    src_env = dict(os.environ if env is None else env)
    target = venv or default_agent_venv(src_env)
    python = target / "bin" / "python"
    if check or (python.exists() and not force):
        try:
            versions = check_agent_python(python)
        except SetupError as exc:
            if check:
                print(f"msreview setup: {exc}", file=sys.stderr)
                return 1
            print(f"msreview setup: reinstalling ({exc})", file=sys.stderr)
        else:
            ok = versions.get("version") == __version__
            state = "ready" if ok else f"version {versions.get('version')} (expected {__version__})"
            print(f"agent environment: {target} ({state}, agents {versions.get('agents')})")
            if ok or check:
                return 0 if ok else 1
    # Absolute PATH entries only: a relative one would find a "uv" in the cwd.
    uv = shutil.which("uv", path=absolute_path_entries(src_env.get("PATH")))
    if uv is None:
        raise SetupError("msreview setup needs uv on PATH (https://docs.astral.sh/uv/)")
    requirements = requirements_path()
    if not requirements.is_file():
        raise SetupError(f"{requirements} is missing from this installation")
    chosen = InstallSource(spec=source, origin="explicit") if source else install_source()
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    for cmd in plan_commands(uv, target, requirements, chosen, sys.executable):
        _run(cmd, src_env)
    versions = check_agent_python(python)
    if versions.get("version") != __version__:
        raise SetupError(
            f"the agent environment has version {versions.get('version')}, expected "
            f"{__version__}; pass --source to install the matching package"
        )
    print(f"agent environment ready: {python}")
    return 0
