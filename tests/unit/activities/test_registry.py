"""Activity registry, policies and the guard-first rule (DESIGN 5.2, 16 WP6)."""

from __future__ import annotations

import ast
import importlib
import inspect
import typing
from datetime import timedelta
from pathlib import Path

import pytest
from mistralai.workflows import get_workflow_definition

from morning_star_review import activities
from morning_star_review.core import contract
from morning_star_review.core.contract import AgentBudgets, ResolvedBudgets
from morning_star_review.workflow import (
    EXECUTION_TIMEOUT,
    SEARCH_KEYS,
    WF_NAME,
    CodeReviewWorkflow,
)

ACTIVITIES_DIR = Path(activities.__file__).resolve().parent

# name -> (start_to_close, heartbeat, max attempts, backoff)
EXPECTED = {
    "msreview_fetch_pr": (timedelta(minutes=3), None, 6, 3.0),
    "msreview_prepare": (timedelta(minutes=10), timedelta(seconds=60), 3, 2.0),
    "msreview_explore": (timedelta(minutes=35), timedelta(seconds=60), 2, 2.0),
    "msreview_structure": (timedelta(minutes=8), timedelta(seconds=60), 3, 2.0),
    "msreview_merge": (timedelta(minutes=2), None, 2, 2.0),
    "msreview_verify": (timedelta(minutes=40), timedelta(seconds=60), 2, 2.0),
    "msreview_finalize": (timedelta(minutes=2), None, 2, 2.0),
    "msreview_publish": (timedelta(minutes=5), None, 6, 3.0),
    "msreview_report_failure": (timedelta(minutes=2), None, 3, 2.0),
}


def test_registry_names_and_policies() -> None:
    assert set(activities.ACTIVITY_NAMES) == set(EXPECTED)
    for fn in activities.ACTIVITIES:
        start, heartbeat, attempts, backoff = EXPECTED[fn.__name__]
        params = fn.__wf_activity_params__
        assert params["start_to_close_timeout_seconds"] == start.total_seconds(), fn.__name__
        expected_hb = heartbeat.total_seconds() if heartbeat else None
        assert params["heartbeat_timeout_seconds"] == expected_hb, fn.__name__
        assert params["retry_policy_max_attempts"] == attempts, fn.__name__
        assert params["retry_policy_backoff_coefficient"] == backoff, fn.__name__


def test_activity_timeouts_cover_the_agent_budgets() -> None:
    from morning_star_review.core.contract import ResolvedBudgets

    caps = ResolvedBudgets(explorer_minutes=25, verifier_minutes=25)
    worst = 25 * 60 + caps.grace_seconds + caps.rescue_seconds + 90
    assert worst < EXPECTED["msreview_explore"][0].total_seconds()
    assert worst < EXPECTED["msreview_verify"][0].total_seconds()


def test_verify_fits_the_verifier_and_its_formatter() -> None:
    """At the 25-minute cap the verifier, then a verify formatter run, end in time."""
    from morning_star_review.activities.common import launch_timeout
    from morning_star_review.activities.structure import FORMAT_SECONDS, STRUCTURE_TIMEOUT
    from morning_star_review.activities.verify import ACTIVITY_MARGIN, VERIFY_TIMEOUT

    caps = ResolvedBudgets(explorer_minutes=25, verifier_minutes=25)
    verifier = 25 * 60 + caps.grace_seconds + caps.rescue_seconds + 90
    fmt_budgets = AgentBudgets(
        max_tool_calls=0,
        max_llm_calls=4,
        grace_llm_calls=0,
        explore_seconds=FORMAT_SECONDS,
        grace_seconds=30,
        rescue_seconds=0,
        max_context_tokens=1000,
        auto_compact_threshold=1000,
        max_tool_output_chars=1000,
        max_cost_usd=1.0,
    )
    formatter = launch_timeout(fmt_budgets, STRUCTURE_TIMEOUT)
    assert verifier + formatter + ACTIVITY_MARGIN <= VERIFY_TIMEOUT.total_seconds()


def test_workflow_definition() -> None:
    d = get_workflow_definition(CodeReviewWorkflow)
    assert d is not None
    assert d.name == WF_NAME == "morning-star-code-review"
    assert d.execution_timeout == EXECUTION_TIMEOUT == timedelta(minutes=90)
    # Explorers then the verifier at their 25-minute caps, a formatter after each, and
    # ten minutes for the deterministic steps fit in one execution.
    budgets = ResolvedBudgets(explorer_minutes=25, verifier_minutes=25)
    agents = 2 * (25 * 60 + budgets.grace_seconds + budgets.rescue_seconds + 90)
    formatters = 2 * (240 + 30 + 60)
    assert agents + formatters + 10 * 60 < EXECUTION_TIMEOUT.total_seconds()
    assert SEARCH_KEYS == ["pr_url", "repo"]


async def test_workflow_loads_in_the_workers_sandbox() -> None:
    """The worker runs workflow code in Temporal's sandbox with the SDK's restrictions.

    Validation imports the workflow module inside the sandbox and builds an instance,
    so a non-passthrough import of I/O code (httpx, subprocess, ...) fails here.
    """
    from mistralai.workflows.core.sandbox import get_sandbox_restrictions
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
    from temporalio.workflow import _Definition

    defn = _Definition.must_from_class(CodeReviewWorkflow)
    runner = SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions())
    runner.prepare_workflow(defn)


# ------------------------------------------------------------------------ guard first


def _is_activity_decorator(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "activity"
    )


def _activity_defs() -> list[tuple[Path, ast.AsyncFunctionDef]]:
    found = []
    for path in sorted(ACTIVITIES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _is_activity_decorator(d) for d in node.decorator_list
            ):
                assert isinstance(node, ast.AsyncFunctionDef), f"{path.name}: {node.name} not async"
                found.append((path, node))
    return found


def _first_statement(fn: ast.AsyncFunctionDef) -> ast.stmt:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # docstring
    return body[0]


def _guard_call(stmt: ast.stmt) -> ast.Call | None:
    value = stmt.value if isinstance(stmt, ast.Assign | ast.Expr) else None
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "guard"
    ):
        return value
    return None


def _names(node: ast.AST) -> set[str]:
    """``inp.<field>`` attribute names used inside ``node``."""
    return {
        n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "inp"
    }


def _verified_elsewhere(fn: ast.AsyncFunctionDef) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "verify":
            out |= _names(n)
    return out


def _sealed_fields(model: type[contract.Contract]) -> set[str]:
    out = set()
    for name, field in model.model_fields.items():
        args = typing.get_args(field.annotation) or (field.annotation,)
        flat = [a for arg in args for a in (typing.get_args(arg) or (arg,))]
        if any(inspect.isclass(a) and issubclass(a, contract.Sealed) for a in flat):
            out.add(name)
    return out


def test_every_activity_calls_guard_first() -> None:
    defs = _activity_defs()
    assert len(defs) == len(activities.ACTIVITIES)
    for path, fn in defs:
        call = _guard_call(_first_statement(fn))
        assert call is not None, f"{path.name}: {fn.name} must start with guard(...)"
        assert call.args, f"{path.name}: guard needs the request"


def test_the_guard_checker_rejects_late_guards() -> None:
    late = ast.parse(
        "async def a(inp):\n    '''doc'''\n    x = 1\n    ctx = guard(inp.req)\n"
    ).body[0]
    first = ast.parse("async def a(inp):\n    '''doc'''\n    ctx = guard(inp.req)\n").body[0]
    assert isinstance(late, ast.AsyncFunctionDef) and isinstance(first, ast.AsyncFunctionDef)
    assert _guard_call(_first_statement(late)) is None
    assert _guard_call(_first_statement(first)) is not None


def test_guard_checks_every_sealed_input() -> None:
    for path, fn in _activity_defs():
        params = list(fn.args.args)
        assert len(params) == 1, f"{fn.name}: one model parameter"
        module = importlib.import_module(f"morning_star_review.activities.{path.stem}")
        live = getattr(module, fn.name)
        assert live in activities.ACTIVITIES
        hints = typing.get_type_hints(live.__original_func__)
        model = hints[params[0].arg]
        call = _guard_call(_first_statement(fn))
        assert call is not None
        if model is contract.ReviewRequest:
            continue
        guarded = set().union(*(_names(a) for a in call.args))
        assert "req" in guarded, f"{path.name}: guard(inp.req, ...)"
        missing = _sealed_fields(model) - guarded - _verified_elsewhere(fn)
        assert not missing, f"{path.name}: sealed inputs not checked: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_activity_names_use_the_project_prefix(name: str) -> None:
    assert name.startswith("msreview_")
