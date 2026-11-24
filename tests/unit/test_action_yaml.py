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
from morning_star_review._version import __version__

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
    assert inputs["deadline-minutes"]["default"] == "90"
    assert "20-300" in inputs["deadline-minutes"]["description"]
    assert inputs["labels"]["default"] == "true"
    assert inputs["suggestions"]["default"] == "true"
    assert inputs["preset"]["default"] == ""  # repository file, else the built-in default
    assert set(doc["outputs"]) == {
        "status", "verdict", "ready", "findings", "execution-id", "report-path",
    }  # fmt: skip
    for name, output in doc["outputs"].items():
        # The publish step's value when it ran, else the review step's (deferred publish).
        assert output["value"] == (
            f"${{{{ steps.publish.outputs.{name} || steps.review.outputs.{name} }}}}"
        )
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
    # Hidden knobs: verbose output and the wind-down test knob (job env only).
    missing = read - env_names - {"MSREVIEW_VERBOSE", cli.ENV_WIND_DOWN}
    assert not missing, f"read by msreview ci but not set by action.yml: {missing}"
    defer = review["env"]["MSREVIEW_DEFER_PUBLISH_DIR"]
    assert "steps.token-read.conclusion == 'success'" in defer
    assert "/msreview/publish" in defer and defer.rstrip("} ").endswith("|| ''")
    assert review["env"]["MISTRAL_API_KEY"] == "${{ inputs.mistral-api-key }}"
    # The event's head SHA must not bind a run of another PR (`pr-url` input), and a
    # false condition must not become the string "false".
    head = review["env"]["MSREVIEW_HEAD_SHA"]
    assert "inputs.pr-url == github.event.pull_request.html_url" in head
    assert head.rstrip("} ").endswith("|| ''")
    # A `/msreview` comment: the PR commented on, and a refresh (no model runs).
    publish = by_id("publish")
    for step in (review, publish):
        url = " ".join(step["env"]["MSREVIEW_PR_URL"].split())
        assert url == (
            "${{ inputs.pr-url || github.event.pull_request.html_url || "
            "github.event.issue.pull_request.html_url }}"
        )
    assert review["env"]["MSREVIEW_REFRESH"] == "${{ github.event_name == 'issue_comment' }}"
    assert cli._ci_settings({**PR, "MSREVIEW_REFRESH": "true"})["refresh"] is True
    assert cli._ci_settings({**PR, "MSREVIEW_REFRESH": "false"})["refresh"] is False
    assert cli._ci_settings(PR)["refresh"] is False


PR = {"MSREVIEW_PR_URL": "https://github.com/o/r/pull/1"}


def step_ids() -> list[str]:
    return [str(s.get("id") or s.get("uses") or s.get("name")) for s in steps_of(load(ACTION))]


def by_id(name: str) -> dict[str, Any]:
    return next(s for s in steps_of(load(ACTION)) if s.get("id") == name)


CREDS = "inputs.client-id != '' && inputs.app-private-key != '' && inputs.post != 'never'"


def test_token_steps_read_before_and_write_after_the_review() -> None:
    ids = step_ids()
    assert ids.index("token-read") < ids.index("review") < ids.index("token-write")
    assert ids.index("token-write") < ids.index("publish") < len(ids) - 1
    assert ids[-1].startswith("actions/upload-artifact@")
    read, write = by_id("token-read"), by_id("token-write")
    assert read["uses"] == write["uses"]  # the same pinned action
    # The review step holds a read-only token (App tokens expire after one hour).
    r = read["with"]
    assert r["permission-contents"] == "read"
    assert r["permission-pull-requests"] == "read"
    assert r["permission-issues"] == "${{ inputs.labels == 'true' && 'read' || '' }}"
    assert not any("write" in str(v) for k, v in r.items() if k.startswith("permission-"))
    w = write["with"]
    assert w["permission-contents"] == "read"
    assert w["permission-pull-requests"] == "write"
    assert w["permission-issues"] == "${{ inputs.labels == 'true' && 'write' || '' }}"
    for step in (read, write):
        assert step["continue-on-error"] is True
        assert CREDS in step["if"]
    assert not read["if"].startswith("${{ always()")
    # After a failed or cancelled review too: failure plans and the label removal.
    for step in (write, by_id("publish")):
        assert step["if"] == f"${{{{ always() && {CREDS} }}}}"


def test_private_key_only_in_the_token_steps() -> None:
    for step in steps_of(load(ACTION)):
        if step.get("id") in ("token-read", "token-write"):
            assert step["with"]["private-key"] == "${{ inputs.app-private-key }}"
            continue
        # Elsewhere it may only be tested for emptiness in a condition.
        rest = {k: v for k, v in step.items() if k != "if"}
        assert "app-private-key" not in repr(rest), step.get("id") or step.get("name")
        assert (
            re.sub(r"inputs\.app-private-key != ''", "", str(step.get("if", ""))).find(
                "app-private-key"
            )
            == -1
        )


def test_publish_step_env_matches_ci_publish() -> None:
    publish = by_id("publish")
    env = publish["env"]
    assert env["MSREVIEW_GITHUB_TOKEN"] == "${{ steps.token-write.outputs.token }}"
    assert env["MSREVIEW_APP_SLUG"] == "${{ steps.token-write.outputs.app-slug }}"
    assert env["MSREVIEW_TOKEN_STEP"] == "${{ steps.token-write.outcome || 'skipped' }}"
    assert env["MSREVIEW_PUBLISH_PLAN"] == "${{ steps.review.outputs.publish-plan }}"
    assert env["MSREVIEW_REPORT_DIR"] == by_id("review")["env"]["MSREVIEW_REPORT_DIR"]
    assert env["MSREVIEW_PR_URL"] == by_id("review")["env"]["MSREVIEW_PR_URL"]
    assert "MISTRAL_API_KEY" not in env and "MSREVIEW_READ_TOKEN" not in env
    assert "ci-publish" in publish["run"]

    class Recorder(dict[str, str]):
        def get(self, key: str, default: Any = None) -> Any:
            seen.add(key)
            return ""

        def pop(self, key: str, default: Any = None) -> Any:
            seen.add(key)
            return default

    seen: set[str] = set()
    assert cli.cmd_ci_publish(Recorder()) == 0  # no plan: nothing to post
    read = {k for k in seen if k.startswith("MSREVIEW_")}
    assert set(env) <= read, f"set by action.yml but not read: {set(env) - read}"
    assert read - set(env) <= {"MSREVIEW_READ_TOKEN"}  # popped, never needed
    # The review step's token is read-only; the plan comes from its outputs.
    review_env = by_id("review")["env"]
    assert review_env["MSREVIEW_GITHUB_TOKEN"] == "${{ steps.token-read.outputs.token }}"
    assert review_env["MSREVIEW_TOKEN_STEP"] == "${{ steps.token-read.outcome || 'skipped' }}"


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


ACTION_REF = "ThomsenDrake/morning-star-code-reviews@"


@pytest.mark.parametrize("path", [EXAMPLE, FORKS], ids=lambda p: p.name)
def test_example_workflows_pin_the_current_release(path: Path) -> None:
    # Users copy the examples as they are: the tag must accept the inputs they show
    # (for example deadline-minutes 90, which 0.1.0 rejected).
    uses = [s["uses"] for s in steps_of(load(path)) if s.get("uses", "").startswith(ACTION_REF)]
    assert uses == [f"{ACTION_REF}v{__version__}"]
    assert "# Pin to the full commit SHA of the release you reviewed." in path.read_text()


def test_docs_name_the_current_release() -> None:
    readme = (ROOT / "README.md").read_text()
    assert f"- Status: {__version__}, " in readme
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"\n## [{__version__}] - " in changelog
    assert f"\n[{__version__}]: https://github.com/{ACTION_REF[:-1]}/" in changelog
    cli_md = (ROOT / "docs" / "cli.md").read_text()
    assert f"{ACTION_REF}v{__version__}\n" in cli_md  # the install example
    assert f"/agent-{__version__}/bin/python" in cli_md  # the config example
    assert f"(`@v{__version__}`)" in (ROOT / "docs" / "security.md").read_text()


def test_example_workflow_skips_forks() -> None:
    doc = load(EXAMPLE)
    assert doc["on"] == {
        "pull_request": {"types": ["opened", "synchronize", "reopened", "ready_for_review"]},
        "issue_comment": {"types": ["created", "edited"]},
    }
    job = doc["jobs"]["review"]
    assert "head.repo.full_name == github.repository" in job["if"]
    # Rate limits make a review slower: about 20 above the 90-minute default deadline.
    assert job["timeout-minutes"] == 110
    assert 'deadline-minutes: "90"' in EXAMPLE.read_text()
    assert doc["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "morning-star-code-review-forks.yml" in EXAMPLE.read_text()


def test_example_workflow_runs_dismiss_commands() -> None:
    doc = load(EXAMPLE)
    job = doc["jobs"]["review"]
    condition = " ".join(job["if"].split())
    # Only comments on pull requests that hold a dismiss command, by people who may
    # have write access (msreview checks the permission itself).
    assert "github.event_name == 'issue_comment' && github.event.issue.pull_request" in condition
    assert "contains(github.event.comment.body, '/msreview dismiss')" in condition
    # Bots never: our own summary mentions the command, and every edit of it is a comment.
    assert "github.event.comment.user.type != 'Bot'" in condition
    assert 'fromJSON(\'["OWNER", "MEMBER", "COLLABORATOR"]\')' in condition
    assert "github.event.comment.author_association" in condition
    # Per job, so the runs other comments start (and skip) never join the group; a push
    # cancels the review in progress, a command waits for it.
    assert "concurrency" not in doc
    group = " ".join(job["concurrency"]["group"].split())
    assert group == (
        "${{ github.workflow }}-${{ github.event.pull_request.number || "
        "github.event.issue.number }}"
    )
    assert job["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


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
    assert review["timeout-minutes"] == 110
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
    # when it skips a fork PR, so a shared group would cancel the fork review. Both groups
    # are keyed by the workflow's name, and the two workflows have different names.
    example = load(EXAMPLE)
    assert "github.workflow" in review["concurrency"]["group"]
    assert "github.workflow" in example["jobs"]["review"]["concurrency"]["group"]
    assert doc["name"] != example["name"]


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
