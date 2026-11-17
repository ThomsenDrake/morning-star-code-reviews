"""Truthful usage for runs that ended early, and Actions output on exit 2 (G5)."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_driver_lifecycle import (  # noqa: E402
    FAST,
    KEY,
    SECRETS,
    FakePlatform,
    Worker,
    make_cfg,
    no_skip,
    outcome_dict,
)

from morning_star_review import driver, outputs  # noqa: E402
from morning_star_review.core.contract import ReviewOutcome, Usage  # noqa: E402
from morning_star_review.platform import PlatformAuthError  # noqa: E402


def meter(phase: str, model: str, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "phase": phase,
        "model": model,
        "started_epoch": 1.0,
        "deadline_epoch": 2.0,
        "llm_call_limit": 40,
        "llm_calls": 3,
        "tool_calls": 7,
        "input_tokens": 1000,
        "cached_input_tokens": 400,
        "output_tokens": 200,
        "reasoning_tokens": 50,
        "cost_usd": 0.125,
        "seconds": 12.5,
        "finish_reasons": ["tool_calls", "stop"],
    }
    data.update(kw)
    return data


def put(run_dir: Path, job: str, data: Any) -> None:
    (run_dir / job).mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (run_dir / job / "meter.json").write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- meters


def test_usage_from_the_agent_meters(tmp_path: Path) -> None:
    run = tmp_path / "run"
    put(run, "explore-zai-glm-5-3", meter("explore", "zai-glm-5-3"))
    put(run, "explore-mistral-large-4", meter("explore", "mistral-large-4", cost_usd=0.5))
    put(run, "format-zai-glm-5-3", meter("format", "mistral-large-4", llm_calls=1))
    put(run, "verify", meter("verify", "zai-glm-5-3", llm_calls=2))
    put(run, "verify-format", meter("format", "mistral-large-4", llm_calls=1))
    # Never read: the checkout, foreign or broken files, a meter with no model call.
    put(run, "repo", meter("explore", "evil-model"))
    put(run, "explore-broken", "{not json")
    put(run, "explore-odd", meter("plan", "zai-glm-5-3"))
    put(run, "explore-idle", meter("explore", "idle-model", llm_calls=0, cost_usd=0))
    outside = tmp_path / "outside"
    put(outside, "x", meter("explore", "linked-model"))
    (run / "explore-linked").symlink_to(outside / "x")

    usage = driver.usage_from_run_dir(run)
    assert set(usage) == {
        "explorer:zai-glm-5-3",
        "explorer:mistral-large-4",
        "formatter:mistral-large-4",
        "verifier:zai-glm-5-3",
    }
    zai = usage["explorer:zai-glm-5-3"]
    assert (zai.llm_calls, zai.tool_calls, zai.input_tokens, zai.cached_input_tokens) == (
        3,
        7,
        1000,
        400,
    )
    assert (zai.output_tokens, zai.reasoning_tokens, zai.seconds) == (200, 50, 12.5)
    assert zai.cost_usd == 0.125 and zai.finish_reasons == ["tool_calls", "stop"]
    assert usage["formatter:mistral-large-4"].llm_calls == 2  # two formatter jobs summed
    assert driver.usage_from_run_dir(tmp_path / "missing") == {}


def test_outcome_usage_is_kept_when_present(tmp_path: Path) -> None:
    put(tmp_path, "explore-zai-glm-5-3", meter("explore", "zai-glm-5-3"))
    own = ReviewOutcome.model_validate(
        outcome_dict(
            "e",
            usage={"explorer:m": Usage(model="m", llm_calls=1, cost_usd=0.01)},
            cost_usd=0.01,
        )
    )
    assert driver.with_metered_usage(own, tmp_path) is own
    bare = ReviewOutcome(status="failed", reason="x", pr_url=own.pr_url, execution_id="e")
    filled = driver.with_metered_usage(bare, tmp_path)
    assert list(filled.usage) == ["explorer:zai-glm-5-3"] and filled.cost_usd == 0.125


def _write_meters_on_start(p: FakePlatform, run_root: Path) -> None:
    def on_start(eid: str) -> None:
        put(run_root / eid, "explore-zai-glm-5-3", meter("explore", "zai-glm-5-3"))
        put(run_root / eid, "verify", meter("verify", "mistral-large-4", cost_usd=0.25))

    p.on_start = on_start


@pytest.mark.parametrize("status", ["FAILED", "TERMINATED", "CANCELED"])
async def test_a_run_that_ended_early_reports_the_metered_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str
) -> None:
    p = FakePlatform()
    p.statuses = ["RUNNING", status]
    _write_meters_on_start(p, tmp_path / "runs")
    hooks = driver.Hooks(
        platform=p, worker=Worker(p), timing=FAST, install_signals=False, precheck=no_skip
    )
    outcome, code = await driver.run_review(make_cfg(tmp_path), SECRETS, hooks)
    assert outcome.status == "failed" and code == driver.EXIT_FAILED
    assert set(outcome.usage) == {"explorer:zai-glm-5-3", "verifier:mistral-large-4"}
    assert outcome.cost_usd == pytest.approx(0.375)
    err = capsys.readouterr().err
    assert "msreview usage: explorer:zai-glm-5-3 llm=3 tools=7" in err
    assert "est_cost=$0.38" in err and "usage: none" not in err


async def test_the_deadline_reports_the_metered_usage(tmp_path: Path) -> None:
    p = FakePlatform()
    p.statuses = ["RUNNING"]
    _write_meters_on_start(p, tmp_path / "runs")
    hooks = driver.Hooks(
        platform=p, worker=Worker(p), timing=FAST, install_signals=False, precheck=no_skip
    )
    outcome, _code = await driver.run_review(make_cfg(tmp_path, deadline_s=1), SECRETS, hooks)
    assert "deadline" in (outcome.reason or "")
    assert outcome.cost_usd == pytest.approx(0.375)
    assert p.terminated == [outcome.execution_id]


# ----------------------------------------------------------------------------- ci exit 2


@pytest.fixture
def actions_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {"output": tmp_path / "gh_output", "summary": tmp_path / "gh_summary"}
    monkeypatch.setenv("GITHUB_OUTPUT", str(paths["output"]))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(paths["summary"]))
    return paths


def _outputs(path: Path) -> dict[str, str]:
    rows = path.read_text(encoding="utf-8").splitlines()
    return dict(row.split("=", 1) for row in rows)


async def test_ci_preflight_failure_reports_to_actions(
    tmp_path: Path, actions_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    p = FakePlatform()
    p.whoami_error = PlatformAuthError(f"whoami failed (HTTP 401) for key {KEY}", 401)
    stop = outputs.StopCommands(stream=io.StringIO())
    stop.start()
    hooks = driver.Hooks(
        platform=p, worker=Worker(p), timing=FAST, install_signals=False, precheck=no_skip
    )
    cfg = make_cfg(tmp_path, mode="ci", stop_commands=stop)
    with pytest.raises(driver.DriverError) as info:
        await driver.run_review(cfg, SECRETS, hooks)

    assert _outputs(actions_env["output"]) == {
        "status": "failed",
        "verdict": "",
        "findings": "0",
        "execution-id": "",
        "report-path": "",
        "deployment": "",
    }
    summary = actions_env["summary"].read_text(encoding="utf-8")
    assert "### Morning Star Code Review" in summary and "did not start" in summary
    assert "the Mistral API key was refused" in summary
    out = capsys.readouterr().out
    errors = [ln for ln in out.splitlines() if ln.startswith("::error ")]
    assert len(errors) == 1
    assert errors[0].startswith("::error title=Morning Star Code Review::the Mistral API key")
    assert KEY not in out + summary
    assert not stop.active  # commands resumed, so the annotation is honoured
    # The CLI may report the same error again: it is written only once.
    driver.report_ci_usage_failure(info.value)
    assert len(actions_env["output"].read_text().splitlines()) == 6
    assert "::error " not in capsys.readouterr().out


async def test_ci_usage_errors_inside_the_driver_are_reported(
    tmp_path: Path, actions_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(driver.DriverError):
        await driver.run_review(
            make_cfg(tmp_path, mode="ci", pr_url="not a url"), SECRETS, driver.Hooks()
        )
    assert _outputs(actions_env["output"])["status"] == "failed"
    assert "::error title=Morning Star Code Review::" in capsys.readouterr().out


async def test_other_modes_write_no_actions_output(
    tmp_path: Path, actions_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    p = FakePlatform()
    p.whoami_error = PlatformAuthError("whoami failed (HTTP 401)", 401)
    hooks = driver.Hooks(
        platform=p, worker=Worker(p), timing=FAST, install_signals=False, precheck=no_skip
    )
    with pytest.raises(driver.DriverError):
        await driver.run_review(make_cfg(tmp_path), SECRETS, hooks)
    assert not actions_env["output"].exists() and not actions_env["summary"].exists()
    assert "::error" not in capsys.readouterr().out


def test_report_redacts_and_escapes(
    actions_env: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    monkeypatch.setenv("MSREVIEW_GITHUB_TOKEN", "ghs_secretvalue0123456789abcdef")
    driver.report_ci_usage_failure(
        "bad value 100%\nfrom ghs_secretvalue0123456789abcdef and other-secret-value-42",
        secrets=["other-secret-value-42"],
        env=os.environ,
    )
    out = capsys.readouterr().out.strip()
    assert out.startswith("::error title=Morning Star Code Review::bad value 100%25 from ")
    assert "ghs_secret" not in out and "other-secret" not in out
    assert "\n" not in out
    assert "other-secret" not in actions_env["summary"].read_text(encoding="utf-8")
