"""Payload sizes: what the activities produce stays bounded, whatever the agents write.

Temporal payloads are limited to about 2 MB. The run-directory design keeps big data on
disk, but ``FinalizeInput`` carries the snapshot, the prepared plan, every explorer's
result and draft, the merge and the verification. These tests build large, hostile
inputs, pass them through the activities' own bounding functions, and check the sizes.
"""

from __future__ import annotations

from morning_star_review.activities.explore import explore_result
from morning_star_review.activities.structure import draft_budget, fit_draft
from morning_star_review.activities.verify import verify_result
from morning_star_review.core import contract as c
from morning_star_review.core.config import resolve
from morning_star_review.core.notes import parse_verdicts

TWO_MB = 2_000_000
EXPLORE_BUDGET = 160_000
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


def test_finalize_input_for_a_3000_file_ensemble_run_fits() -> None:
    cfg = _cfg("best")
    paths = _paths(3000)
    models = [r.model for r in cfg.roles.explorers]
    explores = [_hostile_explore(m, paths) for m in models]
    drafts = [_hostile_draft(m, len(models)) for m in models]
    merge = _merge(models, 1200)  # findings of ordinary length
    inp = c.FinalizeInput(
        req=_req(),
        snap=_snap(paths, cfg),
        prep=_prep(paths, cfg),
        explore=None,  # the workflow sets only the lists in pipeline mode
        draft=None,
        explores=explores,
        drafts=drafts,
        merge=merge,
        verify=_hostile_verify(merge),
    )
    assert size(inp) < TWO_MB
    for model in (
        c.VerifyInput(req=_req(), snap=inp.snap, prep=inp.prep, merge=merge),
        c.MergeInput(req=_req(), prep=inp.prep, drafts=drafts),
        c.StructureInput(req=_req(), prep=inp.prep, explore=explores[0]),
        c.ExploreInput(req=_req(), snap=inp.snap, prep=inp.prep),
    ):
        assert size(model) < TWO_MB, type(model).__name__


def test_finalize_input_with_maximal_candidates_fits_for_a_large_pr() -> None:
    cfg = _cfg("best")
    paths = _paths(1000)
    models = [r.model for r in cfg.roles.explorers]
    merge = _merge(models, 2000)  # every candidate at its maximal text length
    inp = c.FinalizeInput(
        req=_req(),
        snap=_snap(paths, cfg),
        prep=_prep(paths, cfg),
        explore=None,
        draft=None,
        explores=[_hostile_explore(m, paths) for m in models],
        drafts=[_hostile_draft(m, len(models)) for m in models],
        merge=merge,
        verify=_hostile_verify(merge),
    )
    assert size(inp) < TWO_MB
