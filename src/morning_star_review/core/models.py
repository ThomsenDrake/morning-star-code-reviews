"""Model table, presets, effort validation and cost estimation.

This module is data plus small pure functions. The build-phase evaluation may
change ``DEFAULT_PRESET``; everything else reads the presets from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from morning_star_review.core.contract import (
    Effort,
    ModelRole,
    OperatorPolicy,
    PresetName,
    Usage,
)

__all__ = [
    "DEFAULT_PRESET",
    "MODEL_TABLE",
    "PRESETS",
    "REJECTED_MODELS",
    "UNKNOWN_MODEL_PRICE",
    "ModelError",
    "ModelInfo",
    "Preset",
    "estimate_cost",
    "is_known",
    "price",
    "tool_call_limit",
    "validate_effort",
    "validate_model",
]


class ModelError(ValueError):
    """A model id or effort that this tool refuses to send."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """What we know about one model id.

    ``efforts`` lists the efforts the API accepts besides ``"default"`` (which
    means "do not send ``reasoning_effort``" and is valid for every model).
    ``max_tool_calls`` caps each agent run on this model below the run budget;
    fast models keep exploring for a long time when allowed many tools.
    """

    efforts: frozenset[str]
    price_in: float  # USD per 1M input tokens (list price)
    price_out: float  # USD per 1M output tokens (list price)
    lifecycle: str
    max_tool_calls: int | None = None


MODEL_TABLE: dict[str, ModelInfo] = {
    "zai-glm-5-3": ModelInfo(
        efforts=frozenset({"low", "high", "max"}),
        price_in=1.40,
        price_out=4.40,
        lifecycle="GA, third-party (about one month deprecation notice), text only",
    ),
    "mistral-large-4": ModelInfo(
        efforts=frozenset({"low", "medium", "high"}),
        price_in=0.70,
        price_out=2.10,
        lifecycle="GA",
    ),
    "zai-glm-5-3-flash": ModelInfo(
        efforts=frozenset({"low", "high", "max"}),
        price_in=0.15,
        price_out=0.50,
        lifecycle="GA, third-party",
        max_tool_calls=25,
    ),
    "deepseek-v4-1-flash-1017": ModelInfo(
        efforts=frozenset({"low", "high", "max"}),
        price_in=0.30,
        price_out=1.20,
        lifecycle="Public Preview, third-party",
        max_tool_calls=25,
    ),
}

UNKNOWN_MODEL_PRICE: tuple[float, float] = (2.00, 6.00)
"""Budgeting price (in, out per 1M tokens) for ids outside the table."""

REJECTED_MODELS: dict[str, str] = {
    "mistral-large-latest": (
        "the Agents SDK maps this alias to Mistral Large 3; use mistral-large-4"
    ),
}


@dataclass(frozen=True, slots=True)
class Preset:
    name: PresetName
    explorers: tuple[ModelRole, ...]  # primary first; two or more = ensemble
    verifier: ModelRole | None
    formatter: ModelRole
    description: str


def _role(model: str, effort: Effort) -> ModelRole:
    return ModelRole(model=model, effort=effort)


PRESETS: dict[PresetName, Preset] = {
    "best": Preset(
        name="best",
        explorers=(
            _role("zai-glm-5-3", "high"),
            _role("mistral-large-4", "high"),
            _role("deepseek-v4-1-flash-1017", "high"),
        ),
        verifier=_role("zai-glm-5-3", "high"),
        formatter=_role("mistral-large-4", "low"),
        description="three explorers in parallel, merged, then verified",
    ),
    "balanced": Preset(
        name="balanced",
        explorers=(_role("zai-glm-5-3", "high"),),
        verifier=_role("mistral-large-4", "high"),
        formatter=_role("mistral-large-4", "low"),
        description="one explorer, then a verifier from a different model family",
    ),
    "single": Preset(
        name="single",
        explorers=(_role("zai-glm-5-3", "high"),),
        verifier=None,
        formatter=_role("mistral-large-4", "low"),
        description="one explorer, no verification",
    ),
    "fast": Preset(
        name="fast",
        explorers=(_role("zai-glm-5-3-flash", "high"),),
        verifier=None,
        formatter=_role("zai-glm-5-3-flash", "low"),
        description="one fast explorer, no verification; lowest cost",
    ),
    "mistral": Preset(
        name="mistral",
        explorers=(_role("mistral-large-4", "high"),),
        verifier=_role("mistral-large-4", "high"),
        formatter=_role("mistral-large-4", "low"),
        description="Mistral's own model for every role",
    ),
}

# Quality first (ADDENDUM B). The evaluation may switch this to another preset.
DEFAULT_PRESET: PresetName = "best"

# Typed lookup so a validated str narrows back to the Effort literal.
_EFFORTS: dict[str, Effort] = {
    "default": "default",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}


def is_known(model: str) -> bool:
    return model in MODEL_TABLE


def validate_model(model: str, policy: OperatorPolicy) -> str:
    """Return the model id if this run may use it; raise ``ModelError`` otherwise.

    An id must be in ``policy.allowed_models``. Ids outside ``MODEL_TABLE`` also
    need ``policy.allow_unknown_models``. Some ids are refused outright.
    """
    model_id = model.strip()
    if not model_id:
        raise ModelError("model id is empty")
    if model_id in REJECTED_MODELS:
        raise ModelError(f"model {model_id!r} is not supported: {REJECTED_MODELS[model_id]}")
    if model_id not in MODEL_TABLE and not policy.allow_unknown_models:
        known = ", ".join(MODEL_TABLE)
        raise ModelError(
            f"model {model_id!r} is not a known model (known: {known}); "
            "the operator must allow unknown models to use it"
        )
    if model_id not in policy.allowed_models:
        raise ModelError(f"model {model_id!r} is not in the operator's allowed models")
    return model_id


def validate_effort(model: str, effort: str) -> Effort:
    """Return ``effort`` if ``model`` accepts it; raise ``ModelError`` otherwise.

    Unknown models always get ``"default"`` (we do not know what they accept).
    """
    if effort not in _EFFORTS:
        raise ModelError(f"unknown reasoning effort {effort!r}")
    info = MODEL_TABLE.get(model)
    if info is None or effort == "default":
        return "default"
    if effort not in info.efforts:
        allowed = ", ".join(sorted(info.efforts | {"default"}))
        raise ModelError(
            f"model {model!r} does not accept reasoning effort {effort!r} (allowed: {allowed})"
        )
    return _EFFORTS[effort]


def price(model: str) -> tuple[float, float]:
    """List price (input, output) in USD per 1M tokens; unknown ids use the budgeting price."""
    info = MODEL_TABLE.get(model)
    if info is None:
        return UNKNOWN_MODEL_PRICE
    return (info.price_in, info.price_out)


def estimate_cost(model: str, usage: Usage) -> float:
    """Cost estimate at list price. Cached input is charged at the full input price.

    ``input_tokens`` is expected to include cached tokens and ``output_tokens`` to
    include reasoning tokens; the max() keeps the estimate from going low if a
    provider reports them separately.
    """
    price_in, price_out = price(model)
    tokens_in = max(usage.input_tokens, usage.cached_input_tokens)
    tokens_out = max(usage.output_tokens, usage.reasoning_tokens)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def tool_call_limit(model: str, requested: int) -> int:
    """Tool-call budget for one agent run on ``model`` (the per-model cap, if lower)."""
    info = MODEL_TABLE.get(model)
    if info is None or info.max_tool_calls is None:
        return requested
    return min(requested, info.max_tool_calls)
