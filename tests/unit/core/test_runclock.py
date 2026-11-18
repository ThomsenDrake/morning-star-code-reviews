"""Run clock: deadline, wind-down, per-phase run limits, cooldown files, wait format."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from morning_star_review.core import runclock as rc
from morning_star_review.core.contract import RunLimits
from morning_star_review.core.runctx import RunContext

S = 1_700_000_000.0  # start epoch
MIN = 60.0


def ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "execution_id": "msreview-cli-o-r-pr1-aaaaaaa-1234abcd",
        "request_sha256": "0" * 64,
        "seal_key": b"k" * 32,
        "run_root": "/tmp/runs",
        "mode": "cli",
        "on_worker": False,
        "github_token": None,
        "read_token": None,
        "bot_login": None,
        "mistral_api_key": "m",
        "agent_python": "/usr/bin/python3",
    }
    base.update(kw)
    return RunContext(**base)


def minutes_after_start(limits: RunLimits) -> tuple[float, float]:
    return (
        round((limits.wind_down_epoch - S) / MIN, 4),
        round((limits.stop_epoch - S) / MIN, 4),
    )


def test_constants() -> None:
    assert (rc.MIN_DEADLINE_MINUTES, rc.MAX_DEADLINE_MINUTES) == (20, 300)
    assert rc.DEFAULT_DEADLINE_MINUTES == 90
    assert rc.WIND_DOWN_MINUTES == 15
    assert timedelta(minutes=310) == rc.ACTIVITY_CEILING
    assert timedelta(minutes=330) == rc.EXECUTION_TIMEOUT
    assert rc.RL_FALLBACK_MAX_WAIT_S == 3600.0
    assert rc.RUN_DEADLINE_STOP == "run_deadline"
    assert rc.SNAPSHOT_FILE == "snapshot.json"
    assert rc.GITHUB_WAIT_MARGIN_S == 30.0
    assert rc.AGENT_STOP_MARGIN_S == 60.0
    assert rc.VERIFY_START_SLACK_S == 90.0


def test_default_deadline_of_90_minutes() -> None:
    clock = rc.plan_clock(S, 90 * MIN)
    assert clock.scale == 1.0
    assert clock.deadline_s == 90 * MIN
    assert clock.deadline_minutes == 90
    assert clock.hard_epoch == S + 90 * MIN
    assert clock.wind_down_epoch == S + 75 * MIN
    assert minutes_after_start(clock.limits("explore")) == (75, 80)
    assert minutes_after_start(clock.limits("structure")) == (84, 84)
    assert minutes_after_start(clock.limits("verify")) == (78, 82)
    assert minutes_after_start(clock.limits("verify-format")) == (86, 86)
    assert clock.github_wait_until() == S + 90 * MIN - 30


# (D minutes, k, W, explore stop, structure stop, verify tools, verify stop, verify-format stop)
CASES = [
    (20, 0.5, 12.5, 15.0, 17.0, 14.0, 16.0, 18.0),
    (40, 2 / 3, 30.0, 100 / 3, 36.0, 32.0, 104 / 3, 112 / 3),
    (90, 1.0, 75.0, 80.0, 84.0, 78.0, 82.0, 86.0),
    (300, 1.0, 285.0, 290.0, 294.0, 288.0, 292.0, 296.0),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"D={case[0]}")
def test_phase_limits_scale_with_the_deadline(case: tuple[float, ...]) -> None:
    d, k, w, explore, structure, verify_tools, verify_stop, verify_format = case
    clock = rc.plan_clock(S, d * MIN)
    assert clock.scale == pytest.approx(k)
    assert clock.deadline_minutes == d
    assert (clock.wind_down_epoch - S) / MIN == pytest.approx(w)
    lim = clock.limits("explore")
    assert (lim.wind_down_epoch - S) / MIN == pytest.approx(w)
    assert (lim.stop_epoch - S) / MIN == pytest.approx(explore)
    lim = clock.limits("structure")
    assert (lim.stop_epoch - S) / MIN == pytest.approx(structure)
    assert lim.wind_down_epoch == lim.stop_epoch  # no tools
    lim = clock.limits("verify")
    assert (lim.wind_down_epoch - S) / MIN == pytest.approx(verify_tools)
    assert (lim.stop_epoch - S) / MIN == pytest.approx(verify_stop)
    lim = clock.limits("verify-format")
    assert (lim.stop_epoch - S) / MIN == pytest.approx(verify_format)
    assert lim.wind_down_epoch == lim.stop_epoch
    # Every phase ends before the hard deadline, in pipeline order.
    stops = [clock.limits(p).stop_epoch for p in ("explore", "verify", "structure")]
    assert all(s < clock.hard_epoch for s in stops)
    assert clock.limits("verify-format").stop_epoch < clock.github_wait_until() < clock.hard_epoch


def test_wind_down_override_with_short_deadline() -> None:
    clock = rc.plan_clock(S, 25 * MIN, wind_down_after_s=4 * MIN)
    assert clock.scale == 0.5  # 25/60 is clamped up
    assert clock.wind_down_epoch == S + 240
    explore = clock.limits("explore")
    assert explore.wind_down_epoch == S + 240
    assert explore.stop_epoch == S + 240 + 150  # W + 300k
    # The other phases keep their distance from the hard deadline.
    assert clock.limits("verify") == RunLimits(
        wind_down_epoch=S + 1500 - 360, stop_epoch=S + 1500 - 240
    )
    assert clock.limits("structure").stop_epoch == S + 1500 - 180
    assert clock.limits("verify-format").stop_epoch == S + 1500 - 120


@pytest.mark.parametrize(
    ("after_s", "expected_s"),
    [
        (-100.0, 60.0),  # never before start + 60 s
        (0.0, 60.0),
        (30.0, 60.0),
        (61.0, 61.0),
        (1050.0, 1050.0),  # exactly the default for D = 25 min
        (1440.0, 1050.0),  # never later than the default
        (10_000.0, 1050.0),
    ],
)
def test_wind_down_override_is_clamped(after_s: float, expected_s: float) -> None:
    clock = rc.plan_clock(S, 25 * MIN, wind_down_after_s=after_s)
    assert clock.wind_down_epoch == S + expected_s


@pytest.mark.parametrize("deadline_s", [1.0, 30.0, 59.0, 400.0])
def test_tiny_deadlines_do_not_raise(deadline_s: float) -> None:
    clock = rc.plan_clock(S, deadline_s)
    assert S <= clock.wind_down_epoch <= clock.hard_epoch
    for phase in ("explore", "structure", "verify", "verify-format"):
        for verify in (False, True):
            lim = clock.limits(phase, verify=verify)
            assert lim.wind_down_epoch <= lim.stop_epoch


@pytest.mark.parametrize("deadline_s", [0.0, -5.0, float("nan"), float("inf")])
def test_plan_clock_rejects_bad_deadlines(deadline_s: float) -> None:
    with pytest.raises(ValueError, match="deadline_s"):
        rc.plan_clock(S, deadline_s)


def test_plan_clock_rejects_bad_epochs_and_overrides() -> None:
    with pytest.raises(ValueError):
        rc.plan_clock(float("nan"), 600.0)
    with pytest.raises(ValueError):
        rc.plan_clock(S, 600.0, wind_down_after_s=float("inf"))


def test_run_clock_validates_its_epochs() -> None:
    with pytest.raises(ValueError, match="start <= wind-down <= hard"):
        rc.RunClock(start_epoch=S, wind_down_epoch=S + 10, hard_epoch=S + 5)
    with pytest.raises(ValueError, match="start <= wind-down <= hard"):
        rc.RunClock(start_epoch=S, wind_down_epoch=S - 1, hard_epoch=S + 5)
    with pytest.raises(ValueError, match="finite"):
        rc.RunClock(start_epoch=S, wind_down_epoch=S, hard_epoch=float("inf"))
    with pytest.raises(ValueError, match="unknown run phase"):
        rc.plan_clock(S, 3600.0).limits("publish")  # type: ignore[arg-type]


def test_wound_down_and_verify_may_start() -> None:
    clock = rc.plan_clock(S, 90 * MIN)
    w, v = clock.wind_down_epoch, clock.verify_cutoff_epoch
    assert v == w == S + 75 * MIN  # the default wind-down is the verify cutoff
    assert not clock.wound_down(w - 0.001)
    assert clock.verify_may_start(v - 0.001)
    assert clock.wound_down(w)
    assert not clock.verify_may_start(v)
    assert clock.wound_down(clock.hard_epoch + 1)
    # With a verifier, the run winds down when the explorers do (64.5 min).
    tools = clock.explore_wind_down(verify=True)
    assert tools == S + 64.5 * MIN
    assert not clock.wound_down(tools - 0.001, verify=True)
    assert clock.wound_down(tools, verify=True)
    assert not clock.wound_down(tools, verify=False)


# Runs with a verifier: (D minutes, V, explore tools close, explore stop, structure stop).
# V = H - 900k; S_v = V - 90 s; E_v = S_v - 240k; T_v = E_v - 300k.
VERIFYING_CASES = [
    (20, 12.5, 6.5, 9.0, 11.0),
    (30, 22.5, 16.5, 19.0, 21.0),
    (40, 30.0, 22.5, 77.5 / 3, 28.5),
    (60, 45.0, 34.5, 39.5, 43.5),
    (90, 75.0, 64.5, 69.5, 73.5),
    (300, 285.0, 274.5, 279.5, 283.5),
]


@pytest.mark.parametrize("case", VERIFYING_CASES, ids=lambda case: f"D={case[0]}")
def test_a_verifier_keeps_its_window(case: tuple[float, ...]) -> None:
    d, v, tools, explore, structure = case
    clock = rc.plan_clock(S, d * MIN)
    assert (clock.verify_cutoff_epoch - S) / MIN == pytest.approx(v)
    lim = clock.limits("explore", verify=True)
    assert minutes_after_start(lim) == pytest.approx((tools, explore), abs=1e-4)
    lim = clock.limits("structure", verify=True)
    assert minutes_after_start(lim) == pytest.approx((structure, structure), abs=1e-4)
    # The verify phases do not depend on the flag.
    for phase in ("verify", "verify-format"):
        assert clock.limits(phase, verify=True) == clock.limits(phase)
    # Pipeline order: explorers write notes, the structure step and its launcher margin
    # end before V, and the verifier has tools after V, then its stop, the formatter
    # and publishing before H.
    timeline = [
        clock.start_epoch,
        clock.limits("explore", verify=True).wind_down_epoch,
        clock.limits("explore", verify=True).stop_epoch,
        clock.limits("structure", verify=True).stop_epoch + rc.AGENT_STOP_MARGIN_S,
        clock.verify_cutoff_epoch,
        clock.limits("verify").wind_down_epoch,
        clock.limits("verify").stop_epoch,
        clock.limits("verify-format").stop_epoch,
        clock.github_wait_until(),
        clock.hard_epoch,
    ]
    assert timeline == sorted(timeline) and len(set(timeline)) == len(timeline)
    # The verifier keeps at least 180k s of tools and 420k s to its stop after V.
    k = clock.scale
    assert clock.limits("verify").wind_down_epoch - clock.verify_cutoff_epoch >= 180 * k
    assert clock.limits("verify").stop_epoch - clock.verify_cutoff_epoch >= 420 * k
    # Explorers keep their notes window and the structure step its formatter window.
    e = clock.limits("explore", verify=True)
    assert e.stop_epoch - e.wind_down_epoch == pytest.approx(300 * k)
    assert clock.limits("structure", verify=True).stop_epoch - e.stop_epoch == pytest.approx(
        240 * k
    )


@pytest.mark.parametrize("d", range(20, 301, 5))
def test_timelines_are_monotonic_for_every_allowed_deadline(d: int) -> None:
    clock = rc.plan_clock(S, d * MIN)
    for verify in (False, True):
        e = clock.limits("explore", verify=verify)
        st = clock.limits("structure", verify=verify)
        assert S < e.wind_down_epoch < e.stop_epoch < st.stop_epoch < clock.hard_epoch
        assert e.wind_down_epoch <= clock.wind_down_epoch
    # Without a verifier: today's timeline (W, then W + 300k / H - 600k, H - 360k).
    k = clock.scale
    assert clock.limits("explore").wind_down_epoch == clock.wind_down_epoch
    assert clock.limits("explore").stop_epoch == min(
        clock.wind_down_epoch + 300 * k, clock.hard_epoch - 600 * k
    )
    assert clock.limits("structure").stop_epoch == clock.hard_epoch - 360 * k
    # With a verifier, explore and structure only come earlier, and before V.
    v = clock.verify_cutoff_epoch
    assert clock.limits("structure", verify=True).stop_epoch + rc.VERIFY_START_SLACK_S == v
    for phase in ("explore", "structure"):
        with_v, without = clock.limits(phase, verify=True), clock.limits(phase)
        assert with_v.wind_down_epoch <= without.wind_down_epoch
        assert with_v.stop_epoch <= without.stop_epoch
    assert v < clock.limits("verify").wind_down_epoch


def test_an_earlier_wind_down_still_lets_the_verifier_start_until_v() -> None:
    # The test knob: W at 4 min of 25 (k = 0.5). Explorers follow W; V stays.
    clock = rc.plan_clock(S, 25 * MIN, wind_down_after_s=4 * MIN)
    explore = clock.limits("explore", verify=True)
    assert explore == clock.limits("explore")  # W + 300k comes before E_v
    assert explore.wind_down_epoch == S + 240 and explore.stop_epoch == S + 390
    assert clock.verify_cutoff_epoch == S + 1500 - 450
    assert clock.verify_may_start(S + 1000) and not clock.verify_may_start(S + 1050)
    assert clock.limits("structure", verify=True).stop_epoch == S + 1050 - 90


def test_deadline_minutes_rounds() -> None:
    assert rc.plan_clock(S, 89.6 * MIN).deadline_minutes == 90
    assert rc.plan_clock(S, 20 * MIN + 10).deadline_minutes == 20


def test_from_context() -> None:
    assert rc.from_context(ctx()) is None
    assert rc.from_context(ctx(run_started_epoch=S, hard_epoch=S + 5400)) is None
    clock = rc.plan_clock(S, 90 * MIN)
    full = ctx(
        run_started_epoch=clock.start_epoch,
        wind_down_epoch=clock.wind_down_epoch,
        hard_epoch=clock.hard_epoch,
    )
    assert rc.from_context(full) == clock


def test_cooldown_path_uses_safe_model_ids_as_is(tmp_path: Path) -> None:
    for model in ("mistral-large-4", "zai-glm-5-3", "deepseek-v4-1-flash-1017", "m.v2_x"):
        assert rc.cooldown_path(tmp_path, model) == tmp_path / f"cooldown-{model}.json"


@pytest.mark.parametrize(
    "model",
    [
        "Mistral-Large-4",  # uppercase
        "org/model",  # slash
        "../../etc/passwd",
        "..",
        "-leading-dash",
        "model with spaces",
        "a" * 81,  # longer than a safe name may be
        "模型",  # nothing left after slugging
        "",
    ],
)
def test_cooldown_path_hashes_odd_model_ids(tmp_path: Path, model: str) -> None:
    path = rc.cooldown_path(tmp_path, model)
    assert path.parent == tmp_path
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    assert path.name.startswith("cooldown-")
    assert path.name.endswith(f"-{digest}.json")
    stem = path.name[len("cooldown-") : -len(".json")]
    assert len(stem) <= 48 + 1 + 8
    assert all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in stem)


def test_cooldown_paths_differ_per_model(tmp_path: Path) -> None:
    models = ["Mistral-Large-4", "mistral-large-4", "MISTRAL-LARGE-4", "org/model", "org-model"]
    names = {rc.cooldown_path(tmp_path, m).name for m in models}
    assert len(names) == len(models)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (0.4, "0s"),
        (45, "45s"),
        (59.4, "59s"),
        (59.6, "1m 00s"),
        (60, "1m 00s"),
        (430, "7m 10s"),
        (425, "7m 05s"),
        (3599, "59m 59s"),
        (3600, "1h 00m"),
        (3720, "1h 02m"),
        (3779, "1h 02m"),
        (36_000, "10h 00m"),
        (-5, "0s"),
        (float("nan"), "0s"),
        (float("inf"), "0s"),
    ],
)
def test_format_wait(seconds: float, text: str) -> None:
    assert rc.format_wait(seconds) == text


def test_runclock_imports_no_sdk() -> None:
    code = (
        "import sys, morning_star_review.core.runclock\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('mistralai', 'httpx', 'temporalio')]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""
