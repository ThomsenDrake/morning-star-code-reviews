"""Model table, presets, model/effort validation and cost estimation."""

from __future__ import annotations

import pytest

from morning_star_review.core import models as m
from morning_star_review.core.contract import DEFAULT_ALLOWED_MODELS, OperatorPolicy, Usage

ALL_EFFORTS = ["default", "low", "medium", "high", "max"]

# DESIGN 6.1: efforts the API accepts besides "default".
EXPECTED_EFFORTS = {
    "zai-glm-5-3": {"low", "high", "max"},
    "mistral-large-4": {"low", "medium", "high"},
    "zai-glm-5-3-flash": {"low", "high", "max"},
    "deepseek-v4-1-flash-1017": {"low", "high", "max"},
}

# ADDENDUM D, verbatim: (explorers, verifier, formatter), each (model, effort).
EXPECTED_PRESETS = {
    "best": (
        [
            ("zai-glm-5-3", "high"),
            ("mistral-large-4", "high"),
            ("deepseek-v4-1-flash-1017", "high"),
        ],
        ("zai-glm-5-3", "high"),
        ("mistral-large-4", "low"),
    ),
    "balanced": (
        [("zai-glm-5-3", "high")],
        ("mistral-large-4", "high"),
        ("mistral-large-4", "low"),
    ),
    "single": ([("zai-glm-5-3", "high")], None, ("mistral-large-4", "low")),
    "fast": ([("zai-glm-5-3-flash", "high")], None, ("zai-glm-5-3-flash", "low")),
    "mistral": (
        [("mistral-large-4", "high")],
        ("mistral-large-4", "high"),
        ("mistral-large-4", "low"),
    ),
}


def policy(**kw: object) -> OperatorPolicy:
    return OperatorPolicy(trust_repo_config_models=True, **kw)  # type: ignore[arg-type]


def test_model_table_matches_design() -> None:
    assert set(m.MODEL_TABLE) == set(EXPECTED_EFFORTS) == set(DEFAULT_ALLOWED_MODELS)
    for model, efforts in EXPECTED_EFFORTS.items():
        assert m.MODEL_TABLE[model].efforts == frozenset(efforts)
    assert m.price("zai-glm-5-3") == (1.40, 4.40)
    assert m.price("mistral-large-4") == (0.70, 2.10)
    assert m.price("zai-glm-5-3-flash") == (0.15, 0.50)
    assert m.price("deepseek-v4-1-flash-1017") == (0.30, 1.20)
    assert m.price("something-else") == m.UNKNOWN_MODEL_PRICE == (2.00, 6.00)
    assert all(info.lifecycle for info in m.MODEL_TABLE.values())


@pytest.mark.parametrize("name", list(EXPECTED_PRESETS))
def test_presets_match_addendum(name: str) -> None:
    explorers, verifier, formatter = EXPECTED_PRESETS[name]
    preset = m.PRESETS[name]  # type: ignore[index]
    assert preset.name == name
    assert [(r.model, r.effort) for r in preset.explorers] == explorers
    if verifier is None:
        assert preset.verifier is None
    else:
        assert preset.verifier is not None
        assert (preset.verifier.model, preset.verifier.effort) == verifier
    assert (preset.formatter.model, preset.formatter.effort) == formatter
    assert preset.description


def test_preset_set_and_default() -> None:
    assert set(m.PRESETS) == set(EXPECTED_PRESETS)
    assert m.DEFAULT_PRESET in m.PRESETS
    assert m.DEFAULT_PRESET == "best"  # quality first until the eval says otherwise


@pytest.mark.parametrize("name", list(EXPECTED_PRESETS))
def test_preset_roles_are_valid(name: str) -> None:
    preset = m.PRESETS[name]  # type: ignore[index]
    roles = [*preset.explorers, preset.formatter]
    if preset.verifier is not None:
        roles.append(preset.verifier)
    for role in roles:
        assert m.validate_model(role.model, policy()) == role.model
        assert m.validate_effort(role.model, role.effort) == role.effort
    models = [r.model for r in preset.explorers]
    assert len(models) == len(set(models))


@pytest.mark.parametrize("model", list(EXPECTED_EFFORTS))
@pytest.mark.parametrize("effort", ALL_EFFORTS)
def test_effort_table(model: str, effort: str) -> None:
    if effort == "default" or effort in EXPECTED_EFFORTS[model]:
        assert m.validate_effort(model, effort) == effort
    else:
        with pytest.raises(m.ModelError, match="does not accept"):
            m.validate_effort(model, effort)


def test_effort_for_unknown_model_is_forced_to_default() -> None:
    for effort in ALL_EFFORTS:
        assert m.validate_effort("my-own-model", effort) == "default"


@pytest.mark.parametrize("bad", ["none", "xhigh", "HIGH", "", "off"])
def test_unknown_effort_values_rejected(bad: str) -> None:
    with pytest.raises(m.ModelError, match="unknown reasoning effort"):
        m.validate_effort("zai-glm-5-3", bad)


def test_validate_model_accepts_known_ids() -> None:
    for model in m.MODEL_TABLE:
        assert m.validate_model(model, policy()) == model
    assert m.validate_model("  mistral-large-4 \n", policy()) == "mistral-large-4"


def test_mistral_large_latest_is_refused_with_reason() -> None:
    permissive = policy(
        allow_unknown_models=True, allowed_models=["mistral-large-latest", "mistral-large-4"]
    )
    for pol in (policy(), permissive):
        with pytest.raises(m.ModelError) as info:
            m.validate_model("mistral-large-latest", pol)
        message = str(info.value)
        assert "Mistral Large 3" in message
        assert "use mistral-large-4" in message


def test_unknown_model_needs_opt_in_and_allow_list() -> None:
    with pytest.raises(m.ModelError, match="not a known model"):
        m.validate_model("acme-7b", policy())
    with pytest.raises(m.ModelError, match="allowed models"):
        m.validate_model("acme-7b", policy(allow_unknown_models=True))
    pol = policy(allow_unknown_models=True, allowed_models=["acme-7b"])
    assert m.validate_model("acme-7b", pol) == "acme-7b"
    assert not m.is_known("acme-7b")


def test_known_model_outside_allow_list_is_refused() -> None:
    with pytest.raises(m.ModelError, match="allowed models"):
        m.validate_model("zai-glm-5-3", policy(allowed_models=["mistral-large-4"]))


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_model_id_refused(bad: str) -> None:
    with pytest.raises(m.ModelError, match="empty"):
        m.validate_model(bad, policy())


def test_model_error_is_a_value_error() -> None:
    assert issubclass(m.ModelError, ValueError)


def test_estimate_cost_at_list_price() -> None:
    u = Usage(model="zai-glm-5-3", input_tokens=1_000_000, output_tokens=500_000)
    assert m.estimate_cost("zai-glm-5-3", u) == pytest.approx(1.40 + 2.20)
    # Cached input is charged at the full input price (it is part of input_tokens).
    cached = Usage(model="x", input_tokens=1_000_000, cached_input_tokens=900_000)
    assert m.estimate_cost("mistral-large-4", cached) == pytest.approx(0.70)
    assert m.estimate_cost("unknown", Usage(model="u", output_tokens=1_000_000)) == pytest.approx(
        6.0
    )
    assert m.estimate_cost("mistral-large-4", Usage(model="z")) == 0.0


def test_estimate_cost_never_undercounts_separately_reported_tokens() -> None:
    odd = Usage(
        model="m", input_tokens=10, cached_input_tokens=1_000_000, reasoning_tokens=1_000_000
    )
    assert m.estimate_cost("mistral-large-4", odd) == pytest.approx(0.70 + 2.10)


def test_tool_call_limit_caps_fast_models() -> None:
    assert m.tool_call_limit("zai-glm-5-3", 60) == 60
    assert m.tool_call_limit("mistral-large-4", 60) == 60
    assert m.tool_call_limit("zai-glm-5-3-flash", 60) == 25
    assert m.tool_call_limit("deepseek-v4-1-flash-1017", 60) == 25
    assert m.tool_call_limit("zai-glm-5-3-flash", 10) == 10
    assert m.tool_call_limit("unknown", 60) == 60
