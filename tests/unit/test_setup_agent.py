"""``msreview setup``: install source detection, commands, the hashed requirements file."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from morning_star_review import setup_agent
from morning_star_review._version import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_source_from_git_install() -> None:
    raw = json.dumps(
        {
            "url": "https://github.com/ThomsenDrake/morning-star-code-reviews",
            "vcs_info": {"vcs": "git", "commit_id": "a" * 40},
        }
    )
    src = setup_agent.install_source(raw)
    assert src.spec == f"git+https://github.com/ThomsenDrake/morning-star-code-reviews@{'a' * 40}"
    assert src.origin == "git" and not src.editable


def test_source_from_local_checkout() -> None:
    raw = json.dumps({"url": "file:///home/me/src/msr%20x", "dir_info": {"editable": True}})
    src = setup_agent.install_source(raw)
    assert (src.spec, src.editable, src.origin) == ("/home/me/src/msr x", True, "dir")


def test_source_from_wheel_and_fallback() -> None:
    raw = json.dumps({"url": "file:///tmp/pkg.whl", "archive_info": {}})
    assert setup_agent.install_source(raw).spec == "/tmp/pkg.whl"
    fallback = setup_agent.install_source("")
    assert fallback.spec == f"git+{setup_agent.REPO_URL}@v{__version__}"
    assert setup_agent.install_source("{not json").origin == "release"


def test_plan_requires_hashes_and_no_deps(tmp_path: Path) -> None:
    src = setup_agent.InstallSource(spec="/src", editable=True, origin="dir")
    cmds = setup_agent.plan_commands("uv", tmp_path / "v", Path("/r.txt"), src, "/usr/bin/python3")
    assert cmds[0] == [
        "uv",
        "venv",
        "--no-config",
        "--python",
        "/usr/bin/python3",
        str(tmp_path / "v"),
    ]
    # Security review SEC-CI-4: no uv.toml / [tool.uv] from the cwd is ever read.
    assert all("--no-config" in cmd for cmd in cmds)
    assert "--require-hashes" in cmds[1] and cmds[1][-2:] == ["-r", "/r.txt"]
    assert "--no-deps" in cmds[2] and cmds[2][-2:] == ["-e", "/src"]


def test_requirements_file_is_shipped_and_hashed() -> None:
    path = setup_agent.requirements_path()
    text = path.read_text()
    assert "mistralai-agents==" in text
    pins = [ln for ln in text.splitlines() if ln and not ln.startswith((" ", "#"))]
    assert pins and all("==" in p for p in pins)
    # The project itself is installed separately (--no-deps), never from this file.
    assert not [p for p in pins if p.startswith(("morning-star-code-reviews", "-e", "."))]
    assert text.count("--hash=sha256:") >= len(pins)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_requirements_file_matches_the_lock(tmp_path: Path) -> None:
    out = tmp_path / "req.txt"
    env = {
        "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("uv") or "").parent),
        "UV_NO_CONFIG": "1",
        "HOME": str(tmp_path),
    }
    res = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            "agent",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements.txt",
            "--quiet",
            "--output-file",
            str(out),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert out.read_text() == setup_agent.requirements_path().read_text(), (
        "run scripts/export_agent_requirements.sh"
    )


def test_check_reports_a_missing_environment(tmp_path: Path) -> None:
    with pytest.raises(setup_agent.SetupError, match="msreview setup"):
        setup_agent.check_agent_python(tmp_path / "bin" / "python")
    assert setup_agent.run_setup(check=True, venv=tmp_path / "v", env={"PATH": "/usr/bin"}) == 1


def test_check_accepts_a_matching_environment(tmp_path: Path) -> None:
    # A fake interpreter that answers the import probe like a good agent environment.
    py = tmp_path / "v" / "bin" / "python"
    py.parent.mkdir(parents=True)
    answer = json.dumps({"version": __version__, "agents": "1.1.3"})
    py.write_text(f"#!/bin/sh\necho '{answer}'\n")
    py.chmod(0o755)
    assert setup_agent.check_agent_python(py) == {"version": __version__, "agents": "1.1.3"}
    assert setup_agent.run_setup(check=True, venv=tmp_path / "v", env={"PATH": "/usr/bin"}) == 0


def test_setup_runs_the_planned_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    venv = tmp_path / "agent"

    def fake_run(cmd: list[str], env: object) -> None:
        ran.append(list(cmd))
        if cmd[1] == "venv":
            py = venv / "bin" / "python"
            py.parent.mkdir(parents=True)
            answer = json.dumps({"version": __version__, "agents": "1.1.3"})
            py.write_text(f"#!/bin/sh\necho '{answer}'\n")
            py.chmod(0o755)

    monkeypatch.setattr(setup_agent, "_run", fake_run)
    fake_uv = tmp_path / "bin" / "uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text("#!/bin/sh\n")
    fake_uv.chmod(0o755)
    env = {"PATH": str(fake_uv.parent)}
    assert setup_agent.run_setup(source="/src/pkg", venv=venv, env=env) == 0
    assert [c[1] for c in ran] == ["venv", "pip", "pip"]
    assert ran[0][ran[0].index("--python") + 1] == sys.executable
    assert ran[2][-1] == "/src/pkg"


def test_setup_without_uv(tmp_path: Path) -> None:
    with pytest.raises(setup_agent.SetupError, match="uv"):
        setup_agent.run_setup(venv=tmp_path / "v", env={"PATH": str(tmp_path)})
