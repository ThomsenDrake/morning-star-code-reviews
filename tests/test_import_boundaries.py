"""Import boundary rules (DESIGN 3.3), checked statically and in each virtual environment.

1. No module outside `morning_star_review/agent/` imports `mistralai.agents` or
   `mistralai.vibe*` (or `morning_star_review.agent`).
2. `morning_star_review.agent` is never imported in the worker venv.
3. `morning_star_review.cli` and `morning_star_review.core` do not import
   `mistralai.workflows` at import time.
4. `morning_star_review.core` does not import `httpx`, `mistralai` or `temporalio`
   at module level (lazy imports inside functions are allowed).

The subprocess checks run `python -c` with each venv's interpreter. Defaults are
`.venv/bin/python` (worker) and `.venv-agent/bin/python` (agent); CI can point
elsewhere with MSREVIEW_TEST_WORKER_PYTHON / MSREVIEW_TEST_AGENT_PYTHON.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = "morning_star_review"
PKG_DIR = REPO_ROOT / "src" / PKG
AGENT_DIR = PKG_DIR / "agent"
CORE_DIR = PKG_DIR / "core"

# Exact module or any submodule of these:
AGENT_ONLY_MODULES = ("mistralai.agents", f"{PKG}.agent")
# Any module whose dotted name starts with this text (mistralai.vibe, mistralai.vibe_sdk, ...):
AGENT_ONLY_TEXT_PREFIX = "mistralai.vibe"
CORE_FORBIDDEN_TOP = ("httpx", "mistralai", "temporalio")


# ---------------------------------------------------------------------------
# Static (AST) checks: run in any environment.
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG_DIR.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(node: ast.ImportFrom, module: str, is_pkg: bool) -> str:
    """Absolute module named by a (possibly relative) `from X import ...`."""
    if node.level == 0:
        return node.module or ""
    base = module.split(".") if is_pkg else module.split(".")[:-1]
    if node.level > 1:
        base = base[: -(node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _imported_names(node: ast.AST, module: str, is_pkg: bool) -> Iterator[str]:
    """Fully qualified names an import-like node may load."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom):
        base = _resolve_from(node, module, is_pkg)
        yield base
        for alias in node.names:
            yield f"{base}.{alias.name}" if base else alias.name
    elif (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"import_module", "__import__"}:
            yield node.args[0].value


def _in_modules(name: str, modules: tuple[str, ...]) -> bool:
    return any(name == m or name.startswith(m + ".") for m in modules)


def _is_agent_only(name: str) -> bool:
    return _in_modules(name, AGENT_ONLY_MODULES) or name.startswith(AGENT_ONLY_TEXT_PREFIX)


def _line(node: ast.AST) -> int:
    return getattr(node, "lineno", 0)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_time_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Nodes executed when the module is imported (function bodies and TYPE_CHECKING excluded)."""
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            # Decorators and defaults run at import time; the body does not.
            for sub in [*stmt.decorator_list, *stmt.args.defaults, *stmt.args.kw_defaults]:
                if sub is not None:
                    yield from ast.walk(sub)
            continue
        if isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            yield from _import_time_nodes(stmt.orelse)
            continue
        compound = ast.ClassDef | ast.If | ast.Try | ast.TryStar | ast.With | ast.For | ast.While
        if isinstance(stmt, compound):
            for field in ("body", "orelse", "finalbody"):
                yield from _import_time_nodes(getattr(stmt, field, []) or [])
            for handler in getattr(stmt, "handlers", []) or []:
                yield from _import_time_nodes(handler.body)
            for expr_field in ("test", "iter", "items", "decorator_list", "bases"):
                value = getattr(stmt, expr_field, None)
                for expr in value if isinstance(value, list) else [value]:
                    if isinstance(expr, ast.AST):
                        yield from ast.walk(expr)
            continue
        yield from ast.walk(stmt)


def _source_files() -> list[Path]:
    return sorted(PKG_DIR.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_package_tree_is_present() -> None:
    assert (PKG_DIR / "__init__.py").is_file()
    assert (CORE_DIR / "__init__.py").is_file()
    assert (AGENT_DIR / "__init__.py").is_file()


def test_only_agent_package_imports_agent_sdk() -> None:
    offenders: list[str] = []
    for path in _source_files():
        if path.is_relative_to(AGENT_DIR):
            continue
        module, is_pkg = _module_name(path), path.name == "__init__.py"
        for node in ast.walk(_parse(path)):
            for name in _imported_names(node, module, is_pkg):
                if _is_agent_only(name):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{_line(node)}: {name}")
    assert not offenders, "agent-only imports outside agent/:\n" + "\n".join(offenders)


def test_core_has_no_module_level_network_or_sdk_imports() -> None:
    offenders: list[str] = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        module, is_pkg = _module_name(path), path.name == "__init__.py"
        for node in _import_time_nodes(_parse(path).body):
            for name in _imported_names(node, module, is_pkg):
                if name.split(".")[0] in CORE_FORBIDDEN_TOP:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{_line(node)}: {name}")
    assert not offenders, "module-level imports forbidden in core/:\n" + "\n".join(offenders)


def test_cli_has_no_module_level_workflows_import() -> None:
    path = PKG_DIR / "cli.py"
    offenders = [
        f"cli.py:{_line(node)}: {name}"
        for node in _import_time_nodes(_parse(path).body)
        for name in _imported_names(node, f"{PKG}.cli", False)
        if _in_modules(name, ("mistralai.workflows",))
    ]
    assert not offenders, "\n".join(offenders)


# The checker must actually catch violations, or the tree checks above prove nothing.
_SAMPLE = """
import json
from typing import TYPE_CHECKING
import httpx
from . import agent
from ..agent import runner
from mistralai import agents
if TYPE_CHECKING:
    import temporalio.testing
else:
    import mistralai.vibe_sdk
try:
    import mistralai.workflows
except ImportError:
    from temporalio import activity
class C:
    import mistralai
def f(x=__import__("mistralai.agents")):
    import temporalio.client
    importlib.import_module("mistralai.vibe")
"""


def _sample_names(import_time_only: bool) -> set[str]:
    tree = ast.parse(_SAMPLE)
    nodes = _import_time_nodes(tree.body) if import_time_only else ast.walk(tree)
    return {n for node in nodes for n in _imported_names(node, f"{PKG}.core.x", False)}


def test_checker_import_time_scope() -> None:
    names = _sample_names(import_time_only=True)
    assert {"httpx", "mistralai.vibe_sdk", "mistralai.workflows", "temporalio.activity"} <= names
    assert {"mistralai", "mistralai.agents", f"{PKG}.agent", f"{PKG}.core.agent"} <= names
    assert "temporalio.testing" not in names  # under TYPE_CHECKING
    assert "temporalio.client" not in names  # inside a function body
    assert "mistralai.vibe" not in names  # inside a function body


def test_checker_relative_imports_and_dynamic_imports() -> None:
    names = _sample_names(import_time_only=False)
    assert f"{PKG}.core.agent" in names  # `from . import agent` inside core/x.py
    assert f"{PKG}.agent.runner" in names  # `from ..agent import runner`
    assert {"temporalio.client", "mistralai.vibe", "mistralai.agents"} <= names
    assert _is_agent_only(f"{PKG}.agent.runner")
    assert _is_agent_only("mistralai.vibe_harness")
    assert not _is_agent_only(f"{PKG}.activities.agent_launcher")
    assert not _is_agent_only("mistralai.workflows")


# ---------------------------------------------------------------------------
# Subprocess checks: `python -c` in each venv.
# ---------------------------------------------------------------------------

_PROBE = r"""
import importlib, json, sys
mods = json.loads(sys.argv[1])
errors = {}
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        errors[m] = f"{type(exc).__name__}: {exc}"
print(json.dumps({"errors": errors, "loaded": sorted(sys.modules)}))
"""


def _venv_python(kind: str) -> Path:
    override = os.environ.get(f"MSREVIEW_TEST_{kind.upper()}_PYTHON")
    if override:
        return Path(override)
    return REPO_ROOT / (".venv" if kind == "worker" else ".venv-agent") / "bin" / "python"


def _python_or_skip(kind: str) -> Path:
    py = _venv_python(kind)
    if not py.exists():
        pytest.skip(f"{kind} venv interpreter not found at {py}")
    return py


def _run(py: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    # -I: ignore PYTHON* variables, user site and the cwd, so only the venv is visible.
    return subprocess.run(
        [str(py), "-I", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=90
    )


def _probe(py: Path, modules: list[str], cwd: Path) -> tuple[dict[str, str], set[str]]:
    proc = _run(py, ["-c", _PROBE, json.dumps(modules)], cwd)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return data["errors"], set(data["loaded"])


def _modules(*, under: Path, exclude: Path | None = None) -> list[str]:
    out = []
    for path in sorted(under.rglob("*.py")):
        if path.name == "__main__.py":
            continue  # importing it would run the program
        if exclude is not None and path.is_relative_to(exclude):
            continue
        out.append(_module_name(path))
    return out


def _loaded_under(loaded: set[str], *roots: str) -> list[str]:
    return sorted(m for m in loaded if any(m == r or m.startswith(r + ".") for r in roots))


ALL_NON_AGENT = _modules(under=PKG_DIR, exclude=AGENT_DIR)
CORE_MODULES = _modules(under=CORE_DIR)
AGENT_ONLY_ROOTS = ("mistralai.agents", f"{PKG}.agent")


@pytest.mark.parametrize("kind", ["worker", "agent"])
def test_core_import_is_light(kind: str, tmp_path: Path) -> None:
    errors, loaded = _probe(_python_or_skip(kind), CORE_MODULES, tmp_path)
    assert not errors
    heavy = _loaded_under(loaded, *CORE_FORBIDDEN_TOP)
    assert not heavy, f"importing core loaded: {heavy}"


@pytest.mark.parametrize("kind", ["worker", "agent"])
def test_cli_import_does_not_load_workflows(kind: str, tmp_path: Path) -> None:
    errors, loaded = _probe(_python_or_skip(kind), [f"{PKG}.cli"], tmp_path)
    assert not errors
    assert not _loaded_under(loaded, "mistralai.workflows")


@pytest.mark.parametrize("kind", ["worker", "agent"])
def test_non_agent_modules_do_not_load_agent_code(kind: str, tmp_path: Path) -> None:
    errors, loaded = _probe(_python_or_skip(kind), ALL_NON_AGENT, tmp_path)
    assert not errors, errors
    bad = _loaded_under(loaded, *AGENT_ONLY_ROOTS)
    bad += sorted(m for m in loaded if m.startswith(AGENT_ONLY_TEXT_PREFIX))
    assert not bad, f"agent-only modules loaded outside agent/: {bad}"


def test_worker_venv_has_no_agent_sdk_or_plugin(tmp_path: Path) -> None:
    py = _python_or_skip("worker")
    code = (
        "import importlib.metadata as md, importlib.util as u, json\n"
        "names = ['mistralai-agents', 'mistralai-vibe-harness', 'mistralai-vibe-sdk',\n"
        "         'mistralai-workflows-plugins-mistralai']\n"
        "present = []\n"
        "for n in names:\n"
        "    try:\n"
        "        md.distribution(n)\n"
        "        present.append(n)\n"
        "    except md.PackageNotFoundError:\n"
        "        pass\n"
        "spec = u.find_spec('mistralai.agents') is not None\n"
        "print(json.dumps({'present': present, 'spec': spec}))\n"
    )
    proc = _run(py, ["-c", code], tmp_path)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data == {"present": [], "spec": False}
    imp = _run(py, ["-c", "import mistralai.agents"], tmp_path)
    assert imp.returncode != 0
    assert "ModuleNotFoundError" in imp.stderr


def test_agent_venv_can_import_agent_sdk_and_runner(tmp_path: Path) -> None:
    py = _python_or_skip("agent")
    errors, loaded = _probe(py, ["mistralai.agents", f"{PKG}.agent"], tmp_path)
    assert not errors, errors
    assert {"mistralai.agents", f"{PKG}.agent"} <= loaded
