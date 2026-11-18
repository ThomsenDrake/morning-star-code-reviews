"""Packaging smoke tests: the package imports and its metadata is consistent."""

from __future__ import annotations

import importlib
import importlib.metadata

DIST = "morning-star-code-reviews"


def test_package_imports() -> None:
    pkg = importlib.import_module("morning_star_review")
    assert pkg.__doc__


def test_version_is_single_sourced() -> None:
    from morning_star_review._version import __version__

    assert __version__ == "0.1.1"
    assert importlib.metadata.version(DIST) == __version__


def test_console_script_points_at_cli_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts", name="msreview")
    assert [ep.value for ep in scripts] == ["morning_star_review.cli:main"]


def test_base_dependencies_never_pull_workflow_plugin_extras() -> None:
    requires = [r.replace('"', "'") for r in importlib.metadata.requires(DIST) or []]
    base = [r for r in requires if "extra ==" not in r]
    workflows = [r for r in base if r.startswith("mistralai-workflows")]
    assert workflows == ["mistralai-workflows==3.19.0"]
    assert not any("mistralai-agents" in r for r in base)
    agent_extra = [r for r in requires if "extra == 'agent'" in r]
    assert agent_extra == ["mistralai-agents==1.1.3; extra == 'agent'"]
