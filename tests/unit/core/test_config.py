"""Repository config loading and resolution (presets, overrides, caps, trust)."""

from __future__ import annotations

from typing import Any

import pytest

from morning_star_review.core import config as cfgmod
from morning_star_review.core.config import (
    ConfigError,
    OperatorPolicy,
    RepoConfig,
    ResolvedConfig,
    RunOverrides,
    load_repo_config,
    resolve,
)

FULL_FILE = """\
version: 1
preset: balanced
models:
  explorer: zai-glm-5-3
  formatter: mistral-large-4
  verifier: mistral-large-4
reasoning_effort:
  explorer: high
  formatter: low
  verifier: medium
ensemble:
  explorers: [zai-glm-5-3, mistral-large-4]
verify:
  enabled: true
budgets:
  max_tool_calls: 40
  explorer_minutes: 12
  inline_diff_chars: 60000
  max_cost_usd: 5.00
  verifier_max_tool_calls: 20
  verifier_minutes: 6
review:
  drafts: false
  incremental: true
  inline_min_severity: high
  max_inline_comments: 10
  author_may_dismiss: false
  instructions: |
    Money is stored as integer cents; flag float arithmetic on amounts.
  path_instructions:
    - path: "migrations/**"
      instructions: "Every migration must be reversible."
exclude:
  - "docs/**"
summary:
  show_usage: false
labels: false
suggestions: false
context:
  related_definitions: false
"""


def trusted(**kw: Any) -> OperatorPolicy:
    return OperatorPolicy(trust_repo_config_models=True, **kw)


def untrusted(**kw: Any) -> OperatorPolicy:
    return OperatorPolicy(trust_repo_config_models=False, **kw)


def repo(text: str) -> RepoConfig:
    cfg, source, warnings = load_repo_config(text)
    assert source == "base_file", warnings
    assert cfg is not None
    return cfg


def explorers(cfg: ResolvedConfig) -> list[tuple[str, str]]:
    return [(r.model, r.effort) for r in cfg.roles.explorers]


# --------------------------------------------------------------------------------------
# load_repo_config
# --------------------------------------------------------------------------------------


def test_no_file_means_defaults() -> None:
    assert load_repo_config(None) == (None, "default", [])


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n"])
def test_empty_file_is_default_config(text: str) -> None:
    assert load_repo_config(text) == (RepoConfig(), "base_file", [])


def test_full_file_parses() -> None:
    cfg = repo(FULL_FILE)
    assert cfg.preset == "balanced"
    assert cfg.ensemble.explorers == ["zai-glm-5-3", "mistral-large-4"]
    assert cfg.verify.enabled is True
    assert cfg.budgets.max_cost_usd == 5.0
    assert cfg.review.path_instructions[0].path == "migrations/**"
    assert cfg.labels is False and cfg.suggestions is False
    assert cfg.context.related_definitions is False
    assert cfg.summary.show_usage is False


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("bogus: 1\n", "bogus"),
        ("review:\n  colour: red\n", "review.colour"),
        ("version: 2\n", "version"),
        ("preset: economy\n", "preset"),
        ("review:\n  max_inline_comments: 26\n", "max_inline_comments"),
        ("review:\n  inline_min_severity: blocker\n", "inline_min_severity"),
        ("reasoning_effort:\n  explorer: ultra\n", "reasoning_effort.explorer"),
        ("budgets:\n  max_cost_usd: -1\n", "max_cost_usd"),
        ("labels: maybe\n", "labels"),
        ("ensemble:\n  explorers: []\n", "ensemble.explorers"),
        ("exclude: ['']\n", "exclude"),
        (f"review:\n  instructions: '{'x' * 8193}'\n", "instructions"),
    ],
)
def test_invalid_values_fall_back_as_a_whole(text: str, needle: str) -> None:
    cfg, source, warnings = load_repo_config(text)
    assert cfg is None
    assert source == "invalid_fell_back"
    assert "ignored" in warnings[0]
    assert any(needle in w for w in warnings), warnings


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("review: [unclosed\n", "not valid YAML at line"),
        ("- a\n- b\n", "must be a mapping"),
        ("just a string\n", "must be a mapping"),
        ("base: &a {labels: true}\nother: *a\n", "aliases are not allowed"),
        ("x: !!python/object:os.system 'echo'\n", "not valid YAML"),
    ],
)
def test_unusable_files_fall_back(text: str, needle: str) -> None:
    cfg, source, warnings = load_repo_config(text)
    assert (cfg, source) == (None, "invalid_fell_back")
    assert any(needle in w for w in warnings), warnings


def test_oversized_file_falls_back() -> None:
    text = "# " + "x" * (cfgmod.REPO_CONFIG_MAX_BYTES + 1)
    cfg, source, warnings = load_repo_config(text)
    assert (cfg, source) == (None, "invalid_fell_back")
    assert "64 KiB" in warnings[0]


def test_warnings_never_echo_values() -> None:
    secret = "ghp_" + "A" * 36
    _, _, warnings = load_repo_config(f"labels: {secret}\nunknown_key: {secret}\n")
    assert warnings
    assert all(secret not in w for w in warnings)
    assert all(len(w) <= 200 for w in warnings[1:])


def test_many_errors_are_summarised() -> None:
    text = "".join(f"k{i}: 1\n" for i in range(9))
    _, _, warnings = load_repo_config(text)
    assert warnings[-1] == "... and 4 more problem(s)"


def test_yaml_booleans_and_numbers_coerce() -> None:
    cfg = repo("labels: no\nbudgets:\n  max_tool_calls: '30'\n")
    assert cfg.labels is False
    assert cfg.budgets.max_tool_calls == 30


# --------------------------------------------------------------------------------------
# resolve: presets and roles
# --------------------------------------------------------------------------------------


def test_defaults_without_file() -> None:
    cfg, warnings = resolve(None, RunOverrides(), trusted())
    assert warnings == []
    assert cfg.preset == "best"
    assert explorers(cfg) == [
        ("zai-glm-5-3", "high"),
        ("mistral-large-4", "high"),
        ("deepseek-v4-1-flash-1017", "high"),
    ]
    assert cfg.roles.verifier is not None
    assert (cfg.roles.verifier.model, cfg.roles.verifier.effort) == ("zai-glm-5-3", "high")
    assert (cfg.roles.formatter_model, cfg.roles.formatter_effort) == ("mistral-large-4", "low")
    b = cfg.budgets
    assert (b.max_cost_usd, b.max_tool_calls, b.explorer_minutes) == (10.0, 60, 15)
    assert (b.verifier_max_tool_calls, b.verifier_minutes) == (30, 8)
    assert b.max_llm_calls == 90
    assert cfg.inline_min_severity == "medium"
    assert cfg.max_inline_comments == 15
    assert cfg.author_may_dismiss is True
    assert cfg.labels and cfg.suggestions and cfg.related_definitions and cfg.show_usage
    assert cfg.incremental and not cfg.review_drafts


@pytest.mark.parametrize(
    ("preset", "expected_explorers", "verifier", "formatter"),
    [
        ("single", [("zai-glm-5-3", "high")], None, ("mistral-large-4", "low")),
        (
            "balanced",
            [("zai-glm-5-3", "high")],
            ("mistral-large-4", "high"),
            ("mistral-large-4", "low"),
        ),
        ("fast", [("zai-glm-5-3-flash", "high")], None, ("zai-glm-5-3-flash", "low")),
        (
            "mistral",
            [("mistral-large-4", "high")],
            ("mistral-large-4", "high"),
            ("mistral-large-4", "low"),
        ),
    ],
)
def test_preset_override(
    preset: str,
    expected_explorers: list[tuple[str, str]],
    verifier: tuple[str, str] | None,
    formatter: tuple[str, str],
) -> None:
    cfg, warnings = resolve(None, RunOverrides(preset=preset), trusted())  # type: ignore[arg-type]
    assert warnings == []
    assert cfg.preset == preset
    assert explorers(cfg) == expected_explorers
    got = (
        None
        if cfg.roles.verifier is None
        else (cfg.roles.verifier.model, cfg.roles.verifier.effort)
    )
    assert got == verifier
    assert (cfg.roles.formatter_model, cfg.roles.formatter_effort) == formatter
    assert cfg.roles.uses_pipeline is (verifier is not None)


def test_full_trusted_file() -> None:
    cfg, warnings = resolve(repo(FULL_FILE), RunOverrides(), trusted())
    assert warnings == []
    assert cfg.preset == "balanced"
    assert explorers(cfg) == [("zai-glm-5-3", "high"), ("mistral-large-4", "high")]
    assert cfg.roles.verifier is not None
    assert (cfg.roles.verifier.model, cfg.roles.verifier.effort) == ("mistral-large-4", "medium")
    b = cfg.budgets
    assert (b.max_tool_calls, b.explorer_minutes, b.inline_diff_chars, b.max_cost_usd) == (
        40,
        12,
        60000,
        5.0,
    )
    assert (b.verifier_max_tool_calls, b.verifier_minutes) == (20, 6)
    assert b.max_llm_calls == 60
    assert cfg.inline_min_severity == "high"
    assert cfg.max_inline_comments == 10
    assert cfg.author_may_dismiss is False
    assert cfg.instructions.startswith("Money is stored")
    assert cfg.path_instructions == [("migrations/**", "Every migration must be reversible.")]
    assert cfg.exclude == ["docs/**"]
    assert not cfg.show_usage and not cfg.labels and not cfg.suggestions
    assert not cfg.related_definitions


def test_models_explorer_replaces_the_ensemble() -> None:
    cfg, _ = resolve(repo("models:\n  explorer: mistral-large-4\n"), RunOverrides(), trusted())
    assert explorers(cfg) == [("mistral-large-4", "high")]
    assert cfg.roles.verifier is not None  # the best preset still verifies


def test_layer_precedence_for_explorers() -> None:
    file = repo("ensemble:\n  explorers: [mistral-large-4, zai-glm-5-3]\n")
    cfg, _ = resolve(file, RunOverrides(), trusted())
    assert [m for m, _ in explorers(cfg)] == ["mistral-large-4", "zai-glm-5-3"]
    cfg, _ = resolve(file, RunOverrides(explorer_model="zai-glm-5-3-flash"), trusted())
    assert explorers(cfg) == [("zai-glm-5-3-flash", "high")]
    cfg, _ = resolve(
        file,
        RunOverrides(explorer_model="zai-glm-5-3-flash", explorers=["deepseek-v4-1-flash-1017"]),
        trusted(),
    )
    assert [m for m, _ in explorers(cfg)] == ["deepseek-v4-1-flash-1017"]


def test_duplicate_explorers_are_used_once() -> None:
    cfg, warnings = resolve(
        None, RunOverrides(explorers=["zai-glm-5-3", "zai-glm-5-3", "mistral-large-4"]), trusted()
    )
    assert [m for m, _ in explorers(cfg)] == ["zai-glm-5-3", "mistral-large-4"]
    assert any("listed twice" in w for w in warnings)


def test_rejected_model_falls_back_with_warning() -> None:
    cfg, warnings = resolve(
        None, RunOverrides(preset="single", explorer_model="mistral-large-latest"), trusted()
    )
    assert explorers(cfg) == [("zai-glm-5-3", "high")]
    assert any("Mistral Large 3" in w and "mistral-large-4" in w for w in warnings)


def test_all_invalid_override_falls_back_to_preset_explorers() -> None:
    cfg, warnings = resolve(None, RunOverrides(explorer_model="acme-7b"), trusted())
    assert [m for m, _ in explorers(cfg)] == [
        "zai-glm-5-3",
        "mistral-large-4",
        "deepseek-v4-1-flash-1017",
    ]
    assert any("no usable explorer" in w for w in warnings)


def test_partially_invalid_ensemble_keeps_valid_members() -> None:
    cfg, warnings = resolve(None, RunOverrides(explorers=["acme-7b", "mistral-large-4"]), trusted())
    assert [m for m, _ in explorers(cfg)] == ["mistral-large-4"]
    assert any("acme-7b" in w for w in warnings)


def test_restricted_allow_list_shapes_every_role() -> None:
    pol = trusted(allowed_models=["mistral-large-4"])
    cfg, warnings = resolve(None, RunOverrides(), pol)
    # best preset: only the allowed explorer survives; verifier falls back to it.
    assert explorers(cfg) == [("mistral-large-4", "high")]
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"
    assert cfg.roles.formatter_model == "mistral-large-4"
    assert any("zai-glm-5-3" in w for w in warnings)


def test_no_usable_preset_model_uses_first_allowed() -> None:
    cfg, warnings = resolve(
        None, RunOverrides(preset="fast"), trusted(allowed_models=["mistral-large-4"])
    )
    assert explorers(cfg) == [("mistral-large-4", "high")]
    assert cfg.roles.formatter_model == "mistral-large-4"
    assert any("no usable explorer" in w for w in warnings)


def test_policy_without_usable_models_is_an_error() -> None:
    with pytest.raises(ConfigError):
        resolve(None, RunOverrides(), trusted(allowed_models=["acme-7b"]))


def test_unknown_model_when_allowed_gets_default_effort() -> None:
    pol = trusted(allow_unknown_models=True, allowed_models=["acme-7b", "mistral-large-4"])
    cfg, warnings = resolve(
        None, RunOverrides(preset="single", explorer_model="acme-7b", explorer_effort="high"), pol
    )
    assert explorers(cfg) == [("acme-7b", "default")]
    assert warnings == []


# --------------------------------------------------------------------------------------
# resolve: efforts and verification
# --------------------------------------------------------------------------------------


def test_effort_per_model_with_fallback() -> None:
    cfg, warnings = resolve(repo("reasoning_effort:\n  explorer: max\n"), RunOverrides(), trusted())
    # Large 4 does not accept "max": it keeps the preset value, with a warning.
    assert explorers(cfg) == [
        ("zai-glm-5-3", "max"),
        ("mistral-large-4", "high"),
        ("deepseek-v4-1-flash-1017", "max"),
    ]
    assert len(warnings) == 1
    assert "mistral-large-4" in warnings[0] and "preset value" in warnings[0]


def test_effort_override_beats_repo_and_is_validated() -> None:
    efforts = "reasoning_effort:\n  explorer: low\n  formatter: medium\n"
    cfg, warnings = resolve(
        repo("preset: mistral\n" + efforts),
        RunOverrides(explorer_effort="medium", formatter_effort="high"),
        trusted(),
    )
    assert explorers(cfg) == [("mistral-large-4", "medium")]
    assert cfg.roles.formatter_effort == "high"
    assert warnings == []
    cfg, warnings = resolve(
        repo("preset: single\n" + efforts), RunOverrides(explorer_effort="medium"), trusted()
    )
    # GLM rejects "medium" from the override; the repo's "low" is next in line.
    assert explorers(cfg) == [("zai-glm-5-3", "low")]
    assert cfg.roles.formatter_effort == "medium"
    assert any("medium" in w for w in warnings)


# Model precedence: preset < trusted repo file < explicit run preset < explicit overrides.

REPO_MODELS = """\
preset: single
models:
  explorer: mistral-large-4
  formatter: zai-glm-5-3
  verifier: mistral-large-4
reasoning_effort:
  explorer: low
verify:
  enabled: true
budgets:
  max_tool_calls: 40
"""


def test_trusted_repo_file_beats_its_preset() -> None:
    cfg, warnings = resolve(repo(REPO_MODELS), RunOverrides(), trusted())
    assert cfg.preset == "single"
    assert explorers(cfg) == [("mistral-large-4", "low")]
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"
    assert cfg.roles.formatter_model == "zai-glm-5-3"
    assert warnings == []


def test_explicit_run_preset_replaces_the_repo_file_models() -> None:
    cfg, warnings = resolve(repo(REPO_MODELS), RunOverrides(preset="best"), trusted())
    assert cfg.preset == "best"
    assert explorers(cfg) == [
        ("zai-glm-5-3", "high"),
        ("mistral-large-4", "high"),
        ("deepseek-v4-1-flash-1017", "high"),
    ]
    assert cfg.roles.verifier is not None
    assert (cfg.roles.verifier.model, cfg.roles.verifier.effort) == ("zai-glm-5-3", "high")
    assert (cfg.roles.formatter_model, cfg.roles.formatter_effort) == ("mistral-large-4", "low")
    assert cfg.budgets.max_tool_calls == 40  # budgets are not model settings: still lowered
    assert len(warnings) == 1
    assert "models, reasoning_effort, verify" in warnings[0] and "best preset" in warnings[0]


def test_explicit_run_preset_replaces_the_repo_ensemble_and_verify_switch() -> None:
    file = repo(
        "ensemble:\n  explorers: [mistral-large-4, zai-glm-5-3-flash]\nverify:\n  enabled: true\n"
    )
    cfg, warnings = resolve(file, RunOverrides(preset="fast"), trusted())
    assert explorers(cfg) == [("zai-glm-5-3-flash", "high")]
    assert cfg.roles.verifier is None  # fast has no verifier; the repo cannot add one here
    assert not cfg.roles.ensemble
    assert any("ensemble, verify" in w for w in warnings)
    off = repo("verify:\n  enabled: false\n")
    cfg, _ = resolve(off, RunOverrides(preset="balanced"), trusted())
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"


def test_run_preset_equal_to_the_repo_preset_still_replaces_repo_models() -> None:
    file = repo("preset: best\nmodels:\n  verifier: mistral-large-4\n")
    cfg, _ = resolve(file, RunOverrides(), trusted())
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"
    cfg, warnings = resolve(file, RunOverrides(preset="best"), trusted())
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "zai-glm-5-3"
    assert any("models" in w for w in warnings)


def test_explicit_model_overrides_beat_the_explicit_run_preset() -> None:
    cfg, warnings = resolve(
        repo(REPO_MODELS),
        RunOverrides(
            preset="fast",
            explorers=["zai-glm-5-3", "mistral-large-4"],
            explorer_effort="high",
            verify=True,
            verifier_model="mistral-large-4",
            formatter_model="mistral-large-4",
        ),
        trusted(),
    )
    assert explorers(cfg) == [("zai-glm-5-3", "high"), ("mistral-large-4", "high")]
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"
    assert cfg.roles.formatter_model == "mistral-large-4"
    assert cfg.preset == "fast"
    assert len(warnings) == 1  # only the note that the repo's model settings were replaced


def test_run_preset_without_repo_model_settings_adds_no_warning() -> None:
    file = repo("preset: single\nbudgets:\n  max_tool_calls: 40\nlabels: false\n")
    cfg, warnings = resolve(file, RunOverrides(preset="mistral"), trusted())
    assert warnings == []
    assert explorers(cfg) == [("mistral-large-4", "high")]
    assert cfg.budgets.max_tool_calls == 40 and not cfg.labels


def test_repo_disables_verification() -> None:
    cfg, warnings = resolve(repo("verify:\n  enabled: false\n"), RunOverrides(), trusted())
    assert cfg.roles.verifier is None
    assert cfg.roles.ensemble and cfg.roles.uses_pipeline
    assert warnings == []


def test_enabling_verification_without_preset_verifier_uses_primary_explorer() -> None:
    cfg, _ = resolve(repo("preset: fast\nverify:\n  enabled: true\n"), RunOverrides(), trusted())
    assert cfg.roles.verifier is not None
    assert (cfg.roles.verifier.model, cfg.roles.verifier.effort) == ("zai-glm-5-3-flash", "high")
    cfg, _ = resolve(
        repo("preset: single\nverify:\n  enabled: true\nmodels:\n  verifier: mistral-large-4\n"),
        RunOverrides(),
        trusted(),
    )
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "mistral-large-4"


def test_verify_override_wins_and_named_but_disabled_warns() -> None:
    file = repo("verify:\n  enabled: true\n")
    cfg, _ = resolve(file, RunOverrides(verify=False), trusted())
    assert cfg.roles.verifier is None
    cfg, warnings = resolve(
        None, RunOverrides(preset="single", verifier_model="mistral-large-4"), trusted()
    )
    assert cfg.roles.verifier is None
    assert any("verification is off" in w for w in warnings)
    cfg, _ = resolve(
        None,
        RunOverrides(
            preset="single", verify=True, verifier_model="mistral-large-4", verifier_effort="medium"
        ),
        trusted(),
    )
    assert cfg.roles.verifier is not None
    assert (cfg.roles.verifier.model, cfg.roles.verifier.effort) == ("mistral-large-4", "medium")


def test_invalid_verifier_model_falls_back_to_preset() -> None:
    cfg, warnings = resolve(None, RunOverrides(verifier_model="mistral-large-latest"), trusted())
    assert cfg.roles.verifier is not None and cfg.roles.verifier.model == "zai-glm-5-3"
    assert any("verifier model" in w for w in warnings)


def test_invalid_formatter_model_falls_back() -> None:
    cfg, warnings = resolve(None, RunOverrides(formatter_model="nope"), trusted())
    assert cfg.roles.formatter_model == "mistral-large-4"
    assert any("formatter model" in w for w in warnings)


# --------------------------------------------------------------------------------------
# resolve: budgets
# --------------------------------------------------------------------------------------


def test_repo_can_only_lower_budgets() -> None:
    file = repo("budgets:\n  max_cost_usd: 50\n  max_tool_calls: 100\n  explorer_minutes: 5\n")
    cfg, warnings = resolve(
        file, RunOverrides(), trusted(max_cost_usd_cap=80, max_tool_calls_cap=150)
    )
    # Raising is refused even though the operator caps would allow it.
    assert cfg.budgets.max_cost_usd == 10.0
    assert cfg.budgets.max_tool_calls == 60
    assert cfg.budgets.explorer_minutes == 5
    assert sum("can only lower" in w for w in warnings) == 2


def test_inline_diff_chars_can_only_be_lowered() -> None:
    cfg, warnings = resolve(
        repo("budgets:\n  inline_diff_chars: 500000\n"), RunOverrides(), trusted()
    )
    assert cfg.budgets.inline_diff_chars == 80_000
    assert warnings
    cfg, _ = resolve(repo("budgets:\n  inline_diff_chars: 0\n"), RunOverrides(), trusted())
    assert cfg.budgets.inline_diff_chars == 0


def test_overrides_raise_up_to_operator_cap() -> None:
    pol = trusted(max_cost_usd_cap=25, max_tool_calls_cap=120, explorer_minutes_cap=20)
    cfg, warnings = resolve(
        None, RunOverrides(max_cost_usd=20, max_tool_calls=100, explorer_minutes=20), pol
    )
    assert (cfg.budgets.max_cost_usd, cfg.budgets.max_tool_calls) == (20, 100)
    assert cfg.budgets.explorer_minutes == 20
    assert cfg.budgets.max_llm_calls == 150
    assert warnings == []


def test_overrides_above_cap_are_clipped_with_warning() -> None:
    cfg, warnings = resolve(
        None,
        RunOverrides(max_cost_usd=50, verifier_minutes=20, verifier_max_tool_calls=90),
        trusted(),
    )
    assert cfg.budgets.max_cost_usd == 10.0
    assert cfg.budgets.verifier_minutes == 8
    assert cfg.budgets.verifier_max_tool_calls == 30
    assert len([w for w in warnings if "operator cap" in w]) == 3


def test_low_caps_clip_defaults_silently() -> None:
    pol = trusted(
        max_cost_usd_cap=2, max_tool_calls_cap=10, explorer_minutes_cap=3, verifier_minutes_cap=2
    )
    cfg, warnings = resolve(None, RunOverrides(), pol)
    b = cfg.budgets
    assert (b.max_cost_usd, b.max_tool_calls, b.explorer_minutes, b.verifier_minutes) == (
        2,
        10,
        3,
        2,
    )
    assert b.max_llm_calls == 15
    assert warnings == []


# --------------------------------------------------------------------------------------
# resolve: trust (CLI on a foreign repository) and other settings
# --------------------------------------------------------------------------------------


def test_untrusted_repo_cannot_choose_models_or_spend() -> None:
    cfg, warnings = resolve(repo(FULL_FILE), RunOverrides(), untrusted())
    default, _ = resolve(None, RunOverrides(), untrusted())
    assert cfg.roles == default.roles
    assert cfg.budgets == default.budgets
    assert cfg.preset == "best"
    assert len(warnings) == 1
    for name in ("preset", "models", "reasoning_effort", "ensemble", "verify", "budgets"):
        assert name in warnings[0]
    # Settings that cost nothing still apply.
    assert cfg.instructions.startswith("Money is stored")
    assert cfg.exclude == ["docs/**"]
    assert cfg.inline_min_severity == "high"
    assert not cfg.labels and not cfg.suggestions and not cfg.related_definitions


def test_untrusted_repo_without_spend_settings_has_no_warning() -> None:
    cfg, warnings = resolve(repo("review:\n  instructions: hi\n"), RunOverrides(), untrusted())
    assert warnings == []
    assert cfg.instructions == "hi"


def test_untrusted_repo_overrides_still_apply() -> None:
    cfg, _ = resolve(repo("preset: fast\n"), RunOverrides(preset="mistral"), untrusted())
    assert explorers(cfg) == [("mistral-large-4", "high")]


def test_operator_and_repo_switches_combine() -> None:
    cfg, _ = resolve(None, RunOverrides(), trusted(labels=False, show_usage=False))
    assert not cfg.labels and not cfg.show_usage
    cfg, _ = resolve(repo("review:\n  drafts: true\n"), RunOverrides(), trusted())
    assert cfg.review_drafts
    cfg, _ = resolve(None, RunOverrides(), trusted(review_drafts=True))
    assert cfg.review_drafts


def test_inline_min_severity_override() -> None:
    cfg, _ = resolve(
        repo("review:\n  inline_min_severity: low\n"),
        RunOverrides(inline_min_severity="critical"),
        trusted(),
    )
    assert cfg.inline_min_severity == "critical"


def test_config_hash_is_stable_and_ignores_budgets() -> None:
    a, _ = resolve(repo(FULL_FILE), RunOverrides(), trusted())
    b, _ = resolve(repo(FULL_FILE), RunOverrides(), trusted())
    assert a.config_hash == b.config_hash
    cheaper, _ = resolve(repo(FULL_FILE), RunOverrides(max_cost_usd=1.0), trusted())
    assert cheaper.config_hash == a.config_hash
    other_model, _ = resolve(
        repo(FULL_FILE), RunOverrides(explorer_model="zai-glm-5-3-flash"), trusted()
    )
    assert other_model.config_hash != a.config_hash
    assert ResolvedConfig.model_validate_json(a.model_dump_json()) == a


def test_resolve_reexports() -> None:
    for name in (
        "OperatorPolicy",
        "RunOverrides",
        "ResolvedConfig",
        "ResolvedRoles",
        "ResolvedBudgets",
    ):
        assert name in cfgmod.__all__
        assert getattr(cfgmod, name) is not None
    assert cfgmod.REPO_CONFIG_FILE == ".morning-star-review.yml"


@pytest.mark.parametrize(
    "text",
    ["[" * 20000 + "]" * 20000, "{a: " * 5000 + "1" + "}" * 5000, "- " * 10000 + "x"],
)
def test_deeply_nested_yaml_falls_back_instead_of_raising(text: str) -> None:
    cfg, source, warnings = load_repo_config(text)
    assert cfg is None and source == "invalid_fell_back"
    assert warnings and "nested too deeply" in warnings[0]


def test_unknown_keys_are_not_echoed_verbatim_in_warnings() -> None:
    _, source, warnings = load_repo_config('"@everyone [x](//evil.example)": 1\n')
    assert source == "invalid_fell_back"
    joined = "\n".join(warnings)
    assert "@" not in joined and "//evil" not in joined and "[" not in joined
