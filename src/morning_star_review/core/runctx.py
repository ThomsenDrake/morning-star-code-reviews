"""Process-global run context, set once by the driver before the worker starts.

It holds the pins (execution id, request hash), the seal key and the tokens. None of
it is ever written to disk, to the environment or into a payload. Secret fields are
excluded from ``repr`` so that a stray log line cannot leak them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from morning_star_review.core.contract import Mode
from morning_star_review.core.errors import MsreviewNoRunContext

__all__ = [
    "MIN_SEAL_KEY_BYTES",
    "RunContext",
    "TokenBox",
    "clear_run_context",
    "run_context",
    "set_run_context",
]

MIN_SEAL_KEY_BYTES = 32

log = logging.getLogger(__name__)


@dataclass(eq=False)
class TokenBox:
    """A GitHub App installation token that can be re-minted before it expires.

    Installation tokens live one hour. A slow run re-mints before posting (CLI mode;
    ``refresh`` runs the mint child again). ``history`` keeps every token this box
    ever held, so that redaction still covers a token that was replaced.
    """

    token: str = field(repr=False)
    minted_epoch: float
    refresh: Callable[[], Awaitable[str | None]] | None = field(default=None, repr=False)
    history: list[str] = field(default_factory=list, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    TOKEN_LIFETIME_S: ClassVar[float] = 3600.0
    # A token this close to its expiry is no longer used for a write.
    EXPIRY_MARGIN_S: ClassVar[float] = 60.0

    def __post_init__(self) -> None:
        if self.token and self.token not in self.history:
            self.history.insert(0, self.token)

    def is_valid(self, now: Callable[[], float] = time.time) -> bool:
        """True while the current token has more than ``EXPIRY_MARGIN_S`` left."""
        return bool(self.token) and (
            now() - self.minted_epoch < self.TOKEN_LIFETIME_S - self.EXPIRY_MARGIN_S
        )

    async def ensure_fresh(
        self, min_valid_s: float = 900.0, now: Callable[[], float] = time.time
    ) -> bool:
        """Re-mint when less than ``min_valid_s`` of the token's life is left.

        Returns True while the (possibly new) token is still valid. Never raises: a
        failing or empty refresh keeps the old token and returns its validity.
        """
        try:
            async with self._lock:
                age = now() - self.minted_epoch
                if self.refresh is not None and age > self.TOKEN_LIFETIME_S - min_valid_s:
                    await self._refresh(self.refresh, now)
                return self.is_valid(now)
        except Exception as exc:  # e.g. a lock used from another event loop
            log.warning("could not refresh the GitHub token: %s", type(exc).__name__)
            return self._valid_quietly(now)

    async def _refresh(
        self, refresh: Callable[[], Awaitable[str | None]], now: Callable[[], float]
    ) -> None:
        # The new token was minted at or after this moment; counting its life from
        # here errs on the safe side.
        before = now()
        try:
            new = await refresh()
        except Exception as exc:
            log.warning("could not refresh the GitHub token: %s", type(exc).__name__)
            return
        if not new:
            log.warning("could not refresh the GitHub token: no token returned")
            return
        if new not in self.history:
            self.history.append(new)
        self.token = new
        self.minted_epoch = before

    def _valid_quietly(self, now: Callable[[], float]) -> bool:
        try:
            return self.is_valid(now)
        except Exception:
            return False


@dataclass(frozen=True)
class RunContext:
    execution_id: str
    request_sha256: str  # sha256(canonical_json(ReviewRequest)), see seal.request_digest
    seal_key: bytes = field(repr=False)  # random; never written to disk or env
    run_root: str
    mode: Mode
    on_worker: bool  # False for local in-process runs (no workflow-id pin)
    github_token: str | None = field(repr=False)  # installation token or None
    read_token: str | None = field(repr=False)  # optional read-only token
    bot_login: str | None
    mistral_api_key: str = field(repr=False)  # forwarded to the agent subprocess env only
    agent_python: str
    cannot_post_reason: str | None = None
    # Operator opt-in (--telemetry / Action input): the agent subprocess keeps the
    # Agents SDK / OpenTelemetry defaults instead of the three telemetry-off switches.
    telemetry: bool = False
    # Run clock (core/runclock.py), wall-clock epoch seconds: driver start, wind-down
    # and the hard deadline. Activities read them; the workflow never does.
    run_started_epoch: float | None = None
    wind_down_epoch: float | None = None
    hard_epoch: float | None = None
    # CI deferred publish: publish/report_failure write a plan instead of posting.
    defer_publish: bool = False
    # Refreshable GitHub token (CLI re-mint); None = ``github_token`` as is.
    token_box: TokenBox | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.seal_key) < MIN_SEAL_KEY_BYTES:
            raise ValueError(f"seal_key must have at least {MIN_SEAL_KEY_BYTES} bytes")
        if not self.execution_id:
            raise ValueError("execution_id must not be empty")
        if len(self.request_sha256) != 64:
            raise ValueError("request_sha256 must be a hex sha256")
        epochs = (self.run_started_epoch, self.wind_down_epoch, self.hard_epoch)
        if any(e is not None and not math.isfinite(e) for e in epochs):
            raise ValueError("run clock epochs must be finite")
        start, wind_down, hard = epochs
        if (
            start is not None
            and wind_down is not None
            and hard is not None
            and not start <= wind_down <= hard
        ):
            raise ValueError("run clock epochs must satisfy start <= wind-down <= hard")

    @property
    def run_dir(self) -> Path:
        """``<run_root>/<execution_id>``."""
        return Path(self.run_root) / self.execution_id

    def current_github_token(self) -> str | None:
        """The token to write with: the box's current token when a box is set."""
        if self.token_box is not None:
            return self.token_box.token or None
        return self.github_token

    def secrets(self) -> list[str]:
        """Secret strings to pass to ``redact`` as ``extra_secrets``."""
        values = [self.github_token, self.read_token, self.mistral_api_key]
        if self.token_box is not None:
            values.extend(self.token_box.history)
            values.append(self.token_box.token)
        out: list[str] = []
        for v in values:
            if v and v not in out:
                out.append(v)
        return out


_lock = threading.Lock()
_current: RunContext | None = None


def set_run_context(ctx: RunContext) -> None:
    """Install ``ctx``. Setting an equal context again is a no-op; a different one raises.

    The no-op keeps the installed object (and its ``token_box``, which equality ignores).
    """
    global _current
    with _lock:
        if _current is not None:
            if _current != ctx:
                raise RuntimeError("a different run context is already set for this process")
            return
        _current = ctx


def run_context() -> RunContext:
    """The current context; raises ``MsreviewNoRunContext`` when none is set."""
    ctx = _current
    if ctx is None:
        raise MsreviewNoRunContext("no run context is set in this process")
    return ctx


def clear_run_context() -> None:
    """Forget the context (driver shutdown and tests)."""
    global _current
    with _lock:
        _current = None
