"""Result reporting after a run: stderr lines, report files and GitHub Actions outputs.

Only text this package composes is printed to the Actions log. The report Markdown
(which contains model-written text) goes to files and the step summary, never to the
log, so it cannot inject workflow commands.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from morning_star_review.core.contract import ReviewOutcome
from morning_star_review.core.jsonio import (
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)

if TYPE_CHECKING:
    from morning_star_review.driver import DriverConfig

__all__ = [
    "REPORT_FILES",
    "StopCommands",
    "copy_reports",
    "emit",
    "emit_published",
    "escape_command",
    "open_findings",
    "result_line",
    "usage_line",
    "write_github_output",
    "write_step_summary",
]

REPORT_FILES = ("report.md", "findings.json", "run.json")
DEFERRED_NOTE = "posting in the next step"
STEP_SUMMARY_LIMIT = 900_000  # GitHub's limit is 1 MiB per step


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def open_findings(outcome: ReviewOutcome) -> int:
    """Open findings, as the verdict counts them (possible issues do not count)."""
    return len(outcome.counted_findings())


def usage_line(outcome: ReviewOutcome) -> str:
    """``msreview usage: <role:model> llm=.. tools=.. in=.. cached=.. out=.. reasoning=..``.

    ``rl_wait=<s>`` follows a model's cost when it waited on rate limits.
    """
    parts = []
    for key in sorted(outcome.usage):
        u = outcome.usage[key]
        part = (
            f"{key} llm={u.llm_calls} tools={u.tool_calls} in={u.input_tokens} "
            f"cached={u.cached_input_tokens} out={u.output_tokens} "
            f"reasoning={u.reasoning_tokens} cost=${u.cost_usd:.4f}"
        )
        if u.rate_limit_wait_seconds > 0:
            part += f" rl_wait={round(u.rate_limit_wait_seconds)}"
        parts.append(part)
    body = "; ".join(parts) if parts else "none"
    return f"msreview usage: {body}; est_cost=${outcome.cost_usd:.2f}"


def result_line(outcome: ReviewOutcome, *, deferred: bool = False) -> str:
    """``msreview result: status=.. verdict=.. findings=.. review=..``.

    ``deferred``: the review step of the Action, whose publish step posts the review.
    """
    if deferred:
        status = "failed" if outcome.status == "failed" else "deferred"
        return (
            f"msreview result: status={status} verdict={outcome.verdict or '-'} "
            f"findings={open_findings(outcome)}; {DEFERRED_NOTE}"
        )
    review = outcome.review_url or outcome.summary_url or "-"
    return (
        f"msreview result: status={outcome.status} verdict={outcome.verdict or '-'} "
        f"findings={open_findings(outcome)} review={review}"
    )


def escape_command(text: str) -> str:
    """Escape a value for a GitHub Actions workflow command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class StopCommands:
    """``::stop-commands::`` for the run, so no log line can act as a workflow command."""

    def __init__(self, stream: object = None) -> None:
        self._token = secrets.token_hex(16)
        self._stream = stream
        self.active = False

    def _out(self) -> object:
        return self._stream if self._stream is not None else sys.stdout

    def start(self) -> None:
        print(f"::stop-commands::{self._token}", file=self._out(), flush=True)  # type: ignore[call-overload]
        self.active = True

    def resume(self) -> None:
        if self.active:
            print(f"::{self._token}::", file=self._out(), flush=True)  # type: ignore[call-overload]
            self.active = False


def _fallback_report(outcome: ReviewOutcome) -> str:
    if outcome.report_markdown:
        return outcome.report_markdown
    lines = ["### Morning Star Code Review", "", f"Status: **{outcome.status}**"]
    if outcome.reason:
        lines += ["", outcome.reason]
    lines += ["", f"Pull request: {outcome.pr_url}", f"Execution: `{outcome.execution_id}`", ""]
    return "\n".join(lines)


def copy_reports(run_dir: Path | None, out_dir: Path, outcome: ReviewOutcome) -> Path:
    """Put ``report.md``, ``findings.json`` and ``run.json`` into ``out_dir``.

    ``publish`` writes them into the run dir; any file it did not write (the run failed
    before that) is written from the outcome instead, so ``out_dir`` never keeps a file
    from an earlier review next to this one's.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    src = run_dir / "report" if run_dir is not None else None
    for name in REPORT_FILES:
        path = src / name if src is not None else None
        if path is not None and path.is_file() and not path.is_symlink():
            # Atomic replace: a symlink at the destination (for example one shipped in
            # a PR checkout's msreview-reports/) is replaced, never followed.
            write_bytes_atomic(out_dir / name, path.read_bytes(), mode=0o644)
            continue
        if name == "report.md":
            write_text_atomic(out_dir / name, _fallback_report(outcome), mode=0o644)
        elif name == "findings.json":
            write_json_atomic(
                out_dir / name,
                {
                    "pr_url": outcome.pr_url,
                    "head_sha": outcome.head_sha,
                    "verdict": outcome.verdict,
                    "execution_id": outcome.execution_id,
                    "findings": [f.model_dump(mode="json") for f in outcome.findings],
                    "refuted": [r.model_dump(mode="json") for r in outcome.refuted],
                },
                mode=0o644,
            )
        else:
            write_json_atomic(
                out_dir / name,
                {
                    "status": outcome.status,
                    "reason": outcome.reason,
                    "execution_id": outcome.execution_id,
                    "pr_url": outcome.pr_url,
                    "head_sha": outcome.head_sha,
                    "verdict": outcome.verdict,
                    "usage": {k: u.model_dump(mode="json") for k, u in outcome.usage.items()},
                    "cost_usd": outcome.cost_usd,
                    "notes": list(outcome.notes),
                },
                mode=0o644,
            )
    return out_dir / "report.md"


def write_github_output(path: str, values: Mapping[str, str]) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT`` (values are single-line)."""
    with open(path, "a", encoding="utf-8") as fh:  # noqa: PTH123 - append to a runner file
        for key, value in values.items():
            clean = value.replace("\r", " ").replace("\n", " ")
            fh.write(f"{key}={clean}\n")


def write_step_summary(path: str, markdown: str) -> None:
    text = markdown
    if len(text) > STEP_SUMMARY_LIMIT:
        text = text[:STEP_SUMMARY_LIMIT] + "\n\n(truncated; the full report is in the artifact)\n"
    with open(path, "a", encoding="utf-8") as fh:  # noqa: PTH123 - append to a runner file
        fh.write(text if text.endswith("\n") else text + "\n")


def _annotation(outcome: ReviewOutcome) -> str:
    n = open_findings(outcome)
    if outcome.status == "failed":
        return (
            f"::error title=Morning Star Code Review::{escape_command(outcome.reason or 'failed')}"
        )
    head = f"{outcome.status}, verdict {outcome.verdict or '-'}, {n} open finding(s)"
    if outcome.verdict == "incomplete":
        return f"::warning title=Morning Star Code Review::{escape_command(head)}"
    return f"::notice title=Morning Star Code Review::{escape_command(head)}"


def _export_plan(cfg: DriverConfig, run_dir: Path) -> Path | None:
    """Deferred publish: copy the run's plan to ``cfg.defer_publish_dir`` (None: no plan)."""
    dest = getattr(cfg, "defer_publish_dir", None)
    if dest is None:
        return None
    from morning_star_review import deferred  # lazy: imports the GitHub client

    return deferred.export_plan(run_dir, dest)


def _ci_results(
    outcome: ReviewOutcome,
    report_path: Path | None,
    src: Mapping[str, str],
    stop: object,
    deployment: str | None,
) -> None:
    """``$GITHUB_OUTPUT``, the step summary and the annotation of a finished outcome."""
    out = src.get("GITHUB_OUTPUT", "")
    if out:
        values = {
            "status": outcome.status,
            "verdict": outcome.verdict or "",
            "findings": str(open_findings(outcome)),
            "execution-id": outcome.execution_id,
            "report-path": str(report_path) if report_path else "",
        }
        if deployment is not None:
            values["deployment"] = deployment
        write_github_output(out, values)
    summary = src.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        write_step_summary(summary, _fallback_report(outcome))
    if isinstance(stop, StopCommands):
        stop.resume()
    print(_annotation(outcome), flush=True)


def emit(
    cfg: DriverConfig,
    outcome: ReviewOutcome,
    run_dir: Path,
    *,
    deployment: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Print the usage and result lines; write reports; set CI outputs (ci mode).

    Deferred publish (``cfg.defer_publish_dir`` set and a plan in the run directory):
    the plan is copied there first and the outputs only name it (``publish-plan``,
    ``execution-id``, ``deployment``). Status, step summary and annotation come from
    ``msreview ci-publish`` (``emit_published``) after it posted.
    """
    src = os.environ if env is None else env
    plan = _export_plan(cfg, run_dir)
    if plan is not None:
        _say(f"{usage_line(outcome)}; {DEFERRED_NOTE}")
        _say(result_line(outcome, deferred=True))
    else:
        _say(usage_line(outcome))
        _say(result_line(outcome))
    report_path: Path | None = None
    if cfg.out_dir is not None:
        report_path = copy_reports(run_dir, cfg.out_dir, outcome)
    if cfg.mode != "ci":
        return report_path
    if plan is not None:
        out = src.get("GITHUB_OUTPUT", "")
        if out:
            write_github_output(
                out,
                {
                    "publish-plan": str(plan),
                    "execution-id": outcome.execution_id,
                    "deployment": deployment or "",
                },
            )
        return report_path
    stop = getattr(cfg, "stop_commands", None)
    _ci_results(outcome, report_path, src, stop, deployment or "")
    return report_path


def emit_published(
    outcome: ReviewOutcome,
    report_dir: Path | None,
    env: Mapping[str, str] | None = None,
    *,
    run_dir: Path | None = None,
    stop: StopCommands | None = None,
) -> Path | None:
    """``msreview ci-publish``: the lines, report files and Actions outputs after posting.

    Report files come from ``<run_dir>/report`` when ``publish`` wrote them, else from
    the outcome. Writes the same outputs as ``emit`` in ci mode (without ``deployment``,
    which the review step set).
    """
    src = os.environ if env is None else env
    _say(usage_line(outcome))
    _say(result_line(outcome))
    report_path = copy_reports(run_dir, report_dir, outcome) if report_dir is not None else None
    _ci_results(outcome, report_path, src, stop, None)
    return report_path
