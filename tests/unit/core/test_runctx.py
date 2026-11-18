"""Run context: clock epochs, deferred publish flag and the refreshable GitHub token."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from morning_star_review.core import runctx
from morning_star_review.core.runctx import RunContext, TokenBox

OLD = "ghs_old0000000000000000000000000000000000"
NEW = "ghs_new1111111111111111111111111111111111"
NEWER = "ghs_newer22222222222222222222222222222222"
MIN = 60.0
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def at_minute(self, m: float) -> Clock:
        self.t = T0 + m * MIN
        return self


class Refresher:
    """A fake re-mint: returns the queued tokens (an exception is raised instead)."""

    def __init__(self, *results: str | Exception | None) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        await asyncio.sleep(0)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def box(refresh: Callable[[], Awaitable[str | None]] | None = None) -> TokenBox:
    return TokenBox(token=OLD, minted_epoch=T0, refresh=refresh)


def ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "execution_id": "msreview-cli-o-r-pr1-aaaaaaa-1234abcd",
        "request_sha256": "0" * 64,
        "seal_key": b"k" * 32,
        "run_root": "/tmp/runs",
        "mode": "cli",
        "on_worker": False,
        "github_token": OLD,
        "read_token": "ghp_read",
        "bot_login": "msreview[bot]",
        "mistral_api_key": "mistral-key",
        "agent_python": "/usr/bin/python3",
    }
    base.update(kw)
    return RunContext(**base)


# --------------------------------------------------------------------------------------
# TokenBox
# --------------------------------------------------------------------------------------


def test_box_records_the_first_token_in_its_history() -> None:
    b = box()
    assert b.history == [OLD]
    assert TokenBox.TOKEN_LIFETIME_S == 3600.0


async def test_no_refresh_before_45_minutes() -> None:
    refresh = Refresher(NEW)
    b = box(refresh)
    for minute in (0, 10, 44, 45):  # 45 min = exactly 15 min left: still fresh enough
        assert await b.ensure_fresh(900, now=Clock().at_minute(minute))
    assert refresh.calls == 0
    assert b.token == OLD


async def test_refresh_after_45_minutes() -> None:
    refresh = Refresher(NEW)
    b = box(refresh)
    clock = Clock().at_minute(46)
    assert await b.ensure_fresh(now=clock)
    assert refresh.calls == 1
    assert b.token == NEW
    assert b.minted_epoch == clock.t
    assert b.history == [OLD, NEW]
    # The new token is fresh: no second refresh until 45 minutes after the re-mint.
    assert await b.ensure_fresh(now=clock.at_minute(46 + 44))
    assert refresh.calls == 1


async def test_min_valid_s_moves_the_refresh_point() -> None:
    refresh = Refresher(NEW)
    b = box(refresh)
    assert await b.ensure_fresh(min_valid_s=300, now=Clock().at_minute(50))
    assert refresh.calls == 0
    assert await b.ensure_fresh(min_valid_s=300, now=Clock().at_minute(55.5))
    assert refresh.calls == 1 and b.token == NEW


@pytest.mark.parametrize("failure", [RuntimeError("mint child failed"), None, ""])
async def test_failed_refresh_keeps_the_old_token(failure: Exception | str | None) -> None:
    refresh = Refresher(failure, failure)
    b = box(refresh)
    assert await b.ensure_fresh(now=Clock().at_minute(50))  # old token still valid
    assert b.token == OLD and b.minted_epoch == T0
    assert b.history == [OLD]
    assert not await b.ensure_fresh(now=Clock().at_minute(59.5))  # old token about to expire
    assert refresh.calls == 2
    assert b.token == OLD


async def test_failed_refresh_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    b = box(Refresher(RuntimeError(f"boom {NEW}")))
    with caplog.at_level("WARNING"):
        assert await b.ensure_fresh(now=Clock().at_minute(50))
    assert "RuntimeError" in caplog.text
    assert NEW not in caplog.text and OLD not in caplog.text


async def test_without_a_refresher_only_validity_is_reported() -> None:
    b = box(None)
    assert await b.ensure_fresh(now=Clock().at_minute(58.9))
    assert not await b.ensure_fresh(now=Clock().at_minute(59))
    assert b.token == OLD


def test_validity_flag() -> None:
    b = box()
    assert b.is_valid(Clock().at_minute(0))
    assert b.is_valid(Clock().at_minute(58.99))
    assert not b.is_valid(Clock().at_minute(59))  # less than 60 s left
    assert not b.is_valid(Clock().at_minute(61))
    assert not TokenBox(token="", minted_epoch=T0).is_valid(Clock())


async def test_concurrent_callers_refresh_once() -> None:
    refresh = Refresher(NEW, NEWER)
    b = box(refresh)
    clock = Clock().at_minute(50)
    results = await asyncio.gather(*(b.ensure_fresh(now=clock) for _ in range(5)))
    assert results == [True] * 5
    assert refresh.calls == 1
    assert b.token == NEW


async def test_a_second_refresh_keeps_every_token_in_history() -> None:
    b = box(Refresher(NEW, NEWER))
    await b.ensure_fresh(now=Clock().at_minute(50))
    await b.ensure_fresh(now=Clock().at_minute(50 + 46))
    assert b.token == NEWER
    assert b.history == [OLD, NEW, NEWER]


def test_box_repr_hides_tokens() -> None:
    b = box(Refresher(NEW))
    b.history.append(NEW)
    text = repr(b)
    assert OLD not in text and NEW not in text
    assert "minted_epoch" in text


# --------------------------------------------------------------------------------------
# RunContext
# --------------------------------------------------------------------------------------


def test_new_fields_default_off() -> None:
    c = ctx()
    assert (c.run_started_epoch, c.wind_down_epoch, c.hard_epoch) == (None, None, None)
    assert c.defer_publish is False
    assert c.token_box is None
    assert c.current_github_token() == OLD
    assert ctx(github_token=None).current_github_token() is None


async def test_current_token_and_secrets_follow_the_box() -> None:
    b = box(Refresher(NEW))
    c = ctx(token_box=b)
    assert c.current_github_token() == OLD
    await b.ensure_fresh(now=Clock().at_minute(50))
    assert c.current_github_token() == NEW
    secrets = c.secrets()
    assert OLD in secrets and NEW in secrets
    assert "ghp_read" in secrets and "mistral-key" in secrets
    assert len(secrets) == len(set(secrets))


def test_secrets_without_a_box_are_unchanged() -> None:
    assert ctx().secrets() == [OLD, "ghp_read", "mistral-key"]
    assert ctx(github_token=None, read_token=None).secrets() == ["mistral-key"]


def test_context_repr_hides_the_box_and_tokens() -> None:
    c = ctx(token_box=box(), defer_publish=True, run_started_epoch=T0)
    text = repr(c)
    assert OLD not in text and "mistral-key" not in text and "token_box" not in text
    assert "defer_publish=True" in text


def test_box_does_not_change_context_equality() -> None:
    a = ctx(token_box=box())
    b = ctx(token_box=box(Refresher(NEW)))
    assert a == b and hash(a) == hash(b)
    runctx.clear_run_context()
    try:
        runctx.set_run_context(a)
        runctx.set_run_context(b)  # equal context: a no-op, not an error
        assert runctx.run_context() is a
    finally:
        runctx.clear_run_context()


def test_clock_epochs_are_validated() -> None:
    ok = ctx(run_started_epoch=T0, wind_down_epoch=T0 + 4500, hard_epoch=T0 + 5400)
    assert ok.hard_epoch == T0 + 5400
    assert ctx(run_started_epoch=T0).wind_down_epoch is None  # partial: allowed, no clock
    with pytest.raises(ValueError, match="start <= wind-down <= hard"):
        ctx(run_started_epoch=T0, wind_down_epoch=T0 + 6000, hard_epoch=T0 + 5400)
    with pytest.raises(ValueError, match="finite"):
        ctx(hard_epoch=float("inf"))
