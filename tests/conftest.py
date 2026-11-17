"""Shared pytest configuration: environment detection, opt-in suites and a clean cwd."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def _agent_extra_installed() -> bool:
    # Metadata lookup instead of an import, so the test process never loads
    # mistralai.agents just to find out whether it exists.
    try:
        importlib.metadata.distribution("mistralai-agents")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


AGENT_ENV = _agent_extra_installed()

# Directories whose tests import mistralai.agents (venv-agent only). In the worker
# venv they are not even collected, because collecting them would fail on import.
AGENT_ONLY_DIRS = ("unit/agent", "scripted", "canary")

# Opt-in suites: directory -> environment variable that must equal "1".
OPT_IN_DIRS = {
    "platform": "MSREVIEW_PLATFORM_TESTS",
    "e2e": "MSREVIEW_E2E",
    "canary": "MSREVIEW_CANARY",
}

collect_ignore_glob: list[str] = []
if not AGENT_ENV:
    collect_ignore_glob = [f"{d}/*" for d in AGENT_ONLY_DIRS]


def _top_dir(path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(TESTS_DIR)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        top = _top_dir(item.path)
        if top is None:
            continue
        if top in OPT_IN_DIRS:
            item.add_marker(getattr(pytest.mark, top))
            env_var = OPT_IN_DIRS[top]
            if os.environ.get(env_var) != "1":
                item.add_marker(pytest.mark.skip(reason=f"opt-in suite; set {env_var}=1"))
        if top == "scripted" or item.path.is_relative_to(TESTS_DIR / "unit" / "agent"):
            item.add_marker(pytest.mark.agent)
        if item.get_closest_marker("agent") and not AGENT_ENV:
            item.add_marker(pytest.mark.skip(reason="needs the venv-agent environment"))


@pytest.fixture(autouse=True)
def _isolated_cwd(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Workflows SDK reads `.env.test` from the cwd under pytest; run every test
    # from an empty directory so nothing in the checkout leaks into its config.
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
