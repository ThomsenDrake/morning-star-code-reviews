"""Repository, user and operator configuration models and resolution.

``effective = clip(policy caps, override(run overrides, override(repo config, preset)))``

Model choice (explorers, ensemble, verifier on/off and model, formatter, efforts)
follows, lowest to highest precedence:

1. the preset (the repository file's ``preset``, else the built-in default);
2. the trusted repository file's ``models``, ``ensemble``, ``verify`` and
   ``reasoning_effort``;
3. an explicit run preset (CLI ``--preset``, Action ``preset`` input), which replaces
   all of layer 2: the run gets that preset's pipeline as published;
4. explicit run model overrides (``--explorers``, ``--explorer-model``,
   ``--verify``, ``--verifier-model``, ``--formatter-model``, the effort flags).

Budgets are not part of this order: the repository file can only lower them, run
overrides can raise them up to the operator caps, whatever the preset.

The repository file (``.morning-star-review.yml``) is read from the PR's base
commit only. It can lower budgets but never raise them, and when the operator
does not trust it (CLI mode on someone else's repository) its model, effort,
preset, ensemble, verification and budget settings are ignored, so a foreign
repository cannot choose your models or spend your money.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from morning_star_review.core.contract import (
    Contract,
    Effort,
    ModelRole,
    OperatorPolicy,
    PresetName,
    ResolvedBudgets,
    ResolvedConfig,
    ResolvedRoles,
    RunOverrides,
    Severity,
)
from morning_star_review.core.models import (
    DEFAULT_PRESET,
    MODEL_TABLE,
    PRESETS,
    ModelError,
    validate_effort,
    validate_model,
)

__all__ = [
    "REPO_CONFIG_FILE",
    "REPO_CONFIG_MAX_BYTES",
    "ConfigError",
    "ConfigSource",
    "OperatorPolicy",
    "RepoConfig",
    "ResolvedBudgets",
    "ResolvedConfig",
    "ResolvedRoles",
    "RunOverrides",
    "load_repo_config",
    "resolve",
]

REPO_CONFIG_FILE = ".morning-star-review.yml"
REPO_CONFIG_MAX_BYTES = 64 * 1024
MAX_EXPLORERS = 5

ConfigSource = Literal["base_file", "default", "invalid_fell_back"]


class ConfigError(ValueError):
    """The operator policy leaves no usable model (nothing to fall back to)."""


# --------------------------------------------------------------------------------------
# Repository file schema
# --------------------------------------------------------------------------------------


class RepoModels(Contract):
    explorer: str | None = Field(default=None, min_length=1, max_length=100)
    formatter: str | None = Field(default=None, min_length=1, max_length=100)
    verifier: str | None = Field(default=None, min_length=1, max_length=100)


class RepoEfforts(Contract):
    explorer: Effort | None = None  # applies to every explorer
    formatter: Effort | None = None
    verifier: Effort | None = None


class RepoBudgets(Contract):
    """Each value can only lower the default (and is clipped to the operator cap)."""

    max_tool_calls: int | None = Field(default=None, ge=1)
    explorer_minutes: int | None = Field(default=None, ge=1)
    inline_diff_chars: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    verifier_max_tool_calls: int | None = Field(default=None, ge=1)
    verifier_minutes: int | None = Field(default=None, ge=1)


class RepoPathInstruction(Contract):
    path: str = Field(min_length=1, max_length=500)
    instructions: str = Field(max_length=1024)


class RepoReview(Contract):
    drafts: bool = False
    incremental: bool = True
    inline_min_severity: Severity = "medium"
    max_inline_comments: int = Field(default=15, ge=0, le=25)
    author_may_dismiss: bool = True
    instructions: str = Field(default="", max_length=8192)
    path_instructions: list[RepoPathInstruction] = Field(default_factory=list, max_length=30)


class RepoSummary(Contract):
    show_usage: bool = True


class RepoEnsemble(Contract):
    explorers: list[str] | None = Field(default=None, min_length=1, max_length=MAX_EXPLORERS)

    @model_validator(mode="after")
    def _ids(self) -> RepoEnsemble:
        for model in self.explorers or []:
            if not model.strip() or len(model) > 100:
                raise ValueError("ensemble.explorers entries must be model ids")
        return self


class RepoVerify(Contract):
    enabled: bool | None = None  # None = as the preset says


class RepoContext(Contract):
    related_definitions: bool = True


class RepoConfig(Contract):
    """Schema of ``.morning-star-review.yml``. Any unknown key invalidates the file."""

    version: Literal[1] = 1
    preset: PresetName | None = None
    models: RepoModels = Field(default_factory=RepoModels)
    reasoning_effort: RepoEfforts = Field(default_factory=RepoEfforts)
    ensemble: RepoEnsemble = Field(default_factory=RepoEnsemble)
    verify: RepoVerify = Field(default_factory=RepoVerify)
    budgets: RepoBudgets = Field(default_factory=RepoBudgets)
    review: RepoReview = Field(default_factory=RepoReview)
    exclude: list[str] = Field(default_factory=list, max_length=100)
    summary: RepoSummary = Field(default_factory=RepoSummary)
    labels: bool = True
    suggestions: bool = True
    context: RepoContext = Field(default_factory=RepoContext)

    @model_validator(mode="after")
    def _exclude_limits(self) -> RepoConfig:
        if any(not g.strip() or len(g) > 500 for g in self.exclude):
            raise ValueError("exclude entries must be non-empty globs of at most 500 chars")
        return self


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


class _AliasError(yaml.YAMLError):
    pass


class _NoAliasLoader(yaml.SafeLoader):
    """Safe loader that refuses aliases, so a small file cannot expand into a huge one."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise _AliasError("YAML aliases are not allowed")
        return super().compose_node(parent, index)


_SAFE_LOC = re.compile(r"[^A-Za-z0-9_-]")


def _describe_errors(exc: ValidationError, limit: int = 5) -> list[str]:
    # Never echo input values: the file is untrusted and may be large.
    out: list[str] = []
    for err in exc.errors(include_url=False, include_input=False)[:limit]:
        # Unknown keys are input too: keep only name-like characters of each part.
        loc = ".".join(_SAFE_LOC.sub("?", str(p))[:40] for p in err["loc"]) or "(top level)"
        out.append(f"{loc}: {err['msg']}"[:200])
    extra = exc.error_count() - limit
    if extra > 0:
        out.append(f"... and {extra} more problem(s)")
    return out


def load_repo_config(
    text: str | None,
) -> tuple[RepoConfig | None, ConfigSource, list[str]]:
    """Parse the repository config file text (``None`` = no file at the base commit).

    Never raises for bad input: an invalid file falls back to defaults as a whole,
    with warnings for the summary (``invalid_fell_back``); the review still runs.
    """
    if text is None:
        return None, "default", []
    fallback = f"{REPO_CONFIG_FILE} is invalid and was ignored (defaults used)"
    if len(text.encode("utf-8", errors="replace")) > REPO_CONFIG_MAX_BYTES:
        return None, "invalid_fell_back", [f"{fallback}: larger than 64 KiB"]
    try:
        data = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass
    except _AliasError:
        return None, "invalid_fell_back", [f"{fallback}: YAML aliases are not allowed"]
    except RecursionError:  # PyYAML composes nested nodes recursively
        return None, "invalid_fell_back", [f"{fallback}: nested too deeply"]
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}"
        return None, "invalid_fell_back", [f"{fallback}: not valid YAML{where}"]
    if data is None:
        return RepoConfig(), "base_file", []
    if not isinstance(data, dict):
        return None, "invalid_fell_back", [f"{fallback}: the top level must be a mapping"]
    try:
        cfg = RepoConfig.model_validate(data)
    except ValidationError as exc:
        return None, "invalid_fell_back", [fallback, *_describe_errors(exc)]
    return cfg, "base_file", []


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------

# Repository settings that choose models or spend money; ignored when untrusted.
_SPEND_SETTINGS = ("preset", "models", "reasoning_effort", "ensemble", "verify", "budgets")
# Repository settings that choose models; an explicit run preset replaces them.
_MODEL_SETTINGS = ("models", "reasoning_effort", "ensemble", "verify")


def _ignored_spend_settings(repo: RepoConfig) -> list[str]:
    defaults = RepoConfig()
    return [name for name in _SPEND_SETTINGS if getattr(repo, name) != getattr(defaults, name)]


def _without_model_settings(
    spend: RepoConfig, run_preset: PresetName, warnings: list[str]
) -> RepoConfig:
    """``spend`` with the model settings an explicit run preset replaces reset to defaults."""
    defaults = RepoConfig()
    set_here = [name for name in _MODEL_SETTINGS if getattr(spend, name) != getattr(defaults, name)]
    if not set_here:
        return spend
    warnings.append(
        f"{REPO_CONFIG_FILE}: {', '.join(set_here)} not used because this run asked "
        f"for the {run_preset} preset"
    )
    return spend.model_copy(update={name: getattr(defaults, name) for name in set_here})


def _check_model(model: str, label: str, policy: OperatorPolicy, warnings: list[str]) -> str | None:
    try:
        return validate_model(model, policy)
    except ModelError as exc:
        warnings.append(f"{label}: {exc}; ignored")
        return None


def _first_usable(candidates: list[str], policy: OperatorPolicy) -> str | None:
    for model in candidates:
        try:
            return validate_model(model, policy)
        except ModelError:
            continue
    return None


def _usable(candidates: list[str], policy: OperatorPolicy) -> list[str]:
    out: list[str] = []
    for model in candidates:
        try:
            out.append(validate_model(model, policy))
        except ModelError:
            continue
    return out


def _last_resort(policy: OperatorPolicy) -> str:
    # Known models first, in table order, then whatever the operator listed.
    model = _first_usable([*MODEL_TABLE, *policy.allowed_models], policy)
    if model is None:
        raise ConfigError("the operator's allowed models leave no usable model")
    return model


def _pick_effort(
    model: str,
    choices: list[tuple[str, Effort | None]],
    fallback: Effort,
    warnings: list[str],
) -> Effort:
    """First valid effort among ``choices`` (highest precedence first), else ``fallback``."""
    for label, effort in choices:
        if effort is None:
            continue
        try:
            return validate_effort(model, effort)
        except ModelError as exc:
            warnings.append(f"{label}: {exc}; using the preset value")
    try:
        return validate_effort(model, fallback)
    except ModelError:
        return "default"


def _preset_effort(roles: tuple[ModelRole, ...], model: str, generic: Effort) -> Effort:
    for role in roles:
        if role.model == model:
            return role.effort
    return generic


def _resolve_explorers(
    preset_name: PresetName,
    spend: RepoConfig,
    overrides: RunOverrides,
    policy: OperatorPolicy,
    warnings: list[str],
) -> list[ModelRole]:
    preset = PRESETS[preset_name]
    # The last layer that names explorers wins.
    layers: list[tuple[str, list[str] | None]] = [
        (f"preset {preset_name}", [r.model for r in preset.explorers]),
        ("models.explorer", [spend.models.explorer] if spend.models.explorer else None),
        ("ensemble.explorers", spend.ensemble.explorers),
        (
            "explorer model override",
            [overrides.explorer_model] if overrides.explorer_model else None,
        ),
        ("explorers override", overrides.explorers),
    ]
    label, wanted = next((lbl, ids) for lbl, ids in reversed(layers) if ids)
    chosen: list[str] = []
    for model in wanted or []:
        ok = _check_model(model, label, policy, warnings)
        if ok is None:
            continue
        if ok in chosen:
            warnings.append(f"{label}: model {ok!r} is listed twice; using it once")
            continue
        chosen.append(ok)
    if not chosen:
        chosen = _usable([r.model for r in preset.explorers], policy) or [_last_resort(policy)]
        warnings.append(f"no usable explorer model; using {', '.join(chosen)}")
    if len(chosen) > MAX_EXPLORERS:
        warnings.append(f"at most {MAX_EXPLORERS} explorers are used; the rest are ignored")
        chosen = chosen[:MAX_EXPLORERS]
    roles: list[ModelRole] = []
    for model in chosen:
        effort = _pick_effort(
            model,
            [
                ("explorer effort override", overrides.explorer_effort),
                ("reasoning_effort.explorer", spend.reasoning_effort.explorer),
            ],
            _preset_effort(preset.explorers, model, "high"),
            warnings,
        )
        roles.append(ModelRole(model=model, effort=effort))
    return roles


def _resolve_verifier(
    preset_name: PresetName,
    spend: RepoConfig,
    overrides: RunOverrides,
    policy: OperatorPolicy,
    primary: ModelRole,
    warnings: list[str],
) -> ModelRole | None:
    preset = PRESETS[preset_name]
    enabled = preset.verifier is not None
    if spend.verify.enabled is not None:
        enabled = spend.verify.enabled
    if overrides.verify is not None:
        enabled = overrides.verify
    named = overrides.verifier_model or spend.models.verifier
    if not enabled:
        if named:
            warnings.append(f"verifier model {named!r} is set but verification is off; ignored")
        return None
    model: str | None = None
    if named:
        model = _check_model(named, "verifier model", policy, warnings)
    if model is None and preset.verifier is not None:
        model = _first_usable([preset.verifier.model], policy)
    if model is None:
        model = primary.model  # already validated
    preset_roles = (preset.verifier,) if preset.verifier is not None else ()
    effort = _pick_effort(
        model,
        [
            ("verifier effort override", overrides.verifier_effort),
            ("reasoning_effort.verifier", spend.reasoning_effort.verifier),
        ],
        _preset_effort(preset_roles, model, "high"),
        warnings,
    )
    return ModelRole(model=model, effort=effort)


def _resolve_formatter(
    preset_name: PresetName,
    spend: RepoConfig,
    overrides: RunOverrides,
    policy: OperatorPolicy,
    primary: ModelRole,
    warnings: list[str],
) -> ModelRole:
    preset = PRESETS[preset_name]
    model: str | None = None
    named = overrides.formatter_model or spend.models.formatter
    if named:
        model = _check_model(named, "formatter model", policy, warnings)
    if model is None:
        model = _first_usable([preset.formatter.model], policy) or primary.model
    effort = _pick_effort(
        model,
        [
            ("formatter effort override", overrides.formatter_effort),
            ("reasoning_effort.formatter", spend.reasoning_effort.formatter),
        ],
        _preset_effort((preset.formatter,), model, "low"),
        warnings,
    )
    return ModelRole(model=model, effort=effort)


_REPO_BUDGETS = (
    "max_tool_calls",
    "explorer_minutes",
    "inline_diff_chars",
    "max_cost_usd",
    "verifier_max_tool_calls",
    "verifier_minutes",
)
_OVERRIDE_BUDGETS = (
    "max_tool_calls",
    "explorer_minutes",
    "max_cost_usd",
    "verifier_max_tool_calls",
    "verifier_minutes",
)


def _resolve_budgets(
    repo_budgets: RepoBudgets,
    overrides: RunOverrides,
    policy: OperatorPolicy,
    warnings: list[str],
) -> ResolvedBudgets:
    values: dict[str, Any] = ResolvedBudgets().model_dump()
    for name in _REPO_BUDGETS:
        value = getattr(repo_budgets, name)
        if value is None:
            continue
        if value > values[name]:
            warnings.append(
                f"budgets.{name} {value} is above the default {values[name]}; "
                "the repository config can only lower budgets"
            )
            continue
        values[name] = value
    explicit: set[str] = set()
    for name in _OVERRIDE_BUDGETS:
        value = getattr(overrides, name)
        if value is not None:
            values[name] = value
            explicit.add(name)
    caps: dict[str, int | float] = {
        "max_tool_calls": policy.max_tool_calls_cap,
        "explorer_minutes": policy.explorer_minutes_cap,
        "max_cost_usd": policy.max_cost_usd_cap,
        "verifier_max_tool_calls": policy.verifier_tool_calls_cap,
        "verifier_minutes": policy.verifier_minutes_cap,
    }
    for name, cap in caps.items():
        if values[name] > cap:
            if name in explicit:
                warnings.append(f"{name} {values[name]} is above the operator cap; using {cap}")
            values[name] = cap
    # The LLM-call limit is only a backstop; keep it proportional to the tool budget.
    values["max_llm_calls"] = max(10, values["max_tool_calls"] * 3 // 2)
    return ResolvedBudgets.model_validate(values)


def resolve(
    repo: RepoConfig | None,
    overrides: RunOverrides,
    policy: OperatorPolicy,
) -> tuple[ResolvedConfig, list[str]]:
    """Merge preset, repository config, run overrides and operator policy.

    Model precedence: preset < trusted repository file < explicit run preset <
    explicit run model overrides (see the module docstring). Returns the effective
    config and human-readable warnings for the summary. Bad
    model ids or efforts never fail the run: they fall back with a warning. Raises
    ``ConfigError`` only when the policy itself leaves no usable model.
    """
    warnings: list[str] = []
    file = repo if repo is not None else RepoConfig()
    trusted = policy.trust_repo_config_models
    if repo is not None and not trusted:
        ignored = _ignored_spend_settings(repo)
        if ignored:
            warnings.append(
                f"{REPO_CONFIG_FILE}: ignored {', '.join(ignored)} because this "
                "repository's config is not trusted for model and budget settings"
            )
    spend = file if trusted else RepoConfig()
    if overrides.preset is not None:
        spend = _without_model_settings(spend, overrides.preset, warnings)

    preset_name: PresetName = overrides.preset or spend.preset or DEFAULT_PRESET
    explorers = _resolve_explorers(preset_name, spend, overrides, policy, warnings)
    primary = explorers[0]
    verifier = _resolve_verifier(preset_name, spend, overrides, policy, primary, warnings)
    formatter = _resolve_formatter(preset_name, spend, overrides, policy, primary, warnings)
    roles = ResolvedRoles(
        explorer_model=primary.model,
        explorer_effort=primary.effort,
        formatter_model=formatter.model,
        formatter_effort=formatter.effort,
        extra_explorers=explorers[1:],
        verifier=verifier,
    )
    budgets = _resolve_budgets(spend.budgets, overrides, policy, warnings)
    review = file.review
    cfg = ResolvedConfig(
        roles=roles,
        budgets=budgets,
        inline_min_severity=overrides.inline_min_severity or review.inline_min_severity,
        max_inline_comments=review.max_inline_comments,
        review_drafts=review.drafts or policy.review_drafts,
        incremental=review.incremental,
        author_may_dismiss=review.author_may_dismiss,
        instructions=review.instructions,
        path_instructions=[(p.path, p.instructions) for p in review.path_instructions],
        exclude=list(file.exclude),
        show_usage=file.summary.show_usage and policy.show_usage,
        preset=preset_name,
        labels=file.labels and policy.labels,
        suggestions=file.suggestions and policy.suggestions,
        related_definitions=file.context.related_definitions,
    )
    return cfg, warnings
