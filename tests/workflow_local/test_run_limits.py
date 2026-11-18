"""Rate limits slow a run down; only the run's time limit ends it.

In-process runs with the stub agent and a run clock in the run context: rate-limit
waits keep every explorer, the jobs carry the run's limits, a rate-limited explorer
cannot crowd out the verifier, a run past its verify cutoff skips the verifier and
still publishes, and an agent stopped at the run's limit still gives a result.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from harness import Harness
from wfkit import AUTHZ, ROUNDING, SQLI, SWAP, notes_for

from morning_star_review.activities import agent_launcher
from morning_star_review.activities import explore as explore_mod
from morning_star_review.activities.agent_launcher import AgentTimedOut
from morning_star_review.core import runclock
from morning_star_review.core.contract import PrSnapshot
from morning_star_review.core.seal import verify as verify_seal

pytestmark = pytest.mark.timeout(120)

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
OPENED = ["app/db.py", "app/handlers.py", "app/service.py"]
TWENTY_MIN = 20 * 60.0
TIME_WORDS = ("time limit", "did not finish", "produced no result", "deadline")


def _clock(start_ago: float, wind_down_in: float, hard_in: float) -> runclock.RunClock:
    now = time.time()
    return runclock.RunClock(
        start_epoch=now - start_ago, wind_down_epoch=now + wind_down_in, hard_epoch=now + hard_in
    )


def _epochs(clock: runclock.RunClock) -> dict[str, float]:
    return {
        "run_started_epoch": clock.start_epoch,
        "wind_down_epoch": clock.wind_down_epoch,
        "hard_epoch": clock.hard_epoch,
    }


def _limits(
    clock: runclock.RunClock, phase: runclock.RunPhase, *, verify: bool = False
) -> dict[str, Any]:
    return clock.limits(phase, verify=verify).model_dump(mode="json")


def _reasons(out: Any) -> str:
    return " | ".join(str(r) for r in out.verdict_reasons)


async def test_rate_limit_waits_keep_every_explorer(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """20 minutes of waits per explorer: slower, but all three explorers count.

    Each explorer outlives its (shrunk) base allowance while its meter shows a wait in
    progress: the launcher extends the allowance instead of stopping it.
    """
    h = harness
    monkeypatch.setattr(agent_launcher, "HEARTBEAT_EVERY", 0.2)
    # A base allowance of 4 s (room for the stub's start on a busy machine); the stub
    # runs 6 s, so only the wait in progress on its meter keeps it alive.
    monkeypatch.setattr(explore_mod, "launch_timeout", lambda budgets: 4.0)
    waiting = {"rl_wait_seconds": TWENTY_MIN, "rl_waits": 6, "rl_until_epoch": time.time() + 120}
    usage = {
        "llm_calls": 9,
        "tool_calls": 7,
        "rate_limit_waits": 6,
        "rate_limit_wait_seconds": TWENTY_MIN,
    }
    for model, problems in (
        (GLM, (SQLI, AUTHZ)),
        (LARGE, (SQLI, AUTHZ, SWAP)),
        (DEEPSEEK, (SQLI,)),
    ):
        h.spec(
            f"explore:{model}",
            notes=notes_for(h.repo, *problems),
            opened_paths=OPENED,
            usage=usage,
            meter=waiting,
            sleep=6.0,
        )
    h.spec(
        "verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "Inverted": "confirmed",
            "swapped": "refuted",
        },
    )
    clock = _clock(start_ago=60, wind_down_in=74 * 60, hard_in=89 * 60)
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert out.status == "posted", out.reason
    assert sorted(c["model"] for c in h.calls("explore")) == sorted([GLM, LARGE, DEEPSEEK])
    for model in (GLM, LARGE, DEEPSEEK):
        u = out.usage[f"explorer:{model}"]
        assert u.rate_limit_waits == 6 and u.rate_limit_wait_seconds == TWENTY_MIN
    assert not [w for w in TIME_WORDS if w in _reasons(out)], _reasons(out)
    assert out.verdict == "findings"

    # Every job carries its phase's run limits (``best`` verifies: explorers end in time
    # for the verifier) and the model's shared cooldown file.
    for call in h.calls("explore"):
        assert call["run_limits"] == _limits(clock, "explore", verify=True)
        assert call["run_limits"] != _limits(clock, "explore")
        assert call["cooldown_path"] == str(runclock.cooldown_path(h.run_dir, call["model"]))
    (ver,) = h.calls("verify")
    assert ver["run_limits"] == _limits(clock, "verify")
    assert ver["cooldown_path"] == str(runclock.cooldown_path(h.run_dir, GLM))

    err = capsys.readouterr().err
    lines = [x for x in err.splitlines() if x.startswith("msreview agent: phase=explore")]
    assert len(lines) == 3 and all(x.endswith(" rl_wait=1200") for x in lines)
    # One line per explorer for the wait in progress (the stub's meter shows one wait).
    waits = re.findall(r"msreview: (\S+) rate-limited \(explore\); waiting \d+s", err)
    assert sorted(waits) == sorted([GLM, LARGE, DEEPSEEK])


async def test_verify_formatter_gets_its_own_limits(harness: Harness) -> None:
    h = harness
    for model in (GLM, LARGE, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=OPENED)
    h.spec("verify", notes="Both look right to me.\nVERIFY COMPLETE\n")
    h.spec("format:verify", verdicts_by_title={"SQL injection": "confirmed"})
    clock = _clock(start_ago=60, wind_down_in=74 * 60, hard_in=89 * 60)
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert out.status == "posted", out.reason
    (fmt,) = h.calls("format")
    assert fmt["job_dir"] == "verify-format"
    assert fmt["run_limits"] == _limits(clock, "verify-format")
    assert h.calls("verify")[0]["run_limits"] == _limits(clock, "verify")


async def test_a_rate_limited_explorer_cannot_crowd_out_the_verifier(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ``best`` explorer waits on rate limits the whole time; the verifier still runs.

    With a verifier the explorers stop early enough (``RunClock.limits(verify=True)``)
    that the verifier can start before its cutoff V. The straggler is stopped at its
    limit, keeps its partial notes, and its finding is verified with the others.
    """
    h = harness
    monkeypatch.setattr(agent_launcher, "HEARTBEAT_EVERY", 0.2)
    monkeypatch.setattr(agent_launcher, "LATEST_MARGIN_S", 1.0)  # stop the straggler soon
    # D = 90: with a verifier, explorers stop at H - 1230 s; that is 8 s from now.
    clock = _clock(start_ago=90 * 60 - 1238, wind_down_in=1238 - 900, hard_in=1238)
    assert clock.wind_down_epoch == pytest.approx(clock.verify_cutoff_epoch)
    explore = clock.limits("explore", verify=True)
    # Without the reserved window the straggler could wait past V (and the verifier
    # would be skipped); with it, it stops more than five minutes before V.
    assert clock.limits("explore").stop_epoch > clock.verify_cutoff_epoch
    assert explore.stop_epoch + 300 < clock.verify_cutoff_epoch
    h.spec(f"explore:{GLM}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec(f"explore:{DEEPSEEK}", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=OPENED)
    waiting = {"rl_wait_seconds": 900.0, "rl_waits": 7, "rl_until_epoch": time.time() + 900}
    h.spec(
        f"explore:{LARGE}",
        notes=notes_for(h.repo, SQLI, SWAP),
        partial_notes=notes_for(h.repo, SWAP).split("SUMMARY")[0],
        meter=waiting,
        sleep=600.0,  # never finishes on its own: only the run's limit stops it
    )
    h.spec(
        "verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "Inverted": "confirmed",
            "swapped": "confirmed",
        },
    )
    started = time.monotonic()
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert time.monotonic() - started < 60
    assert out.status == "posted", out.reason
    (straggler,) = [c for c in h.calls("explore") if c["model"] == LARGE]
    assert straggler["run_limits"] == _limits(clock, "explore", verify=True)
    # The verifier ran before its cutoff, on every candidate, the straggler's included.
    (ver,) = h.calls("verify")
    assert len(ver["candidates"]) == 3  # SQL injection, inverted check, swapped arguments
    assert {f.verify_status for f in out.findings} == {"confirmed"}
    assert len(out.findings) == 3
    assert any("swapped" in f.title for f in out.findings)
    reasons = _reasons(out)
    assert "the verifier did not run" not in reasons
    assert "time limit" in reasons  # the explorers were wound down early, and it says so
    assert out.verdict == "findings"
    err = capsys.readouterr().err
    (line,) = [
        x for x in err.splitlines() if x.startswith(f"msreview agent: phase=explore model={LARGE}")
    ]
    assert " stop_reason=run_deadline " in line and line.endswith(" rl_wait=900")
    (line,) = [x for x in err.splitlines() if x.startswith("msreview agent: phase=verify")]
    assert " state=Completed " in line and " complete=True " in line


async def test_jobs_without_a_run_clock_have_no_limits(harness: Harness) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    out = await h.run(h.request())
    assert out.status == "posted", out.reason
    (call,) = h.calls("explore")
    assert call["run_limits"] is None and call["cooldown_path"] is None


async def test_past_the_verify_cutoff_the_verifier_is_skipped_and_the_review_posted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verify cutoff has passed when the verifier would start: an honest review.

    The run clock keeps the explorers of a verifying run far enough before the cutoff;
    here the run reached it anyway (it started late): every explorer is stopped at its
    limit with partial notes, and the verifier does not start.
    """
    h = harness
    problems = {GLM: (), LARGE: (SWAP,), DEEPSEEK: (ROUNDING,)}
    stopped: list[str] = []

    async def run_agent(phase: Any, job_dir: Path, ctx: Any, timeout_s: float, **kw: Any) -> Any:
        model = kw["model"]
        stopped.append(model)
        (job_dir / "notes.partial.txt").write_text(notes_for(h.repo, *problems[model]))
        raise AgentTimedOut("explore agent stopped at the run's limit")

    monkeypatch.setattr(explore_mod, "run_agent", run_agent)
    h.spec("verify", verdicts_by_title={"swapped": "confirmed", "Rounding": "confirmed"})
    clock = _clock(start_ago=80 * 60, wind_down_in=-10, hard_in=10 * 60)
    assert not clock.verify_may_start(time.time())
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert h.calls("verify") == []  # no agent started
    assert sorted(stopped) == sorted(problems)
    assert out.status == "posted", out.reason
    # Nothing confirmed: two possible issues, and the verdict says why it is incomplete.
    assert {f.verify_status for f in out.findings} == {"uncertain"}
    assert out.verdict == "incomplete"
    assert "the verifier did not run: the run reached its time limit" in _reasons(out)
    assert h.fake.review_comments == []  # possible issues stay in the summary
    err = capsys.readouterr().err
    (line,) = [x for x in err.splitlines() if x.startswith("msreview agent: phase=verify")]
    assert " state=NotRun " in line and " stop_reason=run_deadline " in line


async def test_explorer_stopped_at_the_run_limit_stays_in_the_ensemble(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AgentTimedOut`` at or after the job's stop: partial notes and metered usage."""
    h = harness
    real_run_agent = explore_mod.run_agent
    stopped: list[float | None] = []

    async def run_agent(phase: Any, job_dir: Path, ctx: Any, timeout_s: float, **kw: Any) -> Any:
        if kw.get("model") != LARGE:
            return await real_run_agent(phase, job_dir, ctx, timeout_s, model=kw.get("model"))
        stopped.append(kw.get("latest_epoch"))
        (job_dir / "notes.partial.txt").write_text(notes_for(h.repo, SWAP).split("SUMMARY")[0])
        meter = {"phase": "explore", "model": LARGE, "llm_calls": 4, "rl_waits": 9}
        (job_dir / "meter.json").write_text(json.dumps({**meter, "rl_wait_seconds": 1500.0}))
        (job_dir / "coverage.json").write_text(json.dumps(["app/service.py", "../etc"]))
        raise AgentTimedOut("explore agent stopped after 1500 s")

    monkeypatch.setattr(explore_mod, "run_agent", run_agent)
    for model in (GLM, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed", "swapped": "refuted"})
    # D = 37 min (k = 0.62): with a verifier the explore stop is V - 90 s - 148 s, already
    # past; the verify cutoff V = H - 555 s is 45 s away, so the verifier still runs.
    clock = _clock(start_ago=27 * 60, wind_down_in=45, hard_in=10 * 60)
    explore_stop = clock.limits("explore", verify=True).stop_epoch
    assert explore_stop < time.time() < clock.verify_cutoff_epoch
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert stopped == [explore_stop]  # one attempt, no retry
    assert out.status == "posted", out.reason
    u = out.usage[f"explorer:{LARGE}"]
    assert u.llm_calls == 4 and u.rate_limit_waits == 9 and u.rate_limit_wait_seconds == 1500
    # The partial notes count: the swapped-arguments finding reached the verifier.
    (ver,) = h.calls("verify")
    assert len(ver["candidates"]) == 2
    # A secondary explorer cut short with a full draft is a note, not a blocker.
    assert "the mistral-large-4 explorer ran out of deadline" in _reasons(out)
    assert "produced no result" not in _reasons(out)
    assert out.verdict != "clean"


async def test_explorer_timed_out_before_the_limit_is_retried_as_before(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    real_run_agent = explore_mod.run_agent
    attempts: list[str] = []

    async def run_agent(phase: Any, job_dir: Path, ctx: Any, timeout_s: float, **kw: Any) -> Any:
        if kw.get("model") != LARGE:
            return await real_run_agent(phase, job_dir, ctx, timeout_s, **kw)
        attempts.append(LARGE)
        raise AgentTimedOut("explore agent stopped after 1500 s")

    monkeypatch.setattr(explore_mod, "run_agent", run_agent)
    for model in (GLM, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed"})
    clock = _clock(start_ago=60, wind_down_in=74 * 60, hard_in=89 * 60)
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert attempts == [LARGE, LARGE]  # retried, then left out (as without a clock)
    assert out.status == "posted", out.reason
    assert "the mistral-large-4 explorer produced no result" in _reasons(out)


async def test_agents_past_the_run_limit_are_stopped_and_the_review_is_posted(
    harness: Harness,
) -> None:
    """Every explore stop has passed: each agent is stopped at once, the run still posts."""
    h = harness
    for model in (GLM, LARGE, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI), sleep=30)
    clock = _clock(start_ago=60 * 60, wind_down_in=-20 * 60, hard_in=60)
    started = time.monotonic()
    out = await h.run(h.request(preset="best"), **_epochs(clock))

    assert time.monotonic() - started < 60
    assert out.status == "posted", out.reason
    assert out.verdict == "incomplete"
    reasons = _reasons(out)
    assert "produced no result" not in reasons
    assert all(f"the {m} explorer did not finish" in reasons for m in (GLM, LARGE, DEEPSEEK))


async def test_fetch_pr_saves_the_sealed_snapshot(harness: Harness) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    out = await h.run(h.request())
    assert out.status == "posted", out.reason
    path = h.run_dir / runclock.SNAPSHOT_FILE
    snap = PrSnapshot.model_validate_json(path.read_text())
    assert h.last_ctx is not None and verify_seal(snap, h.last_ctx)
    assert snap.head_sha == h.repo.head
    assert path.stat().st_mode & 0o077 == 0
