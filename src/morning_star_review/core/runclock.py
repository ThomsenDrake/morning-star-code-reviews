"""The run clock: one overall deadline, a wind-down before it, and per-phase limits.

Rate limits make a review slower, never fail it. So every phase may wait on a
model for as long as the run allows, and the run allows until its hard deadline H
(``deadline-minutes``, counted from the driver start). A wind-down W starts
``WIND_DOWN_MINUTES * k`` before H; after W the run finishes with what it has and
still publishes an honest review. ``k = clamp(D / 60 min, 0.5, 1.0)`` scales every
margin for short deadlines D.

Phase limits (``RunClock.limits``), as ``RunLimits`` (absolute epochs, never paused).
Without a verifier (``verify=False``):

====================  =========================  ===========================
phase                 tools close at             last model call / wait ends
====================  =========================  ===========================
``explore``           W                          ``min(W + 300k, H - 600k)``
``structure``         (no tools)                 ``H - 360k``
``verify``            ``H - 720k``               ``H - 480k``
``verify-format``     (no tools)                 ``H - 240k``
====================  =========================  ===========================

The verifier keeps a reserved window: it may start until the verify cutoff
``V = H - 900k`` (``RunClock.verify_cutoff_epoch``; at least 180k of tools, then its
notes, the verify formatter and publishing before H), and not at or after it. In a
run with a verifier (``verify=True``) the explorers and the structure step end early
enough that the verifier can start before V, even when an explorer is rate-limited
the whole time:

- structure stop ``S_v = V - VERIFY_START_SLACK_S`` (90 s, not scaled: the launcher
  lets an agent process run ``AGENT_STOP_MARGIN_S`` = 60 s past its stop, plus 30 s
  for the merge and the activity hand-over);
- explore stop ``E_v = S_v - 240k`` (the structure formatter's window);
- explore tools close at ``T_v = E_v - 300k`` (the time explorers need to write notes).

The explore limits are then ``tools = min(W, T_v)``, ``stop = min(W + 300k, H - 600k,
E_v)``, and the structure stop is ``min(H - 360k, S_v)``; the verify phases are the
same with or without the flag. The run is wound down (``wound_down``) at W, or with a
verifier at the explorers' earlier wind-down ``min(W, T_v)``.

For D = 90 min: W = V = 75; without a verifier explore stop 80, structure 84; with a
verifier explore tools close at 64.5, explore stop 69.5, structure 73.5; verify tools
78 / stop 82, verify-format 86, publish by 90. GitHub rate-limit waits end by
``H - GITHUB_WAIT_MARGIN_S``.

The driver plans the clock (``plan_clock``) and passes the three epochs in the
``RunContext``; activities rebuild it with ``from_context``. The workflow code never
reads a clock.

``meter.json`` keys for rate-limit waits (written by the agent's meter, read raw by
the launcher and the driver; all optional, older files lack them):

- ``rl_wait_seconds`` (float): total time the phase clocks were paused for waits.
- ``rl_waits`` (int): waits started (runner backoff and shared-cooldown waits).
- ``rl_until_epoch`` (float or null): end of the wait in progress, else null.
- ``rl_last_code`` (str or null): error code of the last temporary failure.
- ``rl_streak`` (int): temporary failures in a row without a model reply.
- ``pending_since`` (float or null): start of the model call in flight.

Run-directory files: ``SNAPSHOT_FILE`` (the sealed ``PrSnapshot`` written by
fetch_pr, read by the driver's deadline backstop) and ``cooldown-<name>.json``
(``cooldown_path``: the shared per-model cooldown of one run).

This module has no SDK imports; the agent, the worker and the driver all use it.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from morning_star_review.core.contract import RunLimits
from morning_star_review.core.naming import slug

if TYPE_CHECKING:
    from morning_star_review.core.runctx import RunContext

__all__ = [
    "ACTIVITY_CEILING",
    "AGENT_STOP_MARGIN_S",
    "DEFAULT_DEADLINE_MINUTES",
    "EXECUTION_TIMEOUT",
    "GITHUB_WAIT_MARGIN_S",
    "MAX_DEADLINE_MINUTES",
    "MIN_DEADLINE_MINUTES",
    "MIN_WIND_DOWN_AFTER_S",
    "RL_FALLBACK_MAX_WAIT_S",
    "RUN_DEADLINE_STOP",
    "SNAPSHOT_FILE",
    "VERIFY_START_SLACK_S",
    "WIND_DOWN_MINUTES",
    "RunClock",
    "RunPhase",
    "cooldown_path",
    "format_wait",
    "from_context",
    "plan_clock",
]

MIN_DEADLINE_MINUTES = 20
MAX_DEADLINE_MINUTES = 300  # GitHub's job maximum is 360 minutes
DEFAULT_DEADLINE_MINUTES = 90
WIND_DOWN_MINUTES = 15  # wind-down starts this long before the deadline (times k)
# start_to_close of the long activities (explore, structure, verify); liveness is the
# heartbeat timeout, this only has to sit above any allowed deadline.
ACTIVITY_CEILING = timedelta(minutes=MAX_DEADLINE_MINUTES + 10)
EXECUTION_TIMEOUT = timedelta(minutes=MAX_DEADLINE_MINUTES + 30)
# Rate-limit waiting allowed per agent job when the job has no run limits.
RL_FALLBACK_MAX_WAIT_S = 3600.0
RUN_DEADLINE_STOP = "run_deadline"  # stop_reason of a phase cut short by the run limits
SNAPSHOT_FILE = "snapshot.json"
GITHUB_WAIT_MARGIN_S = 30.0
MIN_WIND_DOWN_AFTER_S = 60.0  # an overridden wind-down never starts earlier
# The launcher lets an agent process run this long past its job's stop_epoch (to end
# an interrupted turn and write result.json) before it stops the process.
AGENT_STOP_MARGIN_S = 60.0
# With a verifier, the structure step stops this long before the verify cutoff V (not
# scaled): an agent process past its stop, plus 30 s for merge and the hand-over.
VERIFY_START_SLACK_S = AGENT_STOP_MARGIN_S + 30.0

RunPhase = Literal["explore", "structure", "verify", "verify-format"]

# Margins before the hard deadline, in seconds at k = 1 (``RunClock.scale``).
_EXPLORE_AFTER_WIND_DOWN_S = 300.0
_EXPLORE_STOP_BEFORE_S = 600.0
_STRUCTURE_STOP_BEFORE_S = 360.0
_VERIFY_TOOLS_BEFORE_S = 720.0
_VERIFY_STOP_BEFORE_S = 480.0
_VERIFY_FORMAT_STOP_BEFORE_S = 240.0
# The verify cutoff V: the latest start of the verifier (its reserved window).
_VERIFY_START_BEFORE_S = 900.0
# With a verifier: the structure formatter's window before S_v, and the explorers'
# notes window before their stop (both times k).
_STRUCTURE_WINDOW_S = 240.0
_EXPLORE_NOTES_S = 300.0

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _scale(deadline_s: float) -> float:
    return min(1.0, max(0.5, deadline_s / 3600.0))


@dataclass(frozen=True)
class RunClock:
    """Start, wind-down and hard deadline of one run (wall-clock epoch seconds)."""

    start_epoch: float
    wind_down_epoch: float
    hard_epoch: float

    def __post_init__(self) -> None:
        epochs = (self.start_epoch, self.wind_down_epoch, self.hard_epoch)
        if not all(math.isfinite(e) for e in epochs):
            raise ValueError("run clock epochs must be finite")
        if not self.start_epoch <= self.wind_down_epoch <= self.hard_epoch:
            raise ValueError("run clock epochs must satisfy start <= wind-down <= hard")

    @property
    def deadline_s(self) -> float:
        """D: seconds from the start to the hard deadline."""
        return self.hard_epoch - self.start_epoch

    @property
    def deadline_minutes(self) -> int:
        return round(self.deadline_s / 60)

    @property
    def scale(self) -> float:
        """k = clamp(D / 60 min, 0.5, 1.0): margins shrink for short deadlines."""
        return _scale(self.deadline_s)

    @property
    def verify_cutoff_epoch(self) -> float:
        """V = H - 900k: the latest start of the verifier (``verify_may_start``)."""
        return self.hard_epoch - _VERIFY_START_BEFORE_S * self.scale

    def _verifying_stops(self) -> tuple[float, float, float]:
        """(T_v, E_v, S_v): explore tools close, explore stop and structure stop that
        let the verifier start before V (module docstring)."""
        k = self.scale
        structure = self.verify_cutoff_epoch - VERIFY_START_SLACK_S
        explore = structure - _STRUCTURE_WINDOW_S * k
        return explore - _EXPLORE_NOTES_S * k, explore, structure

    def limits(self, phase: RunPhase, *, verify: bool = False) -> RunLimits:
        """The run-level limits of an agent job in ``phase`` (see the module docstring).

        ``verify``: the run has a verifier, so explore and structure end in time for
        it to start before the verify cutoff V. Phases without tools (the formatters)
        get ``wind_down_epoch == stop_epoch``. For a degenerate (tiny) deadline the tool
        limit is pulled back to the stop.
        """
        k = self.scale
        h = self.hard_epoch
        if phase == "explore":
            stop = min(
                self.wind_down_epoch + _EXPLORE_AFTER_WIND_DOWN_S * k,
                h - _EXPLORE_STOP_BEFORE_S * k,
            )
            tools = self.wind_down_epoch
            if verify:
                tools_v, stop_v, _ = self._verifying_stops()
                stop, tools = min(stop, stop_v), min(tools, tools_v)
        elif phase == "structure":
            stop = h - _STRUCTURE_STOP_BEFORE_S * k
            if verify:
                stop = min(stop, self._verifying_stops()[2])
            tools = stop
        elif phase == "verify":
            stop = h - _VERIFY_STOP_BEFORE_S * k
            tools = h - _VERIFY_TOOLS_BEFORE_S * k
        elif phase == "verify-format":
            stop = tools = h - _VERIFY_FORMAT_STOP_BEFORE_S * k
        else:
            raise ValueError(f"unknown run phase {phase!r}")
        return RunLimits(wind_down_epoch=min(tools, stop), stop_epoch=stop)

    def explore_wind_down(self, *, verify: bool = False) -> float:
        """When explorers stop opening files: W, or earlier in a run with a verifier."""
        return self.limits("explore", verify=verify).wind_down_epoch

    def wound_down(self, now: float, *, verify: bool = False) -> bool:
        """True at or after the wind-down (with a verifier: the explorers' wind-down)."""
        return now >= self.explore_wind_down(verify=verify)

    def verify_may_start(self, now: float) -> bool:
        """The verifier only starts before the verify cutoff V (its reserved window)."""
        return now < self.verify_cutoff_epoch

    def github_wait_until(self) -> float:
        """GitHub rate-limit waits during the run must end by this epoch."""
        return self.hard_epoch - GITHUB_WAIT_MARGIN_S


def plan_clock(
    start_epoch: float, deadline_s: float, wind_down_after_s: float | None = None
) -> RunClock:
    """The clock for a run that started at ``start_epoch`` with deadline ``deadline_s``.

    The wind-down defaults to ``WIND_DOWN_MINUTES * k`` before the deadline.
    ``wind_down_after_s`` (a test knob, seconds after the start) can only move it
    earlier, and never earlier than ``MIN_WIND_DOWN_AFTER_S`` after the start.
    """
    if not math.isfinite(start_epoch):
        raise ValueError("start_epoch must be finite")
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("deadline_s must be a positive number of seconds")
    hard = start_epoch + deadline_s
    latest = hard - WIND_DOWN_MINUTES * 60 * _scale(deadline_s)
    wind_down = latest
    if wind_down_after_s is not None:
        if not math.isfinite(wind_down_after_s):
            raise ValueError("wind_down_after_s must be finite")
        wind_down = start_epoch + wind_down_after_s
    wind_down = min(max(wind_down, start_epoch + MIN_WIND_DOWN_AFTER_S), latest)
    # Tiny deadlines (tests): the default would lie before the start.
    wind_down = max(wind_down, start_epoch)
    return RunClock(start_epoch=start_epoch, wind_down_epoch=wind_down, hard_epoch=hard)


def from_context(ctx: RunContext) -> RunClock | None:
    """The run clock from the context, or None unless all three epochs are set."""
    start, wind_down, hard = ctx.run_started_epoch, ctx.wind_down_epoch, ctx.hard_epoch
    if start is None or wind_down is None or hard is None:
        return None
    return RunClock(start_epoch=start, wind_down_epoch=wind_down, hard_epoch=hard)


def cooldown_path(run_dir: Path, model: str) -> Path:
    """``<run_dir>/cooldown-<name>.json``: the shared cooldown file of ``model``.

    ``name`` is the model id when it is a safe file name, else a slug plus a hash
    (the same rule as the job directory names).
    """
    if _SAFE_NAME.match(model):
        name = model
    else:
        digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
        name = f"{slug(model)[:48] or 'model'}-{digest}"
    return run_dir / f"cooldown-{name}.json"


def format_wait(seconds: float) -> str:
    """A short duration: ``45s``, ``7m 05s``, ``1h 02m``."""
    total = round(seconds) if math.isfinite(seconds) and seconds > 0 else 0
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs:02d}s"
    hours, rest = divmod(total, 3600)
    return f"{hours}h {rest // 60:02d}m"
