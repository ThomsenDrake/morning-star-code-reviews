#!/usr/bin/env python3
"""Opt-in live smoke test of the agent runner: explore, then verify, per model.

Runs entirely on this machine (no Workflows worker): it builds the evaluation
fixture repository (eval/fixtures/ledger plus the E1 change), renders an explorer
prompt like ``prepare`` does, and starts ``python -m morning_star_review.agent`` as a
subprocess for each phase, the way the activities do. It needs the agent venv and a
Mistral API key in MISTRAL_API_KEY; model calls are billed to that key.

    MSREVIEW_LIVE=1 .venv-agent/bin/python scripts/agent_smoke.py \\
        --models zai-glm-5-3 mistral-large-4 --out /tmp/agent-smoke

For each model it reports whether the notes parsed without the formatter, the
findings and which planted bugs they match, tool calls, tokens, cost, seconds and
any SDK errors from the agent log. The key is passed to the agent subprocess only
and is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from morning_star_review.agent import prompts  # noqa: E402
from morning_star_review.core import models  # noqa: E402
from morning_star_review.core.contract import (  # noqa: E402
    AgentBudgets,
    AgentJob,
    AgentRunResult,
    Candidate,
    ChangedFileRef,
    FormatterDraft,
    VerifyDraft,
)
from morning_star_review.core.notes import parse_notes, parse_verdicts  # noqa: E402
from morning_star_review.core.redact import redact  # noqa: E402

FIXTURE = REPO_ROOT / "eval" / "fixtures" / "ledger"
CHANGES = REPO_ROOT / "eval" / "fixtures" / "changes"
DEFAULT_MODELS = ["zai-glm-5-3", "mistral-large-4", "zai-glm-5-3-flash", "deepseek-v4-1-flash-1017"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv"}
TOLERANCE = 3

PR_TITLE = "Plan changes and transaction search"
PR_BODY = (
    "Adds a memo search endpoint with paging, and an endpoint that moves an account to "
    "another plan mid-period and charges the new plan pro rata. Also tidies an error "
    "message and a docstring."
)


@dataclass(frozen=True)
class Bug:
    id: str
    path: str
    lines: tuple[int, int]
    alt: tuple[tuple[str, int, int], ...] = ()


def load_bugs(case_file: Path) -> list[Bug]:
    """Planted bugs from the eval answer key (path and line range only)."""
    import yaml

    data = yaml.safe_load(case_file.read_text())
    bugs = []
    for b in data.get("bugs", []):
        alt = tuple((a["path"], a["lines"][0], a["lines"][1]) for a in b.get("alt_locations", []))
        bugs.append(Bug(b["id"], b["path"], (b["lines"][0], b["lines"][1]), alt))
    return bugs


def matches(bug: Bug, path: str, line: int | None) -> bool:
    if line is None:
        return False
    spots = [(bug.path, *bug.lines), *bug.alt]
    return any(path == p and a - TOLERANCE <= line <= b + TOLERANCE for p, a, b in spots)


# ------------------------------------------------------------------ fixture repo


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
    }
    res = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {res.stderr.strip()[:500]}")
    return res.stdout.strip()


def build_repo(root: Path, patch: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    for src in sorted(FIXTURE.rglob("*")):
        rel = src.relative_to(FIXTURE)
        if src.is_file() and not SKIP_PARTS.intersection(rel.parts) and src.suffix != ".pyc":
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, repo / rel)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "apply", "--ignore-whitespace", "--whitespace=nowarn", str(patch))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


# ------------------------------------------------------------------ prompt and jobs


@dataclass
class Prepared:
    repo: Path
    base: str
    head: str
    boundary: str
    prompt: str
    related_entries: str
    changed: list[ChangedFileRef]
    diffs: dict[str, str]  # job-relative diff file -> rendered hunks


def prepare(root: Path, patch: Path) -> Prepared:
    from morning_star_review.diff import related
    from morning_star_review.diff.patch import local_patches
    from morning_star_review.diff.render import render_file, untrusted_block

    repo, base, head = build_repo(root, patch)
    names = git(repo, "diff", "--name-only", base, head).splitlines()
    patches = local_patches(str(repo), base, head, names)
    boundary = secrets.token_hex(4)
    changed: list[ChangedFileRef] = []
    diffs: dict[str, str] = {}
    rendered: list[str] = []
    rows: list[str] = []
    for i, (path, fp) in enumerate(sorted(patches.items())):
        text = render_file(fp)
        diffs[f"diffs/{i}.txt"] = text
        rendered.append(text)
        changed.append(
            ChangedFileRef(
                path=path,
                previous_path=fp.previous_path,
                status=fp.status,
                diff_file=f"diffs/{i}.txt",
            )
        )
        rows.append(f"{path} | {fp.status} | +{fp.added} -{fp.removed} | inline")
    defs = related.collect(str(repo), list(patches.values()), set(patches), rev=head)
    related_block = related.render_block(defs, boundary)
    related_entries = related.render_entries(defs)
    pr = untrusted_block(
        boundary, "pull_request", f"title: {PR_TITLE}\nauthor: smoke-test\n\n{PR_BODY}"
    )
    scope = "\n".join(
        [
            "<review_scope>",
            "scope: full",
            f"changed files ({len(rows)} inline, 0 via show_file_diff;"
            " path | status | +added -removed | where to read):",
            *rows,
            "</review_scope>",
        ]
    )
    diff_block = untrusted_block(boundary, "diff", "\n".join(t.rstrip("\n") for t in rendered))
    sections = [pr, scope, diff_block, related_block, "Budget: about {max_tool_calls} tool calls."]
    prompt = "\n".join(s for s in sections if s) + "\n"
    return Prepared(repo, base, head, boundary, prompt, related_entries, changed, diffs)


def budgets_for(phase: str, model: str, cost_cap: float) -> AgentBudgets:
    if phase == "verify":
        tool_calls, seconds = 30, 8 * 60
    elif phase == "format":
        tool_calls, seconds = 0, 240
    else:
        tool_calls, seconds = models.tool_call_limit(model, 60), 15 * 60
    return AgentBudgets(
        max_tool_calls=tool_calls,
        max_llm_calls=4 if phase == "format" else 90,
        grace_llm_calls=2,
        explore_seconds=seconds,
        grace_seconds=120,
        rescue_seconds=180,
        max_context_tokens=140_000,
        auto_compact_threshold=200_000,
        max_tool_output_chars=24_000,
        max_cost_usd=cost_cap,
    )


def write_job(
    job_dir: Path,
    phase: str,
    prep: Prepared,
    model: str,
    effort: str,
    text: str,
    cost_cap: float,
    *,
    format_target: str = "review",
    candidates: list[Candidate] | None = None,
) -> None:
    job_dir.mkdir(parents=True)
    for rel, body in prep.diffs.items():
        (job_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (job_dir / rel).write_text(body)
    input_name = "notes.txt" if phase == "format" else "prompt.txt"
    (job_dir / input_name).write_text(text)
    if candidates is not None:
        data = [c.model_dump(mode="json") for c in candidates]
        (job_dir / "candidates.json").write_text(json.dumps(data, indent=1))
    price_in, price_out = models.price(model)
    job = AgentJob(
        phase=phase,  # type: ignore[arg-type]
        execution_id=f"msreview-smoke-{secrets.token_hex(3)}",
        repo_dir=str(prep.repo),
        head_sha=prep.head,
        merge_base_sha=prep.base,
        input_path=input_name,
        input_sha256=hashlib.sha256(text.encode()).hexdigest(),
        variant="full",
        model=model,
        effort=effort,  # type: ignore[arg-type]
        budgets=budgets_for(phase, model, cost_cap),
        price_in_per_m=price_in,
        price_out_per_m=price_out,
        changed_files=prep.changed,
        meter_path="meter.json",
        coverage_path="coverage.json",
        result_path="result.json",
        partial_notes_path="notes.partial.txt",
        format_target=format_target,  # type: ignore[arg-type]
    )
    (job_dir / "job.json").write_text(job.model_dump_json(indent=1))


def agent_env(home: Path) -> dict[str, str]:
    """Allowlisted environment, as the launcher builds it (DESIGN 6.8)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "MISTRAL_API_KEY": os.environ["MISTRAL_API_KEY"],
        "AGENTS_SDK_TELEMETRY_ENABLED": "0",
        "AGENTS_SDK_STUDIO_TRACING_ENABLED": "0",
        "OTEL_ENABLED": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def run_agent(phase: str, job_dir: Path, timeout: float) -> tuple[int, AgentRunResult | None, str]:
    home = job_dir / "home"
    home.mkdir()
    log_path = job_dir / "agent.log"
    with log_path.open("wb") as log:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "morning_star_review.agent", phase, str(job_dir)],
                cwd=home,
                env=agent_env(home),
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = -9
    log_text = redact(log_path.read_text(errors="replace"), [os.environ["MISTRAL_API_KEY"]])
    log_path.write_text(log_text)
    result_path = job_dir / "result.json"
    result = None
    if result_path.exists():
        result = AgentRunResult.model_validate_json(result_path.read_text())
    return code, result, log_text


def sdk_errors(log_text: str) -> list[str]:
    keys = ("Traceback", "Error", "error:", "warning", "status_code=429", "crash")
    lines = [ln.strip() for ln in log_text.splitlines() if any(k in ln for k in keys)]
    return [ln[:240] for ln in lines if "catalogue" not in ln][:10]


# ------------------------------------------------------------------ one model


@dataclass
class Report:
    model: str
    explore: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _phase_summary(code: int, res: AgentRunResult | None, seconds: float) -> dict[str, Any]:
    if res is None:
        return {"exit": code, "state": "no result", "wall_seconds": round(seconds, 1)}
    u = res.usage
    return {
        "exit": code,
        "state": res.state,
        "stop_reason": res.stop_reason,
        "complete": res.complete,
        "rescued": res.rescued,
        "budget_exhausted": res.budget_exhausted,
        "exhausted_by": res.exhausted_by,
        "llm_calls": u.llm_calls,
        "tool_calls": u.tool_calls,
        "input_tokens": u.input_tokens,
        "cached_input_tokens": u.cached_input_tokens,
        "output_tokens": u.output_tokens,
        "cost_usd": round(u.cost_usd, 4),
        "seconds": round(u.seconds, 1),
        "wall_seconds": round(seconds, 1),
        "opened": len(res.opened_paths),
        "error": res.error,
    }


def _format(
    job_dir: Path,
    prep: Prepared,
    notes: str,
    model: str,
    cost_cap: float,
    target: str,
    candidates: list[Candidate] | None = None,
) -> tuple[AgentRunResult | None, str]:
    write_job(
        job_dir,
        "format",
        prep,
        model,
        "low",
        notes,
        cost_cap,
        format_target=target,
        candidates=candidates,
    )
    _code, res, log = run_agent("format", job_dir, timeout=400)
    return res, log


def run_model(
    model: str,
    prep: Prepared,
    out: Path,
    bugs: list[Bug],
    cost_cap: float,
    fmt_model: str,
    verify: bool,
) -> Report:
    rep = Report(model=model)
    base = out / model
    explore_budget = models.tool_call_limit(model, 60)
    prompt = prep.prompt.replace("{max_tool_calls}", str(explore_budget))
    write_job(base / "explore", "explore", prep, model, "high", prompt, cost_cap)
    t0 = time.time()
    code, res, log = run_agent("explore", base / "explore", timeout=15 * 60 + 420)
    rep.explore = _phase_summary(code, res, time.time() - t0)
    rep.errors += sdk_errors(log)
    notes = (res.notes if res else None) or ""
    parsed = parse_notes(notes)
    rep.explore["notes_parse_ok"] = parsed.lossless and bool(notes.strip())
    rep.explore["markers"] = parsed.marker_count
    rep.explore["unparsed_blocks"] = len(parsed.unparsed)
    findings = list(parsed.findings)
    if notes.strip() and not parsed.lossless:
        fres, flog = _format(base / "format", prep, notes, fmt_model, cost_cap, "review")
        rep.errors += sdk_errors(flog)
        rep.explore["formatter"] = fres.state if fres else "no result"
        if fres and fres.draft:
            known = {(f.path, f.line) for f in findings}
            for f in FormatterDraft.model_validate(fres.draft).findings:
                if (f.path, f.line) not in known:
                    findings.append(f)
    for f in findings:
        hit = [b.id for b in bugs if matches(b, f.path, f.line)]
        rep.findings.append(
            {
                "path": f.path,
                "line": f.line,
                "severity": f.severity,
                "title": f.title,
                "bugs": hit,
                "suggestion": bool(f.suggestion),
            }
        )
        rep.bugs_found += [b for b in hit if b not in rep.bugs_found]
    if not verify or not findings:
        return rep
    candidates = [
        Candidate.from_draft(f, f"c{i + 1}", [model]) for i, f in enumerate(findings[:60])
    ]
    vprompt = prompts.render_verify_prompt(
        candidates,
        boundary=prep.boundary,
        max_tool_calls=30,
        related_definitions=prep.related_entries,
        pr_header=f"title: {PR_TITLE}\n\n{PR_BODY}",
    )
    write_job(
        base / "verify", "verify", prep, model, "high", vprompt, cost_cap, candidates=candidates
    )
    t0 = time.time()
    code, vres, vlog = run_agent("verify", base / "verify", timeout=8 * 60 + 420)
    rep.verify = _phase_summary(code, vres, time.time() - t0)
    rep.errors += sdk_errors(vlog)
    ids = [c.candidate_id for c in candidates]
    vnotes = (vres.notes if vres else None) or ""
    verdicts = parse_verdicts(vnotes, ids)
    statuses = {e.candidate_id: e.status for e in verdicts.entries}
    rep.verify["verdicts_parse_ok"] = not verdicts.lossy and bool(vnotes.strip())
    if verdicts.lossy and vnotes.strip():
        fres, flog = _format(
            base / "verify-format", prep, vnotes, fmt_model, cost_cap, "verify", candidates
        )
        rep.errors += sdk_errors(flog)
        rep.verify["formatter"] = fres.state if fres else "no result"
        if fres and fres.draft:
            for e in VerifyDraft.model_validate(fres.draft).entries:
                statuses.setdefault(e.candidate_id, e.status)
    rep.verify["unaddressed"] = [i for i in ids if i not in statuses]
    for cid, finding in zip(ids, rep.findings, strict=False):
        finding["verdict"] = statuses.get(cid, "uncertain")
    return rep


MESSY_NOTES = """Review of the plan-change PR.

1. **ledger/service.py, line 118** (HIGH): `money.prorate(new_plan.monthly_cents, period_days,
   remaining_days)` passes the day counts in the wrong order; prorate wants used days first.
   Fix: swap the two arguments.

FINDING 2:
path: ledger/pagination.py
line: 84-85
severity: High
title: first page skipped
evidence: start = request.page * request.per_page
why: pages start at 1
(no END line)

PRIOR f0123456789a: fixed - the old message was reworded
SUMMARY: two problems.
"""

MESSY_VERDICTS = """c1 is real: the call swaps used and period days (confirmed).
VERDICT c2
status: probably wrong
reason: page 1 maps to offset 0 elsewhere... actually the code is fine? not sure
c3 - refuted, the term is bound as a parameter.
"""


def format_check(prep: Prepared, out: Path, model: str, cost_cap: float) -> dict[str, Any]:
    """Run the formatter on messy notes and messy verdicts with ``model`` (effort low)."""
    report: dict[str, Any] = {"model": model}
    res, log = _format(out / model / "format-review", prep, MESSY_NOTES, model, cost_cap, "review")
    draft = FormatterDraft.model_validate(res.draft) if res and res.draft else None
    report["review"] = {
        "state": res.state if res else "no result",
        "findings": len(draft.findings) if draft else None,
        "priors": len(draft.prior_status) if draft else None,
        "llm_calls": res.usage.llm_calls if res else None,
        "seconds": round(res.usage.seconds, 1) if res else None,
        "error": res.error if res else None,
    }
    cands = [
        Candidate(
            candidate_id=f"c{i}",
            votes=[model],
            path="ledger/service.py",
            line=118,
            severity="high",
            category="correctness",
            title=f"candidate {i}",
            evidence="x",
            explanation="y",
        )
        for i in (1, 2, 3)
    ]
    res, log2 = _format(
        out / model / "format-verify", prep, MESSY_VERDICTS, model, cost_cap, "verify", cands
    )
    vdraft = VerifyDraft.model_validate(res.draft) if res and res.draft else None
    report["verify"] = {
        "state": res.state if res else "no result",
        "entries": {e.candidate_id: e.status for e in vdraft.entries} if vdraft else None,
        "llm_calls": res.usage.llm_calls if res else None,
        "error": res.error if res else None,
    }
    report["errors"] = sdk_errors(log) + sdk_errors(log2)
    return report


# ------------------------------------------------------------------ report and main


def render_table(reports: list[Report], bugs: list[Bug]) -> str:
    head = (
        "| model | notes parse ok | findings | bugs | explore tools / calls"
        " | tokens in (cached) / out | cost | seconds"
        " | verify (confirmed/refuted/uncertain) | verify tools | errors |"
    )
    rows = [head, "|" + "---|" * 11]
    for r in reports:
        e, v = r.explore, r.verify
        verdicts = [f.get("verdict") for f in r.findings]
        vtext = (
            f"{verdicts.count('confirmed')}/{verdicts.count('refuted')}/"
            f"{verdicts.count('uncertain')} ({v.get('state', '-')}, complete={v.get('complete')})"
            if v
            else "-"
        )
        rows.append(
            f"| {r.model} | {e.get('notes_parse_ok')} ({e.get('state')}, "
            f"complete={e.get('complete')}, rescued={e.get('rescued')}) | {len(r.findings)} | "
            f"{len(r.bugs_found)}/{len(bugs)} {','.join(sorted(r.bugs_found))} | "
            f"{e.get('tool_calls')} / {e.get('llm_calls')} | "
            f"{e.get('input_tokens')} ({e.get('cached_input_tokens')}) / "
            f"{e.get('output_tokens')} | "
            f"${(e.get('cost_usd') or 0) + (v.get('cost_usd') or 0):.3f} | "
            f"{e.get('seconds')} + {v.get('seconds', 0)} | {vtext} | {v.get('tool_calls', '-')} | "
            f"{len(r.errors)} |"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument(
        "--out", type=Path, default=Path(tempfile.gettempdir()) / "msreview-agent-smoke"
    )
    ap.add_argument("--case", type=Path, default=REPO_ROOT / "eval" / "cases" / "e1-planted.yaml")
    ap.add_argument("--patch", type=Path, default=CHANGES / "e1-planted.patch")
    ap.add_argument("--cost-cap", type=float, default=3.0, help="USD per phase")
    ap.add_argument("--formatter-model", default="mistral-large-4")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--parallel", type=int, default=1, help="models run at the same time")
    ap.add_argument(
        "--format-check", action="store_true", help="only run the formatter on messy notes"
    )
    args = ap.parse_args(argv)
    if os.environ.get("MSREVIEW_LIVE") != "1":
        print("opt-in: set MSREVIEW_LIVE=1 to make live model calls", file=sys.stderr)
        return 2
    if not os.environ.get("MISTRAL_API_KEY"):
        print("MISTRAL_API_KEY is not set", file=sys.stderr)
        return 2
    out: Path = args.out / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    bugs = load_bugs(args.case)
    prep = prepare(out, args.patch)
    (out / "prompt.txt").write_text(prep.prompt)
    print(
        f"fixture ready: {len(prep.changed)} changed files, prompt {len(prep.prompt)} chars,"
        f" related definitions {len(prep.related_entries)} chars; output in {out}",
        flush=True,
    )

    if args.format_check:
        checks = [format_check(prep, out, m, args.cost_cap) for m in args.models]
        (out / "format-check.json").write_text(json.dumps(checks, indent=1))
        print(json.dumps(checks, indent=1))
        return 0

    def one(model: str) -> Report:
        try:
            rep = run_model(
                model, prep, out, bugs, args.cost_cap, args.formatter_model, not args.no_verify
            )
        except Exception as exc:  # report and go on with the other models
            rep = Report(model=model, errors=[f"{type(exc).__name__}: {exc}"[:500]])
        print(
            f"done: {model}: {len(rep.findings)} findings, bugs {sorted(rep.bugs_found)}",
            flush=True,
        )
        return rep

    if args.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            reports = list(pool.map(one, args.models))
    else:
        reports = [one(m) for m in args.models]
    data = [r.__dict__ for r in reports]
    (out / "report.json").write_text(json.dumps(data, indent=1))
    table = render_table(reports, bugs)
    (out / "report.md").write_text(table + "\n")
    print(table)
    for r in reports:
        print(f"\n## {r.model}")
        for f in r.findings:
            print(
                f"- {f['path']}:{f['line']} [{f['severity']}] {f['title']} "
                f"bugs={f['bugs']} verdict={f.get('verdict', '-')} suggestion={f['suggestion']}"
            )
        for err in r.errors:
            print(f"  error: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
