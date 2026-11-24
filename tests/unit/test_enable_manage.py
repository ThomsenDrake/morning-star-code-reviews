"""``msreview status``, ``update`` and ``disable`` against a fake ``gh``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from morning_star_review.enable import template as tpl
from morning_star_review.enable.values import EnableUsageError

WF = tpl.WORKFLOW_PATH


def load_harness() -> ModuleType:
    name = "fake_gh_harness"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[1] / "fixtures" / "fake_gh" / "harness.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


h = load_harness()


@pytest.fixture
def gh(tmp_path: Path) -> Any:
    return h.FakeGitHub(tmp_path)


def test_status_lists_the_apps_repositories(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0", "--preset", "balanced").code == 0
    state = gh.state()
    state["repos"]["octo/one"]["runs"] = [
        {"conclusion": "success", "status": "completed", "created_at": "2026-11-20T10:00:00Z"}
    ]
    gh.save(state)
    before = len(gh.calls())
    res = gh.run("status", "--client-id", h.CLIENT_ID, "--app-key-file", str(gh.key_file))
    assert res.code == 0, res.err
    lines = res.out.splitlines()
    assert lines[:5] == [
        "octo/one",
        "  workflow  v0.2.0 (0123456), preset balanced",
        "  secrets   MSREVIEW_MISTRAL_API_KEY set, MSREVIEW_APP_PRIVATE_KEY set",
        "  variable  MSREVIEW_APP_CLIENT_ID set",
        "  last run  success (2026-11-20)",
    ]
    assert "  workflow  not enabled" in lines
    # Only the App's installation: octo/other belongs to another App.
    assert [line for line in lines if not line.startswith(" ")] == sorted(h.INSTALLED)
    for call in gh.calls()[before:]:  # read-only
        assert call["argv"][0] in ("auth", "api"), call["argv"]
        assert call["argv"][0] == "auth" or call["argv"][1:3] == ["-X", "GET"], call["argv"]


def test_status_json_and_named_repositories(gh: Any) -> None:
    res = gh.run("status", "octo/two", "octo/missing", "--json")
    assert res.code == 1  # one repository could not be read
    rows = json.loads(res.out)
    assert rows[0]["repo"] == "octo/two"
    assert rows[0]["workflow"]["present"] is False
    assert rows[0]["secrets"] == {
        "MSREVIEW_MISTRAL_API_KEY": False,
        "MSREVIEW_APP_PRIVATE_KEY": False,
    }
    assert rows[0]["legacy_secret"] is None
    assert rows[0]["last_run"] is None
    assert rows[1] == {
        "repo": "octo/missing",
        "error": "not found, or your GitHub login cannot see it",
    }


def test_status_needs_names_or_the_app_credentials(gh: Any) -> None:
    with pytest.raises(EnableUsageError, match="name the repositories"):
        gh.run("status")
    with pytest.raises(EnableUsageError, match=r"private key \(--app-key-file\)"):
        gh.run("status", "--client-id", h.CLIENT_ID)


def test_status_lists_with_the_app_key_the_cli_posts_with(gh: Any, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[app]\nclient_id = "{h.CLIENT_ID}"\nprivate_key_path = "{gh.key_file}"\n')
    res = gh.run("status", env={"MSREVIEW_CONFIG": str(config)})
    assert res.code == 0, res.err
    assert "with the App key configured for the CLI" in res.err
    assert [line for line in res.out.splitlines() if not line.startswith(" ")] == sorted(
        h.INSTALLED
    )
    gh.key_file.chmod(0o644)  # the CLI refuses keys that others can read
    with pytest.raises(EnableUsageError, match="configured for the CLI is unusable"):
        gh.run("status", env={"MSREVIEW_CONFIG": str(config)})


def test_update_keeps_the_preset_and_changes_only_the_pin(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.1.1", "--preset", "fast").code == 0
    res = gh.run("update", "octo/one", "--version", "v0.2.0")
    assert res.code == 0, res.err + res.out
    assert "octo/one: updated v0.1.1 (89abcde) -> v0.2.0: committed" in res.out
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020, "fast")
    messages = [c["message"] for c in gh.repo("octo/one")["commits"]]
    assert messages[-1] == "Update Morning Star Code Review to v0.2.0"
    again = gh.run("update", "octo/one", "--version", "v0.2.0")
    assert "octo/one: already at v0.2.0" in again.out
    assert len(gh.repo("octo/one")["commits"]) == 2


def test_update_keeps_edits_unless_forced(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.1.1", "--preset", "single").code == 0
    state = gh.state()
    files = state["repos"]["octo/one"]["branches"]["main"]["files"]
    files[WF] = files[WF].replace("timeout-minutes: 110", "timeout-minutes: 200")
    gh.save(state)
    res = gh.run("update", "octo/one", "--version", "v0.2.0", "--pr")
    assert "kept the file's other lines" in res.out and "opened https://" in res.out
    text = gh.file("octo/one", WF, branch="msreview/update") or ""
    assert "timeout-minutes: 200" in text and f"@{h.SHA_020} # v0.2.0" in text
    forced = gh.run("update", "octo/one", "--version", "v0.2.0", "--force")
    assert forced.code == 0, forced.out
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020, "single")


def test_update_all_skips_repositories_without_the_workflow(gh: Any) -> None:
    assert gh.enable("octo/two", "--version", "v0.1.1").code == 0
    res = gh.run(
        "update", "--all", "--version", "v0.2.0", "--client-id", h.CLIENT_ID,
        "--app-key-file", str(gh.key_file),
    )  # fmt: skip
    assert res.code == 0, res.err
    assert res.out.splitlines() == [
        f"octo/two: updated v0.1.1 (89abcde) -> v0.2.0: committed "
        f"{gh.repo('octo/two')['branches']['main']['sha'][:7]} to main"
    ]
    named = gh.run("update", "octo/three", "--version", "v0.2.0")
    assert named.code == 1 and "run `msreview enable`" in named.out
    with pytest.raises(EnableUsageError, match="or pass --all"):
        gh.run("update")


def test_disable_removes_the_file_and_the_secrets(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    res = gh.run("disable", "octo/one")
    assert res.code == 0, res.out
    assert "workflow removed: committed" in res.out
    assert (
        "deleted MSREVIEW_MISTRAL_API_KEY, MSREVIEW_APP_PRIVATE_KEY, MSREVIEW_APP_CLIENT_ID"
        in res.out
    )
    repo = gh.repo("octo/one")
    assert gh.file("octo/one", WF) is None
    assert repo["secrets"] == {} and repo["variables"] == {}
    assert repo["commits"][-1]["message"] == "Disable Morning Star Code Review"
    again = gh.run("disable", "octo/one")
    assert "no workflow file; no secrets or variable to delete" in again.out


def test_disable_keep_secrets_and_pr(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    res = gh.run("disable", "octo/one", "--keep-secrets", "--pr")
    assert res.code == 0, res.out
    assert "workflow removal: opened https://github.com/octo/one/pull/1" in res.out
    repo = gh.repo("octo/one")
    assert set(repo["secrets"]) == {"MSREVIEW_MISTRAL_API_KEY", "MSREVIEW_APP_PRIVATE_KEY"}
    assert gh.file("octo/one", WF) is not None  # until the PR is merged
    assert gh.file("octo/one", WF, branch="msreview/disable") is None
    assert not any(c["argv"][1:2] == ["delete"] for c in gh.calls())


def test_status_shows_the_legacy_secret_name(gh: Any) -> None:
    gh.put(
        "octo/one",
        files={WF: h.legacy_workflow("v0.1.1", h.SHA_011)},
        MISTRAL_API_KEY=h.OWN_KEY,
        MSREVIEW_APP_PRIVATE_KEY=h.PEM,
    )
    res = gh.run("status", "octo/one")
    assert res.code == 0, res.err
    lines = res.out.splitlines()
    assert lines[2] == (
        "  secrets   legacy secret name MISTRAL_API_KEY set, MSREVIEW_APP_PRIVATE_KEY set"
    )
    assert lines[-1].startswith("  hint      the workflow uses the secret MISTRAL_API_KEY")
    assert "`msreview enable octo/one --secrets-only`" in lines[-1]
    rows = json.loads(gh.run("status", "octo/one", "--json").out)
    assert rows[0]["legacy_secret"] == "MISTRAL_API_KEY"
    assert rows[0]["secrets"] == {"MISTRAL_API_KEY": True, "MSREVIEW_APP_PRIVATE_KEY": True}
    # Once moved to the new name, the legacy name is no longer shown.
    gh.put("octo/one", files={WF: tpl.render("v0.2.0", h.SHA_020)})
    moved = gh.run("status", "octo/one").out.splitlines()
    assert moved[2] == "  secrets   MSREVIEW_MISTRAL_API_KEY missing, MSREVIEW_APP_PRIVATE_KEY set"
    assert not any("hint" in line for line in moved)


def test_update_keeps_the_legacy_secret_and_says_how_to_move(gh: Any) -> None:
    legacy = h.legacy_workflow("v0.1.1", h.SHA_011, "fast")
    gh.put("octo/one", files={WF: legacy}, MISTRAL_API_KEY=h.OWN_KEY)
    res = gh.run("update", "octo/one", "--version", "v0.2.0")
    assert res.code == 0, res.err + res.out
    text = gh.file("octo/one", WF) or ""
    assert text == legacy.replace(f"{h.SHA_011} # v0.1.1", f"{h.SHA_020} # v0.2.0")
    assert "secrets.MISTRAL_API_KEY" in text
    hint = (
        "  the workflow uses the secret MISTRAL_API_KEY (the 0.1.x name); to move to "
        "MSREVIEW_MISTRAL_API_KEY, run `msreview enable octo/one --secrets-only`, then "
        "`msreview update octo/one --force` (it re-renders the file)"
    )
    assert hint in res.out.splitlines()
    again = gh.run("update", "octo/one", "--version", "v0.2.0")
    assert "octo/one: already at v0.2.0" in again.out and hint in again.out.splitlines()
    # --force before the new secret exists: done, with a warning.
    forced = gh.run("update", "octo/one", "--version", "v0.2.0", "--force", "--pr")
    assert forced.code == 0, forced.out
    assert "warning: MSREVIEW_MISTRAL_API_KEY is not set here" in forced.out
    proposed = gh.file("octo/one", WF, branch="msreview/update")
    assert proposed == tpl.render("v0.2.0", h.SHA_020, "fast")
    # The documented order: the secret first, then --force; no hint or warning then.
    assert gh.enable("octo/one", "--secrets-only").code == 0
    done = gh.run("update", "octo/one", "--version", "v0.2.0", "--force")
    assert done.code == 0, done.out
    assert done.out.count("\n") == 1  # one line: no hint, no warning
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020, "fast")
    assert gh.repo("octo/one")["secrets"]["MISTRAL_API_KEY"] == h.OWN_KEY


def test_disable_never_deletes_the_repositorys_mistral_api_key(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    gh.put("octo/one", MISTRAL_API_KEY=h.OWN_KEY)
    res = gh.run("disable", "octo/one")
    assert res.code == 0, res.out
    assert gh.repo("octo/one")["secrets"] == {"MISTRAL_API_KEY": h.OWN_KEY}
    assert "leaves it in place" not in res.out  # the workflow did not use it
    # A 0.1.x workflow that used it: still left in place, and the output says so.
    gh.put("octo/two", files={WF: h.legacy_workflow("v0.1.1", h.SHA_011)})
    gh.put("octo/two", MISTRAL_API_KEY=h.OWN_KEY, MSREVIEW_APP_PRIVATE_KEY=h.PEM)
    legacy = gh.run("disable", "octo/two")
    assert legacy.code == 0, legacy.out
    assert "octo/two: workflow removed: committed" in legacy.out
    assert "deleted MSREVIEW_APP_PRIVATE_KEY" in legacy.out
    assert "the workflow used the secret MISTRAL_API_KEY: msreview leaves it in place" in (
        legacy.out
    )
    assert gh.repo("octo/two")["secrets"] == {"MISTRAL_API_KEY": h.OWN_KEY}
    deleted = [c["argv"][2] for c in gh.calls() if c["argv"][:2] == ["secret", "delete"]]
    assert "MISTRAL_API_KEY" not in deleted and "MSREVIEW_MISTRAL_API_KEY" in deleted


def test_disable_pr_rerun_says_the_removal_is_already_proposed(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    first = gh.run("disable", "octo/one", "--pr", "--keep-secrets")
    assert "workflow removal: opened https://github.com/octo/one/pull/1" in first.out
    again = gh.run("disable", "octo/one", "--pr", "--keep-secrets")
    assert again.code == 0, again.out
    assert again.out.splitlines() == [
        "octo/one: workflow removal: already proposed in https://github.com/octo/one/pull/1"
    ]
    assert "workflow removed" not in first.out + again.out
    assert len(gh.repo("octo/one")["commits"]) == 2  # enable + the one removal commit


def without_comment_trigger(text: str) -> str:
    """A workflow as 0.2.x wrote it: no issue_comment trigger."""
    trigger = "  issue_comment:\n    types: [created, edited]\n"
    assert trigger in text
    return text.replace(trigger, "")


def test_status_and_update_say_how_to_enable_dismiss_commands(gh: Any) -> None:
    old = without_comment_trigger(tpl.render("v0.2.0", h.SHA_020, "fast"))
    gh.put("octo/one", files={WF: old})
    lines = gh.run("status", "octo/one").out.splitlines()
    hint = (
        "the workflow does not run on comments, so `/msreview dismiss` takes effect at the "
        "next review only; `msreview update octo/one --force` adds it (it re-renders the file)"
    )
    assert lines[-1] == f"  hint      {hint}"
    rows = json.loads(gh.run("status", "octo/one", "--json").out)
    assert rows[0]["workflow"]["commands"] is False
    # The last run is the last review: runs that comments start (and skip) do not count.
    runs = [" ".join(c["argv"]) for c in gh.calls() if "/runs" in " ".join(c["argv"])]
    assert runs and all("/runs?event=pull_request&per_page=1" in r for r in runs)
    # update keeps the file's other lines (no trigger) and says so; --force adds it.
    res = gh.run("update", "octo/one", "--version", "v0.2.0")
    assert f"  {hint}" in res.out.splitlines()
    forced = gh.run("update", "octo/one", "--version", "v0.2.0", "--force")
    assert forced.code == 0 and hint not in forced.out
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020, "fast")
    assert not any("hint" in line for line in gh.run("status", "octo/one").out.splitlines())
    rows = json.loads(gh.run("status", "octo/one", "--json").out)
    assert rows[0]["workflow"]["commands"] is True
