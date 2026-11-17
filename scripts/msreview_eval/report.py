"""Aggregate run scores and render the evaluation table as Markdown."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import mean

from msreview_eval.cases import CaseKey
from msreview_eval.scoring import RunScore


@dataclass
class Aggregate:
    """Totals over a group of runs (one config x one case, or one config overall)."""

    runs: int = 0
    bug_runs: int = 0  # sum over runs of the number of planted bugs
    detected: int = 0
    inline: int = 0
    detected_by_bug: Counter[str] = field(default_factory=Counter)
    inline_by_bug: Counter[str] = field(default_factory=Counter)
    min_detected: int | None = None
    false_positives: int = 0
    cosmetic_hits: int = 0
    stale_hits: int = 0
    duplicates: int = 0
    anchored: int = 0
    anchors_valid: int = 0
    forbidden_hits: int = 0
    refuted_bugs: int = 0
    passed: int = 0
    errors: int = 0
    complete_known: int = 0
    complete: int = 0
    formatter_known: int = 0
    formatter_used: int = 0
    verdicts: Counter[str] = field(default_factory=Counter)
    cost: list[float] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    input_tokens: list[int] = field(default_factory=list)
    cached_tokens: list[int] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)
    tool_calls: list[int] = field(default_factory=list)

    def add(self, s: RunScore) -> None:
        self.runs += 1
        self.bug_runs += len(s.bug_ids)
        self.detected += s.n_detected
        self.inline += s.n_inline
        self.detected_by_bug.update(b for b, ok in s.detected.items() if ok)
        self.inline_by_bug.update(b for b, ok in s.inline_detected.items() if ok)
        if s.bug_ids:
            self.min_detected = (
                s.n_detected if self.min_detected is None else min(self.min_detected, s.n_detected)
            )
        self.false_positives += len(s.false_positives)
        self.cosmetic_hits += len(s.cosmetic_hits)
        self.stale_hits += len(s.stale_hits)
        self.duplicates += s.duplicates
        self.anchored += s.anchored
        self.anchors_valid += s.anchors_valid
        self.forbidden_hits += len(s.forbidden_hits)
        self.refuted_bugs += len(s.refuted_bugs)
        self.passed += s.passed
        self.errors += s.error is not None
        if s.complete is not None:
            self.complete_known += 1
            self.complete += s.complete
        if s.usage.formatter_used is not None:
            self.formatter_known += 1
            self.formatter_used += s.usage.formatter_used
        self.verdicts[s.verdict or "-"] += 1
        if s.status is not None:
            self.cost.append(s.cost_usd)
            self.input_tokens.append(s.usage.input_tokens)
            self.cached_tokens.append(s.usage.cached_input_tokens)
            self.output_tokens.append(s.usage.output_tokens)
            self.tool_calls.append(s.usage.tool_calls)
        if s.seconds is not None:
            self.seconds.append(s.seconds)


def aggregate(scores: Iterable[RunScore]) -> Aggregate:
    agg = Aggregate()
    for s in scores:
        agg.add(s)
    return agg


def _ratio(num: int, den: int) -> str:
    return f"{num}/{den}" if den else "-"


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.0f}%" if den else "-"


def _mean_int(values: Sequence[int]) -> str:
    return f"{mean(values):,.0f}" if values else "-"


def _mean_money(values: Sequence[float]) -> str:
    return f"${mean(values):.3f}" if values else "-"


def _mean_seconds(values: Sequence[float]) -> str:
    return f"{mean(values):.0f}s" if values else "-"


def _verdicts(counter: Counter[str]) -> str:
    return ", ".join(f"{k} x{v}" for k, v in sorted(counter.items())) or "-"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _group(scores: Iterable[RunScore]) -> dict[tuple[str, str], list[RunScore]]:
    groups: dict[tuple[str, str], list[RunScore]] = {}
    for s in scores:
        groups.setdefault((s.config, s.case), []).append(s)
    return groups


def summary_table(scores: Sequence[RunScore]) -> list[str]:
    lines = [
        "| Config | Runs | Recall | Inline recall | FP >= medium | FP per run | Cosmetic hits"
        " | Anchor validity | Complete | Formatter used | Mean cost | Mean time | Runs passed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    configs = sorted({s.config for s in scores})
    for config in configs:
        agg = aggregate(s for s in scores if s.config == config)
        fp_per_run = f"{agg.false_positives / agg.runs:.2f}" if agg.runs else "-"
        complete = _ratio(agg.complete, agg.complete_known)
        lines.append(
            f"| {_cell(config)} | {agg.runs} | {_ratio(agg.detected, agg.bug_runs)}"
            f" ({_pct(agg.detected, agg.bug_runs)}) | {_ratio(agg.inline, agg.bug_runs)}"
            f" | {agg.false_positives} | {fp_per_run} | {agg.cosmetic_hits}"
            f" | {_pct(agg.anchors_valid, agg.anchored)} | {complete}"
            f" | {_ratio(agg.formatter_used, agg.formatter_known)} | {_mean_money(agg.cost)}"
            f" | {_mean_seconds(agg.seconds)} | {_ratio(agg.passed, agg.runs)} |"
        )
    return lines


def case_table(case: CaseKey, scores: Sequence[RunScore]) -> list[str]:
    bug_ids = [b.id for b in case.bugs]
    head = ["Config", "Runs", *bug_ids, "Min detected", "FP >= medium", "Cosmetic", "Stale"]
    head += ["Anchors valid", "Verdicts", "Tokens in", "Cached", "Out", "Tools", "Cost", "Time"]
    head += ["Passed"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for (config, _case), group in sorted(_group(scores).items()):
        agg = aggregate(group)
        cells = [_cell(config), str(agg.runs)]
        cells += [
            f"{agg.detected_by_bug[b]}/{agg.runs} ({agg.inline_by_bug[b]} inline)" for b in bug_ids
        ]
        cells.append("-" if agg.min_detected is None else f"{agg.min_detected}/{len(bug_ids)}")
        cells += [str(agg.false_positives), str(agg.cosmetic_hits), str(agg.stale_hits)]
        cells += [_ratio(agg.anchors_valid, agg.anchored), _verdicts(agg.verdicts)]
        cells += [_mean_int(agg.input_tokens), _mean_int(agg.cached_tokens)]
        cells += [_mean_int(agg.output_tokens), _mean_int(agg.tool_calls)]
        cells += [_mean_money(agg.cost), _mean_seconds(agg.seconds), _ratio(agg.passed, agg.runs)]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def run_table(scores: Sequence[RunScore]) -> list[str]:
    lines = [
        "| Config | Case | Run | Status | Verdict | Detected | Inline | FP | Anchors | Complete"
        " | Cost | Time | Failed checks |",
        "|---|---|---:|---|---|---|---|---:|---|---|---:|---:|---|",
    ]
    for s in sorted(scores, key=lambda x: (x.config, x.case, x.run)):
        detected = ",".join(b for b, ok in s.detected.items() if ok) or "-"
        inline = ",".join(b for b, ok in s.inline_detected.items() if ok) or "-"
        failed = ",".join(k for k, ok in s.checks.items() if not ok) or "-"
        complete = "-" if s.complete is None else str(s.complete).lower()
        seconds = "-" if s.seconds is None else f"{s.seconds:.0f}s"
        lines.append(
            f"| {_cell(s.config)} | {s.case} | {s.run} | {s.status or '-'} | {s.verdict or '-'}"
            f" | {detected} | {inline} | {len(s.false_positives)}"
            f" | {_ratio(s.anchors_valid, s.anchored)} | {complete} | ${s.cost_usd:.3f}"
            f" | {seconds} | {failed} |"
        )
    return lines


def notes_section(scores: Sequence[RunScore]) -> list[str]:
    """Per-run lists a human should look at: FPs, bad anchors, leaks, errors."""
    out: list[str] = []
    for s in sorted(scores, key=lambda x: (x.config, x.case, x.run)):
        items: list[str] = []
        if s.error:
            items.append(f"error: {s.error}")
        items += [f"false positive: {x}" for x in s.false_positives]
        items += [f"invalid anchor: {x}" for x in s.invalid_anchors]
        items += [f"located but keywords missing: {x}" for x in s.located_only]
        items += [f"refuted by the verifier although real: {x}" for x in s.refuted_bugs]
        items += [f"forbidden text in output: {x}" for x in s.forbidden_hits]
        if s.head_sha_ok is False:
            items.append("head SHA differs from the expected PR head")
        if items:
            out.append(f"- **{_cell(s.config)} / {s.case} / run {s.run}**")
            out += [f"  - {_cell(item)}" for item in items]
    return out or ["- none"]


def render_markdown(
    scores: Sequence[RunScore],
    cases: Mapping[str, CaseKey],
    header: Sequence[str] = (),
) -> str:
    lines = ["# Evaluation results", ""]
    lines += [f"- {h}" for h in header]
    if header:
        lines.append("")
    lines += ["## Summary by configuration", "", *summary_table(scores), ""]
    for case_id in sorted({s.case for s in scores}):
        case = cases[case_id]
        lines += [f"## {case.case.upper()}: {case.name}", ""]
        lines += [_criteria(case), "", *case_table(case, [s for s in scores if s.case == case_id])]
        lines.append("")
    lines += ["## Runs", "", *run_table(scores), ""]
    lines += ["## Items for manual review", "", *notes_section(scores), ""]
    return "\n".join(lines)


def _criteria(case: CaseKey) -> str:
    e = case.expect
    parts = []
    if e.verdicts:
        parts.append("verdict " + " or ".join(e.verdicts))
    if e.verdicts_if_missed:
        parts.append("verdict " + " or ".join(e.verdicts_if_missed) + " when a bug is missed")
    if e.min_detected_per_run is not None:
        parts.append(f">= {e.min_detected_per_run}/{len(case.bugs)} bugs per run")
    if e.max_fp_medium is not None:
        parts.append(f"<= {e.max_fp_medium} findings >= medium that match no bug")
    if e.max_cosmetic_hits is not None:
        parts.append(f"<= {e.max_cosmetic_hits} findings >= medium on cosmetic edits")
    if e.max_cost_usd is not None:
        parts.append(f"cost <= ${e.max_cost_usd:.2f}")
    if e.forbidden_output or e.forbidden_patterns:
        count = len(e.forbidden_output) + len(e.forbidden_patterns)
        parts.append(f"none of {count} injected strings or markers in the rendered output")
    return "Pass criteria per run: " + ("; ".join(parts) or "none") + "."
