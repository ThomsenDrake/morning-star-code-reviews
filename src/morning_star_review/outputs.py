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
    "escape_command",
    "open_findings",
    "result_line",
    "usage_line",
    "write_github_output",
    "write_step_summary",
]

REPORT_FILES = ("report.md", "findings.json", "run.json")
STEP_SUMMARY_LIMIT = 900_000  # GitHub's limit is 1 MiB per step


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def open_findings(outcome: ReviewOutcome) -> int:
    return sum(1 for f in outcome.findings if f.state in ("open", "summary_only"))


def usage_line(outcome: ReviewOutcome) -> str:
    """``msreview usage: <role:model> llm=.. tools=.. in=.. cached=.. out=.. reasoning=..``."""
    parts = []
    for key in sorted(outcome.usage):
        u = outcome.usage[key]
        parts.append(
            f"{key} llm={u.llm_calls} tools={u.tool_calls} in={u.input_tokens} "
            f"cached={u.cached_input_tokens} out={u.output_tokens} "
            f"reasoning={u.reasoning_tokens} cost=${u.cost_usd:.4f}"
        )
    body = "; ".join(parts) if parts else "none"
    return f"msreview usage: {body}; est_cost=${outcome.cost_usd:.2f}"


def result_line(outcome: ReviewOutcome) -> str:
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


def copy_reports(run_dir: Path, out_dir: Path, outcome: ReviewOutcome) -> Path:
    """Put ``report.md``, ``findings.json`` and ``run.json`` into ``out_dir``.

    ``publish`` writes them into the run dir; any file it did not write (the run failed
    before that) is written from the outcome instead, so ``out_dir`` never keeps a file
    from an earlier review next to this one's.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    src = run_dir / "report"
    for name in REPORT_FILES:
        path = src / name
        if path.is_file() and not path.is_symlink():
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


def emit(
    cfg: DriverConfig,
    outcome: ReviewOutcome,
    run_dir: Path,
    *,
    deployment: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Print the usage and result lines; write reports; set CI outputs (ci mode)."""
    src = os.environ if env is None else env
    _say(usage_line(outcome))
    _say(result_line(outcome))
    report_path: Path | None = None
    if cfg.out_dir is not None:
        report_path = copy_reports(run_dir, cfg.out_dir, outcome)
    if cfg.mode != "ci":
        return report_path
    out = src.get("GITHUB_OUTPUT", "")
    if out:
        write_github_output(
            out,
            {
                "status": outcome.status,
                "verdict": outcome.verdict or "",
                "findings": str(open_findings(outcome)),
                "execution-id": outcome.execution_id,
                "report-path": str(report_path) if report_path else "",
                "deployment": deployment or "",
            },
        )
    summary = src.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        write_step_summary(summary, _fallback_report(outcome))
    stop = getattr(cfg, "stop_commands", None)
    if isinstance(stop, StopCommands):
        stop.resume()
    print(_annotation(outcome), flush=True)
    return report_path
