"""Result reporting: stderr lines, report files, $GITHUB_OUTPUT, step summary, annotations."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from morning_star_review import deferred, outputs
from morning_star_review.core.contract import (
    FailureInput,
    OperatorPolicy,
    ReviewOutcome,
    ReviewRequest,
    RunOverrides,
    Usage,
)
from morning_star_review.driver import DriverConfig

PR = "https://github.com/octo-org/widgets/pull/7"


def outcome(**kw: Any) -> ReviewOutcome:
    data: dict[str, Any] = {
        "status": "posted",
        "verdict": "findings",
        "pr_url": PR,
        "execution_id": "msreview-ci-x-pr7-latest-0a0b0c0d",
        "review_url": "https://github.com/octo-org/widgets/pull/7#pullrequestreview-1",
        "report_markdown": "### Morning Star Code Review\n\nreport body ::set-output name=x::y\n",
        "usage": {
            "explorer:zai-glm-5-3": Usage(
                model="zai-glm-5-3", llm_calls=3, tool_calls=9, input_tokens=100, cost_usd=0.02
            )
        },
        "cost_usd": 0.02,
    }
    data.update(kw)
    return ReviewOutcome.model_validate(data)


def cfg(tmp_path: Path, mode: str = "ci", out: bool = True) -> DriverConfig:
    return DriverConfig(
        mode=mode,  # type: ignore[arg-type]
        pr_url=PR,
        expected_head_sha=None,
        post="auto",
        force=False,
        overrides=RunOverrides(),
        policy=OperatorPolicy(trust_repo_config_models=True),
        run_label="gha:test",
        deadline_s=60,
        out_dir=tmp_path / "report" if out else None,
        fail_on="never",
    )


def test_usage_and_result_lines() -> None:
    o = outcome()
    line = outputs.usage_line(o)
    assert line.startswith("msreview usage: explorer:zai-glm-5-3 llm=3 tools=9 in=100 ")
    assert line.endswith("est_cost=$0.02")
    assert outputs.result_line(o) == (
        "msreview result: status=posted verdict=findings findings=0 "
        "review=https://github.com/octo-org/widgets/pull/7#pullrequestreview-1"
    )


def _finding(state: str, verify_status: str | None) -> dict[str, Any]:
    return {
        "id": f"f{state[:4]}{verify_status or 'none'}"[:13].ljust(13, "0"),
        "path": "a.py",
        "line": 1,
        "anchor": "inline",
        "severity": "high",
        "category": "correctness",
        "title": "t",
        "evidence": "x",
        "explanation": "e",
        "state": state,
        "verify_status": verify_status,
    }


def test_open_findings_leave_out_possible_issues() -> None:
    # Possible issues (uncertain, summary-only) do not count, as for the verdict.
    o = outcome(
        verdict="clean",
        findings=[_finding("summary_only", "uncertain"), _finding("fixed", "confirmed")],
    )
    assert o.possible_issues == 1
    assert outputs.open_findings(o) == 0
    assert "findings=0 " in outputs.result_line(o)
    o = outcome(
        findings=[
            _finding("summary_only", "uncertain"),
            _finding("open", "uncertain"),  # posted inline as "(unconfirmed)"
            _finding("summary_only", "confirmed"),
        ]
    )
    assert outputs.open_findings(o) == 2


def test_ci_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh_out = tmp_path / "gh_output"
    summary = tmp_path / "summary.md"
    run_dir = tmp_path / "run"
    (run_dir / "report").mkdir(parents=True)
    (run_dir / "report" / "report.md").write_text("from publish\n")
    (run_dir / "report" / "run.json").write_text("{}")
    (run_dir / "report" / "findings.json").write_text("{}")
    env = {"GITHUB_OUTPUT": str(gh_out), "GITHUB_STEP_SUMMARY": str(summary)}
    path = outputs.emit(cfg(tmp_path), outcome(), run_dir, deployment="msreview-ci-d", env=env)
    assert path == tmp_path / "report" / "report.md"
    assert path.read_text() == "from publish\n"
    lines = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert lines["status"] == "posted"
    assert lines["verdict"] == "findings"
    assert lines["findings"] == "0"
    assert lines["execution-id"] == "msreview-ci-x-pr7-latest-0a0b0c0d"
    assert lines["report-path"] == str(path)
    assert "report body" in summary.read_text()
    out = capsys.readouterr()
    # The report (model text) never reaches the log, so it cannot act as a command.
    assert "set-output" not in out.out
    assert out.out.startswith("::notice title=Morning Star Code Review::posted")
    assert "msreview result: status=posted" in out.err


def test_failed_run_gets_a_fallback_report_and_error_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    o = outcome(status="failed", verdict=None, report_markdown="", reason="prepare failed:\n50%")
    path = outputs.emit(cfg(tmp_path), o, tmp_path / "missing", env={})
    assert path is not None
    assert "prepare failed" in path.read_text()
    run = json.loads((tmp_path / "report" / "run.json").read_text())
    assert run["status"] == "failed"
    out = capsys.readouterr().out
    assert out.strip() == "::error title=Morning Star Code Review::prepare failed:%0A50%25"


def test_cli_mode_writes_no_ci_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh_out = tmp_path / "gh_output"
    outputs.emit(
        cfg(tmp_path, mode="cli", out=False),
        outcome(),
        tmp_path,
        env={"GITHUB_OUTPUT": str(gh_out)},
    )
    assert not gh_out.exists()
    assert capsys.readouterr().out == ""


def test_incomplete_verdict_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs.emit(cfg(tmp_path, out=False), outcome(verdict="incomplete"), tmp_path, env={})
    assert capsys.readouterr().out.startswith("::warning ")


def test_symlinked_report_files_are_not_copied(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "report").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy")
    (run_dir / "report" / "report.md").symlink_to(secret)
    outputs.copy_reports(run_dir, tmp_path / "out", outcome())
    assert "do not copy" not in (tmp_path / "out" / "report.md").read_text()


def test_partial_report_leaves_no_stale_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "findings.json").write_text('{"pr_url": "an earlier review"}')
    run_dir = tmp_path / "run"
    (run_dir / "report").mkdir(parents=True)
    (run_dir / "report" / "report.md").write_text("# this review\n")
    outputs.copy_reports(run_dir, out, outcome())
    assert (out / "report.md").read_text() == "# this review\n"
    assert "an earlier review" not in (out / "findings.json").read_text()
    assert (out / "run.json").is_file()


def test_step_summary_is_capped(tmp_path: Path) -> None:
    path = tmp_path / "s.md"
    outputs.write_step_summary(str(path), "x" * (outputs.STEP_SUMMARY_LIMIT + 10))
    assert len(path.read_text()) < outputs.STEP_SUMMARY_LIMIT + 200


def test_stop_commands_round_trip() -> None:
    buf = io.StringIO()
    stop = outputs.StopCommands(buf)
    stop.start()
    stop.resume()
    stop.resume()  # idempotent
    first, second = buf.getvalue().splitlines()
    token = first.removeprefix("::stop-commands::")
    assert len(token) == 32 and second == f"::{token}::"


def test_github_output_values_are_single_line(tmp_path: Path) -> None:
    path = tmp_path / "o"
    outputs.write_github_output(str(path), {"status": "a\nb=c"})
    assert path.read_text() == "status=a b=c\n"


def test_symlinks_in_the_report_dir_are_replaced_not_followed(tmp_path: Path) -> None:
    # Security review SEC-CI-3: a PR checkout ships msreview-reports/report.md -> ~/.bashrc.
    victim = tmp_path / "victim.rc"
    victim.write_text("export SAFE=1\n")
    out = tmp_path / "prcheckout" / "msreview-reports"
    out.mkdir(parents=True)
    for name in outputs.REPORT_FILES:
        (out / name).symlink_to(victim)
    run_dir = tmp_path / "run"
    (run_dir / "report").mkdir(parents=True)
    (run_dir / "report" / "report.md").write_text('x = "$(touch pwned)"\n')
    outputs.copy_reports(run_dir, out, outcome())
    assert victim.read_text() == "export SAFE=1\n"
    for name in outputs.REPORT_FILES:
        assert not (out / name).is_symlink()
    assert (out / "report.md").read_text() == 'x = "$(touch pwned)"\n'


def test_usage_line_shows_rate_limit_waits() -> None:
    usage = {
        "explorer:mistral-large-4": Usage(
            model="mistral-large-4", llm_calls=4, rate_limit_waits=5, rate_limit_wait_seconds=430.7
        ),
        "explorer:zai-glm-5-3": Usage(model="zai-glm-5-3", llm_calls=3),
    }
    line = outputs.usage_line(outcome(usage=usage))
    first, second = line.removeprefix("msreview usage: ").split("; ")[:2]
    assert first.endswith("cost=$0.0000 rl_wait=431")
    assert "rl_wait" not in second


def _failure_plan(run_dir: Path) -> ReviewOutcome:
    req = ReviewRequest(
        pr_url=PR,
        repo="octo-org/widgets",
        mode="ci",
        policy=OperatorPolicy(trust_repo_config_models=True),
        run_label="gha:test",
    )
    inp = FailureInput(req=req, snap=None, stage="explore", error_type="E", error_text="boom")
    o = outcome(status="failed", verdict=None, reason="explore failed: E: boom", review_url=None)
    plan = deferred.DeferredPlan(
        kind="failure", execution_id=o.execution_id, failure=inp, outcome=o
    )
    deferred.write_plan(run_dir, plan)
    return o


def test_deferred_emit_names_only_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_out, summary = tmp_path / "gh_output", tmp_path / "summary.md"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    o = _failure_plan(run_dir)
    c = cfg(tmp_path)
    c.defer_publish_dir = tmp_path / "publish"
    env = {"GITHUB_OUTPUT": str(gh_out), "GITHUB_STEP_SUMMARY": str(summary)}
    path = outputs.emit(c, o, run_dir, deployment="msreview-ci-d", env=env)
    plan = tmp_path / "publish" / deferred.PUBLISH_PLAN_JSON
    assert deferred.load_plan(plan).kind == "failure"
    assert plan.stat().st_mode & 0o777 == 0o600
    lines = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    # Status, verdict, findings and the summary come from the publish step.
    assert lines == {
        "publish-plan": str(plan),
        "execution-id": o.execution_id,
        "deployment": "msreview-ci-d",
    }
    assert not summary.exists()
    assert path is not None and path.is_file()  # the report files are still written
    out = capsys.readouterr()
    assert out.out == ""  # no annotation
    usage, result = out.err.strip().splitlines()
    assert usage.startswith("msreview usage: ") and usage.endswith("; posting in the next step")
    assert result == "msreview result: status=failed verdict=- findings=0; posting in the next step"


def test_deferred_dir_without_a_plan_emits_as_usual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_out = tmp_path / "gh_output"
    c = cfg(tmp_path)
    c.defer_publish_dir = tmp_path / "publish"
    outputs.emit(
        c,
        outcome(status="skipped", verdict=None),
        tmp_path / "run",
        env={"GITHUB_OUTPUT": str(gh_out)},
    )
    lines = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert lines["status"] == "skipped" and "publish-plan" not in lines
    assert not (tmp_path / "publish").exists()
    assert capsys.readouterr().out.startswith("::notice ")


def test_emit_published_writes_the_ci_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_out, summary = tmp_path / "gh_output", tmp_path / "summary.md"
    run_dir = tmp_path / "plan" / "run"
    (run_dir / "report").mkdir(parents=True)
    (run_dir / "report" / "report.md").write_text("posted report\n")
    stop = outputs.StopCommands(stream=io.StringIO())
    stop.start()
    env = {"GITHUB_OUTPUT": str(gh_out), "GITHUB_STEP_SUMMARY": str(summary)}
    posted = outcome().model_copy(update={"ready": False})
    path = outputs.emit_published(posted, tmp_path / "report", env, run_dir=run_dir, stop=stop)
    assert path == tmp_path / "report" / "report.md" and path.read_text() == "posted report\n"
    assert json.loads((tmp_path / "report" / "run.json").read_text())["status"] == "posted"
    lines = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert lines == {
        "status": "posted",
        "verdict": "findings",
        "ready": "false",
        "findings": "0",
        "execution-id": "msreview-ci-x-pr7-latest-0a0b0c0d",
        "report-path": str(path),
    }
    assert "report body" in summary.read_text()
    assert not stop.active
    out = capsys.readouterr()
    assert out.out.startswith("::notice title=Morning Star Code Review::posted")
    assert "msreview result: status=posted" in out.err
    # Without a run directory the files come from the outcome.
    outputs.emit_published(outcome(status="failed", reason="r"), tmp_path / "r2", {}, run_dir=None)
    assert json.loads((tmp_path / "r2" / "run.json").read_text())["status"] == "failed"
