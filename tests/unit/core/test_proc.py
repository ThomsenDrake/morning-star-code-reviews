"""Child-process environments, hardened git, and key commands."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from morning_star_review.core import proc
from morning_star_review.core.errors import MsreviewGitError, MsreviewSecretCommandError
from morning_star_review.core.proc import (
    ENDPOINT_REDIRECT_VARS,
    child_env,
    git_auth_env,
    run_git,
    run_git_bytes,
    run_secret_command,
)

PEM_HEAD = "-----BEGIN " + "PRIVATE KEY-----"

POISON = {
    "MISTRAL_API_KEY": "parent-mistral-key-000000",
    "GITHUB_TOKEN": "ghs_" + "p" * 36,
    "GH_TOKEN": "gho_" + "q" * 36,
    "MSREVIEW_APP_PRIVATE_KEY": PEM_HEAD + "\nabc",
    "MSREVIEW_MISTRAL_API_KEY_COMMAND": "echo key",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "http.extraheader",
    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic xyz",
    "GIT_DIR": "/elsewhere",
    "SERVER_URL": "https://attacker.example",
    "MISTRAL_SERVER_URL": "https://attacker.example",
    "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE": "https://attacker.example",
    "PYTHONPATH": "/evil",
    "LD_PRELOAD": "/evil.so",
    "BWS_ACCESS_TOKEN": "bws-token-000000000",
    "HOME": "/Users/someone",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}


def test_git_env_is_exactly_the_allowlist() -> None:
    env = child_env("git", source=POISON)
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
        "GIT_LFS_SKIP_SMUDGE",
        "GIT_ALLOW_PROTOCOL",
    }
    assert env["PATH"] == POISON["PATH"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] != POISON["HOME"] and Path(env["HOME"]).is_dir()
    assert list(Path(env["HOME"]).iterdir()) == []


def test_agent_env_allowlist_and_telemetry_switch() -> None:
    env = child_env("agent", source=POISON, MISTRAL_API_KEY="agent-key-0000000000")
    assert env["MISTRAL_API_KEY"] == "agent-key-0000000000"
    assert env["AGENTS_SDK_TELEMETRY_ENABLED"] == "0"
    assert env["AGENTS_SDK_STUDIO_TRACING_ENABLED"] == "0"
    assert env["OTEL_ENABLED"] == "false"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["PYTHONUNBUFFERED"] == "1"
    assert env["TMPDIR"] == env["HOME"]
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "PYTHONPATH", "LD_PRELOAD", "BWS_ACCESS_TOKEN"):
        assert name not in env
    assert not ENDPOINT_REDIRECT_VARS & set(env)
    assert not [n for n in env if n.startswith("MSREVIEW_")]
    assert not [
        n for n in env if n.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY", "GIT_CONFIG_VALUE"))
    ]
    on = child_env("agent", telemetry=True, source=POISON, home="/h", tmpdir="/t")
    assert "AGENTS_SDK_TELEMETRY_ENABLED" not in on and "OTEL_ENABLED" not in on
    assert (on["HOME"], on["TMPDIR"]) == ("/h", "/t")


@pytest.mark.parametrize("name", sorted(ENDPOINT_REDIRECT_VARS))
def test_agent_env_refuses_endpoint_redirects(name: str) -> None:
    with pytest.raises(ValueError, match="must not be forwarded"):
        extra: dict[str, str | None] = {name: "https://attacker.example"}
        child_env("agent", **extra)  # type: ignore[arg-type]  # names chosen at run time


def test_mint_env_passes_password_manager_variables_only() -> None:
    source = dict(POISON, OP_SESSION_me="op-session", EXTRA_ONE="x", EXTRA_TWO="y")
    source[proc.MINT_PASS_ENV_VAR] = "EXTRA_ONE, NOT_SET"
    env = child_env("mint", source=source)
    assert env["HOME"] == "/Users/someone"
    assert env["BWS_ACCESS_TOKEN"] == "bws-token-000000000"
    assert env["OP_SESSION_me"] == "op-session"
    assert env["EXTRA_ONE"] == "x" and "EXTRA_TWO" not in env and "NOT_SET" not in env
    assert env["MSREVIEW_APP_PRIVATE_KEY"].startswith(PEM_HEAD)  # the mint child may hold it
    for name in ("MISTRAL_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SERVER_URL", "PYTHONPATH"):
        assert name not in env
    assert "MSREVIEW_MISTRAL_API_KEY_COMMAND" not in env


def test_extra_overrides_removes_and_validates_names() -> None:
    env = child_env("git", source=POISON, LANG="en_US.UTF-8", GIT_OPTIONAL_LOCKS=None)
    assert env["LANG"] == "en_US.UTF-8" and "GIT_OPTIONAL_LOCKS" not in env
    with pytest.raises(ValueError, match="invalid environment variable name"):
        child_env("git", lower="x")


def test_agent_process_sees_only_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """DESIGN 15.1: spawn a fake agent that dumps its env, with a poisoned parent env."""
    for name, value in POISON.items():
        if name not in ("PATH", "HOME"):
            monkeypatch.setenv(name, value)
    env = child_env("agent", MISTRAL_API_KEY="agent-key-0000000000")
    out = subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    seen = json.loads(out.stdout)
    dumped = json.dumps(seen)
    assert not [n for n in seen if n.startswith(("MSREVIEW_", "GITHUB", "GH_"))]
    assert not [n for n in seen if n.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_"))]
    assert "SERVER_URL" not in seen and "MISTRAL_SERVER_URL" not in seen
    assert PEM_HEAD not in dumped and POISON["GITHUB_TOKEN"] not in dumped
    assert seen["MISTRAL_API_KEY"] == "agent-key-0000000000"


def test_git_auth_env_carries_the_token_as_basic_header() -> None:
    env = git_auth_env("ghs_" + "t" * 36)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    scheme, value = env["GIT_CONFIG_VALUE_0"].split(": ", 1)
    assert scheme == "AUTHORIZATION"
    kind, b64 = value.split(" ")
    assert kind == "basic"
    assert base64.b64decode(b64).decode() == "x-access-token:ghs_" + "t" * 36


# --------------------------------------------------------------------------------------
# run_git
# --------------------------------------------------------------------------------------

_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run_git(path, ["init", "-q", "-b", "main"], timeout=30, check=True)
    (path / "a.txt").write_text("hello\n")
    run_git(path, ["add", "a.txt"], timeout=30, check=True)
    run_git(path, [*_ID, "commit", "-q", "-m", "first"], timeout=30, check=True)
    return path


def test_run_git_returns_text_and_bytes(repo: Path) -> None:
    res = run_git(repo, ["log", "--format=%s"], timeout=30)
    assert res.returncode == 0 and res.stdout.strip() == "first"
    raw = run_git_bytes(repo, ["show", "HEAD:a.txt"], timeout=30)
    assert raw.stdout == b"hello\n"
    assert Path(raw.args[0]).name == "git" and Path(raw.args[0]).is_absolute()
    assert "core.hooksPath=" + os.devnull in raw.args


def test_run_git_disables_hooks(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    marker = repo.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (repo / "a.txt").write_text("changed\n")
    run_git(repo, [*_ID, "commit", "-q", "-am", "second"], timeout=30, check=True)
    assert not marker.exists()


def test_run_git_ignores_global_config(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = repo.parent / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[alias]\n\tpwned = log\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    res = run_git(repo, ["pwned"], timeout=30)
    assert res.returncode != 0


def test_run_git_check_raises_redacted_error(repo: Path) -> None:
    secret_env = {"MSREVIEW_TEST_SECRET": "supersecretvalue123"}
    with pytest.raises(MsreviewGitError, match="git rev-parse failed with exit code") as info:
        run_git(
            repo,
            ["rev-parse", "--verify", "supersecretvalue123"],
            env_extra=secret_env,
            timeout=30,
            check=True,
        )
    assert "supersecretvalue123" not in str(info.value)
    assert run_git(repo, ["rev-parse", "--verify", "nope"], timeout=30).returncode != 0


def test_run_git_env_extra_reaches_git_and_is_validated(repo: Path) -> None:
    res = run_git(
        repo,
        ["config", "--get", "x.y"],
        env_extra={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x.y", "GIT_CONFIG_VALUE_0": "z"},
        timeout=30,
    )
    assert res.stdout.strip() == "z"
    with pytest.raises(ValueError):
        run_git(repo, ["status"], env_extra={"bad name": "x"}, timeout=30)


def test_run_git_timeout_kills_the_process_group(repo: Path) -> None:
    start = time.monotonic()
    with pytest.raises(MsreviewGitError, match="timed out"):
        run_git(repo, ["-c", "alias.slow=!sleep 20", "slow"], timeout=0.5)
    assert time.monotonic() - start < 10


def test_run_git_input_text(repo: Path) -> None:
    res = run_git(repo, ["hash-object", "--stdin"], input_text="hello\n", timeout=30)
    assert res.stdout.strip() == "ce013625030ba8dba906f756967f9e9ca394464a"


# --------------------------------------------------------------------------------------
# run_secret_command
# --------------------------------------------------------------------------------------


def test_secret_command_returns_stripped_stdout() -> None:
    assert run_secret_command("printf '  value-123 \\n\\n'") == "value-123"
    pem = run_secret_command(f"printf '%s\\nabc\\n' '{PEM_HEAD}'")
    assert pem == f"{PEM_HEAD}\nabc"


def test_secret_command_uses_the_given_env() -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MY_VAULT_VALUE": "from-env-777"}
    assert run_secret_command('printf "%s" "$MY_VAULT_VALUE"', env=env) == "from-env-777"


def test_secret_command_failure_hides_command_and_output() -> None:
    cmd = "printf 'leaked-secret-999'; echo 'oops leaked-secret-999 failed' >&2; exit 3"
    with pytest.raises(MsreviewSecretCommandError) as info:
        run_secret_command(cmd)
    text = str(info.value)
    assert "exit code 3" in text
    assert "leaked-secret-999" not in text
    assert "printf" not in text and "oops" in text


@pytest.mark.parametrize("cmd", ["true", "printf '   \\n'", "  "])
def test_secret_command_without_output_fails(cmd: str) -> None:
    with pytest.raises(MsreviewSecretCommandError, match=r"empty|printed nothing"):
        run_secret_command(cmd)


def test_secret_command_timeout_kills_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    start = time.monotonic()
    with pytest.raises(MsreviewSecretCommandError, match="timed out"):
        run_secret_command(f"sleep 30 & echo $! > {pid_file}; wait", timeout=0.5)
    assert time.monotonic() - start < 10
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("background child survived the timeout")


def test_secret_command_missing_program() -> None:
    with pytest.raises(MsreviewSecretCommandError, match="exit code 127"):
        run_secret_command("definitely-not-a-real-program-msreview")


# --------------------------------------------------------------------------------------
# git is never found through a relative PATH entry (security review SEC-CI-1)
# --------------------------------------------------------------------------------------


def _plant_git(repo: Path, marker: Path) -> None:
    for sub in ("bin", "."):
        fake = repo / sub / "git"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(f'#!/bin/sh\necho "HIJACKED $GIT_CONFIG_VALUE_0" > {marker}\nexit 0\n')
        fake.chmod(0o755)


@pytest.mark.parametrize("prefix", ["./bin:", "bin:", ":", ".:"])
def test_git_in_the_checkout_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    repo = tmp_path / "repo"
    run_git(None, ["init", "-q", str(repo)], timeout=30, check=True)
    marker = tmp_path / "PWNED"
    _plant_git(repo, marker)
    monkeypatch.setenv("PATH", prefix + os.environ.get("PATH", "/usr/bin:/bin"))
    res = run_git(
        repo,
        ["rev-parse", "--verify", "--quiet", "0" * 40 + "^{commit}"],
        env_extra=git_auth_env("ghs_" + "f" * 36),
        timeout=30,
    )
    assert res.returncode != 0  # the real git: no such commit
    assert not marker.exists()
    assert Path(res.args[0]).is_absolute() and res.args[0] != str(repo / "bin" / "git")


def test_child_envs_keep_only_absolute_path_entries() -> None:
    src = {"PATH": "./bin::/usr/bin:node_modules/.bin:/bin:"}
    for base in ("git", "agent", "mint"):
        assert child_env(base, source=src)["PATH"] == "/usr/bin:/bin"  # type: ignore[arg-type]
    assert child_env("git", source={"PATH": "./bin:."})["PATH"] == proc._DEFAULT_PATH


def test_missing_git_is_a_git_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(MsreviewGitError, match="not on PATH"):
        run_git(None, ["--version"], timeout=30)
