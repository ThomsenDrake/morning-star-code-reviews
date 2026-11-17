"""The agent launcher: allowlisted env, exit codes, timeouts, cancellation (DESIGN 6.8)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import secrets
import sys
import textwrap
import time
from pathlib import Path

import pytest

from morning_star_review.activities import agent_launcher as launcher
from morning_star_review.activities.agent_launcher import (
    AgentCrashed,
    AgentJobInvalid,
    AgentTimedOut,
    run_agent,
)
from morning_star_review.core.contract import AgentRunResult, Usage
from morning_star_review.core.runctx import RunContext

pytestmark = pytest.mark.timeout(60)

KEY = "mk-launcher-test-key-0123456789"
TOKEN = "ghs_launchertesttoken0123456789"


def _ctx(tmp_path: Path, agent_python: Path) -> RunContext:
    return RunContext(
        execution_id="msreview-local-o-r-pr1-aaaaaaa-0123abcd",
        request_sha256="0" * 64,
        seal_key=secrets.token_bytes(32),
        run_root=str(tmp_path / "runs"),
        mode="local",
        on_worker=False,
        github_token=TOKEN,
        read_token=None,
        bot_login="app[bot]",
        mistral_api_key=KEY,
        agent_python=str(agent_python),
    )


def _agent(tmp_path: Path, body: str) -> Path:
    """A fake agent: ``body`` runs with ``phase`` and ``job_dir`` (``argv[3]``, ``argv[4]``)."""
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import json, os, sys, time, subprocess\n"
        "from pathlib import Path\n"
        "phase, job_dir = sys.argv[3], Path(sys.argv[4])\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    wrapper = tmp_path / "agent-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _result(phase: str = "explore") -> str:
    res = AgentRunResult(
        phase=phase,  # type: ignore[arg-type]
        state="Completed",
        stop_reason=None,
        notes="NO FINDINGS\nREVIEW COMPLETE\n",
        complete=True,
        usage=Usage(model="m", llm_calls=2),
    )
    return res.model_dump_json()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_success_returns_the_result_and_runs_in_a_clean_env(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text("stale", encoding="utf-8")
    agent = _agent(
        tmp_path,
        f"""
        assert sys.argv[1:3] == ["-m", "morning_star_review.agent"], sys.argv
        info = {{"env": dict(os.environ), "cwd": os.getcwd(), "args": sys.argv[1:]}}
        (job_dir / "seen.json").write_text(json.dumps(info))
        (job_dir / "result.json").write_text({_result()!r})
        """,
    )
    res = await run_agent("explore", job, _ctx(tmp_path, agent), 30)
    assert res.state == "Completed" and res.usage.llm_calls == 2

    seen = json.loads((job / "seen.json").read_text())
    env = seen["env"]
    assert env["MISTRAL_API_KEY"] == KEY
    assert os.path.realpath(env["HOME"]) == os.path.realpath(seen["cwd"])
    assert env["HOME"] == env["TMPDIR"]
    assert not Path(seen["cwd"]).exists()  # the scratch directory is removed afterwards
    assert all(TOKEN not in v for v in env.values())
    assert not [k for k in env if k.startswith(("GITHUB", "MSREVIEW_", "GIT_CONFIG_COUNT"))]
    assert (job / "agent.log").exists()


async def test_endpoint_overrides_are_never_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SERVER_URL", "MISTRAL_API_URL", "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    env = launcher.agent_env(_ctx(tmp_path, tmp_path), str(tmp_path))
    assert "SERVER_URL" not in env and "MISTRAL_API_URL" not in env
    assert "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE" not in env
    assert "GITHUB_TOKEN" not in env


TELEMETRY_SWITCHES = (
    "AGENTS_SDK_TELEMETRY_ENABLED",
    "AGENTS_SDK_STUDIO_TRACING_ENABLED",
    "OTEL_ENABLED",
)


async def test_telemetry_switches_follow_the_run_context(tmp_path: Path) -> None:
    off = launcher.agent_env(_ctx(tmp_path, tmp_path), str(tmp_path))
    assert off["AGENTS_SDK_TELEMETRY_ENABLED"] == "0"
    assert off["AGENTS_SDK_STUDIO_TRACING_ENABLED"] == "0"
    assert off["OTEL_ENABLED"] == "false"

    opted_in = dataclasses.replace(_ctx(tmp_path, tmp_path), telemetry=True)
    on = launcher.agent_env(opted_in, str(tmp_path))
    assert not [name for name in TELEMETRY_SWITCHES if name in on]
    assert on["MISTRAL_API_KEY"] == KEY  # everything else is unchanged
    assert {k: v for k, v in off.items() if k not in TELEMETRY_SWITCHES} == on


async def test_telemetry_opt_in_reaches_the_agent_process(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    agent = _agent(
        tmp_path,
        f"""
        (job_dir / "seen.json").write_text(json.dumps(dict(os.environ)))
        (job_dir / "result.json").write_text({_result()!r})
        """,
    )
    ctx = dataclasses.replace(_ctx(tmp_path, agent), telemetry=True)
    await run_agent("explore", job, ctx, 30)
    env = json.loads((job / "seen.json").read_text())
    assert not [name for name in TELEMETRY_SWITCHES if name in env]


async def test_exit_three_is_an_invalid_job_with_a_redacted_log_tail(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    agent = _agent(
        tmp_path,
        """
        print("invalid job; key was " + os.environ["MISTRAL_API_KEY"], file=sys.stderr)
        sys.exit(3)
        """,
    )
    with pytest.raises(AgentJobInvalid) as err:
        await run_agent("explore", job, _ctx(tmp_path, agent), 30)
    assert "invalid job" in str(err.value)
    assert KEY not in str(err.value)
    assert AgentJobInvalid.non_retryable


async def test_crash_and_missing_result_are_retryable(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    crash = _agent(tmp_path, "sys.exit(1)\n")
    with pytest.raises(AgentCrashed):
        await run_agent("explore", job, _ctx(tmp_path, crash), 30)
    silent = _agent(tmp_path, "pass\n")
    with pytest.raises(AgentCrashed, match="without a valid result"):
        await run_agent("explore", job, _ctx(tmp_path, silent), 30)
    wrong = _agent(tmp_path, f"(job_dir / 'result.json').write_text({_result('verify')!r})\n")
    with pytest.raises(AgentCrashed):
        await run_agent("explore", job, _ctx(tmp_path, wrong), 30)
    assert not AgentCrashed.non_retryable


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    agent = _agent(
        tmp_path,
        """
        child = subprocess.Popen(["sleep", "30"])
        (job_dir / "child.pid").write_text(str(child.pid))
        time.sleep(30)
        """,
    )
    started = time.monotonic()
    with pytest.raises(AgentTimedOut):
        await run_agent("explore", job, _ctx(tmp_path, agent), 1.5)
    assert time.monotonic() - started < 15
    child = int((job / "child.pid").read_text())
    for _ in range(50):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)


async def test_cancellation_stops_the_agent_and_propagates(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    agent = _agent(
        tmp_path,
        """
        (job_dir / "agent.pid").write_text(str(os.getpid()))
        time.sleep(30)
        """,
    )
    task = asyncio.create_task(run_agent("explore", job, _ctx(tmp_path, agent), 30))
    for _ in range(100):
        if (job / "agent.pid").exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int((job / "agent.pid").read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        await asyncio.sleep(0.1)
    assert not _alive(pid)


async def test_heartbeats_carry_meter_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "meter.json").write_text(
        json.dumps({"llm_calls": 4, "tool_calls": 7, "cost_usd": 0.123456, "notes": "x"})
    )
    beats: list[dict[str, object]] = []
    monkeypatch.setattr(launcher, "heartbeat", beats.append)
    monkeypatch.setattr(launcher, "HEARTBEAT_EVERY", 0.2)
    agent = _agent(
        tmp_path, f"time.sleep(1.0)\n(job_dir / 'result.json').write_text({_result()!r})\n"
    )
    await run_agent("explore", job, _ctx(tmp_path, agent), 30)
    assert beats
    assert beats[0] == {"phase": "explore", "llm_calls": 4, "tool_calls": 7, "cost": 0.1235}
