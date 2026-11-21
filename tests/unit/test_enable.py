"""``msreview enable`` against a fake ``gh`` (tests/fixtures/fake_gh)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from morning_star_review import cli
from morning_star_review.enable import template as tpl
from morning_star_review.enable.gh import GhError
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


def secret_calls(gh: Any) -> list[dict[str, Any]]:
    return [c for c in gh.calls() if c["argv"][:1] in (["secret"], ["variable"])]


def test_values_are_collected_once_for_many_repos(gh: Any) -> None:
    counting = h.CountingPrompts()
    res = gh.run(
        "enable", "octo/one", "octo/two", "octo/three", "--version", "v0.2.0",
        "--app-key-file", str(gh.key_file), prompts=counting.prompts(),
    )  # fmt: skip
    assert res.code == 0, res.err + res.out
    assert counting.asked == ["text", "secret"]  # client id, then the hidden key: once
    assert gh.verifier.calls == [("mistral", h.MISTRAL_KEY)]
    [api] = gh.app_apis  # one App client for the whole run, closed at the end
    assert api.client_id == h.CLIENT_ID and api.key == (h.PEM.strip() + "\n").encode()
    assert [c for c in api.calls if c[0] != "repo_installation"] == [("app",)]
    for name in h.INSTALLED:
        repo = gh.repo(name)
        assert repo["secrets"] == {"MSREVIEW_MISTRAL_API_KEY": h.MISTRAL_KEY,
                                   "MSREVIEW_APP_PRIVATE_KEY": h.PEM.strip() + "\n"}  # fmt: skip
        assert repo["variables"] == {"MSREVIEW_APP_CLIENT_ID": h.CLIENT_ID}
        assert gh.file(name, WF) == tpl.render("v0.2.0", h.SHA_020)
        assert [c["message"] for c in repo["commits"]] == ["Enable Morning Star Code Review"]
        assert f"{name}: enabled: secrets and variable set; workflow v0.2.0: committed" in res.out
    assert "enabled 3 of 3 repositories" in res.err


def test_secret_values_go_on_stdin_never_on_argv(gh: Any) -> None:
    # A Mistral key the shell exports stays out of gh's environment too.
    res = gh.enable("octo/one", "--version", "v0.2.0", env={"MISTRAL_API_KEY": "mk-shell-key"})
    assert res.code == 0, res.err
    pem_line = h.PEM.splitlines()[1]
    for call in gh.calls():
        joined = " ".join(call["argv"])
        assert h.MISTRAL_KEY not in joined and pem_line not in joined
        assert call["msreview_env"] == []  # MSREVIEW_* and MISTRAL_API_KEY stay out of gh
        if call["argv"][:1] == ["api"]:  # request bodies never carry a secret either
            assert h.MISTRAL_KEY not in call["stdin"] and pem_line not in call["stdin"]
    sets = {c["argv"][2]: c for c in secret_calls(gh) if c["argv"][1] == "set"}
    assert sets["MSREVIEW_MISTRAL_API_KEY"]["stdin"] == h.MISTRAL_KEY
    assert pem_line in sets["MSREVIEW_APP_PRIVATE_KEY"]["stdin"]
    assert sets["MSREVIEW_APP_CLIENT_ID"]["argv"][:2] == ["variable", "set"]
    for call in sets.values():
        assert "--body" not in call["argv"] and "-b" not in call["argv"]
    assert h.MISTRAL_KEY not in res.out + res.err and pem_line not in res.out + res.err


def test_app_not_installed_on_the_repository_is_skipped_with_the_link(gh: Any) -> None:
    res = gh.enable("octo/other", "octo/one", "--version", "v0.2.0")
    assert res.code == 1
    assert "octo/other: skipped: the GitHub App cannot see octo/other" in res.out
    assert "https://github.com/settings/installations/11" in res.out
    assert "octo/one: enabled" in res.out  # the run goes on
    other = gh.repo("octo/other")
    assert other["secrets"] == {} and "commits" not in other
    forced = gh.enable("octo/other", "--version", "v0.2.0", "--force")
    assert forced.code == 0, forced.out
    assert "the App is not installed here yet" in forced.out
    assert gh.file("octo/other", WF) is not None


def test_app_not_installed_on_the_account_links_the_install_page(gh: Any) -> None:
    res = gh.enable("acme/tool", "--version", "v0.2.0")
    assert res.code == 1
    assert "not installed on acme" in res.out
    assert f"https://github.com/apps/{h.SLUG}/installations/new" in res.out


def test_a_different_workflow_file_is_not_overwritten(gh: Any) -> None:
    state = gh.state()
    state["repos"]["octo/one"]["branches"]["main"]["files"][WF] = "name: mine\n"
    gh.save(state)
    res = gh.enable("octo/one", "--version", "v0.2.0")
    assert res.code == 1
    assert f"failed: a different {WF} exists; --force replaces it" in res.out
    assert "    -name: mine" in res.out  # the short diff
    repo = gh.repo("octo/one")
    assert "commits" not in repo and repo["secrets"] == {}  # nothing touched
    assert gh.enable("octo/one", "--version", "v0.2.0", "--force").code == 0
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020)


def test_an_older_msreview_workflow_points_at_update(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.1.1").code == 0
    res = gh.enable("octo/one", "--version", "v0.2.0")
    assert res.code == 1
    assert "it uses v0.1.1 (89abcde); `msreview update` re-pins it" in res.out


def test_rerun_with_the_same_content_makes_no_commit(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0", "--preset", "fast").code == 0
    again = gh.enable("octo/one", "--version", "v0.2.0", "--preset", "fast")
    assert again.code == 0
    assert "workflow already at v0.2.0" in again.out
    assert len(gh.repo("octo/one")["commits"]) == 1
    assert "preset: fast " in (gh.file("octo/one", WF) or "")


def test_pr_mode_opens_one_pull_request(gh: Any) -> None:
    res = gh.enable("octo/one", "--version", "v0.2.0", "--pr")
    assert res.code == 0, res.out
    assert "opened https://github.com/octo/one/pull/1" in res.out
    assert gh.file("octo/one", WF) is None  # the default branch is untouched
    assert gh.file("octo/one", WF, branch="msreview/enable") == tpl.render("v0.2.0", h.SHA_020)
    repo = gh.repo("octo/one")
    assert [(p["head"], p["base"]) for p in repo["pulls"]] == [("msreview/enable", "main")]
    assert repo["commits"][0]["branch"] == "msreview/enable"
    # Again with another preset: the same branch is reset and the open PR reused.
    again = gh.enable("octo/one", "--version", "v0.2.0", "--pr", "--preset", "balanced")
    assert again.code == 0, again.out
    assert "updated https://github.com/octo/one/pull/1" in again.out
    assert len(gh.repo("octo/one")["pulls"]) == 1
    assert "preset: balanced" in (gh.file("octo/one", WF, branch="msreview/enable") or "")


def test_secrets_only_needs_no_workflow_scope_and_writes_no_file(gh: Any) -> None:
    gh.edit(auth={"login": "octo", "scopes": "repo"})
    res = gh.enable("octo/one", "--secrets-only")
    assert res.code == 0, res.err + res.out
    repo = gh.repo("octo/one")
    assert set(repo["secrets"]) == {"MSREVIEW_MISTRAL_API_KEY", "MSREVIEW_APP_PRIVATE_KEY"}
    assert "commits" not in repo
    # No release lookup either: the file is not written.
    assert not any("git/ref/tags" in " ".join(c["argv"]) for c in gh.calls())


def test_no_secrets_writes_only_the_workflow(gh: Any) -> None:
    res = gh.run(
        "enable", "octo/one", "--no-secrets", "--version", "v0.2.0",
        env={"MSREVIEW_ENABLE_APP_CLIENT_ID": h.CLIENT_ID},
    )  # fmt: skip
    assert res.code == 0, res.err + res.out
    assert secret_calls(gh) == []
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020)
    assert gh.verifier.calls == [] and gh.app_apis == []  # no keys: nothing to check
    assert "enabled: the App installation was not checked; workflow v0.2.0: committed" in res.out
    with_key = gh.enable("octo/two", "--no-secrets", "--version", "v0.2.0")
    assert "octo/two: enabled: workflow v0.2.0" in with_key.out  # checked with the key
    bare = gh.run("enable", "octo/three", "--no-secrets", "--version", "v0.2.0")
    assert bare.code == 0, bare.err  # no client id is needed (or asked for) then


def test_missing_workflow_scope_says_how_to_add_it(
    gh: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.edit(auth={"login": "octo", "scopes": "gist, repo"})
    with pytest.raises(EnableUsageError, match=r"gh auth refresh -h github\.com -s workflow"):
        gh.enable("octo/one", "--version", "v0.2.0")
    assert gh.state()["repos"]["octo/one"]["secrets"] == {}
    # Through the CLI: exit code 2 and the message on stderr.
    for name, value in gh.env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["enable", "octo/one", "--secrets-only", "--client-id", "x"]) == 2
    assert "client id should look like" in capsys.readouterr().err
    assert cli.main(["update", "octo/one"]) == 2
    err = capsys.readouterr().err
    assert "lacks the scope(s) workflow" in err and "gh auth refresh" in err


def test_a_token_from_the_environment_cannot_be_refreshed(gh: Any) -> None:
    gh.edit(auth={"login": "octo", "scopes": "repo", "tokenSource": "GH_TOKEN"})
    with pytest.raises(EnableUsageError, match="the token in GH_TOKEN lacks the scope"):
        gh.enable("octo/one", "--version", "v0.2.0")


def test_older_gh_without_json_status_is_parsed(gh: Any) -> None:
    gh.edit(auth={"login": "octo", "scopes": "repo, workflow", "json": False})
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    gh.edit(auth={"login": "octo", "scopes": "repo", "json": False})
    with pytest.raises(EnableUsageError, match="lacks the scope"):
        gh.enable("octo/one", "--version", "v0.2.0")
    gh.edit(auth={"state": "failure", "json": False})
    with pytest.raises(GhError, match="gh auth login"):
        gh.enable("octo/one", "--version", "v0.2.0")


def test_a_failing_repository_does_not_stop_the_others(gh: Any) -> None:
    res = gh.enable("octo/missing", "octo/one", "--version", "v0.2.0")
    assert res.code == 1
    assert "octo/missing: failed: not found, or your GitHub login cannot see it" in res.out
    assert "octo/one: enabled" in res.out
    assert "enabled 1 of 2 repositories" in res.err


def test_default_release_is_the_installed_version_and_tags_resolve(gh: Any) -> None:
    from morning_star_review._version import __version__

    state = gh.state()
    state["tags"][f"v{__version__}"] = {"type": "commit", "sha": h.SHA_020}
    state["tags"]["v0.0.9"] = {"type": "tag", "sha": h.TAG_OBJECT}
    gh.save(state)
    assert gh.enable("octo/one").code == 0
    assert gh.file("octo/one", WF) == tpl.render(__version__, h.SHA_020)
    # An annotated tag is followed to its commit.
    assert gh.enable("octo/two", "--version", "0.0.9").code == 0
    assert f"@{h.SHA_011} # v0.0.9" in (gh.file("octo/two", WF) or "")
    with pytest.raises(EnableUsageError, match=r"has no release v9\.9\.9"):
        gh.enable("octo/one", "--version", "v9.9.9")
    with pytest.raises(EnableUsageError, match="expected a release like"):
        gh.enable("octo/one", "--version", "main")


def test_client_id_from_the_cli_config_file(gh: Any, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[app]\nclient_id = "{h.CLIENT_ID}"\n')
    res = gh.run(
        "enable", "octo/one", "--version", "v0.2.0", "--app-key-file", str(gh.key_file),
        env={"MSREVIEW_CONFIG": str(config), "MSREVIEW_ENABLE_MISTRAL_API_KEY": h.MISTRAL_KEY},
    )  # fmt: skip
    assert res.code == 0, res.err
    assert f"App client id {h.CLIENT_ID} (from the config file)" in res.err


def test_without_a_terminal_missing_values_are_usage_errors(gh: Any) -> None:
    with pytest.raises(EnableUsageError, match="no App client id"):
        gh.run("enable", "octo/one", "--version", "v0.2.0")
    env = {"MSREVIEW_ENABLE_APP_CLIENT_ID": h.CLIENT_ID}
    with pytest.raises(EnableUsageError, match="MSREVIEW_ENABLE_MISTRAL_API_KEY"):
        gh.run("enable", "octo/one", "--version", "v0.2.0", env=env)
    env["MSREVIEW_ENABLE_MISTRAL_API_KEY"] = h.MISTRAL_KEY
    with pytest.raises(EnableUsageError, match="--app-key-file"):
        gh.run("enable", "octo/one", "--version", "v0.2.0", env=env)
    env["MSREVIEW_ENABLE_APP_PRIVATE_KEY"] = h.PEM
    assert gh.run("enable", "octo/one", "--version", "v0.2.0", env=env).code == 0


def test_pr_rerun_with_the_same_change_makes_no_commit(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0", "--pr").code == 0
    again = gh.enable("octo/one", "--version", "v0.2.0", "--pr")
    assert again.code == 0, again.out
    assert "already proposed in https://github.com/octo/one/pull/1" in again.out
    assert len(gh.repo("octo/one")["commits"]) == 1


def test_installation_check_failure_fails_unless_forced(gh: Any) -> None:
    gh.edit(app_api_down=True)
    res = gh.enable("octo/one", "--version", "v0.2.0", "--no-verify")
    assert res.code == 1
    assert "could not check the App installation" in res.out and "HTTP 502" in res.out
    assert gh.repo("octo/one")["secrets"] == {}
    forced = gh.enable("octo/one", "--version", "v0.2.0", "--no-verify", "--force")
    assert forced.code == 0, forced.out
    assert "the App installation was not checked" in forced.out


def test_secrets_only_warns_about_ignored_flags(gh: Any) -> None:
    res = gh.enable("octo/one", "--secrets-only", "--preset", "fast")
    assert res.code == 0
    assert "have no effect with --secrets-only" in res.err


def test_refused_app_credentials_stop_before_any_change(gh: Any) -> None:
    gh.edit(app_refused=True)
    with pytest.raises(EnableUsageError, match="refused the App credentials"):
        gh.enable("octo/one", "--version", "v0.2.0")
    assert gh.repo("octo/one")["secrets"] == {}
    assert all(api.closed for api in gh.app_apis)


def test_a_protected_default_branch_suggests_a_pull_request(gh: Any) -> None:
    state = gh.state()
    state["repos"]["octo/one"]["protected"] = ["main"]
    gh.save(state)
    res = gh.enable("octo/one", "--version", "v0.2.0")
    assert res.code == 1
    assert "(HTTP 409) (a protected branch? --pr opens a pull request)" in res.out
    assert gh.enable("octo/one", "--version", "v0.2.0", "--pr").code == 0


def test_a_different_preset_of_the_same_release_does_not_point_at_update(gh: Any) -> None:
    assert gh.enable("octo/one", "--version", "v0.2.0").code == 0
    res = gh.enable("octo/one", "--version", "v0.2.0", "--preset", "fast")
    assert res.code == 1
    assert f"failed: a different {WF} exists; --force replaces it" in res.out
    assert "+          preset: fast" in res.out


def test_missing_or_logged_out_gh_stops_before_any_change(gh: Any) -> None:
    with pytest.raises(GhError, match=r"GitHub CLI \(gh\) is not on PATH"):
        gh.enable("octo/one", "--version", "v0.2.0", env={"PATH": "/nonexistent-bin"})
    gh.edit(auth={"state": "failure"})  # gh with `auth status --json`
    with pytest.raises(GhError, match="not logged in; run `gh auth login`"):
        gh.enable("octo/one", "--version", "v0.2.0")
    assert gh.state()["repos"]["octo/one"]["secrets"] == {}
    assert all(c["argv"][:2] == ["auth", "status"] for c in gh.calls())


def test_enable_never_touches_the_repositorys_own_mistral_api_key(gh: Any) -> None:
    # A daily workflow of octo/one uses its own MISTRAL_API_KEY: enable must not change it.
    gh.put("octo/one", MISTRAL_API_KEY=h.OWN_KEY)
    note = (
        "  the existing secret MISTRAL_API_KEY is left untouched "
        "(msreview uses MSREVIEW_MISTRAL_API_KEY)"
    )
    for argv in (
        ("--version", "v0.2.0"),
        ("--secrets-only",),
        ("--version", "v0.2.0", "--pr", "--preset", "fast", "--force"),
    ):
        res = gh.enable("octo/one", "octo/two", *argv)
        assert res.code == 0, res.err + res.out
        assert res.out.splitlines().count(note) == 1  # octo/one only
        assert gh.repo("octo/one")["secrets"]["MISTRAL_API_KEY"] == h.OWN_KEY
        assert gh.repo("octo/two")["secrets"]["MSREVIEW_MISTRAL_API_KEY"] == h.MISTRAL_KEY
    assert "MISTRAL_API_KEY" not in gh.repo("octo/two")["secrets"]
    names = [c["argv"][2] for c in secret_calls(gh)]
    assert "MISTRAL_API_KEY" not in names and "MSREVIEW_MISTRAL_API_KEY" in names
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020)


def test_enable_force_moves_a_legacy_workflow_to_the_new_secret(gh: Any) -> None:
    gh.put(
        "octo/one",
        files={WF: h.legacy_workflow("v0.2.0", h.SHA_020)},
        MISTRAL_API_KEY=h.OWN_KEY,
    )
    res = gh.enable("octo/one", "--version", "v0.2.0")
    assert res.code == 1  # a different file: refused without --force, nothing touched
    assert "-          mistral-api-key: ${{ secrets.MISTRAL_API_KEY }}" in res.out
    assert gh.repo("octo/one")["secrets"] == {"MISTRAL_API_KEY": h.OWN_KEY}
    forced = gh.enable("octo/one", "--version", "v0.2.0", "--force")
    assert forced.code == 0, forced.out
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020)
    secrets = gh.repo("octo/one")["secrets"]
    assert secrets["MISTRAL_API_KEY"] == h.OWN_KEY
    assert secrets["MSREVIEW_MISTRAL_API_KEY"] == h.MISTRAL_KEY


def test_enable_force_no_secrets_warns_when_a_legacy_workflow_moves(gh: Any) -> None:
    # --no-secrets sets nothing, so the moved file needs MSREVIEW_MISTRAL_API_KEY already.
    gh.put(
        "octo/one",
        files={WF: h.legacy_workflow("v0.2.0", h.SHA_020)},
        MISTRAL_API_KEY=h.OWN_KEY,
    )
    warning = (
        "  warning: MSREVIEW_MISTRAL_API_KEY is not set here, so reviews fail until you "
        "run `msreview enable octo/one --secrets-only`"
    )
    res = gh.enable("octo/one", "--version", "v0.2.0", "--no-secrets", "--force")
    assert res.code == 0, res.err + res.out
    assert warning in res.out.splitlines()
    assert gh.file("octo/one", WF) == tpl.render("v0.2.0", h.SHA_020)
    assert secret_calls(gh) == []
    assert gh.repo("octo/one")["secrets"] == {"MISTRAL_API_KEY": h.OWN_KEY}
    # Once the new secret is set, the same move gives no warning.
    gh.put("octo/two", files={WF: h.legacy_workflow("v0.2.0", h.SHA_020)})
    assert gh.enable("octo/two", "--secrets-only").code == 0
    quiet = gh.enable("octo/two", "--version", "v0.2.0", "--no-secrets", "--force")
    assert quiet.code == 0 and "warning" not in quiet.out


def test_secrets_are_only_ever_written_under_msreview_names(gh: Any) -> None:
    from morning_star_review.enable.gh import Gh
    from morning_star_review.enable.ops import Repo

    repo = Repo(Gh(str(gh.tmp / "bin" / "gh"), gh.env), "octo/one")
    with pytest.raises(GhError, match="does not change the secret MISTRAL_API_KEY"):
        repo.set_secret("MISTRAL_API_KEY", b"x")
    with pytest.raises(GhError, match="does not change the secret MISTRAL_API_KEY"):
        repo.delete_secret("MISTRAL_API_KEY")
    assert gh.calls() == []
