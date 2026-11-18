"""Request-shape canary (DESIGN 15.7): opt-in with MSREVIEW_CANARY=1.

Points the Agents SDK at a local capture server through the undocumented
``AGENTS_SDK_COMPLETION_GATEWAY_API_BASE`` setting (test only; the launcher never
forwards it). The server records each chat-completion request and answers 400, so
no model runs and no key is needed. The captured bodies must show:

- explorer and verifier: exactly our five tools, no ``response_format``;
- formatter: no tools and a strict ``json_schema`` ``response_format``;
- ``reasoning_effort`` as configured, and absent for effort ``default``;
- the same request keys with our SDK retry config (``completion=``,
  ``ratelimit.sdk_completion``) as with the SDK's default config.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from morning_star_review.agent import hooks, ratelimit, runner, tools
from morning_star_review.core.contract import AgentBudgets, AgentJob

FIVE = {"list_repo_files", "read_repo_file", "read_base_file", "search_repo", "show_file_diff"}


class _Capture(BaseHTTPRequestHandler):
    bodies: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            type(self).bodies.append(json.loads(raw))
        except ValueError:
            type(self).bodies.append({"_raw": raw.decode("utf-8", "replace")})
        payload = b'{"object": "error", "message": "canary", "type": "invalid_request_error"}'
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def _server() -> Iterator[str]:
    # One server per module: the SDK caches its provider settings per process.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def capture(_server: str, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _Capture.bodies.clear()
    monkeypatch.setenv("AGENTS_SDK_COMPLETION_GATEWAY_API_BASE", _server)
    monkeypatch.setenv("MISTRAL_API_KEY", "canary-not-a-key")
    return _Capture.bodies


def _job(tmp_path: Path, effort: str, target: str = "review") -> AgentJob:
    return AgentJob(
        phase="explore",
        execution_id="msreview-canary",
        repo_dir=str(tmp_path),
        head_sha="0" * 40,
        merge_base_sha="0" * 40,
        input_path="prompt.txt",
        input_sha256="0" * 64,
        variant="full",
        model="mistral-large-4",
        effort=effort,  # type: ignore[arg-type]
        budgets=AgentBudgets(
            max_tool_calls=5,
            max_llm_calls=3,
            grace_llm_calls=1,
            explore_seconds=60,
            grace_seconds=10,
            rescue_seconds=10,
            max_context_tokens=100_000,
            auto_compact_threshold=200_000,
            max_tool_output_chars=24_000,
            max_cost_usd=1.0,
        ),
        price_in_per_m=0.7,
        price_out_per_m=2.1,
        changed_files=[],
        meter_path="meter.json",
        coverage_path="coverage.json",
        result_path="result.json",
        partial_notes_path="notes.partial.txt",
        format_target=target,  # type: ignore[arg-type]
    )


def _install(job: AgentJob) -> None:
    hooks.install_meter(
        hooks.Meter.start(
            phase="explore",
            model=job.model,
            budgets=job.budgets,
            price_in_per_m=0.7,
            price_out_per_m=2.1,
            path=None,
        )
    )


def _tool_names(body: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or []}


async def test_explorer_and_verifier_requests(
    capture: list[dict[str, Any]], tmp_path: Path
) -> None:
    for build, effort in ((runner.build_explorer, "high"), (runner.build_verifier, "default")):
        capture.clear()
        job = _job(tmp_path, effort)
        _install(job)
        tools.configure(job, tmp_path, None)
        await build(job).run("canary", timeout=60)
        assert capture, "no completion request reached the capture server"
        body = capture[0]
        assert _tool_names(body) == FIVE
        assert "response_format" not in body or body["response_format"] in (None, {})
        if effort == "default":
            assert "reasoning_effort" not in body
        else:
            assert body.get("reasoning_effort") == effort


async def test_formatter_request(capture: list[dict[str, Any]], tmp_path: Path) -> None:
    for target in ("review", "verify"):
        capture.clear()
        job = _job(tmp_path, "low", target)
        _install(job)
        await runner.build_formatter(job).run("<notes>\nNO FINDINGS\n</notes>", timeout=60)
        assert capture, "no completion request reached the capture server"
        body = capture[0]
        assert not body.get("tools")
        fmt = body.get("response_format") or {}
        assert fmt.get("type") == "json_schema"
        assert fmt.get("json_schema", {}).get("strict") is True
        assert body.get("reasoning_effort") == "low"


async def test_sdk_retry_config_keeps_the_request_shape(
    capture: list[dict[str, Any]], tmp_path: Path
) -> None:
    assert ratelimit.sdk_completion("mistral-large-4") is not None
    cases = (
        (runner.build_explorer, "high", "review"),
        (runner.build_verifier, "default", "review"),
        (runner.build_formatter, "low", "review"),
        (runner.build_formatter, "default", "verify"),
    )
    for build, effort, target in cases:
        bodies = []
        for sdk_retry in (True, False):
            capture.clear()
            job = _job(tmp_path, effort, target)
            _install(job)
            tools.configure(job, tmp_path, None)
            agent = build(job, sdk_retry=sdk_retry)
            assert (agent.completion is not None) is sdk_retry
            await agent.run("<notes>\nNO FINDINGS\n</notes>", timeout=60)
            assert capture, "no completion request reached the capture server"
            bodies.append(capture[0])
        configured, default = bodies
        assert set(configured) == set(default), (build.__name__, effort, target)
        for key in ("model", "response_format", "reasoning_effort", "tools"):
            assert configured.get(key) == default.get(key), key
        if build is runner.build_formatter:
            assert (configured.get("response_format") or {}).get("type") == "json_schema"
        if effort == "default":
            assert "reasoning_effort" not in configured
        else:
            assert configured.get("reasoning_effort") == effort
