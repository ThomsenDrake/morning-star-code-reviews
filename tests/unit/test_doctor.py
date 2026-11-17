"""``msreview doctor`` against fakes (no network)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from morning_star_review import doctor
from morning_star_review._version import __version__
from morning_star_review.platform import (
    DeploymentState,
    ExecutionState,
    PlatformAuthError,
    Whoami,
)

KEY = "mk-test-not-a-real-key-0123456789"


class FakePlatform:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def whoami(self) -> Whoami:
        if self.fail:
            raise PlatformAuthError("whoami failed (HTTP 401)", 401)
        return Whoami(namespace="0123456789abcdef", raw={})

    async def deployments(self, prefix: str, limit: int = 100) -> list[DeploymentState]:
        return [DeploymentState(f"{prefix}cli-x-pr1", False, 0, datetime.now(UTC))]

    # Doctor is read-only: everything below must never be called.
    async def deployment(self, name: str) -> DeploymentState | None:
        raise AssertionError("doctor must not poll deployments")

    async def start(
        self, *, workflow: str, deployment: str, execution_id: str, payload: Any
    ) -> None:
        raise AssertionError("doctor must not start executions")

    async def execution(self, execution_id: str) -> ExecutionState:
        raise AssertionError("doctor must not read executions")

    async def terminate(self, execution_id: str) -> None:
        raise AssertionError("doctor must not terminate executions")

    async def running_for_pr(self, workflow: str, pr_url: str) -> list[str]:
        raise AssertionError("doctor must not list runs")

    async def aclose(self) -> None:
        return None


def agent_env(tmp_path: Path) -> dict[str, str]:
    py = tmp_path / "agent" / "bin" / "python"
    py.parent.mkdir(parents=True)
    answer = json.dumps({"version": __version__, "agents": "1.1.3"})
    py.write_text(f"#!/bin/sh\necho '{answer}'\n")
    py.chmod(0o755)
    return {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MISTRAL_API_KEY": KEY,
        "MSREVIEW_AGENT_PYTHON": str(py),
    }


def test_all_good(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = agent_env(tmp_path)
    code = doctor.run_doctor(env=env, deployments=True, platform_factory=lambda k: FakePlatform())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "workspace namespace 01234567..." in out
    assert "msreview-cli-x-pr1" in out
    assert KEY not in out


def test_refused_key_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = agent_env(tmp_path)
    code = doctor.run_doctor(env=env, platform_factory=lambda k: FakePlatform(fail=True))
    assert code == 1
    assert "[FAIL] workflows access" in capsys.readouterr().out


def test_missing_agent_environment_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = agent_env(tmp_path)
    env["MSREVIEW_AGENT_PYTHON"] = str(tmp_path / "missing")
    assert doctor.run_doctor(env=env, platform_factory=lambda k: FakePlatform()) == 1
    assert "[FAIL] agent environment" in capsys.readouterr().out


def test_missing_key_fails(tmp_path: Path) -> None:
    env = agent_env(tmp_path)
    del env["MISTRAL_API_KEY"]
    assert doctor.run_doctor(env=env, platform_factory=lambda k: FakePlatform()) == 1


def test_app_settings_checks(tmp_path: Path) -> None:
    env: dict[str, Any] = {"HOME": str(tmp_path)}
    ok, detail = doctor.check_app_settings(env)
    assert not ok and "report mode" in detail
    key = tmp_path / "app.pem"
    key.write_text("x")
    key.chmod(0o644)
    env.update(MSREVIEW_APP_CLIENT_ID="Iv1", MSREVIEW_APP_PRIVATE_KEY_PATH=str(key))
    ok, detail = doctor.check_app_settings(env)
    assert not ok and "chmod 600" in detail
    key.chmod(0o600)
    assert doctor.check_app_settings(env)[0]
    cmd_env = {
        "HOME": str(tmp_path),
        "MSREVIEW_APP_CLIENT_ID": "Iv1",
        "MSREVIEW_APP_PRIVATE_KEY_COMMAND": "printf secret",
    }
    ok, detail = doctor.check_app_settings(cmd_env)
    assert ok and "not run" in detail
