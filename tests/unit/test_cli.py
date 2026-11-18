"""``msreview`` argument parsing, the ``ci`` environment contract and command wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from morning_star_review import cli, driver
from morning_star_review.core.contract import ReviewOutcome
from morning_star_review.github.mint_child import MintResult

PR = "https://github.com/octo-org/widgets/pull/7"
KEY = "mk-test-not-a-real-key-0123456789"
TOKEN = "ghs_clitesttoken0123456789abcdef"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip().startswith("msreview ")


def test_hidden_mint_routes_to_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr("morning_star_review.github.mint_child.main", fake)
    assert cli.main(["_mint", "--repo", "o/r"]) == 0
    assert seen == [["--repo", "o/r"]]
    assert "_mint" not in cli.build_parser().format_help()


def test_no_command_prints_help() -> None:
    assert cli.main([]) == 2


def test_overrides_and_policy_caps() -> None:
    ov = cli.build_overrides(
        preset="best", explorers=["zai-glm-5-3", "mistral-large-4"], max_cost_usd=25.0, verify=False
    )
    assert ov.preset == "best" and ov.verify is False
    pol = cli.build_policy(trust_repo_config=True, overrides=ov)
    assert pol.max_cost_usd_cap == 25.0  # an explicit budget raises the cap
    assert pol.max_tool_calls_cap == 60  # untouched caps keep their defaults
    low = cli.build_policy(trust_repo_config=False, overrides=cli.build_overrides(max_cost_usd=2.0))
    assert low.max_cost_usd_cap == 10.0
    with pytest.raises(cli.UsageError):
        cli.build_overrides(explorer_effort="extreme")
    with pytest.raises(cli.UsageError):
        cli.build_overrides(preset="cheapest")


def test_policy_switches() -> None:
    pol = cli.build_policy(
        trust_repo_config=False,
        overrides=cli.build_overrides(),
        labels=False,
        suggestions=False,
        allowed_models=["zai-glm-5-3"],
    )
    assert (pol.labels, pol.suggestions, pol.allowed_models) == (False, False, ["zai-glm-5-3"])


def test_policy_carries_the_operator_default_preset() -> None:
    ov = cli.build_overrides()
    pol = cli.build_policy(trust_repo_config=True, overrides=ov, default_preset="fast")
    assert pol.default_preset == "fast" and ov.preset is None
    assert cli.build_policy(trust_repo_config=True, overrides=ov).default_preset is None
    with pytest.raises(cli.UsageError, match=r"defaults\.preset"):
        cli.build_policy(trust_repo_config=True, overrides=ov, default_preset="cheapest")


def ci_env(tmp_path: Path, **kw: str) -> dict[str, str]:
    agent = tmp_path / "agent-python"
    agent.write_text("#!/bin/sh\n")
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "MISTRAL_API_KEY": KEY,
        "MSREVIEW_PR_URL": PR,
        "MSREVIEW_HEAD_SHA": "a" * 40,
        "MSREVIEW_RUN_LABEL": "gha:octo-org/widgets/1/1",
        "MSREVIEW_POST": "auto",
        "MSREVIEW_GITHUB_TOKEN": TOKEN,
        "MSREVIEW_APP_SLUG": "morning-star-code-reviews",
        "MSREVIEW_TOKEN_STEP": "success",
        "MSREVIEW_READ_TOKEN": "ghs_readonlytoken0123456789abcdef",
        "MSREVIEW_AGENT_PYTHON": str(agent),
        "MSREVIEW_PRESET": "",
        "MSREVIEW_EXPLORERS": "zai-glm-5-3, mistral-large-4",
        "MSREVIEW_VERIFY": "true",
        "MSREVIEW_VERIFIER_MODEL": "mistral-large-4",
        "MSREVIEW_VERIFIER_EFFORT": "high",
        "MSREVIEW_MAX_COST_USD": "",
        "MSREVIEW_LABELS": "true",
        "MSREVIEW_SUGGESTIONS": "false",
        "MSREVIEW_FAIL_ON": "high",
        "MSREVIEW_DEADLINE_MINUTES": "40",
        "MSREVIEW_REPORT_DIR": str(tmp_path / "report"),
        "MSREVIEW_RUN_ROOT": str(tmp_path / "runs"),
        "MSREVIEW_REMOVE_LABEL": "msreview: safe-to-review",
    }
    env.update(kw)
    return env


def test_ci_settings_parse(tmp_path: Path) -> None:
    s = cli._ci_settings(ci_env(tmp_path))
    assert s["deadline_s"] == 2400
    assert s["overrides"]["explorers"] == ["zai-glm-5-3", "mistral-large-4"]
    assert s["overrides"]["verify"] is True
    assert s["overrides"]["preset"] is None
    assert s["overrides"]["max_cost_usd"] is None
    assert s["labels"] is True and s["suggestions"] is False
    for bad in ({"MSREVIEW_POST": "maybe"}, {"MSREVIEW_FAIL_ON": "low"}, {"MSREVIEW_PR_URL": ""}):
        with pytest.raises(cli.UsageError):
            cli._ci_settings(ci_env(tmp_path, **bad))
    with pytest.raises(cli.UsageError):
        cli._ci_settings(ci_env(tmp_path, MSREVIEW_MAX_TOOL_CALLS="many"))
    with pytest.raises(cli.UsageError):
        cli._ci_settings(ci_env(tmp_path, MSREVIEW_VERIFY="perhaps"))


def test_ci_secrets_pop_tokens(tmp_path: Path) -> None:
    env = ci_env(tmp_path)
    sec = cli.ci_secrets(env, KEY, "auto", "morning-star-code-reviews", "success")
    assert sec.github_token == TOKEN
    assert sec.bot_login == "morning-star-code-reviews[bot]"
    assert "MSREVIEW_GITHUB_TOKEN" not in env and "MSREVIEW_READ_TOKEN" not in env
    assert TOKEN not in repr(sec) and KEY not in repr(sec)


def test_ci_secrets_failed_token_step(tmp_path: Path) -> None:
    env = ci_env(tmp_path, MSREVIEW_GITHUB_TOKEN="")
    sec = cli.ci_secrets(env, KEY, "auto", None, "failure")
    assert sec.github_token is None and sec.bot_login is None
    assert sec.cannot_post_reason is not None and "(failure)" in sec.cannot_post_reason
    never = cli.ci_secrets(ci_env(tmp_path), KEY, "never", "slug", "skipped")
    assert never.github_token is None and never.cannot_post_reason is None


def test_mint_reasons() -> None:
    assert cli.mint_reason(MintResult(status="ok", token="t", slug="s"), "o", "r") is None
    text = cli.mint_reason(MintResult(status="not_installed", slug="app"), "o", "r")
    assert text is not None and "'app'" in text and "o/r" in text
    assert "no GitHub App credentials" in (
        cli.mint_reason(MintResult("no_credentials"), "o", "r") or ""
    )
    assert "Pull requests: write" in (cli.mint_reason(MintResult("insufficient"), "o", "r") or "")
    err = cli.mint_reason(MintResult(status="error", reason="boom"), "o", "r")
    assert err == "Not posted: minting a GitHub App token failed (boom)."


def test_read_token_modes() -> None:
    assert cli.read_token_from("none", {"GITHUB_TOKEN": "x"}) is None
    assert cli.read_token_from("env", {"GH_TOKEN": "y"}) == "y"
    assert cli.read_token_from("env", {"GITHUB_TOKEN": "x", "GH_TOKEN": "y"}) == "x"
    with pytest.raises(cli.UsageError):
        cli.read_token_from("gh", {"PATH": "/nonexistent"})


def test_agent_python_resolution(tmp_path: Path) -> None:
    with pytest.raises(cli.UsageError, match="msreview setup"):
        cli.resolve_agent_python(None, {"HOME": str(tmp_path)}, None)
    with pytest.raises(cli.UsageError, match="does not exist"):
        cli.resolve_agent_python(str(tmp_path / "nope"), {}, None)
    py = tmp_path / "py"
    py.write_text("")
    assert cli.resolve_agent_python(None, {"MSREVIEW_AGENT_PYTHON": str(py)}, None) == str(py)


def _fake_run_review(captured: dict[str, Any], status: str = "posted") -> Any:
    async def fake(cfg: driver.DriverConfig, sec: driver.DriverSecrets) -> Any:
        captured["cfg"], captured["sec"] = cfg, sec
        out = ReviewOutcome(status=status, pr_url=PR, execution_id="e", verdict="clean")  # type: ignore[arg-type]
        return out, 0

    return fake


def test_cmd_ci_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    removed: list[tuple[str, str, str]] = []

    async def fake_remove(token: str, pr_url: str, name: str) -> str:
        removed.append((token, pr_url, name))
        return "removed"

    monkeypatch.setattr(driver, "run_review", _fake_run_review(captured))
    monkeypatch.setattr(cli, "remove_pr_label", fake_remove)
    env = ci_env(tmp_path)
    assert cli.cmd_ci(env) == 0
    cfg, sec = captured["cfg"], captured["sec"]
    assert cfg.mode == "ci" and cfg.deadline_s == 2400 and cfg.fail_on == "high"
    assert cfg.expected_head_sha == "a" * 40
    assert cfg.policy.trust_repo_config_models is True
    assert cfg.policy.suggestions is False
    assert cfg.overrides.verifier_model == "mistral-large-4"
    assert cfg.out_dir == (tmp_path / "report").resolve()
    assert sec.github_token == TOKEN and sec.mistral_api_key == KEY
    assert removed == [(TOKEN, PR, "msreview: safe-to-review")]
    assert "MSREVIEW_GITHUB_TOKEN" not in env


def test_cmd_ci_removes_the_label_when_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[str] = []

    async def failing_run(cfg: driver.DriverConfig, sec: driver.DriverSecrets) -> Any:
        raise driver.DriverError("preflight refused")

    async def fake_remove(token: str, pr_url: str, name: str) -> str:
        removed.append(name)
        return "removed"

    monkeypatch.setattr(driver, "run_review", failing_run)
    monkeypatch.setattr(cli, "remove_pr_label", fake_remove)
    with pytest.raises(driver.DriverError):
        cli.cmd_ci(ci_env(tmp_path))
    assert removed == ["msreview: safe-to-review"]


def test_cmd_ci_usage_error_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output, summary = tmp_path / "gh-output", tmp_path / "gh-summary"
    env = ci_env(
        tmp_path,
        MSREVIEW_PR_URL="",
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(summary),
    )
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["ci"]) == 2
    # A usage error before the run still tells Actions why, once.
    assert "status=failed" in output.read_text(encoding="utf-8")
    assert "MSREVIEW_PR_URL is empty" in summary.read_text(encoding="utf-8")
    assert capsys.readouterr().out.count("::error title=Morning Star Code Review::") == 1


def test_cmd_ci_empty_key_names_the_action_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Live: an empty `mistral-api-key` input gave the CLI's remedies (variables, config
    # file), which do not apply to the Action.
    output, summary = tmp_path / "gh-output", tmp_path / "gh-summary"
    env = ci_env(
        tmp_path, MISTRAL_API_KEY="  ", GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary)
    )
    monkeypatch.delenv("MSREVIEW_MISTRAL_API_KEY_COMMAND", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["ci"]) == 2
    out = capsys.readouterr().out
    assert out.count("::error title=Morning Star Code Review::") == 1
    assert "the Action input `mistral-api-key` is empty" in out
    assert "MISTRAL_API_KEY secret is missing" in out
    assert "api_key_command" not in out
    text = summary.read_text(encoding="utf-8")
    assert "`mistral-api-key` is empty" in text and "status=failed" in output.read_text()


def test_cli_empty_key_keeps_the_cli_remedies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "")
    monkeypatch.delenv("MSREVIEW_MISTRAL_API_KEY_COMMAND", raising=False)
    agent = tmp_path / "py"
    agent.write_text("")
    monkeypatch.setenv("MSREVIEW_AGENT_PYTHON", str(agent))
    assert cli.main(["review", "octo-org/widgets#7", "--post", "never"]) == 2
    err = capsys.readouterr().err
    assert "set MISTRAL_API_KEY, MSREVIEW_MISTRAL_API_KEY_COMMAND" in err
    assert "mistral-api-key" not in err


def test_cmd_ci_reports_a_driver_error_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(cfg: driver.DriverConfig, sec: driver.DriverSecrets) -> Any:
        exc = driver.DriverError("preflight refused")
        driver.report_ci_usage_failure(exc, stop_commands=cfg.stop_commands, env={})
        raise exc

    async def fake_remove(token: str, pr_url: str, name: str) -> str:
        return "removed"

    monkeypatch.setattr(driver, "run_review", failing_run)
    monkeypatch.setattr(cli, "remove_pr_label", fake_remove)
    with pytest.raises(driver.DriverError):
        cli.cmd_ci(ci_env(tmp_path))
    assert capsys.readouterr().out.count("::error title=Morning Star Code Review::") == 1


def test_cmd_review_json_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    async def fake_mint(repo: str, **kw: Any) -> MintResult:
        print("noise that must not reach stdout")
        return MintResult(status="ok", token=TOKEN, slug="morning-star-code-reviews")

    monkeypatch.setattr(driver, "run_review", _fake_run_review(captured))
    monkeypatch.setattr("morning_star_review.github.mint_child.mint_via_child", fake_mint)
    agent = tmp_path / "py"
    agent.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", KEY)
    monkeypatch.delenv("MSREVIEW_MISTRAL_API_KEY_COMMAND", raising=False)
    monkeypatch.setenv("MSREVIEW_AGENT_PYTHON", str(agent))
    code = cli.main(
        [
            "review",
            "octo-org/widgets#7",
            "--json",
            "--preset",
            "balanced",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert code == 0
    out = capfd.readouterr().out
    data = json.loads(out)
    assert data["status"] == "posted"
    # Why the verdict is what it is, and how many unconfirmed possible issues were listed.
    assert data["verdict_reasons"] == [] and data["possible_issues"] == 0
    assert out.count("\n") == 1
    cfg, sec = captured["cfg"], captured["sec"]
    assert cfg.mode == "cli" and cfg.pr_url == PR and cfg.overrides.preset == "balanced"
    assert cfg.policy.trust_repo_config_models is False
    assert sec.bot_login == "morning-star-code-reviews[bot]"


def test_cmd_review_post_never_skips_minting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    async def no_mint(repo: str, **kw: Any) -> MintResult:
        raise AssertionError("must not mint")

    monkeypatch.setattr(driver, "run_review", _fake_run_review(captured, "report"))
    monkeypatch.setattr("morning_star_review.github.mint_child.mint_via_child", no_mint)
    agent = tmp_path / "py"
    agent.write_text("")
    monkeypatch.setenv("MISTRAL_API_KEY", KEY)
    monkeypatch.delenv("MSREVIEW_MISTRAL_API_KEY_COMMAND", raising=False)
    monkeypatch.setenv("MSREVIEW_AGENT_PYTHON", str(agent))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["review", PR, "--post", "never", "--local", "--read-token", "none"]) == 0
    assert captured["cfg"].mode == "local"
    assert captured["sec"].github_token is None and captured["sec"].read_token is None
    assert "report" in capfd.readouterr().out


def _review_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str) -> None:
    agent = tmp_path / "py"
    agent.write_text("")
    conf = tmp_path / "config.toml"
    conf.write_text(config)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MSREVIEW_CONFIG", str(conf))
    monkeypatch.setenv("MISTRAL_API_KEY", KEY)
    monkeypatch.delenv("MSREVIEW_MISTRAL_API_KEY_COMMAND", raising=False)
    monkeypatch.setenv("MSREVIEW_AGENT_PYTHON", str(agent))


@pytest.mark.parametrize(
    ("flags", "explicit"),
    [([], None), (["--preset", "balanced"], "balanced")],
)
def test_cmd_review_user_config_preset_is_an_operator_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    flags: list[str],
    explicit: str | None,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(driver, "run_review", _fake_run_review(captured, "report"))
    _review_env(tmp_path, monkeypatch, '[defaults]\npreset = "fast"\n')
    argv = ["review", PR, "--post", "never", "--read-token", "none", "--trust-repo-config"]
    assert cli.main([*argv, *flags]) == 0
    cfg = captured["cfg"]
    # The config file's preset is a default below a trusted repository file; only
    # --preset is an explicit run preset (which replaces the repository's model settings).
    assert cfg.overrides.preset == explicit
    assert cfg.policy.default_preset == "fast"
    assert cfg.policy.trust_repo_config_models is True
    capfd.readouterr()


def test_cmd_review_rejects_an_unknown_user_config_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(driver, "run_review", _fake_run_review({}, "report"))
    _review_env(tmp_path, monkeypatch, '[defaults]\npreset = "cheapest"\n')
    assert cli.main(["review", PR, "--post", "never", "--read-token", "none"]) == 2
    assert "defaults.preset" in capfd.readouterr().err


def test_cmd_review_refuses_a_symlinked_report_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # Security review SEC-CI-3: a PR checkout can ship ./msreview-reports as a link.
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "msreview-reports").symlink_to(target)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(driver, "run_review", _fake_run_review({}))
    monkeypatch.setenv("HOME", str(tmp_path))
    code = cli.main(["review", "octo-org/widgets#7", "--post", "never"])
    assert code != 0
    assert "symbolic link" in capfd.readouterr().err
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("minutes", ["56", "90", "-5"])
def test_ci_deadline_stays_within_the_app_token_lifetime(tmp_path: Path, minutes: str) -> None:
    # Security review SEC-CI-7: the installation token is minted once and lasts 60 minutes.
    with pytest.raises(cli.UsageError, match="between 1 and 55"):
        cli._ci_settings(ci_env(tmp_path, MSREVIEW_DEADLINE_MINUTES=minutes))
    assert cli._ci_settings(ci_env(tmp_path, MSREVIEW_DEADLINE_MINUTES="55"))["deadline_s"] == 3300


def test_cli_deadline_stays_within_the_app_token_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(driver, "run_review", _fake_run_review({}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", KEY)
    agent = tmp_path / "py"
    agent.write_text("")
    monkeypatch.setenv("MSREVIEW_AGENT_PYTHON", str(agent))
    code = cli.main(["review", "octo-org/widgets#7", "--deadline-minutes", "90"])
    assert code != 0 and "between 1 and 55" in capfd.readouterr().err
