"""Payload sizes: what the activities produce stays bounded, whatever the agents write.

Temporal payloads are limited to about 2 MB. The run-directory design keeps big data on
disk, but the activity inputs still carry the snapshot and the prepared plan, and
``FinalizeInput`` also the merge and the verification (every explorer's result and draft
only by seal). These tests build large, hostile inputs, pass them through the
activities' own bounding functions, and check the sizes stay under 1.9 MB.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from morning_star_review.activities.explore import explore_result
from morning_star_review.activities.structure import (
    DRAFT_MIN_BYTES,
    DRAFTS_TOTAL_BYTES,
    draft_budget,
    fit_draft,
)
from morning_star_review.activities.verify import verify_result
from morning_star_review.core import contract as c
from morning_star_review.core.config import MAX_EXPLORERS, resolve
from morning_star_review.core.notes import parse_verdicts

LIMIT = 1_900_000  # Temporal payloads are limited to ~2 MB; keep a margin
EXPLORE_BUDGET = 160_000
# The largest ensemble: the known models plus one with a maximal-length id.
ENSEMBLE = [*c.DEFAULT_ALLOWED_MODELS, "custom-" + "m" * 93]
VERIFY_BUDGET = 160_000


def size(model: c.Contract) -> int:
    return len(model.model_dump_json().encode("utf-8"))


def x(n: int, ch: str = "x") -> str:
    return ch * n


def _cfg(preset: str = "best") -> c.ResolvedConfig:
    cfg, _ = resolve(
        None,
        c.RunOverrides(preset=preset),  # type: ignore[arg-type]
        c.OperatorPolicy(trust_repo_config_models=True),
    )
    return cfg


def _paths(n: int, width: int = 60) -> list[str]:
    return [f"src/{i:05d}/" + x(width - 10) for i in range(n)]


def _req() -> c.ReviewRequest:
    return c.ReviewRequest(
        pr_url="https://github.com/o/r/pull/1",
        repo="o/r",
        mode="ci",
        policy=c.OperatorPolicy(trust_repo_config_models=True),
        run_label="gha:o/r/1/1",
    )


def _snap(paths: list[str], cfg: c.ResolvedConfig) -> c.PrSnapshot:
    return c.PrSnapshot(
        seal="f" * 64,
        owner="o",
        repo="r",
        number=1,
        html_url="https://github.com/o/r/pull/1",
        title=x(300),
        body_excerpt=x(4000),
        author="a",
        author_association="MEMBER",
        draft=False,
        state="open",
        merged=False,
        head_sha="a" * 40,
        head_repo="o/r",
        base_ref="main",
        base_sha="b" * 40,
        merge_base_sha="c" * 40,
        additions=len(paths),
        deletions=0,
        changed_files=len(paths),
        files=[
            c.PrFile(path=p, status="modified", additions=1, deletions=1, has_patch=True)
            for p in paths
        ],
        files_json_sha256="0" * 64,
        can_post=True,
        bot_login="app[bot]",
        config=cfg,
        config_source="default",
        run_dir="/tmp/run",
    )


def _prep(paths: list[str], cfg: c.ResolvedConfig) -> c.Prepared:
    return c.Prepared(
        seal="f" * 64,
        job_root="/tmp/run",
        repo_dir="/tmp/run/repo",
        scope="full",
        since_sha=None,
        prompt_sha256="0" * 64,
        prompt_chars=80_000,
        inlined_paths=paths[:200],
        on_demand_paths=paths[200:],
        excluded=[],
        reviewable_paths=paths,
        anchors_sha256="0" * 64,
        prior_for_prompt=[f"f{i:012x}" for i in range(30)],
        roles=cfg.roles,
        budgets=cfg.budgets,
        boundary="0123abcd",
    )


def _hostile_explore(model: str, paths: list[str]) -> c.ExploreResult:
    """An agent result far over every bound: notes of 4-byte characters, all paths."""
    res = c.AgentRunResult(
        phase="explore",
        state="Completed",
        stop_reason=x(5000),
        notes=x(300_000, "\U0001f600"),
        complete=True,
        exhausted_by=["deadline", "nonsense"],
        opened_paths=[*paths, *paths],
        usage=c.Usage(model="other", finish_reasons=["stop"] * 200),
        error=x(50_000),
    )
    result = explore_result(res, model, set(paths), [])
    assert result.seal == ""
    return result.model_copy(update={"seal": "f" * 64})


def _max_finding(i: int) -> c.DraftFinding:
    return c.DraftFinding(
        path=x(500),
        line=i + 1,
        end_line=i + 3,
        severity=("critical", "high", "medium", "low")[i % 4],
        category="security",
        title=x(160),
        evidence=x(500),
        explanation=x(4000),
        fix=x(2000),
        suggestion=x(2000),
    )


def _hostile_draft(model: str, explorers: int) -> c.ReviewDraft:
    draft = c.ReviewDraft(
        findings=[_max_finding(i) for i in range(80)],
        prior_status=[c.PriorStatus(id=x(40), status="unsure", reason=x(500))] * 60,
        summary=x(4000),
        source="parser",
        marker_count=80,
        unparsed_blocks=[x(2000)] * 20,
        formatter_usage=c.Usage(model="fmt"),
        explorer_model=model,
    )
    fitted = fit_draft(draft, draft_budget(explorers))
    return fitted.model_copy(update={"seal": "f" * 64})


def _merge(explorers: list[str], text: int) -> c.MergeResult:
    finding = c.DraftFinding(
        path="src/app/module.py",
        line=10,
        severity="high",
        category="correctness",
        title=x(min(text, 160)),
        evidence=x(min(text, 500)),
        explanation=x(text),
        fix=x(min(text, 1000)),
        suggestion=x(min(text, 2000)),
    )
    return c.MergeResult(
        seal="f" * 64,
        candidates=[c.Candidate.from_draft(finding, f"c{i + 1}", explorers) for i in range(60)],
        explorer_models=explorers,
        input_findings=240,
    )


def _hostile_verify(merge: c.MergeResult) -> c.VerifyResult:
    ids = [cand.candidate_id for cand in merge.candidates]
    notes = "\n".join(
        f"VERDICT {i}\nstatus: confirmed\nline: 7\nevidence: {x(600)}\nseverity: low\n"
        f"reason: {x(5000)}\nEND VERDICT"
        for i in ids
    )
    res = c.AgentRunResult(
        phase="verify",
        state="Completed",
        stop_reason=None,
        notes=notes,
        complete=True,
        opened_paths=_paths(3000),
        usage=c.Usage(model="v"),
    )
    out = verify_result(res, notes, parse_verdicts(notes, ids), None, ids, "v", [])
    return out.model_copy(update={"seal": "f" * 64})


def test_agent_outputs_are_bounded_whatever_the_agent_writes() -> None:
    paths = _paths(3000)
    explore = _hostile_explore("m", paths)
    assert size(explore) < EXPLORE_BUDGET
    assert explore.usage.model == "m"
    assert explore.exhausted_by == ["deadline"]
    assert not explore.complete  # the notes were cut
    for n in (1, 3, 5):
        draft = _hostile_draft("m", n)
        assert size(draft) <= draft_budget(n)
        assert draft.source == "parser_lossy"  # findings had to be dropped
        assert all(len(f.explanation) <= 2000 for f in draft.findings)
    verify = _hostile_verify(_merge(["m"], 2000))
    assert len(verify.entries) == 60
    assert size(verify) < VERIFY_BUDGET


def _cfg_with(models: list[str]) -> c.ResolvedConfig:
    """The ``best`` preset with exactly ``models`` as explorers (unknown ids allowed)."""
    policy = c.OperatorPolicy(
        trust_repo_config_models=True, allow_unknown_models=True, allowed_models=models
    )
    cfg, _ = resolve(None, c.RunOverrides(preset="best", explorers=models), policy)
    assert [r.model for r in cfg.roles.explorers] == models
    return cfg


@pytest.mark.parametrize("explorers", [1, 3, MAX_EXPLORERS])
def test_worst_case_activity_inputs_stay_under_the_limit(explorers: int) -> None:
    """MAX_FILES changed files, every explorer at its bounds, 60 maximal candidates.

    ``FinalizeInput`` names the explorers' results and drafts by seal (they stay in
    the run directory), so its size does not grow with the number of explorers.
    """
    models = ENSEMBLE[:explorers]
    cfg = _cfg_with(models)
    paths = _paths(c.MAX_FILES)
    explores = [_hostile_explore(m, paths) for m in models]
    drafts = [_hostile_draft(m, explorers) for m in models]
    merge = _merge(models, 4000)  # more text than trimmed drafts can even produce
    verify = _hostile_verify(merge)
    snap, prep = _snap(paths, cfg), _prep(paths, cfg)
    inputs: list[c.Contract] = [
        c.FinalizeInput(
            req=_req(),
            snap=snap,
            prep=prep,
            explore_seals=[e.seal for e in explores],
            draft_seals=[d.seal for d in drafts],
            merge=merge,
            verify=verify,
        ),
        c.VerifyInput(req=_req(), snap=snap, prep=prep, merge=merge),
        c.MergeInput(req=_req(), prep=prep, drafts=drafts),
        c.StructureInput(req=_req(), prep=prep, explore=explores[0]),
        c.ExploreInput(req=_req(), snap=snap, prep=prep, explorer=cfg.roles.explorers[-1]),
    ]
    for model in inputs:
        assert size(model) < LIMIT, type(model).__name__
    assert sum(size(d) for d in drafts) <= max(DRAFTS_TOTAL_BYTES, explorers * DRAFT_MIN_BYTES)


def test_finalize_input_does_not_grow_with_the_explorers() -> None:
    paths = _paths(100)

    def finalize_input(n: int) -> c.FinalizeInput:
        cfg = _cfg_with(ENSEMBLE[:n])
        return c.FinalizeInput(
            req=_req(),
            snap=_snap(paths, cfg),
            prep=_prep(paths, cfg),
            explore_seals=["f" * 64] * n,
            draft_seals=["e" * 64] * n,
        )

    one, five = finalize_input(1), finalize_input(MAX_EXPLORERS)
    assert size(five) - size(one) < 3_000  # the seals and the role lists only
    with pytest.raises(ValidationError):
        c.FinalizeInput(req=_req(), snap=one.snap, prep=one.prep, explore_seals=["../x"])
