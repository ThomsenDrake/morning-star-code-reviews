"""Rate-limit policy of the agent runner: rate limits slow a phase down, never end it.

- Temporary model errors (``RL_CODES``: HTTP 429, no capacity, 409, 5xx) are retried
  without a count limit, bounded only by the job's run limits, the cost cap or, for
  jobs without run limits, ``RL_FALLBACK_MAX_WAIT_S`` of waiting. Lost streams and
  similar (``TRANSIENT_CODES``) give up after ``TRANSIENT_MAX_STREAK`` failures in a
  row, so a deterministic failure cannot loop forever.
- ``backoff``: 15, 30, 60, 120, 180, 180, ... seconds for the n-th temporary failure in
  a row (plus 0-25 % jitter), at least the provider's Retry-After (capped at 300 s).
- The SDK tries every model call ``SDK_MAX_ATTEMPTS`` times itself (about 10 s) and
  waits at most ``SDK_RETRY_AFTER_CAP_S`` per Retry-After (``sdk_completion``), so long
  waits happen here, where the meter sees and pauses them.
- A shared cooldown file per model (``<run_dir>/cooldown-<name>.json``) lets the agent
  processes of one run wait out a rate limit together. It is advisory: torn or missing
  files read as "no cooldown", and concurrent writers may overwrite each other.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from morning_star_review.core.jsonio import write_text_atomic

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "JITTER",
    "RETRY_AFTER_MAX_S",
    "RL_CODES",
    "SDK_MAX_ATTEMPTS",
    "SDK_RETRY_AFTER_CAP_S",
    "TEMPORARY_CODES",
    "TRANSIENT_CODES",
    "TRANSIENT_MAX_STREAK",
    "backoff",
    "extend_cooldown",
    "read_cooldown",
    "retry_after_of",
    "sdk_completion",
]

# The provider refused the call before any output (nothing billed): wait and try again,
# for as long as the run allows.
RL_CODES = frozenset({"completion_rate_limited", "completion_unavailable"})
# The call failed on the way (lost stream, HTTP 408): retried a bounded number of times.
TRANSIENT_CODES = frozenset({"completion_timed_out", "completion_failed"})
TEMPORARY_CODES = RL_CODES | TRANSIENT_CODES

BACKOFF_BASE_S = 15.0
BACKOFF_MAX_S = 180.0
JITTER = 0.25
RETRY_AFTER_MAX_S = 300.0
TRANSIENT_MAX_STREAK = 6
SDK_MAX_ATTEMPTS = 5
SDK_RETRY_AFTER_CAP_S = 60.0

COOLDOWN_MAX_BYTES = 4096
# A cooldown never reaches further ahead than this (a garbled or hostile file).
COOLDOWN_MAX_AHEAD_S = 2 * RETRY_AFTER_MAX_S


def _log(message: str) -> None:
    print(f"msreview-agent: {message}", file=sys.stderr, flush=True)


def backoff(
    streak: int,
    retry_after: float | None = None,
    *,
    rand: Callable[[float, float], float] = random.uniform,
) -> float:
    """Seconds to wait after the ``streak``-th temporary failure in a row."""
    n = max(1, int(streak))
    base = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** min(n - 1, 16))
    delay = base + rand(0.0, base * JITTER)
    if retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
        delay = max(delay, min(retry_after, RETRY_AFTER_MAX_S))
    return delay


def retry_after_of(details: Any) -> float | None:
    """``details["retry_after_seconds"]`` of a failed turn's error, when it is a number."""
    if not isinstance(details, dict):
        return None
    value = details.get("retry_after_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            raw = fh.read(COOLDOWN_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > COOLDOWN_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _until_of(data: dict[str, Any] | None, model: str | None) -> float | None:
    if data is None or (model is not None and data.get("model") != model):
        return None
    value = data.get("until_epoch")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    until = float(value)
    return until if math.isfinite(until) else None


def read_cooldown(
    path: str | Path | None, model: str | None = None, *, now: float | None = None
) -> float | None:
    """The shared cooldown's end (epoch seconds), or ``None`` (no file, torn, other model).

    With ``now``, the end is capped at ``now + COOLDOWN_MAX_AHEAD_S``.
    """
    if path is None:
        return None
    until = _until_of(_load(Path(path)), model)
    if until is not None and now is not None:
        until = min(until, now + COOLDOWN_MAX_AHEAD_S)
    return until


def extend_cooldown(
    path: str | Path | None,
    model: str,
    until: float,
    streak: int,
    *,
    now: float | None = None,
) -> float:
    """Move the shared cooldown of ``model`` to at least ``until``; return its end.

    The end is the later of ``until`` and the file's current end (capped like
    ``read_cooldown``). Never raises: a failed write only loses the sharing.
    """
    stamp = time.time() if now is None else now
    if path is None:
        return until
    p = Path(path)
    current = read_cooldown(p, model, now=stamp)
    end = until if current is None else max(current, until)
    data = {
        "model": model,
        "until_epoch": end,
        "streak": max(0, int(streak)),
        "updated_epoch": stamp,
    }
    try:
        write_text_atomic(p, json.dumps(data) + "\n")
    except OSError as exc:
        _log(f"cannot write the shared cooldown file ({type(exc).__name__})")
    return end


_SDK_CONFIG_WARNED = False


def sdk_completion(model: str) -> Any | None:
    """The completion config with the SDK retry policy of this module, or ``None``.

    ``agents.Agent(completion=...)`` with this config sends the same requests as the
    default config; only the SDK's Retry-After wait is capped. The config classes are
    not re-exported by ``mistralai.agents``, so a failing import or construction falls
    back to the SDK defaults (logged once per process).
    """
    global _SDK_CONFIG_WARNED
    try:
        from mistralai.vibe.harness.runtime.completion import RetryConfig
        from mistralai.vibe.harness.runtime.completion.config import MistralCompletionConfig

        return MistralCompletionConfig(
            model=model,
            retry=RetryConfig(
                max_attempts=SDK_MAX_ATTEMPTS,
                retry_after_max_wait_seconds=SDK_RETRY_AFTER_CAP_S,
            ),
        )
    except Exception:  # any SDK change: keep running with its defaults
        if not _SDK_CONFIG_WARNED:
            _SDK_CONFIG_WARNED = True
            _log("sdk retry config unavailable; using SDK defaults")
        return None
