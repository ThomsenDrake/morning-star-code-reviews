"""Fixtures for scripted-model tests: a tiny git repository and agent job directories."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from morning_star_review.core.contract import (
    AgentBudgets,
    AgentJob,
    Candidate,
    ChangedFileRef,
    RunLimits,
)

BASE_FILES = {
    "shop/money.py": (
        "def prorate(amount, days_used, days_total):\n"
        '    """Share of amount for days_used out of days_total."""\n'
        "    return amount * days_used / days_total\n"
    ),
    "shop/service.py": (
        "from shop.money import prorate\n\n\n"
        "def refund(amount, used, total):\n"
        "    return amount - prorate(amount, used, total)\n"
    ),
}
HEAD_SERVICE = (
    "from shop.money import prorate\n\n\n"
    "def refund(amount, used, total):\n"
    "    share = prorate(amount, total, used)\n"
    "    return amount - share\n"
)
DIFF_TEXT = (
    "=== shop/service.py (modified)\n"
    "@@ new line 4 @@ def refund\n"
    "    4   def refund(amount, used, total):\n"
    "        -     return amount - prorate(amount, used, total)\n"
    "    5 +     share = prorate(amount, total, used)\n"
    "    6 +     return amount - share\n"
)
PROMPT = (
    '<untrusted-ab12cd34 kind="diff">\n' + DIFF_TEXT + "</untrusted-ab12cd34>\n"
    "Budget: about 10 tool calls.\n"
)


def _git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LANG": "C.UTF-8",
    }
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
    res = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in BASE_FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "shop/service.py").write_text(HEAD_SERVICE)
    _git(repo, "commit", "-q", "-am", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def budgets(**overrides: Any) -> AgentBudgets:
    values: dict[str, Any] = {
        "max_tool_calls": 10,
        "max_llm_calls": 12,
        "grace_llm_calls": 2,
        "explore_seconds": 300,
        "grace_seconds": 60,
        "rescue_seconds": 60,
        "max_context_tokens": 140_000,
        "auto_compact_threshold": 200_000,
        "max_tool_output_chars": 24_000,
        "max_cost_usd": 5.0,
    }
    values.update(overrides)
    return AgentBudgets(**values)


def candidate(cid: str, line: int = 5, severity: str = "high") -> Candidate:
    return Candidate(
        candidate_id=cid,
        votes=["zai-glm-5-3", "mistral-large-4"],
        path="shop/service.py",
        line=line,
        severity=severity,  # type: ignore[arg-type]
        category="correctness",
        title=f"Arguments swapped ({cid})",
        evidence="    share = prorate(amount, total, used)",
        explanation="prorate takes days_used before days_total.",
    )


@dataclass
class JobKit:
    root: Path
    repo: Path
    base: str
    head: str

    def make(
        self,
        phase: str,
        *,
        name: str | None = None,
        text: str = PROMPT,
        model: str = "mistral-large-4",
        effort: str = "default",
        format_target: str = "review",
        candidates: list[Candidate] | None = None,
        meter: dict[str, Any] | None = None,
        run_limits: RunLimits | None = None,
        cooldown_path: Path | None = None,
        **budget_overrides: Any,
    ) -> Path:
        job_dir = self.root / (name or f"job-{phase}")
        job_dir.mkdir()
        (job_dir / "diffs").mkdir()
        (job_dir / "diffs" / "0.txt").write_text(DIFF_TEXT)
        input_name = "notes.txt" if phase == "format" else "prompt.txt"
        (job_dir / input_name).write_text(text)
        if candidates is not None:
            data = [c.model_dump(mode="json") for c in candidates]
            (job_dir / "candidates.json").write_text(json.dumps(data))
        job = AgentJob(
            phase=phase,  # type: ignore[arg-type]
            execution_id="msreview-test-1",
            repo_dir=str(self.repo),
            head_sha=self.head,
            merge_base_sha=self.base,
            input_path=input_name,
            input_sha256=hashlib.sha256(text.encode()).hexdigest(),
            variant="full",
            model=model,
            effort=effort,  # type: ignore[arg-type]
            budgets=budgets(**budget_overrides),
            price_in_per_m=0.70,
            price_out_per_m=2.10,
            changed_files=[
                ChangedFileRef(
                    path="shop/service.py",
                    previous_path=None,
                    status="modified",
                    diff_file="diffs/0.txt",
                )
            ],
            meter_path="meter.json",
            coverage_path="coverage.json",
            result_path="result.json",
            partial_notes_path="notes.partial.txt",
            format_target=format_target,  # type: ignore[arg-type]
            run_limits=run_limits,
            cooldown_path=str(cooldown_path) if cooldown_path is not None else None,
        )
        (job_dir / "job.json").write_text(job.model_dump_json())
        if meter is not None:
            (job_dir / "meter.json").write_text(json.dumps(meter))
        return job_dir


@pytest.fixture(scope="session")
def _repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    return make_repo(tmp_path_factory.mktemp("agentrepo"))


@pytest.fixture
def kit(tmp_path: Path, _repo: tuple[Path, str, str]) -> JobKit:
    repo, base, head = _repo
    return JobKit(root=tmp_path, repo=repo, base=base, head=head)


@pytest.fixture(autouse=True)
def _no_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("AGENTS_SDK_TELEMETRY_ENABLED", "0")
    monkeypatch.setenv("AGENTS_SDK_STUDIO_TRACING_ENABLED", "0")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    yield
    from morning_star_review.agent import hooks

    hooks.install_meter(None)


ReadJson = Callable[[Path, str], Any]


@pytest.fixture
def read_json() -> ReadJson:
    def read(job_dir: Path, name: str) -> Any:
        return json.loads((job_dir / name).read_text())

    return read


def usage_model(steps: list[Any], *, tokens_in: int = 1000, tokens_out: int = 100) -> Any:
    """A ScriptedModel that reports token usage and can end a reply with ``length``.

    Steps are ``testing.call(...)``, a list of them (parallel calls in one reply),
    ``testing.reply(...)`` or ``("length", text)``.
    """
    from mistralai.agents import testing
    from mistralai.vibe.harness.core.protocol import TextContentBlock
    from mistralai.vibe.harness.runtime.completion.models import (
        CompletionUsage,
        ModelAssistantContentDelta,
        ModelCompletionFinished,
        ModelToolCallArgumentsDelta,
        ModelToolCallStartedDelta,
    )

    class UsageModel(testing.ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])
            self.steps = list(steps)
            self.used = 0

        def __deepcopy__(self, memo: dict[int, object]) -> UsageModel:
            memo[id(self)] = self
            return self

        async def complete(self, request: Any) -> Any:
            if self.used >= len(self.steps):
                raise AssertionError(f"model call {self.used + 1} has no scripted step")
            step = self.steps[self.used]
            self.used += 1
            usage = CompletionUsage(input_tokens=tokens_in, output_tokens=tokens_out)
            if isinstance(step, testing.ToolCallStep | list):
                # A list of tool calls is one reply with parallel calls.
                calls = step if isinstance(step, list) else [step]
                for i, c in enumerate(calls):
                    call_id = f"call_{request.action_id}_{i}"
                    yield ModelToolCallStartedDelta(call_id=call_id, name=c.name)
                    yield ModelToolCallArgumentsDelta(call_id=call_id, json=json.dumps(c.arguments))
                yield ModelCompletionFinished(finish_reason="tool_call", usage=usage)
                return
            finish = "stop"
            text = step.text if isinstance(step, testing.ReplyStep) else step[1]
            if isinstance(step, tuple):
                finish = step[0]
            yield ModelAssistantContentDelta(content=[TextContentBlock(text=text)])
            yield ModelCompletionFinished(finish_reason=finish, usage=usage)

        def assert_consumed(self) -> None:
            assert self.used == len(self.steps), f"used {self.used} of {len(self.steps)} steps"

    return UsageModel()


NOTES_ONE = """FINDING
file: shop/service.py
line: 5
severity: high
category: correctness
title: prorate arguments are swapped
evidence:     share = prorate(amount, total, used)
why: prorate expects days_used before days_total, so refunds use the inverse share.
fix: call prorate(amount, used, total).
suggestion:
<<<
    share = prorate(amount, used, total)
>>>
END FINDING
SUMMARY: Checked the refund change against prorate in shop/money.py; the call swaps two arguments.
REVIEW COMPLETE
"""


# ------------------------------------------------------------------ failing models, fake time

FAILURES = {
    # step: (failure kind, HTTP status, details reason, retryable) -> the SDK's error code
    "429": ("provider", 429, "rate_limited", True),  # completion_rate_limited
    "capacity": ("provider", 429, "service_tier_capacity_exceeded", True),  # unavailable
    "503": ("provider", 503, "unavailable", True),  # completion_unavailable
    "400": ("provider", 400, "invalid_request_error", False),  # completion_rejected
    "lost": ("incomplete", None, None, True),  # completion_failed (a lost stream)
}


def failure(code: str, extra: dict[str, Any] | None = None) -> Any:
    """The SDK exception a provider call raises for the failing step ``code``."""
    from mistralai.vibe.harness.runtime.completion.models import (
        CompletionError,
        CompletionFailure,
    )

    kind, status, reason, retryable = FAILURES[code]
    details: dict[str, Any] = {}
    if status is not None:
        details = {"reason": reason, "status_code": status}
    details.update(extra or {})
    message = "Rate limit exceeded" if status == 429 else f"failure {code}"
    if code == "400":
        message = "Invalid model"
    return CompletionError(
        CompletionFailure(
            kind=kind,  # type: ignore[arg-type]
            retryable=retryable,
            status_code=status,
            message=message,
            details=details or None,
        )
    )


def flaky_model(steps: list[Any], clock: Callable[[], float] | None = None) -> Any:
    """A ScriptedModel whose steps may also fail.

    A failing step is a key of ``FAILURES`` or ``(key, extra_details)``, for example
    ``("429", {"retry_after_seconds": 200})``. ``requests`` keeps the last user message
    of every model call; ``times`` the ``clock`` value at every call (when given).
    """
    from mistralai.agents import testing

    class FlakyModel(testing.ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])
            self.steps = list(steps)
            self.used = 0
            self.requests: list[str] = []
            self.times: list[float] = []

        def __deepcopy__(self, memo: dict[int, object]) -> FlakyModel:
            memo[id(self)] = self
            return self

        async def complete(self, request: Any) -> Any:
            assert self.used < len(self.steps), f"model call {self.used + 1} has no step"
            step = self.steps[self.used]
            self.used += 1
            users = [m for m in request.messages if getattr(m, "role", "") == "user"]
            self.requests.append(str(users[-1].content) if users else "")
            if clock is not None:
                self.times.append(clock())
            if isinstance(step, str):
                raise failure(step)
            if isinstance(step, tuple):
                raise failure(step[0], step[1])
            inner = testing.ScriptedModel([step])
            async for item in inner.complete(request):
                yield item

        def assert_consumed(self) -> None:
            assert self.used == len(self.steps), f"used {self.used} of {len(self.steps)} steps"

    return FlakyModel()


class FakeTime:
    """A clock for the meter and a sleep that advances it.

    ``real`` makes every sleep also wait that many real seconds (an awaited hook that
    really sleeps); otherwise a sleep only yields to the event loop.
    """

    def __init__(self, real: float = 0.0) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []
        self.real = real

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        import asyncio

        self.sleeps.append(seconds)
        await asyncio.sleep(self.real)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    """Fake time for the runner's backoff and the hook's cooldown waits."""
    from morning_star_review.agent import hooks, runner

    ft = FakeTime()
    monkeypatch.setattr(runner, "_sleep", ft.sleep)
    monkeypatch.setattr(hooks, "_sleep", ft.sleep)
    return ft
