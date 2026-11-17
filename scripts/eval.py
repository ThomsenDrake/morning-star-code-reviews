#!/usr/bin/env python3
"""Run the review evaluation and render the results table.

Two subcommands:

``run``    runs ``msreview review <PR> --post never --json`` for every
           configuration x case x run, stores stdout/stderr/metadata in a raw
           directory, then scores everything (like ``score``). It is the
           default: ``eval.py --models ... --runs 3`` means ``eval.py run ...``.
``score``  scores an existing raw directory and writes the Markdown table
           (plus a JSON file with every run score next to it).

The PR numbers come from the JSON that ``scripts/make_test_prs.py`` prints;
its ``head_sha`` per case is passed as ``--expected-head-sha`` so every run
reviews the commit the answer key describes. ``--runs`` applies to every case;
without it a case's own ``eval.runs`` (E6: 1) or 3 is used.
The rendered table never contains the repository name; raw outputs do, so
they are written outside the repository by default.

Examples::

    python scripts/eval.py run --prs prs.json --models zai-glm-5-3,mistral-large-4 \\
        --presets best,balanced --runs 3 --out eval/results/2026-11-20.md
    python scripts/eval.py score --raw-dir ~/.cache/morning-star-review/eval/<stamp> \\
        --out eval/results/2026-11-20.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from msreview_eval.build import head_tree
from msreview_eval.cases import CaseKey, load_cases, select_cases
from msreview_eval.report import render_markdown
from msreview_eval.scoring import RunScore, score_run

DEFAULT_RAW_ROOT = Path.home() / ".cache" / "morning-star-review" / "eval"
DEFAULT_RUNS = 3  # per case when neither --runs nor the case's `eval.runs` says otherwise


@dataclass(frozen=True)
class Config:
    label: str  # shown in the table, e.g. "model:zai-glm-5-3" or "preset:best"
    args: tuple[str, ...]  # extra CLI arguments for `msreview review`

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", self.label)


@dataclass(frozen=True)
class RunSpec:
    config: Config
    case: CaseKey
    run: int
    pr_url: str
    expected_head_sha: str | None


def parse_config(spec: str, effort: str) -> Config:
    """``model:<id>`` (single explorer with the shipped formatter) or ``preset:<name>``."""
    kind, _, value = spec.partition(":")
    if not value:
        raise SystemExit(f"bad config {spec!r}: use model:<id> or preset:<name>")
    if kind == "model":
        return Config(spec, ("--preset", "single", "--explorer-model", value, "--effort", effort))
    if kind == "preset":
        return Config(spec, ("--preset", value))
    raise SystemExit(f"bad config {spec!r}: use model:<id> or preset:<name>")


def configs_from_args(args: argparse.Namespace) -> list[Config]:
    specs = [f"model:{m}" for m in _split(args.models)]
    specs += [f"preset:{p}" for p in _split(args.presets)]
    specs += list(args.config or [])
    if not specs:
        raise SystemExit("nothing to run: give --models, --presets or --config")
    seen: dict[str, Config] = {}
    for spec in specs:
        seen.setdefault(spec, parse_config(spec, args.effort))
    return list(seen.values())


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_command(
    msreview: str, spec: RunSpec, report_dir: Path, extra: Sequence[str], force: bool
) -> list[str]:
    cmd = [msreview, "review", spec.pr_url, "--post", "never", "--json"]
    cmd += ["--out", str(report_dir)]
    if spec.expected_head_sha:
        # Review exactly the commit the answer key describes; a moved PR head is
        # refused instead of being scored against the wrong code.
        cmd += ["--expected-head-sha", spec.expected_head_sha]
    if force:
        cmd.append("--force")
    return [*cmd, *spec.config.args, *extra]


def parse_outcome(stdout: str) -> dict[str, Any] | None:
    """The JSON outcome printed with ``--json`` (tolerates log lines around it)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
        # The object starts at the beginning of a line; earlier log lines may hold "{".
        for match in re.finditer(r"^\{", text, re.MULTILINE):
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.start() :])
                break
            except json.JSONDecodeError:
                continue
    return value if isinstance(value, dict) else None


def run_cli(cmd: Sequence[str], timeout_s: float, grace_s: float = 30.0) -> tuple[str, str, int]:
    """Run the CLI in its own process group; on timeout stop the whole group.

    SIGTERM first so that msreview can cancel its execution and stop its agent
    subprocesses, then SIGKILL after ``grace_s``. Raises ``TimeoutExpired`` with
    the output collected so far.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=grace_s)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(list(cmd), timeout_s, stdout, stderr) from None


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def run_paths(raw_dir: Path, spec: RunSpec) -> tuple[Path, Path, Path]:
    folder = raw_dir / spec.config.slug / spec.case.case
    stem = f"run-{spec.run}"
    return folder / f"{stem}.json", folder / f"{stem}.stderr.txt", folder / f"{stem}.meta.json"


def report_dir_for(raw_dir: Path, spec: RunSpec) -> Path:
    """The ``--out`` directory of one run (next to its raw outputs)."""
    return raw_dir / spec.config.slug / spec.case.case / f"run-{spec.run}-reports"


def execute_run(
    spec: RunSpec,
    *,
    msreview: str,
    raw_dir: Path,
    extra: Sequence[str],
    force: bool,
    timeout_s: float,
) -> None:
    """Run the CLI once and store its outputs; never raises for a failed review."""
    out_path, err_path, meta_path = run_paths(raw_dir, spec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(msreview, spec, report_dir_for(raw_dir, spec), extra, force)
    meta: dict[str, Any] = {
        "config": spec.config.label,
        "case": spec.case.case,
        "run": spec.run,
        "argv": cmd[1:],  # no secrets: keys come from the environment
        "expected_head_sha": spec.expected_head_sha,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    started = time.monotonic()
    try:
        stdout, stderr, meta["exit_code"] = run_cli(cmd, timeout_s)
    except subprocess.TimeoutExpired as exc:
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
        meta["exit_code"] = None
        meta["error"] = f"timed out after {timeout_s:.0f}s"
    meta["seconds"] = round(time.monotonic() - started, 1)
    out_path.write_text(stdout, encoding="utf-8")
    err_path.write_text(stderr, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(
        f"eval: {spec.config.label} {spec.case.case} run {spec.run}:"
        f" exit={meta['exit_code']} {meta['seconds']}s",
        file=sys.stderr,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def load_prs(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data["repo"]), dict(data["cases"])


def runs_for(case: CaseKey, runs: int | None) -> int:
    """Runs of ``case``: an explicit ``--runs`` wins for every case, then the case's
    own ``eval.runs`` (E6 runs once by default), then ``DEFAULT_RUNS``."""
    if runs is not None:
        return runs
    return case.eval.runs or DEFAULT_RUNS


def plan_runs(
    configs: Sequence[Config],
    cases: Sequence[CaseKey],
    runs: int | None,
    repo: str,
    prs: Mapping[str, Mapping[str, Any]],
) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for case in cases:
        entry = prs.get(case.case) or {}
        number = case.pr or entry.get("number")
        if not number:
            raise SystemExit(f"no PR number for case {case.case}; run make_test_prs.py first")
        url = f"https://github.com/{repo}/pull/{int(number)}"
        sha = _opt_sha(entry.get("head_sha"))
        if sha is None:
            print(
                f"eval: no head SHA for case {case.case} (give --prs from make_test_prs.py);"
                " its runs review whatever the PR head is",
                file=sys.stderr,
            )
        for config in configs:
            for run in range(1, runs_for(case, runs) + 1):
                specs.append(RunSpec(config, case, run, url, sha))
    return specs


def _opt_sha(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", text):
        raise SystemExit(f"bad head_sha {text[:50]!r} in --prs: expected 40 hex characters")
    return text


def score_raw_dir(raw_dir: Path, cases: Mapping[str, CaseKey]) -> list[RunScore]:
    """Score every ``*.meta.json`` under ``raw_dir``."""
    heads: dict[str, dict[str, list[str]]] = {}
    scores: list[RunScore] = []
    for meta_path in sorted(raw_dir.rglob("run-*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        case = cases.get(str(meta.get("case")))
        if case is None:
            continue
        if case.case not in heads:
            heads[case.case] = {p: t.splitlines() for p, t in head_tree(case).items()}
        stem = meta_path.name.removesuffix(".meta.json")
        out_path = meta_path.with_name(stem + ".json")
        err_path = meta_path.with_name(stem + ".stderr.txt")
        stdout = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        stderr = err_path.read_text(encoding="utf-8") if err_path.is_file() else ""
        scores.append(
            score_run(
                case,
                parse_outcome(stdout),
                config=str(meta.get("config")),
                run=int(meta.get("run") or 0),
                stderr=stderr,
                head=heads[case.case],
                meta=meta,
            )
        )
    return scores


# Flags that change budgets or limits: their presence means "not the shipped budgets".
_BUDGET_FLAGS = (
    "--max-cost-usd",
    "--max-tool-calls",
    "--explorer-minutes",
    "--verifier-max-tool-calls",
    "--verifier-minutes",
    "--deadline-minutes",
)


def load_metas(raw_dir: Path) -> list[dict[str, Any]]:
    """Every run's metadata under ``raw_dir`` (sorted by path)."""
    metas: list[dict[str, Any]] = []
    for meta_path in sorted(raw_dir.rglob("run-*.meta.json")):
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            metas.append(data)
    return metas


def mode_of(argv: Sequence[str]) -> str:
    """How one run was made, from its recorded ``msreview review`` argv."""
    args = [str(a) for a in argv]
    post = "auto"
    if "--post" in args and args.index("--post") + 1 < len(args):
        post = args[args.index("--post") + 1]
    parts = ["report (`--post never`)" if post == "never" else f"posting (`--post {post}`)"]
    parts.append("in-process (`--local`)" if "--local" in args else "Workflows worker")
    budgets = sorted({a.split("=", 1)[0] for a in args} & set(_BUDGET_FLAGS))
    parts.append(
        f"custom budgets ({', '.join(f'`{b}`' for b in budgets)})"
        if budgets
        else "production prompts, tools and budgets"
    )
    return ", ".join(parts)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def describe_runs(metas: Sequence[Mapping[str, Any]]) -> list[str]:
    """Header lines saying what was run: configurations, cases with their run
    counts, and the mode (both ``run`` and ``score`` write them)."""
    configs = _unique(str(m.get("config")) for m in metas)
    cases = _unique(str(m.get("case")) for m in metas)
    per_pair: dict[tuple[str, str], int] = {}
    for m in metas:
        key = (str(m.get("case")), str(m.get("config")))
        per_pair[key] = per_pair.get(key, 0) + 1
    counts = []
    for case in cases:
        ns = sorted({n for (c, _), n in per_pair.items() if c == case})
        counts.append(f"{case} x{ns[0]}" if len(ns) == 1 else f"{case} x{ns[0]}-{ns[-1]}")
    modes = _unique(mode_of(m.get("argv") or ()) for m in metas)
    pinned = sum("--expected-head-sha" in (m.get("argv") or ()) for m in metas)
    return [
        f"configs: {', '.join(configs) or '-'}",
        f"cases (runs per configuration): {', '.join(counts) or '-'}",
        f"mode: {'; '.join(modes) or '-'}",
        f"head SHA pinned (`--expected-head-sha`): {pinned}/{len(metas)} runs",
    ]


def repo_from_raw(raw_dir: Path) -> str | None:
    """The ``owner/name`` the raw runs reviewed (read from the recorded argv)."""
    for meta_path in sorted(raw_dir.rglob("run-*.meta.json")):
        argv = json.loads(meta_path.read_text(encoding="utf-8")).get("argv") or []
        for arg in argv:
            match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/\d+", str(arg))
            if match:
                return match.group(1)
    return None


def redact_repo(text: str, repo: str | None) -> str:
    """Belt and braces: the committed table must not name the test repository."""
    if not repo:
        return text
    name = repo.partition("/")[2]
    for secret in (repo, name):  # the owner alone is public and harmless
        if secret:
            text = re.sub(re.escape(secret), "<test-repo>", text, flags=re.IGNORECASE)
    return text


def write_results(
    scores: Sequence[RunScore],
    cases: Mapping[str, CaseKey],
    out: Path,
    header: Sequence[str],
    repo: str | None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(scores, cases, header)
    out.write_text(redact_repo(markdown, repo), encoding="utf-8")
    payload = json.dumps([asdict(s) for s in scores], indent=2, sort_keys=True)
    out.with_suffix(".json").write_text(redact_repo(payload, repo) + "\n", encoding="utf-8")
    print(f"eval: wrote {out} ({len(scores)} runs)", file=sys.stderr)


def _version(msreview: str) -> str:
    try:
        proc = subprocess.run(
            [msreview, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run msreview on the eval PRs, then score")
    run.add_argument("--prs", type=Path, help="JSON printed by make_test_prs.py")
    run.add_argument("--repo", help="owner/name (default: the repo named in --prs)")
    run.add_argument("--models", help="comma-separated explorer models (preset single)")
    run.add_argument("--presets", help="comma-separated presets, e.g. best,balanced")
    run.add_argument("--config", action="append", help="raw config: model:<id> or preset:<name>")
    run.add_argument("--effort", default="high", help="explorer effort for model configs")
    run.add_argument(
        "--runs",
        type=int,
        default=None,
        help=f"runs per case and config, for every case (default: the case's own "
        f"`eval.runs`, else {DEFAULT_RUNS})",
    )
    run.add_argument("--parallel", type=int, default=1, help="concurrent CLI runs")
    run.add_argument("--timeout-minutes", type=float, default=50.0)
    run.add_argument("--msreview", default=shutil.which("msreview") or "msreview")
    run.add_argument("--extra-arg", action="append", default=[], help="passed to every run")
    run.add_argument("--no-force", action="store_true", help="do not pass --force")
    run.add_argument("--resume", action="store_true", help="skip runs that already have output")
    run.add_argument("--dry-run", action="store_true", help="print the commands only")

    score = sub.add_parser("score", help="score an existing raw directory")
    score.add_argument("--repo", help="owner/name to redact from the table")

    for p in (run, score):
        p.add_argument("--cases", help="comma-separated case ids (default: the eval set)")
        p.add_argument("--raw-dir", type=Path, help="where raw CLI outputs live")
        p.add_argument("--out", type=Path, help="Markdown table to write")
    return parser


def _cmd_run(args: argparse.Namespace, cases: dict[str, CaseKey]) -> int:
    if args.resume and args.raw_dir is None:
        raise SystemExit("--resume needs --raw-dir (the directory of the run to continue)")
    if args.prs is not None:
        repo_from_file, prs = load_prs(args.prs)
    elif args.repo:
        repo_from_file, prs = args.repo, {}  # PR numbers from the answer keys' `pr`
    else:
        raise SystemExit("run needs --prs (output of make_test_prs.py) or --repo")
    repo = args.repo or repo_from_file
    selected = select_cases(cases, _split(args.cases))
    configs = configs_from_args(args)
    if args.runs is not None and args.runs < 1:
        raise SystemExit("--runs must be at least 1")
    specs = plan_runs(configs, selected, args.runs, repo, prs)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = args.raw_dir or DEFAULT_RAW_ROOT / stamp
    force = not args.no_force
    if args.dry_run:
        for spec in specs:
            report_dir = report_dir_for(raw_dir, spec)
            print(" ".join(build_command(args.msreview, spec, report_dir, args.extra_arg, force)))
        return 0
    todo = [s for s in specs if not (args.resume and run_paths(raw_dir, s)[2].is_file())]
    print(f"eval: {len(todo)} of {len(specs)} runs -> {raw_dir}", file=sys.stderr)

    def one(spec: RunSpec) -> None:
        execute_run(
            spec,
            msreview=args.msreview,
            raw_dir=raw_dir,
            extra=args.extra_arg,
            force=force,
            timeout_s=args.timeout_minutes * 60,
        )

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(one, todo))
    scores = score_raw_dir(raw_dir, cases)
    out = args.out or Path("eval/results") / f"{datetime.now(UTC).date().isoformat()}.md"
    header = [
        f"date: {datetime.now(UTC).date().isoformat()}",
        f"msreview: {_version(args.msreview)}",
        *describe_runs(load_metas(raw_dir)),
    ]
    write_results(scores, cases, out, header, repo)
    return 0


def _cmd_score(args: argparse.Namespace, cases: dict[str, CaseKey]) -> int:
    if args.raw_dir is None:
        raise SystemExit("score needs --raw-dir")
    wanted = set(_split(args.cases))
    scores = [s for s in score_raw_dir(args.raw_dir, cases) if not wanted or s.case in wanted]
    out = args.out or args.raw_dir / "results.md"
    metas = [
        m
        for m in load_metas(args.raw_dir)
        if str(m.get("case")) in cases and (not wanted or m.get("case") in wanted)
    ]
    header = [
        f"scored: {datetime.now(UTC).date().isoformat()}",
        f"runs: {len(scores)}",
        *describe_runs(metas),
    ]
    write_results(scores, cases, out, header, args.repo or repo_from_raw(args.raw_dir))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # DESIGN's interface is `eval.py --models ... --cases ... --runs 3 --out ...`: no
    # subcommand means `run`.
    if raw and raw[0].startswith("-") and raw[0] not in ("-h", "--help"):
        raw.insert(0, "run")
    args = _parser().parse_args(raw)
    cases = load_cases()
    if args.command == "run":
        return _cmd_run(args, cases)
    return _cmd_score(args, cases)


if __name__ == "__main__":
    sys.exit(main())
