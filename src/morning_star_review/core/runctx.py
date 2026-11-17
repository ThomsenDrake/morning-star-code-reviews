"""Process-global run context, set once by the driver before the worker starts.

It holds the pins (execution id, request hash), the seal key and the tokens. None of
it is ever written to disk, to the environment or into a payload. Secret fields are
excluded from ``repr`` so that a stray log line cannot leak them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from morning_star_review.core.contract import Mode
from morning_star_review.core.errors import MsreviewNoRunContext

__all__ = [
    "MIN_SEAL_KEY_BYTES",
    "RunContext",
    "clear_run_context",
    "run_context",
    "set_run_context",
]

MIN_SEAL_KEY_BYTES = 32


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

    def __post_init__(self) -> None:
        if len(self.seal_key) < MIN_SEAL_KEY_BYTES:
            raise ValueError(f"seal_key must have at least {MIN_SEAL_KEY_BYTES} bytes")
        if not self.execution_id:
            raise ValueError("execution_id must not be empty")
        if len(self.request_sha256) != 64:
            raise ValueError("request_sha256 must be a hex sha256")

    @property
    def run_dir(self) -> Path:
        """``<run_root>/<execution_id>``."""
        return Path(self.run_root) / self.execution_id

    def secrets(self) -> list[str]:
        """Secret strings to pass to ``redact`` as ``extra_secrets``."""
        values = (self.github_token, self.read_token, self.mistral_api_key)
        return [v for v in values if v]


_lock = threading.Lock()
_current: RunContext | None = None


def set_run_context(ctx: RunContext) -> None:
    """Install ``ctx``. Setting an equal context again is a no-op; a different one raises."""
    global _current
    with _lock:
        if _current is not None and _current != ctx:
            raise RuntimeError("a different run context is already set for this process")
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
