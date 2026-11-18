"""Explicit tool-call budgets replace the preset and per-model defaults (docs/models.md).

Each case goes the way a real run goes: CLI flags (or Action inputs) are parsed into run
overrides and the operator policy, ``config.resolve`` merges them with the repository
file, and the activities' own helpers compute the agent budget that ends up in the job.
"""

from __future__ import annotations

from typing import Any

import pytest

from morning_star_review import cli
from morning_star_review.activities.explore import explorer_tool_calls
from morning_star_review.activities.verify import verifier_tool_calls
from morning_star_review.core.config import RepoConfig, load_repo_config, resolve
from morning_star_review.core.contract import ResolvedConfig


def _cli_config(*flags: str, repo_file: str | None = None) -> tuple[ResolvedConfig, list[str]]:
    """The resolved config of ``msreview review <url> <flags>`` (no user config file)."""
    args = cli.build_parser().parse_args(
        ["review", "https://github.com/o/r/pull/1", "--post", "never", *flags]
    )
    overrides = cli.build_overrides(
        preset=args.preset,
        explorers=cli._csv(args.explorers),
        explorer_model=cli._opt_str(args.explorer_model),
        verify=args.verify,
        verifier_model=cli._opt_str(args.verifier_model),
        max_tool_calls=args.max_tool_calls,
        verifier_max_tool_calls=args.verifier_max_tool_calls,
    )
    policy = cli.build_policy(trust_repo_config=args.trust_repo_config, overrides=overrides)
    repo: RepoConfig | None = None
    if repo_file is not None:
        repo, _source, _warnings = load_repo_config(repo_file)
    return resolve(repo, overrides, policy)


def _explorer_calls(cfg: ResolvedConfig) -> dict[str, int]:
    return {
        r.model: explorer_tool_calls(cfg.budgets, r.model, cfg.preset) for r in cfg.roles.explorers
    }


def test_best_preset_with_explicit_100_gives_every_explorer_100() -> None:
    cfg, warnings = _cli_config("--preset", "best", "--max-tool-calls", "100")
    assert cfg.budgets.explicit == ("max_tool_calls",)
    assert cfg.budgets.max_tool_calls == 100  # the flag raised the operator cap
    assert set(_explorer_calls(cfg).values()) == {100}
    assert len(_explorer_calls(cfg)) == 3
    assert not [w for w in warnings if "cap" in w]


def test_fast_preset_with_explicit_60_gives_60() -> None:
    cfg, _ = _cli_config("--preset", "fast", "--max-tool-calls", "60")
    assert _explorer_calls(cfg) == {"zai-glm-5-3-flash": 60}


def test_single_flash_explorer_with_explicit_80_gives_80() -> None:
    cfg, _ = _cli_config(
        "--preset", "single", "--explorer-model", "deepseek-v4-1-flash-1017",
        "--max-tool-calls", "80",
    )  # fmt: skip
    assert _explorer_calls(cfg) == {"deepseek-v4-1-flash-1017": 80}


def test_explicit_budget_above_the_hard_maximum_is_refused() -> None:
    with pytest.raises(cli.UsageError):
        _cli_config("--preset", "best", "--max-tool-calls", "201")


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (("--preset", "best"), {"zai-glm-5-3": 60, "mistral-large-4": 60,
                                "deepseek-v4-1-flash-1017": 60}),
        (("--preset", "fast"), {"zai-glm-5-3-flash": 25}),
        (("--preset", "single"), {"zai-glm-5-3": 60}),
        (("--preset", "single", "--explorer-model", "deepseek-v4-1-flash-1017"),
         {"deepseek-v4-1-flash-1017": 25}),
    ],
)  # fmt: skip
def test_defaults_apply_when_nothing_is_explicit(
    flags: tuple[str, ...], expected: dict[str, int]
) -> None:
    cfg, _ = _cli_config(*flags)
    assert cfg.budgets.explicit == ()
    assert _explorer_calls(cfg) == expected


def test_flash_verifier_default_and_explicit() -> None:
    flags = ("--preset", "balanced", "--verifier-model", "zai-glm-5-3-flash")
    cfg, _ = _cli_config(*flags)
    assert verifier_tool_calls(cfg.budgets, "zai-glm-5-3-flash") == 25
    cfg, _ = _cli_config(*flags, "--verifier-max-tool-calls", "30")
    assert cfg.budgets.explicit == ("verifier_max_tool_calls",)
    assert verifier_tool_calls(cfg.budgets, "zai-glm-5-3-flash") == 30
    # The explorer budget stays implicit when only the verifier's is set.
    assert _explorer_calls(cfg) == {"zai-glm-5-3": 60}


def test_trusted_repository_budget_is_explicit() -> None:
    text = "preset: fast\nbudgets:\n  max_tool_calls: 40\n"
    cfg, _ = _cli_config("--trust-repo-config", repo_file=text)
    assert cfg.budgets.explicit == ("max_tool_calls",)
    assert _explorer_calls(cfg) == {"zai-glm-5-3-flash": 40}


def test_untrusted_repository_budget_is_not_explicit() -> None:
    text = "budgets:\n  max_tool_calls: 40\n"
    cfg, warnings = _cli_config("--preset", "fast", repo_file=text)
    assert cfg.budgets.explicit == ()
    assert _explorer_calls(cfg) == {"zai-glm-5-3-flash": 25}
    assert any("not trusted" in w for w in warnings)


def test_repository_value_above_the_default_is_ignored_and_not_explicit() -> None:
    text = "preset: fast\nbudgets:\n  max_tool_calls: 90\n"
    cfg, warnings = _cli_config("--trust-repo-config", repo_file=text)
    assert cfg.budgets.explicit == ()
    assert _explorer_calls(cfg) == {"zai-glm-5-3-flash": 25}
    assert any("can only lower" in w for w in warnings)


def test_action_input_is_explicit() -> None:
    env: dict[str, Any] = {
        "MSREVIEW_PR_URL": "https://github.com/o/r/pull/1",
        "MSREVIEW_PRESET": "fast",
        "MSREVIEW_MAX_TOOL_CALLS": "60",
    }
    settings = cli._ci_settings(env)
    overrides = cli.build_overrides(**settings["overrides"])
    policy = cli.build_policy(trust_repo_config=True, overrides=overrides)
    cfg, _ = resolve(None, overrides, policy)
    assert cfg.budgets.explicit == ("max_tool_calls",)
    assert _explorer_calls(cfg) == {"zai-glm-5-3-flash": 60}


def test_explicit_budgets_do_not_change_the_config_hash() -> None:
    plain, _ = _cli_config("--preset", "fast")
    raised, _ = _cli_config("--preset", "fast", "--max-tool-calls", "60")
    assert plain.config_hash == raised.config_hash
