"""Child processes: environment allowlists, hardened git, and key commands.

Child environments are built from scratch (never ``os.environ.copy()``) so that
tokens, keys and endpoint overrides in the parent cannot leak into git, the agent
runner or the mint child by accident.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from morning_star_review.core.errors import MsreviewGitError, MsreviewSecretCommandError
from morning_star_review.core.redact import redact

__all__ = [
    "ENDPOINT_REDIRECT_VARS",
    "GIT_SAFE_CONFIG",
    "MINT_PASS_ENV_VAR",
    "EnvBase",
    "absolute_path_entries",
    "child_env",
    "git_auth_env",
    "git_executable",
    "run_git",
    "run_git_bytes",
    "run_secret_command",
]

EnvBase = Literal["git", "agent", "mint"]

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_ERROR_TAIL = 2000

# Variables that redirect where a Mistral key is sent. Never forwarded to the agent.
ENDPOINT_REDIRECT_VARS = frozenset(
    {
        "SERVER_URL",
        "MISTRAL_API_URL",
        "MISTRAL_SERVER_URL",
        "MISTRAL_PROVIDER_API_BASE",
        "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE",
    }
)

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_ALLOW_PROTOCOL": "https:file",
}

_AGENT_TELEMETRY_OFF = {
    "AGENTS_SDK_TELEMETRY_ENABLED": "0",
    "AGENTS_SDK_STUDIO_TRACING_ENABLED": "0",
    "OTEL_ENABLED": "false",
}

# The mint child runs the user's key command (for example a password-manager CLI),
# so it also receives the variables such tools commonly need. Nothing else.
_MINT_PASS_NAMES = frozenset(
    {
        "USER",
        "LOGNAME",
        "SHELL",
        "GNUPGHOME",
        "GPG_TTY",
        "SSH_AUTH_SOCK",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "MSREVIEW_CONFIG",  # the mint child reads [app] from the same file as the driver
    }
)
_MINT_PASS_PREFIXES = ("BWS_", "OP_", "VAULT_", "MSREVIEW_APP_")
MINT_PASS_ENV_VAR = "MSREVIEW_MINT_PASS_ENV"  # noqa: S105 - a variable name, not a secret

# Prepended to every git command: no hooks, no fsmonitor, no external diff, no
# credential helpers, no submodule recursion, no LFS filters.
GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "credential.helper=",
    "-c",
    "submodule.recurse=false",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.required=false",
)

_empty_home: str | None = None


def absolute_path_entries(path: str | None) -> str:
    """``path`` with relative and empty entries dropped (``_DEFAULT_PATH`` if none is left).

    A relative entry (``./bin``, ``node_modules/.bin``, or the empty entry a leading or
    trailing ``:`` makes) is resolved against the child's working directory, which may
    be the untrusted PR checkout: a ``bin/git`` in the PR would run instead of git.
    """
    kept = [entry for entry in (path or "").split(os.pathsep) if Path(entry).is_absolute()]
    return os.pathsep.join(kept) or _DEFAULT_PATH


def git_executable(path: str | None = None) -> str | None:
    """The absolute path of ``git`` on the absolute entries of ``path`` (default: ``$PATH``),
    or ``None``. Every entry searched is absolute, so the result is too."""
    search = absolute_path_entries(os.environ.get("PATH") if path is None else path)
    return shutil.which("git", path=search)


def _scratch_home() -> str:
    """An empty per-process directory used as HOME/TMPDIR when none is given."""
    global _empty_home
    if _empty_home is None or not Path(_empty_home).is_dir():
        _empty_home = tempfile.mkdtemp(prefix="msreview-home-")
        atexit.register(shutil.rmtree, _empty_home, True)
    return _empty_home


def child_env(
    base: EnvBase,
    *,
    home: str | None = None,
    tmpdir: str | None = None,
    telemetry: bool = False,
    source: Mapping[str, str] | None = None,
    **extra: str | None,
) -> dict[str, str]:
    """Environment for a child process, built from an allowlist.

    - ``git``: PATH, HOME (empty dir), LANG, and git switches that ignore system and
      global config, never prompt, take no optional locks, skip LFS and allow only
      https and file transports.
    - ``agent``: PATH, HOME/TMPDIR (empty dirs), LANG, Python switches, telemetry off
      (unless ``telemetry``), the git switches the agent's read-only git calls need.
      Pass the key as ``MISTRAL_API_KEY=...``. Endpoint redirect variables are refused.
    - ``mint``: PATH, the user's real HOME, LANG, TMPDIR, and the variables a
      password-manager CLI typically needs (``BWS_*``, ``OP_*``, ``VAULT_*``, GnuPG,
      SSH agent, XDG dirs), ``MSREVIEW_APP_*``, plus names listed in
      ``MSREVIEW_MINT_PASS_ENV``.

    ``extra`` adds or overrides variables; a ``None`` value removes one. ``source``
    defaults to ``os.environ`` and is only read for the allowlisted names.
    """
    src = os.environ if source is None else source
    # Only absolute PATH entries: a relative one would resolve inside the child's cwd.
    env: dict[str, str] = {"PATH": absolute_path_entries(src.get("PATH")), "LANG": "C.UTF-8"}
    if base == "git":
        env["HOME"] = home or _scratch_home()
        env.update(_GIT_ENV)
    elif base == "agent":
        scratch = home or _scratch_home()
        env["HOME"] = scratch
        env["TMPDIR"] = tmpdir or scratch
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        if not telemetry:
            env.update(_AGENT_TELEMETRY_OFF)
    elif base == "mint":
        env["HOME"] = home or src.get("HOME") or _scratch_home()
        env["TMPDIR"] = tmpdir or src.get("TMPDIR") or tempfile.gettempdir()
        wanted = set(_MINT_PASS_NAMES)
        wanted.update(n.strip() for n in src.get(MINT_PASS_ENV_VAR, "").split(",") if n.strip())
        for name, inherited in src.items():
            if name in wanted or name.startswith(_MINT_PASS_PREFIXES):
                env[name] = inherited
    else:  # pragma: no cover - guarded by the Literal type
        raise ValueError(f"unknown environment base: {base!r}")
    for name, value in extra.items():
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if base == "agent" and name in ENDPOINT_REDIRECT_VARS:
            raise ValueError(f"{name} must not be forwarded to the agent")
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def git_auth_env(token: str, host: str = "github.com") -> dict[str, str]:
    """Per-command git config that sends ``token`` as a Basic auth header to ``host``.

    Use as ``run_git(..., env_extra=git_auth_env(token))`` for fetches only. The token
    never lands in ``.git/config``, the remote URL or the process arguments.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # already gone
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.communicate(timeout=5)  # drain and close the pipes
    except subprocess.TimeoutExpired:  # pragma: no cover - a grandchild escaped the group
        proc.kill()


def _run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float,
    input_bytes: bytes | None,
    shell: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run in a new session so a timeout kills the whole process group."""
    cmd: str | Sequence[str] = argv[0] if shell else list(argv)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        shell=shell,
    )
    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout)
    except BaseException:  # timeout, KeyboardInterrupt, cancellation
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def run_git_bytes(
    repo_dir: str | Path | None,
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: float,
    check: bool = False,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git <safe -c options> <args>`` in ``repo_dir`` with the ``git`` env allowlist.

    ``args`` is an argv list (never a shell string). A timeout kills the process group
    and raises ``MsreviewGitError``; with ``check=True`` a non-zero exit raises it too.
    Error text is redacted, including any secret values passed in ``env_extra``.
    """
    env = child_env("git")
    secrets: list[str] = []
    if env_extra:
        for name, value in env_extra.items():
            if not _ENV_NAME.match(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            env[name] = value
            secrets.append(value)
    sub = args[0] if args else "git"
    # An absolute path: never a "git" file from the checkout found through a relative
    # PATH entry (``cwd`` is often the untrusted PR head).
    git = git_executable(env["PATH"])
    if git is None:
        raise MsreviewGitError(f"git {sub} could not start: git is not on PATH")
    argv = [git, *GIT_SAFE_CONFIG, *args]
    try:
        res = _run(argv, cwd=repo_dir, env=env, timeout=timeout, input_bytes=input_bytes)
    except subprocess.TimeoutExpired as exc:
        raise MsreviewGitError(f"git {sub} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise MsreviewGitError(f"git {sub} could not start: {exc.strerror}") from exc
    if check and res.returncode != 0:
        tail = res.stderr.decode("utf-8", "replace")[-_ERROR_TAIL:].strip()
        raise MsreviewGitError(
            f"git {sub} failed with exit code {res.returncode}: {redact(tail, secrets)}"
        )
    return res


def run_git(
    repo_dir: str | Path | None,
    args: Sequence[str],
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: float,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """``run_git_bytes`` with stdout/stderr decoded as UTF-8 (invalid bytes replaced)."""
    res = run_git_bytes(
        repo_dir,
        args,
        env_extra=env_extra,
        timeout=timeout,
        check=check,
        input_bytes=None if input_text is None else input_text.encode("utf-8"),
    )
    return subprocess.CompletedProcess(
        res.args,
        res.returncode,
        res.stdout.decode("utf-8", "replace"),
        res.stderr.decode("utf-8", "replace"),
    )


def run_secret_command(
    cmd: str, *, timeout: float = 60, env: Mapping[str, str] | None = None
) -> str:
    """Run an operator-configured key command through the shell; return stripped stdout.

    Used for ``private_key_command`` (in the mint child) and ``api_key_command`` (in the
    driver). The output is never logged. Errors name neither the command nor the
    output, and the stderr excerpt they carry is redacted (also against the output).
    ``env`` defaults to the current process environment.
    """
    if not cmd.strip():
        raise MsreviewSecretCommandError("key command is empty")
    try:
        # The shell is the point: the operator configures a command line.
        res = _run([cmd], cwd=None, env=env, timeout=timeout, input_bytes=None, shell=True)  # noqa: S604
    except subprocess.TimeoutExpired:
        raise MsreviewSecretCommandError(f"key command timed out after {timeout:g}s") from None
    except OSError as exc:
        raise MsreviewSecretCommandError(f"key command could not start: {exc.strerror}") from None
    value = res.stdout.decode("utf-8", "replace").strip()
    if res.returncode != 0:
        tail = res.stderr.decode("utf-8", "replace")[-300:].strip()
        detail = redact(tail, [value, *value.splitlines()]) if tail else "no error output"
        raise MsreviewSecretCommandError(
            f"key command failed with exit code {res.returncode}: {detail}"
        )
    if not value:
        raise MsreviewSecretCommandError("key command printed nothing")
    return value
