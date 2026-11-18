"""Pydantic data contracts shared by the workflow, activities and agent.

Every model forbids unknown fields and is frozen: payloads cross process and
activity boundaries, and a sealed payload must not change after it is sealed.
Field order is normative (it is the order of the JSON the models emit).
String and list limits keep payload sizes bounded by construction.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["security", "correctness", "reliability", "performance", "other"]
Effort = Literal["default", "low", "medium", "high", "max"]  # "default" = do not send it
Mode = Literal["ci", "cli", "local"]
Verdict = Literal["clean", "findings", "incomplete"]
OutcomeStatus = Literal["posted", "report", "skipped", "superseded", "failed"]
SkipReason = Literal["closed", "draft", "superseded", "already_reviewed"]
AnchorKind = Literal["inline", "snapped", "file", "outside"]
Scope = Literal["full", "incremental", "carry_over"]
PriorStatusValue = Literal["fixed", "still_open", "unsure"]
FindingState = Literal["open", "fixed", "dismissed", "summary_only"]
VerifyStatus = Literal["confirmed", "refuted", "uncertain"]
PresetName = Literal["best", "balanced", "single", "fast", "mistral"]
Role = Literal["explorer", "verifier", "formatter"]
ExhaustReason = Literal["tool_calls", "deadline", "context", "cost", "iterations", "length"]
# Budgets that a run override, the operator or a trusted repository file can set.
BudgetName = Literal[
    "max_tool_calls",
    "explorer_minutes",
    "inline_diff_chars",
    "max_cost_usd",
    "verifier_max_tool_calls",
    "verifier_minutes",
]
AgentPhase = Literal["explore", "format", "verify"]
CompareStatus = Literal["ahead", "diverged", "behind", "identical", "unknown"]
Stage = Literal[
    "fetch_pr", "prepare", "explore", "structure", "merge", "verify", "finalize", "publish"
]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
"""Lower rank = more severe. Use with ``severity_at_least``."""

DEFAULT_ALLOWED_MODELS: tuple[str, ...] = (
    "zai-glm-5-3",
    "mistral-large-4",
    "zai-glm-5-3-flash",
    "deepseek-v4-1-flash-1017",
)

MAX_FILES = 3000  # changed files a PR snapshot (and every path list derived from it) holds
MAX_USAGE_KEYS = 30  # role:model pairs in a usage map

_SHA_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"  # SHA-1 or SHA-256 object ids
_CANDIDATE_ID = r"^[A-Za-z0-9_-]{1,40}$"

# Bounded strings for list items, so every payload has a size bound.
ModelId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
CandidateId = Annotated[str, StringConstraints(pattern=_CANDIDATE_ID)]
Note = Annotated[str, StringConstraints(max_length=300)]
Message = Annotated[str, StringConstraints(max_length=500)]
Glob = Annotated[str, StringConstraints(min_length=1, max_length=500)]
Block = Annotated[str, StringConstraints(max_length=2000)]


def severity_at_least(severity: Severity, threshold: Severity) -> bool:
    """True when ``severity`` is as severe as ``threshold`` or more."""
    return SEVERITY_ORDER[severity] <= SEVERITY_ORDER[threshold]


def usage_key(role: Role, model: str) -> str:
    """Key for per-role, per-model usage maps (``FinalReview.usage``, ``ReviewOutcome.usage``)."""
    return f"{role}:{model}"


class Contract(BaseModel):
    """Base for all contract models: unknown fields are rejected, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------
# Request and policy
# --------------------------------------------------------------------------------------


class OperatorPolicy(Contract):
    """Set by Action inputs or CLI flags/config. Repository config can only lower these."""

    allowed_models: list[ModelId] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_MODELS), min_length=1, max_length=50
    )
    allow_unknown_models: bool = False
    max_cost_usd_cap: float = Field(default=10.00, gt=0, le=100)
    max_tool_calls_cap: int = Field(default=60, ge=1, le=200)
    explorer_minutes_cap: int = Field(default=15, ge=1, le=25)
    review_drafts: bool = False
    # True in ci mode (own repo); False in cli mode unless --trust-repo-config.
    trust_repo_config_models: bool
    show_usage: bool = True  # run-details block in the summary
    verifier_tool_calls_cap: int = Field(default=30, ge=1, le=200)
    verifier_minutes_cap: int = Field(default=8, ge=1, le=25)
    labels: bool = True  # Action input `labels`; the repo file can only turn labels off
    suggestions: bool = True  # Action input `suggestions`; the repo file can only turn them off
    # The operator's default preset (CLI user config ``[defaults] preset``). Not an explicit
    # run preset: a trusted repository file's preset and model settings win over it.
    default_preset: PresetName | None = None


class RunOverrides(Contract):
    """Action inputs / CLI flags (trusted). ``None`` = not set."""

    preset: PresetName | None = None
    explorer_model: str | None = Field(default=None, max_length=100)  # one explorer, no ensemble
    formatter_model: str | None = Field(default=None, max_length=100)
    explorer_effort: Effort | None = None  # applies to every explorer
    max_tool_calls: int | None = Field(default=None, ge=1, le=200)
    explorer_minutes: int | None = Field(default=None, ge=1, le=25)
    max_cost_usd: float | None = Field(default=None, gt=0, le=100)
    inline_min_severity: Severity | None = None
    explorers: list[ModelId] | None = Field(default=None, min_length=1, max_length=5)  # ensemble
    formatter_effort: Effort | None = None
    verify: bool | None = None
    verifier_model: str | None = Field(default=None, max_length=100)
    verifier_effort: Effort | None = None
    verifier_max_tool_calls: int | None = Field(default=None, ge=1, le=200)
    verifier_minutes: int | None = Field(default=None, ge=1, le=25)


class ReviewRequest(Contract):
    """Workflow input. ``pr_url`` and ``repo`` are search keys (not sensitive)."""

    schema_version: Literal[1] = 1
    pr_url: str = Field(max_length=300)  # canonical https://github.com/<owner>/<repo>/pull/<n>
    repo: str = Field(max_length=200)  # "<owner>/<repo>"
    mode: Mode
    expected_head_sha: str | None = Field(default=None, pattern=_SHA_PATTERN)
    post: Literal["auto", "never"] = "auto"
    force: bool = False  # bypass already_reviewed / closed / draft (never superseded)
    overrides: RunOverrides = Field(default_factory=RunOverrides)
    policy: OperatorPolicy
    run_label: str = Field(max_length=200)  # "gha:<repo>/<run_id>/<attempt>" or "cli:<rand>"


# --------------------------------------------------------------------------------------
# Resolved configuration (re-exported by core.config)
# --------------------------------------------------------------------------------------


class ModelRole(Contract):
    """One agent role assignment: a model id and the reasoning effort to send."""

    model: str = Field(min_length=1, max_length=100)
    effort: Effort = "default"


class ResolvedRoles(Contract):
    """Models for each role. The primary explorer keeps the flat v1 fields."""

    explorer_model: str = Field(min_length=1, max_length=100)
    explorer_effort: Effort
    formatter_model: str = Field(min_length=1, max_length=100)
    formatter_effort: Effort
    extra_explorers: list[ModelRole] = Field(default_factory=list, max_length=4)
    verifier: ModelRole | None = None

    @model_validator(mode="after")
    def _unique_explorers(self) -> ResolvedRoles:
        models = [r.model for r in self.explorers]
        if len(set(models)) != len(models):
            # Each explorer gets a job dir named after its model, so ids must be unique.
            raise ValueError("explorer models must be unique")
        return self

    @property
    def explorers(self) -> list[ModelRole]:
        """All explorers, primary first."""
        primary = ModelRole(model=self.explorer_model, effort=self.explorer_effort)
        return [primary, *self.extra_explorers]

    @property
    def ensemble(self) -> bool:
        return bool(self.extra_explorers)

    @property
    def verify_enabled(self) -> bool:
        return self.verifier is not None

    @property
    def uses_pipeline(self) -> bool:
        """True when the run uses the explore-merge-verify shape (ADDENDUM D)."""
        return self.ensemble or self.verify_enabled


class ResolvedBudgets(Contract):
    """Per-run budgets after clipping to the operator caps.

    ``max_tool_calls`` / ``explorer_minutes`` apply to each explorer;
    ``verifier_*`` to the verifier; ``max_cost_usd`` is the total for the run.
    """

    max_tool_calls: int = Field(default=60, ge=1, le=200)
    max_llm_calls: int = Field(default=90, ge=1, le=400)  # backstop; max_iterations = this + 4
    explorer_minutes: int = Field(default=15, ge=1, le=25)
    grace_seconds: int = Field(default=120, ge=0, le=1800)  # wall time after the tool deadline
    rescue_seconds: int = Field(default=180, ge=0, le=1800)
    max_context_tokens: int = Field(default=140_000, ge=1_000)  # tools close above this estimate
    auto_compact_threshold: int = Field(default=200_000, ge=50_000)
    inline_diff_chars: int = Field(default=80_000, ge=0)
    max_file_share: float = Field(default=0.4, gt=0, le=1)
    max_tool_output_chars: int = Field(default=24_000, ge=1_000)
    max_cost_usd: float = Field(default=10.00, gt=0, le=100)
    grace_llm_calls: int = Field(default=2, ge=0, le=10)
    verifier_max_tool_calls: int = Field(default=30, ge=1, le=200)
    verifier_minutes: int = Field(default=8, ge=1, le=25)
    # The budgets that someone set (a CLI flag, an Action input, the operator's config or
    # a trusted repository file), in sorted order. An explicit tool-call budget applies
    # as set; without it the preset or per-model default applies (models.tool_call_limit).
    explicit: tuple[BudgetName, ...] = ()


PathInstruction = tuple[Glob, Annotated[str, StringConstraints(max_length=1024)]]  # (glob, text)

CONFIG_HASH_VERSION = 1


class ResolvedConfig(Contract):
    """Repository config (base branch) merged with run overrides and operator policy."""

    roles: ResolvedRoles
    budgets: ResolvedBudgets = Field(default_factory=ResolvedBudgets)
    inline_min_severity: Severity = "medium"
    max_inline_comments: int = Field(default=15, ge=0, le=25)
    review_drafts: bool = False
    incremental: bool = True
    author_may_dismiss: bool = True
    instructions: str = Field(default="", max_length=8192)
    path_instructions: list[PathInstruction] = Field(default_factory=list, max_length=30)
    exclude: list[Glob] = Field(default_factory=list, max_length=100)
    show_usage: bool = True
    preset: PresetName | None = None  # informational: the preset the roles started from
    labels: bool = True
    suggestions: bool = True
    related_definitions: bool = True
    # sha256 over the settings that change what a review finds; budgets, labels and
    # usage display are excluded so changing them does not force a full re-review.
    # Leave empty to have it computed; a non-empty value must match. model_copy() does
    # not re-validate, so build changed configs with model_validate or the constructor.
    config_hash: str = ""

    @model_validator(mode="after")
    def _check_hash(self) -> ResolvedConfig:
        expected = compute_config_hash(self)
        if not self.config_hash:
            # Frozen model: set the derived value once, during validation.
            object.__setattr__(self, "config_hash", expected)
        elif self.config_hash != expected:
            raise ValueError("config_hash does not match the configuration")
        return self


def compute_config_hash(cfg: ResolvedConfig) -> str:
    """Stable hash of the review-relevant settings of ``cfg`` (see ``ResolvedConfig``)."""
    payload = {
        "v": CONFIG_HASH_VERSION,
        "roles": cfg.roles.model_dump(mode="json"),
        "inline_min_severity": cfg.inline_min_severity,
        "max_inline_comments": cfg.max_inline_comments,
        "instructions": cfg.instructions,
        "path_instructions": [list(p) for p in cfg.path_instructions],
        "exclude": list(cfg.exclude),
        "suggestions": cfg.suggestions,
        "related_definitions": cfg.related_definitions,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Seals
# --------------------------------------------------------------------------------------


class Sealed(Contract):
    """Payload carrying an HMAC seal (see ``core.seal``).

    ``seal`` = hex HMAC-SHA256(key=RunContext.seal_key, msg=execution_id + "\\x00" +
    type(self).__name__ + "\\x00" + canonical_json(self without seal)).
    """

    seal: str = Field(default="", max_length=128)


# --------------------------------------------------------------------------------------
# GitHub-side models
# --------------------------------------------------------------------------------------


class PrFile(Contract):
    path: str
    previous_path: str | None = None
    status: Literal["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    has_patch: bool  # False for binary or too-large files (GitHub omits the patch)
    binary: bool = False


class PriorFinding(Contract):
    """Decoded from our state marker; trusted only when authored by the bot."""

    id: str = Field(max_length=40)
    path: str = Field(max_length=500)
    line: int | None
    severity: Severity
    title: str = Field(max_length=160)
    evidence: str = Field(max_length=300)
    comment_id: int | None = None  # REST id of the inline comment (None = summary only)
    thread_id: str | None = Field(default=None, max_length=200)  # GraphQL node id
    state: FindingState  # as recorded
    thread_resolved: bool = False  # live, from GraphQL
    resolved_by: str | None = Field(default=None, max_length=100)  # live
    resolver_can_dismiss: bool = False  # live: resolver has write+ (and the author rule)


class PriorState(Contract):
    version: int
    tool_version: str = Field(max_length=50)
    head_sha: str = Field(max_length=64)
    merge_base_sha: str = Field(max_length=64)
    complete: bool
    verdict: Verdict
    config_hash: str = Field(max_length=64)
    reviewed_at: str = Field(max_length=40)  # ISO-8601 UTC
    findings: list[PriorFinding] = Field(default_factory=list, max_length=60)
    summary_comment_id: int | None = None
    # Thread ids GitHub refused to resolve.
    refused_resolves: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=60
    )


class PrSnapshot(Sealed):
    """Output of ``fetch_pr``."""

    owner: str = Field(max_length=100)
    repo: str = Field(max_length=100)
    number: int = Field(ge=1)
    html_url: str = Field(max_length=300)
    title: str = Field(max_length=300)
    body_excerpt: str = Field(max_length=4000)  # HTML comments stripped
    author: str = Field(max_length=100)
    author_association: str = Field(max_length=50)
    draft: bool
    state: Literal["open", "closed"]
    merged: bool
    head_sha: str = Field(max_length=64)
    head_repo: str = Field(max_length=200)  # "<owner>/<repo>" of the head (fork aware)
    base_ref: str = Field(max_length=300)
    base_sha: str = Field(max_length=64)
    merge_base_sha: str = Field(max_length=64)  # from compare base...head
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changed_files: int = Field(ge=0)
    files: list[PrFile] = Field(max_length=MAX_FILES)
    files_json_sha256: str = Field(max_length=64)  # sha256 of run_dir/pr_files.json
    can_post: bool
    cannot_post_reason: str | None = Field(default=None, max_length=500)
    bot_login: str | None  # "<slug>[bot]"
    prior: PriorState | None = None
    compare_from_prior: CompareStatus | None = None
    prior_to_head_merges: bool = False  # any merge commit in prior..head
    config: ResolvedConfig
    config_source: Literal["base_file", "default", "invalid_fell_back"]
    config_warnings: list[Message] = Field(default_factory=list, max_length=20)
    skip: SkipReason | None = None
    run_dir: str = Field(max_length=4096)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


# --------------------------------------------------------------------------------------
# Prepared work
# --------------------------------------------------------------------------------------


class AnchorFile(Contract):
    path: str
    right_lines: list[int]  # sorted RIGHT-side lines present in GitHub's patch (added + context)
    hunks: list[tuple[int, int]]  # (start, end) RIGHT-side ranges, inclusive


class AnchorMap(Contract):
    """Written to anchors.json; not a payload."""

    files: dict[str, AnchorFile]
    renamed_from: dict[str, str]  # old path -> new path


class ExcludedFile(Contract):
    path: str
    reason: str = Field(max_length=200)
    changed_lines: int = Field(ge=0)


class Prepared(Sealed):
    """Output of ``prepare``."""

    job_root: str = Field(max_length=4096)  # run_dir
    repo_dir: str = Field(max_length=4096)  # run_dir/repo
    scope: Scope
    since_sha: str | None  # prior head for incremental/carry_over
    prompt_sha256: str = Field(max_length=64)
    prompt_chars: int = Field(ge=0)
    inlined_paths: list[str] = Field(max_length=MAX_FILES)
    # Reviewable but not inlined (available via show_file_diff).
    on_demand_paths: list[str] = Field(max_length=MAX_FILES)
    excluded: list[ExcludedFile] = Field(max_length=MAX_FILES)
    reviewable_paths: list[str] = Field(max_length=MAX_FILES)  # inlined + on_demand
    anchors_sha256: str = Field(max_length=64)
    # Prior finding ids shown to the explorer.
    prior_for_prompt: list[Annotated[str, StringConstraints(max_length=40)]] = Field(max_length=60)
    partial_reason: str | None = Field(default=None, max_length=500)  # hard ceiling hit
    roles: ResolvedRoles
    budgets: ResolvedBudgets
    boundary: str = Field(max_length=100)  # random token used in untrusted-block tags


# --------------------------------------------------------------------------------------
# Agent results
# --------------------------------------------------------------------------------------


class Usage(Contract):
    model: str = Field(max_length=100)
    llm_calls: int = 0
    tool_calls: int = 0
    # input_tokens includes cached_input_tokens; output_tokens includes reasoning_tokens.
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0  # estimate at list price; cached tokens at full input price
    seconds: float = 0.0
    finish_reasons: list[Annotated[str, StringConstraints(max_length=20)]] = Field(
        default_factory=list, max_length=200
    )
    # Rate-limit waits (429 / capacity / 5xx) the agent sat out: waits started by the
    # runner or the shared-cooldown hook, and the time they took (not part of any budget).
    rate_limit_waits: int = Field(default=0, ge=0)
    rate_limit_wait_seconds: float = Field(default=0.0, ge=0)

    def plus(self, other: Usage) -> Usage:
        """Sum of two usages (``model`` of ``self`` is kept)."""
        return Usage(
            model=self.model,
            llm_calls=self.llm_calls + other.llm_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            seconds=self.seconds + other.seconds,
            finish_reasons=[*self.finish_reasons, *other.finish_reasons][-200:],
            rate_limit_waits=self.rate_limit_waits + other.rate_limit_waits,
            rate_limit_wait_seconds=self.rate_limit_wait_seconds + other.rate_limit_wait_seconds,
        )


AgentState = Literal["Completed", "Failed", "Error", "NotRun"]


class ExploreResult(Sealed):
    """Output of one ``explore`` activity (one per explorer model)."""

    notes: str = Field(max_length=65_536)
    state: AgentState
    stop_reason: str | None = Field(max_length=200)  # None | "iteration_limit" | "skipped" | ...
    rescued: bool  # a rescue turn was needed
    complete: bool  # rules in DESIGN 6.6
    budget_exhausted: bool
    exhausted_by: list[ExhaustReason] = Field(default_factory=list, max_length=6)
    # Coverage: files opened via read_repo_file/show_file_diff.
    opened_paths: list[str] = Field(max_length=MAX_FILES)
    usage: Usage
    error: str | None = Field(default=None, max_length=2000)  # redacted

    @property
    def model(self) -> str:
        return self.usage.model


class DraftFinding(Contract):
    path: str = Field(max_length=500)
    line: int | None
    end_line: int | None = None
    severity: Severity
    category: Category
    title: str = Field(max_length=160)
    evidence: str = Field(max_length=500)
    explanation: str = Field(max_length=4000)
    fix: str | None = Field(default=None, max_length=2000)
    # Raw replacement text between the `<<<` / `>>>` markers (ADDENDUM E); validated later.
    suggestion: str | None = Field(default=None, max_length=2000)


class PriorStatus(Contract):
    id: str = Field(max_length=40)
    status: PriorStatusValue
    reason: str = Field(default="", max_length=500)


DraftSource = Literal["parser", "parser+formatter", "parser_lossy", "empty"]


class ReviewDraft(Sealed):
    """Output of ``structure`` (one per explorer)."""

    findings: list[DraftFinding] = Field(max_length=80)
    prior_status: list[PriorStatus] = Field(max_length=60)
    summary: str = Field(max_length=4000)
    source: DraftSource
    marker_count: int = Field(ge=0)  # FINDING markers seen in the notes
    unparsed_blocks: list[Block] = Field(default_factory=list, max_length=20)  # shown if lossy
    formatter_usage: Usage | None = None
    explorer_model: str | None = Field(default=None, max_length=100)  # votes key for merge


class FormatterDraft(Contract):
    """Strict ``output_type`` of the notes formatter (no seal)."""

    findings: list[DraftFinding] = Field(max_length=80)
    prior_status: list[PriorStatus] = Field(max_length=60)
    summary: str = Field(max_length=4000)


# --------------------------------------------------------------------------------------
# Ensemble merge and verification (ADDENDUM D)
# --------------------------------------------------------------------------------------


class Candidate(DraftFinding):
    """A merged finding: one or more explorers reported it."""

    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    votes: list[ModelId] = Field(min_length=1, max_length=5)  # models that reported it

    @classmethod
    def from_draft(cls, finding: DraftFinding, candidate_id: str, votes: list[str]) -> Candidate:
        return cls(**finding.model_dump(), candidate_id=candidate_id, votes=votes)

    def to_draft(self) -> DraftFinding:
        return DraftFinding(**self.model_dump(exclude={"candidate_id", "votes"}))


class MergeResult(Sealed):
    """Output of ``merge``: deduplicated candidates across explorers, at most 60."""

    candidates: list[Candidate] = Field(max_length=60)
    explorer_models: list[ModelId] = Field(max_length=5)  # drafts that were merged, in order
    input_findings: int = Field(ge=0)  # total draft findings before merging
    dropped_over_cap: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _unique_ids(self) -> MergeResult:
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique")
        return self


class VerifyEntry(Contract):
    """The verifier's decision on one candidate. Optional fields are corrections."""

    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    status: VerifyStatus
    line: int | None = None
    evidence: str | None = Field(default=None, max_length=500)
    severity: Severity | None = None
    reason: str = Field(default="", max_length=1000)


class VerifyDraft(Contract):
    """Strict ``output_type`` of the formatter when verifier notes do not parse (no seal)."""

    entries: list[VerifyEntry] = Field(max_length=60)


VerifySource = Literal["parser", "parser+formatter", "parser_lossy", "empty"]


class VerifyResult(Sealed):
    """Output of ``verify``. Candidates missing from ``entries`` count as ``uncertain``."""

    entries: list[VerifyEntry] = Field(max_length=60)
    unaddressed: list[CandidateId] = Field(default_factory=list, max_length=60)
    source: VerifySource
    state: AgentState
    stop_reason: str | None = Field(max_length=200)
    rescued: bool
    complete: bool
    budget_exhausted: bool
    exhausted_by: list[ExhaustReason] = Field(default_factory=list, max_length=6)
    opened_paths: list[str] = Field(max_length=MAX_FILES)
    usage: Usage
    formatter_usage: Usage | None = None
    error: str | None = Field(default=None, max_length=2000)  # redacted

    def status_of(self, candidate_id: str) -> VerifyStatus:
        for entry in self.entries:
            if entry.candidate_id == candidate_id:
                return entry.status
        return "uncertain"


class DroppedCandidate(Contract):
    """A candidate the verifier refuted; kept for the report JSON, never published."""

    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    path: str = Field(max_length=500)
    line: int | None
    severity: Severity
    title: str = Field(max_length=160)
    votes: list[ModelId] = Field(default_factory=list, max_length=5)
    reason: str = Field(default="", max_length=300)


# --------------------------------------------------------------------------------------
# Agent subprocess contract (DESIGN 6.8)
# --------------------------------------------------------------------------------------


class AgentBudgets(Contract):
    max_tool_calls: int = Field(ge=0)
    max_llm_calls: int = Field(ge=1)
    grace_llm_calls: int = Field(ge=0)
    # The runner fixes deadline_epoch in meter.json on its first start.
    explore_seconds: int = Field(ge=0)
    grace_seconds: int = Field(ge=0)
    rescue_seconds: int = Field(ge=0)
    max_context_tokens: int = Field(ge=1)
    auto_compact_threshold: int = Field(ge=1)
    max_tool_output_chars: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)


class ChangedFileRef(Contract):
    path: str
    previous_path: str | None
    status: str = Field(max_length=20)
    diff_file: str | None  # rendered hunks file, relative to the job dir


class RunLimits(Contract):
    """Run-level limits of one agent job (wall-clock epoch seconds).

    Tools close at the latest at wind_down_epoch; no model call starts and no
    rate-limit wait ends after stop_epoch. Never paused.
    """

    wind_down_epoch: float = Field(allow_inf_nan=False)
    stop_epoch: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordered(self) -> RunLimits:
        if self.stop_epoch < self.wind_down_epoch:
            raise ValueError("stop_epoch must not be before wind_down_epoch")
        return self


class AgentJob(Contract):
    """``job.json`` read by ``python -m morning_star_review.agent <phase> <job_dir>``."""

    schema_version: Literal[1] = 1
    phase: AgentPhase
    execution_id: str = Field(max_length=200)
    repo_dir: str
    head_sha: str = Field(max_length=64)
    merge_base_sha: str = Field(max_length=64)
    input_path: str  # prompt.txt (explore/verify) or notes.txt (format)
    input_sha256: str = Field(max_length=64)
    variant: Literal["full", "incremental"]
    model: str = Field(max_length=100)
    effort: Effort
    budgets: AgentBudgets
    price_in_per_m: float = Field(ge=0)
    price_out_per_m: float = Field(ge=0)
    changed_files: list[ChangedFileRef]
    meter_path: str
    coverage_path: str
    result_path: str
    partial_notes_path: str
    # Phase "format" only: which strict schema the formatter fills.
    format_target: Literal["review", "verify"] = "review"
    # Run-level ceiling (core/runclock.py); None = only the job's own budgets apply.
    run_limits: RunLimits | None = None
    # Absolute path of the run's shared per-model cooldown file (runclock.cooldown_path).
    cooldown_path: str | None = None


class AgentRunResult(Contract):
    """``result.json`` written by the agent runner (exit code 0, any state)."""

    phase: AgentPhase
    state: Literal["Completed", "Failed", "InvalidOutput", "Error", "NotRun"]
    stop_reason: str | None
    notes: str | None = None  # explore / verify
    draft: dict[str, Any] | None = None  # format: FormatterDraft or VerifyDraft JSON
    rescued: bool = False
    complete: bool = False
    budget_exhausted: bool = False
    exhausted_by: list[str] = Field(default_factory=list)
    opened_paths: list[str] = Field(default_factory=list)
    usage: Usage
    error: str | None = None


# --------------------------------------------------------------------------------------
# Final review and outcome
# --------------------------------------------------------------------------------------


class Suggestion(Contract):
    """A validated GitHub suggestion block for head lines ``start_line..end_line``.

    Only ``review.suggest.validate`` builds these; the model guarantees the text is
    never empty, because an empty suggestion renders as "delete these lines".
    """

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)  # replacement lines, no trailing newline

    @model_validator(mode="after")
    def _check(self) -> Suggestion:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not be before start_line")
        if not self.text.strip():
            raise ValueError("suggestion text must not be blank")
        return self


class FinalFinding(Contract):
    id: str = Field(max_length=40)  # "f" + 12 hex
    path: str = Field(max_length=500)
    line: int | None  # head line (anchored) or None
    start_line: int | None = None  # multi-line range start (same hunk)
    anchor: AnchorKind
    severity: Severity
    category: Category
    title: str = Field(max_length=160)
    evidence: str = Field(max_length=500)
    explanation: str = Field(max_length=2000)
    fix: str | None = Field(default=None, max_length=1000)
    state: FindingState  # open | summary_only (below threshold / over cap / not anchorable)
    carried: bool = False  # same id already has an open bot thread
    thread_id: str | None = Field(default=None, max_length=200)
    comment_id: int | None = None
    votes: list[ModelId] = Field(default_factory=list, max_length=5)  # explorer models
    verify_status: VerifyStatus | None = None  # None when verification did not run
    suggestion: Suggestion | None = None
    # Internal only (e.g. why a suggestion was dropped); never published.
    notes: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=4
    )


class ResolvedPrior(Contract):
    id: str = Field(max_length=40)
    title: str = Field(max_length=160)
    path: str = Field(max_length=500)
    action: Literal["resolve", "keep_open", "dismissed", "file_deleted"]
    note: str = Field(max_length=300)


class FinalReview(Sealed):
    """Output of ``finalize``."""

    verdict: Verdict
    verdict_reasons: list[Note] = Field(max_length=20)  # e.g. "2 changed files were not opened"
    findings: list[FinalFinding] = Field(max_length=50)
    priors: list[ResolvedPrior] = Field(max_length=60)
    summary_md: str = Field(max_length=60_000)  # summary comment body without the state marker
    report_md: str = Field(max_length=262_144)
    state: PriorState  # state to write after successful publication
    usage: dict[str, Usage] = Field(max_length=MAX_USAGE_KEYS)  # keyed by usage_key()
    cost_usd: float
    # {"reviewable": n, "covered": m, "excluded": k}
    coverage: dict[str, int] = Field(max_length=10)
    refuted: list[DroppedCandidate] = Field(default_factory=list, max_length=60)


class ReviewOutcome(Contract):
    """Workflow result."""

    status: OutcomeStatus
    reason: str | None = Field(default=None, max_length=1000)  # set unless status == "posted"
    verdict: Verdict | None = None
    pr_url: str = Field(max_length=300)
    head_sha: str | None = Field(default=None, max_length=64)
    execution_id: str = Field(max_length=200)
    findings: list[FinalFinding] = Field(default_factory=list, max_length=50)
    report_markdown: str = Field(default="", max_length=262_144)
    review_url: str | None = Field(default=None, max_length=500)
    summary_url: str | None = Field(default=None, max_length=500)
    # Keyed by usage_key(role, model).
    usage: dict[str, Usage] = Field(default_factory=dict, max_length=MAX_USAGE_KEYS)
    cost_usd: float = 0.0
    refuted: list[DroppedCandidate] = Field(default_factory=list, max_length=60)
    # Non-fatal problems worth reporting (e.g. a label update that failed).
    notes: list[Note] = Field(default_factory=list, max_length=50)
    # FinalReview.verdict_reasons: why the review is incomplete, or notes about it.
    verdict_reasons: list[Note] = Field(default_factory=list, max_length=20)
    # Possible issues: findings the verifier left uncertain that were not posted inline
    # (review.verdict.is_possible). They never make the verdict "findings". Counted from
    # ``findings`` when the caller does not set it.
    possible_issues: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _count_possible_issues(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "possible_issues" in data:
            return data
        findings = data.get("findings") or []
        count = sum(1 for f in findings if ReviewOutcome._is_possible_issue(f))
        return {**data, "possible_issues": count}

    @staticmethod
    def _is_possible_issue(f: Any) -> bool:
        """``review.verdict.is_possible`` for a finding or its raw data."""
        data = f if isinstance(f, dict) else {k: getattr(f, k, None) for k in _POSSIBLE_KEYS}
        return (
            data.get("verify_status") == "uncertain"
            and data.get("state") == "summary_only"
            and not data.get("carried")
        )

    def counted_findings(self) -> list[FinalFinding]:
        """Open findings other than possible issues (``review.verdict.counts_as_finding``).

        These make the verdict ``findings``; ``fail-on``, the Action's ``findings``
        output and the result line count them too.
        """
        return [
            f
            for f in self.findings
            if f.state in ("open", "summary_only") and not self._is_possible_issue(f)
        ]


_POSSIBLE_KEYS = ("verify_status", "state", "carried")


# --------------------------------------------------------------------------------------
# Activity inputs
# --------------------------------------------------------------------------------------


class PrepareInput(Contract):
    req: ReviewRequest
    snap: PrSnapshot


class ExploreInput(Contract):
    req: ReviewRequest
    snap: PrSnapshot
    prep: Prepared
    explorer: ModelRole | None = None  # None = the primary explorer of prep.roles


class StructureInput(Contract):
    req: ReviewRequest
    prep: Prepared
    explore: ExploreResult


class MergeInput(Contract):
    req: ReviewRequest
    prep: Prepared
    drafts: list[ReviewDraft] = Field(min_length=1, max_length=5)  # one per explorer


class VerifyInput(Contract):
    req: ReviewRequest
    snap: PrSnapshot
    prep: Prepared
    merge: MergeResult


SealRef = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FinalizeInput(Contract):
    """Input of ``finalize``.

    ``explore_seals``/``draft_seals`` name every explorer's ``ExploreResult`` and
    ``ReviewDraft`` that reached this point (explorer order, primary first) by their
    seals. The sealed payloads themselves stay in the run directory, where the
    activities that made them saved a copy (``activities.finalize.keep_for_finalize``):
    with an ensemble they would add up to ~0.8 MB here, on top of the snapshot, the
    plan and the merge, and Temporal payloads are limited to ~2 MB. ``finalize``
    loads them and checks their seals, so it sees exactly the payloads the workflow
    received. ``merge``/``verify`` are set when the pipeline ran.
    """

    req: ReviewRequest
    snap: PrSnapshot
    prep: Prepared
    explore_seals: list[SealRef] = Field(default_factory=list, max_length=5)
    draft_seals: list[SealRef] = Field(default_factory=list, max_length=5)
    merge: MergeResult | None = None
    verify: VerifyResult | None = None


class PublishInput(Contract):
    """``final`` is None only when ``snap.skip`` is set."""

    req: ReviewRequest
    snap: PrSnapshot
    final: FinalReview | None

    @model_validator(mode="after")
    def _final_or_skip(self) -> PublishInput:
        if self.final is None and self.snap.skip is None:
            raise ValueError("final may be None only when the snapshot is skipped")
        return self


class FailureInput(Contract):
    req: ReviewRequest
    snap: PrSnapshot | None
    stage: Stage
    error_type: str = Field(max_length=100)
    error_text: str = Field(max_length=2000)
