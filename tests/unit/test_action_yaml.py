"""Sanity checks of action.yml and the workflow files (plus actionlint when installed)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from morning_star_review import cli

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "action.yml"
EXAMPLE = ROOT / "examples" / "workflows" / "morning-star-code-review.yml"
FORKS = ROOT / "examples" / "workflows" / "morning-star-code-review-forks.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
WORKFLOWS = (EXAMPLE, FORKS, CI, RELEASE)
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    # YAML 1.1 reads the key `on` as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def steps_of(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    if "runs" in doc:
        return list(doc["runs"]["steps"])
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


def test_action_inputs_and_defaults() -> None:
    doc = load(ACTION)
    inputs = doc["inputs"]
    expected = {
        "mistral-api-key", "client-id", "app-private-key", "post", "preset", "explorers",
        "explorer-model", "formatter-model", "explorer-effort", "verify", "verifier-model",
        "verifier-effort", "max-cost-usd", "max-tool-calls", "explorer-minutes",
        "verifier-max-tool-calls", "verifier-minutes", "allowed-models", "review-drafts",
        "labels", "suggestions", "fail-on", "deadline-minutes", "telemetry", "pr-url",
        "expected-head-sha", "remove-label",
    }  # fmt: skip
    assert set(inputs) == expected
    assert inputs["mistral-api-key"]["required"] is True
    assert inputs["deadline-minutes"]["default"] == "40"
    assert inputs["labels"]["default"] == "true"
    assert inputs["suggestions"]["default"] == "true"
    assert inputs["preset"]["default"] == ""  # repository file, else the built-in default
    assert set(doc["outputs"]) == {"status", "verdict", "findings", "execution-id", "report-path"}
    assert doc["name"] == "Morning Star Code Review"


def test_action_env_matches_the_ci_contract() -> None:
    doc = load(ACTION)
    review = next(s for s in steps_of(doc) if s.get("id") == "review")
    env_names = {k for k in review["env"] if k.startswith("MSREVIEW_")}

    class Recorder(dict[str, str]):
        def get(self, key: str, default: Any = None) -> Any:
            seen.add(key)
            return {"MSREVIEW_PR_URL": "https://github.com/o/r/pull/1"}.get(key, "")

    seen: set[str] = set()
    cli._ci_settings(Recorder())
    read = {k for k in seen if k.startswith("MSREVIEW_")}
    # Tokens are popped by ci_secrets; the agent python/report/run root are read too.
    popped = {"MSREVIEW_GITHUB_TOKEN", "MSREVIEW_READ_TOKEN"}
    assert env_names - popped <= read
    missing = read - env_names - {"MSREVIEW_VERBOSE"}
    assert not missing, f"read by msreview ci but not set by action.yml: {missing}"
    assert review["env"]["MISTRAL_API_KEY"] == "${{ inputs.mistral-api-key }}"
    # The event's head SHA must not bind a run of another PR (`pr-url` input), and a
    # false condition must not become the string "false".
    head = review["env"]["MSREVIEW_HEAD_SHA"]
    assert "inputs.pr-url == github.event.pull_request.html_url" in head
    assert head.rstrip("} ").endswith("|| ''")


def test_token_step_permissions() -> None:
    token = next(s for s in steps_of(load(ACTION)) if s.get("id") == "token")
    w = token["with"]
    assert w["permission-contents"] == "read"
    assert w["permission-pull-requests"] == "write"
    assert "labels" in w["permission-issues"]  # issues: write only when labels are on
    assert token["continue-on-error"] is True
    assert "inputs.post != 'never'" in token["if"]


@pytest.mark.parametrize("path", [ACTION, *WORKFLOWS], ids=lambda p: p.name)
def test_no_expression_in_run_scripts(path: Path) -> None:
    # Expressions in `run:` are a script-injection risk; values go through `env:`.
    for step in steps_of(load(path)):
        assert "${{" not in step.get("run", ""), step.get("name") or step.get("id")


@pytest.mark.parametrize("path", [ACTION, CI, RELEASE, FORKS], ids=lambda p: p.name)
def test_third_party_actions_are_pinned_by_sha(path: Path) -> None:
    for step in steps_of(load(path)):
        uses = step.get("uses")
        if uses is None or uses.startswith("ThomsenDrake/morning-star-code-reviews@"):
            continue
        assert PINNED.match(uses), uses


def test_example_workflow_skips_forks() -> None:
    doc = load(EXAMPLE)
    assert doc["on"] == {
        "pull_request": {"types": ["opened", "synchronize", "reopened", "ready_for_review"]}
    }
    job = doc["jobs"]["review"]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert job["timeout-minutes"] == 45
    assert doc["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "morning-star-code-review-forks.yml" in EXAMPLE.read_text()


def test_fork_workflow_follows_addendum_g() -> None:
    doc = load(FORKS)
    assert doc["on"] == {
        "pull_request_target": {
            "types": ["opened", "synchronize", "reopened", "ready_for_review", "labeled"]
        }
    }
    assert doc["permissions"] == {}
    review = doc["jobs"]["review"]
    assert review["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "head.repo.full_name != github.repository" in review["if"]
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert assoc in review["if"]
    assert "msreview: safe-to-review" in review["if"]
    gate, step = review["steps"]
    assert step["uses"].startswith("ThomsenDrake/morning-star-code-reviews@")
    assert step["with"]["expected-head-sha"] == "${{ github.event.pull_request.head.sha }}"
    assert step["with"]["remove-label"] == "msreview: safe-to-review"
    # The triage role can add labels too: the approval path first checks the permission
    # of the person who added the label, with the read-only workflow token.
    assert gate["if"] == "github.event.action == 'labeled'"
    assert gate["env"]["SENDER"] == "${{ github.event.sender.login }}"
    assert gate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "collaborators/$SENDER/permission" in gate["run"]
    assert "continue-on-error" not in gate
    revoke = doc["jobs"]["revoke-approval"]
    assert "synchronize" in revoke["if"] and "!contains" in revoke["if"]
    assert revoke["permissions"] == {}
    # Security review SEC-CI-6: the automatic path also needs the head repository to be
    # the trusted author's own; anything else goes through the label (and its revocation).
    owner_check = (
        "github.event.pull_request.head.repo.owner.login == github.event.pull_request.user.login"
    )
    assert owner_check in " ".join(review["if"].split())
    assert owner_check.replace("==", "!=") in " ".join(revoke["if"].split())
    text = FORKS.read_text()
    assert "actions/checkout" not in text  # never check out PR code
    # Concurrency groups are repository-wide: the non-fork workflow joins its group even
    # when it skips a fork PR, so a shared name would cancel the fork review.
    assert review["concurrency"]["group"] != load(EXAMPLE)["concurrency"]["group"]


def approval_gate_script() -> str:
    gate = load(FORKS)["jobs"]["review"]["steps"][0]
    assert gate["shell"] == "bash"
    return str(gate["run"])


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize(
    ("answer", "allowed"),
    [
        ("admin", True),
        ("write", True),  # write, maintain, and custom roles based on them
        ("read", False),  # read and triage
        ("none", False),
        ("", False),
        ("Admin", False),
        (None, False),  # the API call failed, for example HTTP 404
    ],
)
def test_fork_approval_gate_needs_write_access(
    tmp_path: Path, answer: str | None, allowed: bool
) -> None:
    # A stand-in `gh` prints what `gh api ... --jq .permission` would print.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    if answer is None:
        fake.write_text("#!/bin/sh\necho 'gh: Not Found (HTTP 404)' >&2\nexit 1\n")
    else:
        fake.write_text(f'#!/bin/sh\necho "$*" > "{tmp_path}/args"\necho "{answer}"\n')
    fake.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "REPO": "o/r", "SENDER": "someone"}
    res = subprocess.run(
        ["bash", "-c", approval_gate_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (res.returncode == 0) is allowed, res.stdout + res.stderr
    if answer is not None:
        assert (tmp_path / "args").read_text().split()[:2] == [
            "api",
            "repos/o/r/collaborators/someone/permission",
        ]


def test_release_has_no_pypi_publish() -> None:
    text = RELEASE.read_text().lower()
    assert "pypi" not in text.replace("no pypi upload", "")
    assert "gh release create" in text
    assert load(RELEASE)["on"] == {"push": {"tags": ["v*"]}}


def test_ci_has_a_dry_run_of_both_environments() -> None:
    job = load(CI)["jobs"]["action-dry-run"]
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert "--extra agent" in script and 'msreview" --version' in script


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_actionlint() -> None:
    res = subprocess.run(
        ["actionlint", *[str(p) for p in WORKFLOWS]],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=ROOT,
    )
    assert res.returncode == 0, res.stdout + res.stderr
