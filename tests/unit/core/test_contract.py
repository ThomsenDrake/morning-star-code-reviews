"""Contract models: limits, invariants, round trips and payload size bounds."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from morning_star_review.core import contract as c

SHA = "a" * 40


def x(n: int) -> str:
    return "x" * n


def all_contract_models() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(c, inspect.isclass)
        if issubclass(obj, c.Contract) and obj.__module__ == c.__name__
    ]


def policy(**kw: Any) -> c.OperatorPolicy:
    return c.OperatorPolicy(trust_repo_config_models=True, **kw)


def request(**kw: Any) -> c.ReviewRequest:
    base: dict[str, Any] = {
        "pr_url": "https://github.com/o/r/pull/1",
        "repo": "o/r",
        "mode": "ci",
        "policy": policy(),
        "run_label": "gha:o/r/1/1",
    }
    base.update(kw)
    return c.ReviewRequest(**base)


def roles(**kw: Any) -> c.ResolvedRoles:
    base: dict[str, Any] = {
        "explorer_model": "zai-glm-5-3",
        "explorer_effort": "high",
        "formatter_model": "mistral-large-4",
        "formatter_effort": "low",
    }
    base.update(kw)
    return c.ResolvedRoles(**base)


def draft_finding(**kw: Any) -> c.DraftFinding:
    base: dict[str, Any] = {
        "path": "a.py",
        "line": 3,
        "severity": "high",
        "category": "correctness",
        "title": "Off by one",
        "evidence": "for i in range(n + 1):",
        "explanation": "Reads past the end.",
    }
    base.update(kw)
    return c.DraftFinding(**base)


def usage(model: str = "zai-glm-5-3", **kw: Any) -> c.Usage:
    return c.Usage(model=model, **kw)


# --------------------------------------------------------------------------------------
# Generic properties
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", all_contract_models(), ids=lambda m: m.__name__)
def test_every_model_forbids_extra_and_is_frozen(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


def test_models_are_immutable() -> None:
    req = request()
    with pytest.raises(ValidationError):
        req.mode = "cli"  # type: ignore[misc]


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        c.Usage(model="m", surprise=1)  # type: ignore[call-arg]


def test_sealed_models_start_with_seal_field() -> None:
    sealed = [m for m in all_contract_models() if issubclass(m, c.Sealed) and m is not c.Sealed]
    names = {m.__name__ for m in sealed}
    assert {
        "PrSnapshot",
        "Prepared",
        "ExploreResult",
        "ReviewDraft",
        "FinalReview",
        "MergeResult",
        "VerifyResult",
    } <= names
    for m in sealed:
        assert next(iter(m.model_fields)) == "seal"
        assert m.model_fields["seal"].default == ""


def test_helpers() -> None:
    assert c.severity_at_least("critical", "medium")
    assert c.severity_at_least("medium", "medium")
    assert not c.severity_at_least("low", "medium")
    assert c.usage_key("verifier", "mistral-large-4") == "verifier:mistral-large-4"


# --------------------------------------------------------------------------------------
# Request and policy
# --------------------------------------------------------------------------------------


def test_policy_defaults_follow_addendum() -> None:
    p = policy()
    assert p.allowed_models == list(c.DEFAULT_ALLOWED_MODELS)
    assert p.max_cost_usd_cap == 10.00
    assert p.max_tool_calls_cap == 60
    assert p.explorer_minutes_cap == 15
    assert p.verifier_tool_calls_cap == 30
    assert p.verifier_minutes_cap == 8
    assert p.labels is True


def test_policy_requires_trust_flag() -> None:
    with pytest.raises(ValidationError):
        c.OperatorPolicy()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_cost_usd_cap", 0),
        ("max_cost_usd_cap", 101),
        ("max_tool_calls_cap", 0),
        ("max_tool_calls_cap", 201),
        ("explorer_minutes_cap", 26),
        ("allowed_models", []),
    ],
)
def test_policy_bounds(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        policy(**{field: value})


def test_request_round_trip_and_sha_pattern() -> None:
    req = request(expected_head_sha=SHA, overrides=c.RunOverrides(preset="fast", verify=False))
    again = c.ReviewRequest.model_validate(req.model_dump(mode="json"))
    assert again == req
    assert c.ReviewRequest.model_validate_json(req.model_dump_json()) == req
    for bad in ["ABC", "a" * 39, "A" * 40, "g" * 40]:
        with pytest.raises(ValidationError):
            request(expected_head_sha=bad)
    assert request(expected_head_sha="b" * 64).expected_head_sha == "b" * 64


def test_overrides_reject_unknown_preset() -> None:
    with pytest.raises(ValidationError):
        c.RunOverrides(preset="economy")  # type: ignore[arg-type]
    assert c.RunOverrides(preset="best").preset == "best"


# --------------------------------------------------------------------------------------
# Resolved config
# --------------------------------------------------------------------------------------


def test_roles_explorers_and_pipeline_flags() -> None:
    single = roles()
    assert [r.model for r in single.explorers] == ["zai-glm-5-3"]
    assert not single.ensemble
    assert not single.verify_enabled
    assert not single.uses_pipeline

    ens = roles(extra_explorers=[c.ModelRole(model="mistral-large-4", effort="high")])
    assert [r.model for r in ens.explorers] == ["zai-glm-5-3", "mistral-large-4"]
    assert ens.explorers[0].effort == "high"
    assert ens.ensemble and ens.uses_pipeline

    ver = roles(verifier=c.ModelRole(model="mistral-large-4", effort="high"))
    assert ver.verify_enabled and ver.uses_pipeline and not ver.ensemble


def test_roles_reject_duplicate_explorers() -> None:
    with pytest.raises(ValidationError, match="unique"):
        roles(extra_explorers=[c.ModelRole(model="zai-glm-5-3")])


def test_budget_defaults_follow_addendum() -> None:
    b = c.ResolvedBudgets()
    assert (b.max_tool_calls, b.explorer_minutes, b.max_cost_usd) == (60, 15, 10.00)
    assert (b.verifier_max_tool_calls, b.verifier_minutes) == (30, 8)
    assert b.auto_compact_threshold == 200_000
    assert b.max_llm_calls >= b.max_tool_calls


def test_config_hash_is_computed_and_checked() -> None:
    cfg = c.ResolvedConfig(roles=roles())
    assert len(cfg.config_hash) == 64
    assert cfg.config_hash == c.compute_config_hash(cfg)
    # Round trip keeps the hash and validates it.
    again = c.ResolvedConfig.model_validate(cfg.model_dump(mode="json"))
    assert again == cfg
    tampered = cfg.model_dump(mode="json") | {"instructions": "changed"}
    with pytest.raises(ValidationError, match="config_hash"):
        c.ResolvedConfig.model_validate(tampered)


def test_config_hash_ignores_budgets_labels_and_usage_display() -> None:
    base = c.ResolvedConfig(roles=roles())
    same = c.ResolvedConfig(
        roles=roles(),
        budgets=c.ResolvedBudgets(max_tool_calls=5, max_cost_usd=1.0),
        labels=False,
        show_usage=False,
        review_drafts=True,
        preset="single",
    )
    assert same.config_hash == base.config_hash


@pytest.mark.parametrize(
    "change",
    [
        {"roles": roles(explorer_effort="max")},
        {"roles": roles(verifier=c.ModelRole(model="mistral-large-4", effort="high"))},
        {"inline_min_severity": "high"},
        {"max_inline_comments": 3},
        {"instructions": "Use cents."},
        {"path_instructions": [("db/**", "Check SQL.")]},
        {"exclude": ["docs/**"]},
        {"suggestions": False},
        {"related_definitions": False},
    ],
)
def test_config_hash_changes_with_review_settings(change: dict[str, Any]) -> None:
    base = c.ResolvedConfig(roles=roles())
    other = c.ResolvedConfig(**({"roles": roles()} | change))
    assert other.config_hash != base.config_hash


def test_path_instruction_limits() -> None:
    c.ResolvedConfig(roles=roles(), path_instructions=[(x(500), x(1024))])
    with pytest.raises(ValidationError):
        c.ResolvedConfig(roles=roles(), path_instructions=[(x(501), "t")])
    with pytest.raises(ValidationError):
        c.ResolvedConfig(roles=roles(), path_instructions=[("g", x(1025))])


# --------------------------------------------------------------------------------------
# Findings, suggestions, merge and verify
# --------------------------------------------------------------------------------------


def test_draft_finding_limits_and_suggestion_field() -> None:
    f = draft_finding(suggestion="    return n\n")
    assert f.suggestion == "    return n\n"
    assert draft_finding().suggestion is None
    with pytest.raises(ValidationError):
        draft_finding(title=x(161))
    with pytest.raises(ValidationError):
        draft_finding(severity="blocker")


def test_suggestion_never_blank_and_ordered() -> None:
    s = c.Suggestion(start_line=3, end_line=4, text="a = 1\nb = 2")
    assert s.end_line == 4
    for text in ["", "   ", "\n\n"]:
        with pytest.raises(ValidationError):
            c.Suggestion(start_line=3, end_line=3, text=text)
    with pytest.raises(ValidationError):
        c.Suggestion(start_line=5, end_line=4, text="a")


def test_final_finding_additions() -> None:
    f = c.FinalFinding(
        id="f" + "0" * 12,
        path="a.py",
        line=3,
        anchor="inline",
        severity="high",
        category="correctness",
        title="t",
        evidence="e",
        explanation="why",
        state="open",
        votes=["zai-glm-5-3", "mistral-large-4"],
        verify_status="confirmed",
        suggestion=c.Suggestion(start_line=3, end_line=3, text="x = 2"),
        notes=["suggestion dropped: does not parse"],
    )
    assert f.verify_status == "confirmed"
    assert c.FinalFinding.model_validate_json(f.model_dump_json()) == f
    with pytest.raises(ValidationError):
        f.model_validate(f.model_dump() | {"notes": [x(201)]})
    with pytest.raises(ValidationError):
        f.model_validate(f.model_dump() | {"verify_status": "maybe"})


def test_candidate_round_trip() -> None:
    d = draft_finding(suggestion="pass")
    cand = c.Candidate.from_draft(d, "c1", ["zai-glm-5-3"])
    assert isinstance(cand, c.DraftFinding)
    assert cand.to_draft() == d
    assert cand.candidate_id == "c1" and cand.votes == ["zai-glm-5-3"]
    with pytest.raises(ValidationError):
        c.Candidate.from_draft(d, "c 1", ["m"])
    with pytest.raises(ValidationError):
        c.Candidate.from_draft(d, "c1", [])


def test_merge_result_requires_unique_ids() -> None:
    d = draft_finding()
    one = c.Candidate.from_draft(d, "c1", ["m"])
    ok = c.MergeResult(candidates=[one], explorer_models=["m"], input_findings=1)
    assert ok.dropped_over_cap == 0
    with pytest.raises(ValidationError, match="unique"):
        c.MergeResult(candidates=[one, one], explorer_models=["m"], input_findings=2)


def test_verify_result_status_defaults_to_uncertain() -> None:
    v = c.VerifyResult(
        entries=[c.VerifyEntry(candidate_id="c1", status="refuted", reason="guarded above")],
        unaddressed=["c2"],
        source="parser",
        state="Completed",
        stop_reason=None,
        rescued=False,
        complete=True,
        budget_exhausted=False,
        opened_paths=["a.py"],
        usage=usage("mistral-large-4"),
    )
    assert v.status_of("c1") == "refuted"
    assert v.status_of("c2") == "uncertain"
    assert v.status_of("c9") == "uncertain"
    draft = c.VerifyDraft(entries=list(v.entries))
    assert c.VerifyDraft.model_validate_json(draft.model_dump_json()) == draft


def test_verify_draft_schema_is_strict_object() -> None:
    schema = c.VerifyDraft.model_json_schema()
    assert schema["additionalProperties"] is False
    fmt = c.FormatterDraft.model_json_schema()
    assert fmt["additionalProperties"] is False
    assert "suggestion" in fmt["$defs"]["DraftFinding"]["properties"]


def test_usage_plus() -> None:
    a = usage(llm_calls=1, input_tokens=10, cost_usd=0.5, finish_reasons=["stop"])
    b = usage(llm_calls=2, output_tokens=5, cost_usd=0.25, finish_reasons=["length"])
    s = a.plus(b)
    assert (s.llm_calls, s.input_tokens, s.output_tokens) == (3, 10, 5)
    assert s.cost_usd == pytest.approx(0.75)
    assert s.finish_reasons == ["stop", "length"]
    assert (s.rate_limit_waits, s.rate_limit_wait_seconds) == (0, 0.0)


def test_usage_rate_limit_defaults_and_plus() -> None:
    u = usage()
    assert u.rate_limit_waits == 0
    assert u.rate_limit_wait_seconds == 0.0
    a = usage(rate_limit_waits=3, rate_limit_wait_seconds=105.5)
    b = usage(rate_limit_waits=2, rate_limit_wait_seconds=30.25)
    s = a.plus(b)
    assert s.rate_limit_waits == 5
    assert s.rate_limit_wait_seconds == pytest.approx(135.75)
    assert a.plus(usage()) == a


@pytest.mark.parametrize(
    "kw", [{"rate_limit_waits": -1}, {"rate_limit_wait_seconds": -0.5}], ids=str
)
def test_usage_rate_limit_fields_are_not_negative(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        usage(**kw)


def test_usage_json_without_rate_limit_keys_still_loads() -> None:
    old = {
        "model": "mistral-large-4",
        "llm_calls": 4,
        "tool_calls": 7,
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "output_tokens": 20,
        "reasoning_tokens": 0,
        "cost_usd": 0.01,
        "seconds": 12.5,
        "finish_reasons": ["stop"],
    }
    u = c.Usage.model_validate_json(json.dumps(old))
    assert (u.rate_limit_waits, u.rate_limit_wait_seconds) == (0, 0.0)
    # The new keys come after the old ones, so the old JSON is a prefix of the new.
    assert list(u.model_dump())[: len(old)] == list(old)


# --------------------------------------------------------------------------------------
# Activity inputs and agent contract
# --------------------------------------------------------------------------------------


def snapshot(**kw: Any) -> c.PrSnapshot:
    base: dict[str, Any] = {
        "owner": "o",
        "repo": "r",
        "number": 1,
        "html_url": "https://github.com/o/r/pull/1",
        "title": "t",
        "body_excerpt": "",
        "author": "a",
        "author_association": "OWNER",
        "draft": False,
        "state": "open",
        "merged": False,
        "head_sha": SHA,
        "head_repo": "o/r",
        "base_ref": "main",
        "base_sha": SHA,
        "merge_base_sha": SHA,
        "additions": 1,
        "deletions": 0,
        "changed_files": 1,
        "files": [
            c.PrFile(path="a.py", status="modified", additions=1, deletions=0, has_patch=True)
        ],
        "files_json_sha256": "0" * 64,
        "can_post": True,
        "bot_login": "morning-star-code-reviews[bot]",
        "config": c.ResolvedConfig(roles=roles()),
        "config_source": "default",
        "run_dir": "/tmp/run",
    }
    base.update(kw)
    return c.PrSnapshot(**base)


def test_snapshot_round_trip() -> None:
    snap = snapshot()
    assert snap.full_name == "o/r"
    assert c.PrSnapshot.model_validate_json(snap.model_dump_json()) == snap


def test_publish_input_final_none_only_when_skipped() -> None:
    req = request()
    with pytest.raises(ValidationError, match="skipped"):
        c.PublishInput(req=req, snap=snapshot(), final=None)
    ok = c.PublishInput(req=req, snap=snapshot(skip="draft"), final=None)
    assert ok.final is None


def test_failure_input_accepts_new_stages() -> None:
    for stage in ["merge", "verify", "fetch_pr", "publish"]:
        data = {"req": request(), "snap": None, "stage": stage, "error_type": "E", "error_text": ""}
        assert c.FailureInput.model_validate(data).stage == stage
    with pytest.raises(ValidationError):
        c.FailureInput(req=request(), snap=None, stage="nope", error_type="E", error_text="")  # type: ignore[arg-type]


def test_agent_job_and_result_accept_verify_phase() -> None:
    budgets = c.AgentBudgets(
        max_tool_calls=30,
        max_llm_calls=45,
        grace_llm_calls=2,
        explore_seconds=480,
        grace_seconds=120,
        rescue_seconds=180,
        max_context_tokens=140_000,
        auto_compact_threshold=200_000,
        max_tool_output_chars=24_000,
        max_cost_usd=2.0,
    )
    job = c.AgentJob(
        phase="verify",
        execution_id="msreview-ci-o-r-pr1-aaaaaaa-1234abcd",
        repo_dir="/tmp/run/repo",
        head_sha=SHA,
        merge_base_sha=SHA,
        input_path="prompt.txt",
        input_sha256="0" * 64,
        variant="full",
        model="mistral-large-4",
        effort="high",
        budgets=budgets,
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        changed_files=[
            c.ChangedFileRef(path="a.py", previous_path=None, status="modified", diff_file=None)
        ],
        meter_path="meter.json",
        coverage_path="coverage.json",
        result_path="result.json",
        partial_notes_path="partial.txt",
    )
    assert job.format_target == "review"
    assert c.AgentJob.model_validate_json(job.model_dump_json()) == job
    res = c.AgentRunResult(
        phase="verify", state="Completed", stop_reason=None, notes="VERIFY COMPLETE", usage=usage()
    )
    assert res.phase == "verify"
    fmt = c.AgentRunResult(
        phase="format",
        state="InvalidOutput",
        stop_reason=None,
        draft={"entries": []},
        usage=usage(),
    )
    assert fmt.draft == {"entries": []}


# job.json and result.json as 0.1.0 wrote them (no run limits, no rate-limit usage).
OLD_JOB_JSON: dict[str, Any] = {
    "schema_version": 1,
    "phase": "explore",
    "execution_id": "msreview-cli-o-r-pr1-aaaaaaa-1234abcd",
    "repo_dir": "/tmp/run/repo",
    "head_sha": SHA,
    "merge_base_sha": SHA,
    "input_path": "/tmp/run/explore-m/prompt.txt",
    "input_sha256": "0" * 64,
    "variant": "full",
    "model": "mistral-large-4",
    "effort": "high",
    "budgets": {
        "max_tool_calls": 60,
        "max_llm_calls": 90,
        "grace_llm_calls": 2,
        "explore_seconds": 900,
        "grace_seconds": 120,
        "rescue_seconds": 180,
        "max_context_tokens": 140000,
        "auto_compact_threshold": 200000,
        "max_tool_output_chars": 24000,
        "max_cost_usd": 2.0,
    },
    "price_in_per_m": 0.7,
    "price_out_per_m": 2.1,
    "changed_files": [
        {"path": "a.py", "previous_path": None, "status": "modified", "diff_file": None}
    ],
    "meter_path": "/tmp/run/explore-m/meter.json",
    "coverage_path": "/tmp/run/explore-m/coverage.json",
    "result_path": "/tmp/run/explore-m/result.json",
    "partial_notes_path": "/tmp/run/explore-m/notes.partial.txt",
    "format_target": "review",
}

OLD_RESULT_JSON: dict[str, Any] = {
    "phase": "explore",
    "state": "Completed",
    "stop_reason": None,
    "notes": "EXPLORATION COMPLETE",
    "draft": None,
    "rescued": False,
    "complete": True,
    "budget_exhausted": False,
    "exhausted_by": [],
    "opened_paths": ["a.py"],
    "usage": {
        "model": "mistral-large-4",
        "llm_calls": 3,
        "tool_calls": 2,
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.001,
        "seconds": 30.0,
        "finish_reasons": ["stop"],
    },
    "error": None,
}


def test_old_agent_job_and_result_json_still_validate() -> None:
    job = c.AgentJob.model_validate_json(json.dumps(OLD_JOB_JSON))
    assert job.run_limits is None
    assert job.cooldown_path is None
    res = c.AgentRunResult.model_validate_json(json.dumps(OLD_RESULT_JSON))
    assert res.usage.rate_limit_waits == 0
    assert res.usage.rate_limit_wait_seconds == 0.0


def test_agent_job_round_trips_run_limits_and_cooldown_path() -> None:
    data = {
        **OLD_JOB_JSON,
        "run_limits": {"wind_down_epoch": 1000.0, "stop_epoch": 1300.0},
        "cooldown_path": "/tmp/run/cooldown-mistral-large-4.json",
    }
    job = c.AgentJob.model_validate(data)
    assert job.run_limits == c.RunLimits(wind_down_epoch=1000.0, stop_epoch=1300.0)
    assert job.cooldown_path == "/tmp/run/cooldown-mistral-large-4.json"
    assert c.AgentJob.model_validate_json(job.model_dump_json()) == job
    res = c.AgentRunResult.model_validate(
        {
            **OLD_RESULT_JSON,
            "usage": {
                **OLD_RESULT_JSON["usage"],
                "rate_limit_waits": 4,
                "rate_limit_wait_seconds": 271.5,
            },
        }
    )
    assert (res.usage.rate_limit_waits, res.usage.rate_limit_wait_seconds) == (4, 271.5)


def test_run_limits_validator() -> None:
    assert c.RunLimits(wind_down_epoch=5.0, stop_epoch=5.0).stop_epoch == 5.0
    assert c.RunLimits(wind_down_epoch=5.0, stop_epoch=9.5).wind_down_epoch == 5.0
    with pytest.raises(ValidationError, match="stop_epoch"):
        c.RunLimits(wind_down_epoch=10.0, stop_epoch=9.0)
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            c.RunLimits(wind_down_epoch=1.0, stop_epoch=bad)
    with pytest.raises(ValidationError):
        c.RunLimits.model_validate({"wind_down_epoch": 1.0, "stop_epoch": 2.0, "extra": 1})


def test_explore_input_explorer_is_optional() -> None:
    assert "explorer" in c.ExploreInput.model_fields
    assert c.ExploreInput.model_fields["explorer"].default is None
    assert c.FinalizeInput.model_fields["explore_seals"].default_factory is not None
    assert c.FinalizeInput.model_fields["draft_seals"].default_factory is not None


# --------------------------------------------------------------------------------------
# Payload size bounds (DESIGN 4.7: maximal instances serialise below 900 KB)
# --------------------------------------------------------------------------------------

PAYLOAD_LIMIT = 900_000
FINAL_REVIEW_LIMIT = 1_100_000
HARD_LIMIT_MARGIN = 1_900_000  # Workflows payload limit is 2 MB


def max_usage(role: str) -> c.Usage:
    return c.Usage(
        model=x(100),
        llm_calls=10**6,
        tool_calls=10**6,
        input_tokens=10**12,
        cached_input_tokens=10**12,
        output_tokens=10**12,
        reasoning_tokens=10**12,
        cost_usd=1e6,
        seconds=1e6,
        finish_reasons=[x(20)] * 200,
        rate_limit_waits=10**6,
        rate_limit_wait_seconds=1e6,
    )


def max_usage_map() -> dict[str, c.Usage]:
    # Three explorers, a verifier and the formatter for each structure/verify step.
    keys = (
        [f"explorer:m{i}" for i in range(5)]
        + ["verifier:v"]
        + [f"formatter:f{i}" for i in range(3)]
    )
    return {k: max_usage(k) for k in keys}


def max_final_finding(i: int) -> c.FinalFinding:
    return c.FinalFinding(
        id=f"f{i:012x}",
        path=x(500),
        line=10**9,
        start_line=10**9 - 20,
        anchor="snapped",
        severity="critical",
        category="reliability",
        title=x(160),
        evidence=x(500),
        explanation=x(2000),
        fix=x(1000),
        state="summary_only",
        carried=True,
        thread_id=x(200),
        comment_id=10**15,
        votes=[x(100)] * 5,
        verify_status="uncertain",
        suggestion=c.Suggestion(start_line=10**9 - 20, end_line=10**9, text=x(2000)),
        notes=[x(200)] * 4,
    )


def max_dropped(i: int) -> c.DroppedCandidate:
    return c.DroppedCandidate(
        candidate_id=f"c{i}".ljust(40, "0"),
        path=x(500),
        line=10**9,
        severity="critical",
        title=x(160),
        votes=[x(100)] * 5,
        reason=x(300),
    )


def max_prior_state() -> c.PriorState:
    finding = c.PriorFinding(
        id=x(40),
        path=x(500),
        line=10**9,
        severity="high",
        title=x(160),
        evidence=x(300),
        comment_id=10**15,
        thread_id=x(200),
        state="open",
        thread_resolved=True,
        resolved_by=x(100),
        resolver_can_dismiss=True,
    )
    return c.PriorState(
        version=1,
        tool_version=x(50),
        head_sha=x(64),
        merge_base_sha=x(64),
        complete=True,
        verdict="findings",
        config_hash=x(64),
        reviewed_at=x(40),
        findings=[finding] * 60,
        summary_comment_id=10**15,
        refused_resolves=[x(200)] * 60,
    )


def size(model: BaseModel) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def test_max_review_outcome_fits() -> None:
    outcome = c.ReviewOutcome(
        status="posted",
        reason=x(1000),
        verdict="findings",
        pr_url=x(300),
        head_sha=x(64),
        execution_id=x(200),
        findings=[max_final_finding(i) for i in range(50)],
        report_markdown=x(262_144),
        review_url=x(500),
        summary_url=x(500),
        usage=max_usage_map(),
        cost_usd=1e6,
        refuted=[max_dropped(i) for i in range(60)],
        notes=[x(300)] * 50,
    )
    assert size(outcome) < PAYLOAD_LIMIT
    # Also JSON-compatible through the plain json module (what the SDK converter uses).
    assert json.loads(outcome.model_dump_json())["status"] == "posted"


def test_max_final_review_fits() -> None:
    final = c.FinalReview(
        seal="f" * 64,
        verdict="findings",
        verdict_reasons=[x(300)] * 20,
        findings=[max_final_finding(i) for i in range(50)],
        priors=[
            c.ResolvedPrior(id=x(40), title=x(160), path=x(500), action="keep_open", note=x(300))
        ]
        * 60,
        summary_md=x(60_000),
        report_md=x(262_144),
        state=max_prior_state(),
        usage=max_usage_map(),
        cost_usd=1e6,
        coverage={"reviewable": 3000, "covered": 3000, "excluded": 3000},
        refuted=[max_dropped(i) for i in range(60)],
    )
    # The ensemble/verify additions (votes, suggestion, notes, refuted, per-model usage)
    # push the theoretical maximum past 900 KB; it must still leave room for the
    # snapshot inside PublishInput under the 2 MB payload limit (checked below).
    assert size(final) < FINAL_REVIEW_LIMIT
    files = [
        c.PrFile(
            path=f"src/{i:04d}/" + x(110),
            status="modified",
            additions=1,
            deletions=1,
            has_patch=True,
        )
        for i in range(3000)
    ]
    snap = snapshot(files=files, changed_files=3000, config_warnings=[x(500)] * 20)
    publish = c.PublishInput(req=request(), snap=snap, final=final)
    assert size(publish) < HARD_LIMIT_MARGIN


def test_max_agent_outputs_fit() -> None:
    finding = draft_finding(
        path=x(500),
        title=x(160),
        evidence=x(500),
        explanation=x(4000),
        fix=x(2000),
        suggestion=x(2000),
    )
    review_draft = c.ReviewDraft(
        seal="f" * 64,
        findings=[finding] * 80,
        prior_status=[c.PriorStatus(id=x(40), status="unsure", reason=x(500))] * 60,
        summary=x(4000),
        source="parser_lossy",
        marker_count=10**6,
        unparsed_blocks=[x(2000)] * 20,
        formatter_usage=max_usage("formatter"),
        explorer_model=x(100),
    )
    assert size(review_draft) < PAYLOAD_LIMIT
    merge = c.MergeResult(
        candidates=[c.Candidate.from_draft(finding, f"c{i}", [x(100)] * 5) for i in range(60)],
        explorer_models=[x(100)] * 5,
        input_findings=400,
        dropped_over_cap=340,
    )
    assert size(merge) < PAYLOAD_LIMIT
    verify = c.VerifyResult(
        entries=[
            c.VerifyEntry(
                candidate_id=f"c{i}",
                status="confirmed",
                line=10**9,
                evidence=x(500),
                severity="low",
                reason=x(1000),
            )
            for i in range(60)
        ],
        unaddressed=[x(40)] * 60,
        source="parser+formatter",
        state="Completed",
        stop_reason=x(200),
        rescued=True,
        complete=False,
        budget_exhausted=True,
        exhausted_by=["tool_calls", "deadline", "context", "cost", "iterations", "length"],
        opened_paths=[x(200)] * 500,
        usage=max_usage("verifier"),
        formatter_usage=max_usage("formatter"),
        error=x(2000),
    )
    assert size(verify) < PAYLOAD_LIMIT
    explore = c.ExploreResult(
        notes=x(65_536),
        state="Completed",
        stop_reason=x(200),
        rescued=True,
        complete=True,
        budget_exhausted=True,
        exhausted_by=["cost"],
        opened_paths=[x(200)] * 500,
        usage=max_usage("explorer"),
        error=x(2000),
    )
    assert size(explore) < PAYLOAD_LIMIT


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (c.Prepared, "inlined_paths"),
        (c.Prepared, "on_demand_paths"),
        (c.Prepared, "reviewable_paths"),
        (c.Prepared, "prior_for_prompt"),
        (c.ExploreResult, "opened_paths"),
        (c.VerifyResult, "opened_paths"),
        (c.FinalReview, "usage"),
        (c.FinalReview, "coverage"),
        (c.ReviewOutcome, "usage"),
    ],
)
def test_path_lists_and_usage_maps_are_bounded(model: type[BaseModel], field: str) -> None:
    # Payload sizes must hold by construction (DESIGN 4): every list or map is capped.
    schema = model.model_json_schema()
    props = schema["properties"][field]
    assert "maxItems" in props or "maxProperties" in props, (model.__name__, field)


def test_usage_map_rejects_too_many_keys() -> None:
    with pytest.raises(ValidationError):
        c.ReviewOutcome(
            status="failed",
            pr_url="https://github.com/o/r/pull/1",
            execution_id="e",
            usage={f"explorer:m{i}": usage() for i in range(c.MAX_USAGE_KEYS + 1)},
        )
