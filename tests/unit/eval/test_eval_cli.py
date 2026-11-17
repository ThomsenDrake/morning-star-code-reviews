"""scripts/eval.py: planning, CLI argv, output parsing and an offline end-to-end run."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType

import pytest
from msreview_eval.cases import CaseKey

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("msreview_eval_cli", SCRIPTS_DIR / "eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


ev = _load()


def test_parse_config() -> None:
    model = ev.parse_config("model:zai-glm-5-3", "high")
    assert model.args == (
        "--preset", "single", "--explorer-model", "zai-glm-5-3", "--effort", "high",
    )  # fmt: skip
    assert ev.parse_config("preset:best", "high").args == ("--preset", "best")
    assert model.slug == "model_zai-glm-5-3"
    for bad in ("zai", "model:", "other:x"):
        with pytest.raises(SystemExit):
            ev.parse_config(bad, "high")


def test_build_command_and_plan(cases: dict[str, CaseKey], tmp_path: Path) -> None:
    configs = [ev.parse_config("preset:best", "high"), ev.parse_config("model:m", "low")]
    prs = {"e1": {"number": 7, "head_sha": "c" * 40}, "e6": {"number": 9}}
    specs = ev.plan_runs(configs, [cases["e1"], cases["e6"]], 3, "acme/play", prs)
    assert len(specs) == 2 * 3 + 2 * 1  # E6 runs once
    first = specs[0]
    assert first.pr_url == "https://github.com/acme/play/pull/7"
    assert first.expected_head_sha == "c" * 40
    cmd = ev.build_command("msreview", first, tmp_path, ["--verbose"], True)
    assert cmd[:6] == ["msreview", "review", first.pr_url, "--post", "never", "--json"]
    assert cmd[-3:] == ["--preset", "best", "--verbose"] and "--force" in cmd
    with pytest.raises(SystemExit):
        ev.plan_runs(configs, [cases["e4"]], 1, "acme/play", prs)


def test_parse_outcome_variants() -> None:
    assert ev.parse_outcome('{"status": "report"}') == {"status": "report"}
    assert ev.parse_outcome('log line\n{"status": "posted"}\n') == {"status": "posted"}
    assert ev.parse_outcome("") is None
    assert ev.parse_outcome("no json here") is None
    assert ev.parse_outcome("[1, 2]") is None
    noisy = 'msreview: config {"preset": "best"}\n{"status": "report", "findings": []}\n'
    assert ev.parse_outcome(noisy) == {"status": "report", "findings": []}


def test_timeout_stops_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-alive"
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = (
            "import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
        )
        subprocess.Popen([sys.executable, "-c", child])
        print("partial", flush=True)
        time.sleep(60)
        """
    )
    with pytest.raises(subprocess.TimeoutExpired) as info:
        ev.run_cli([sys.executable, "-c", script], timeout_s=1.5, grace_s=2)
    assert "partial" in (info.value.stdout or "")
    time.sleep(3)
    assert not marker.exists()  # the grandchild was stopped with its group


def test_redact_repo() -> None:
    text = "see Acme/Secret-Play and secret-play/pull/1 but keep acme"
    assert ev.redact_repo(text, "acme/secret-play") == (
        "see <test-repo> and <test-repo>/pull/1 but keep acme"
    )
    assert ev.redact_repo("x", None) == "x"


FAKE_MSREVIEW = """\
#!{python}
import json, re, sys
if sys.argv[1] == "--version":
    print("msreview 0.0.0-test")
    sys.exit(0)
url = sys.argv[2]
number = int(re.search(r"/pull/(\\d+)", url).group(1))
assert "--post" in sys.argv and "never" in sys.argv
print("msreview: scope=full inlined=4 on_demand=0 excluded=0", file=sys.stderr)
print("msreview agent: complete=true rescued=false budget_exhausted=false", file=sys.stderr)
findings = []
if number == 11:
    findings.append({{
        "path": "ledger/db.py", "line": 147, "anchor": "inline", "severity": "critical",
        "title": "SQL injection in search", "evidence": "LIKE '%{{term}}%'",
        "explanation": "", "state": "open",
    }})
print(json.dumps({{
    "status": "report", "verdict": "findings" if findings else "clean",
    "pr_url": url, "head_sha": "d" * 40, "execution_id": "x", "findings": findings,
    "usage": {{}}, "cost_usd": 0.02, "report_markdown": "report for " + url,
}}))
"""


def test_run_and_score_end_to_end(tmp_path: Path) -> None:
    fake = tmp_path / "fake-msreview"
    fake.write_text(FAKE_MSREVIEW.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    prs = tmp_path / "prs.json"
    prs.write_text(
        json.dumps(
            {
                "repo": "acme/hidden-playground",
                "cases": {"e1": {"number": 11, "head_sha": "d" * 40}, "e4": {"number": 12}},
            }
        )
    )
    raw = tmp_path / "raw"
    out = tmp_path / "results" / "table.md"
    argv = ["run", "--prs", str(prs), "--presets", "best", "--cases", "e1,e4", "--runs", "2"]
    argv += ["--msreview", str(fake), "--raw-dir", str(raw), "--out", str(out)]
    assert ev.main(argv) == 0
    md = out.read_text()
    assert "hidden-playground" not in md
    assert "msreview 0.0.0-test" in md
    assert "| preset:best | 4 | 2/8 (25%) | 2/8 |" in md
    scores = json.loads(out.with_suffix(".json").read_text())
    assert len(scores) == 4
    assert {s["case"] for s in scores} == {"e1", "e4"}
    assert all(s["head_sha_ok"] in (True, None) for s in scores)
    meta = json.loads((raw / "preset_best" / "e1" / "run-1.meta.json").read_text())
    assert meta["exit_code"] == 0 and meta["argv"][0] == "review"

    # --resume skips finished runs; score re-renders and still redacts the repo.
    metas = sorted(raw.rglob("run-*.meta.json"))
    before = [m.read_text() for m in metas]
    assert ev.main([*argv, "--resume"]) == 0
    assert [m.read_text() for m in metas] == before  # no run was repeated
    rescored = tmp_path / "rescored.md"
    assert ev.main(["score", "--raw-dir", str(raw), "--out", str(rescored)]) == 0
    text = rescored.read_text()
    assert "hidden-playground" not in text and "| preset:best | 4 |" in text


def test_dry_run_prints_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps({"repo": "acme/p", "cases": {"e1": {"number": 1}}}))
    argv = ["run", "--prs", str(prs), "--models", "a,b", "--cases", "e1", "--runs", "1"]
    assert ev.main([*argv, "--dry-run", "--msreview", "msreview", "--raw-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert all(" --explorer-model " in line for line in lines)


def test_fake_cli_is_valid_python() -> None:
    compile(textwrap.dedent(FAKE_MSREVIEW.format(python="python")), "fake", "exec")


def test_run_is_the_default_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """DESIGN's interface: `eval.py --models ... --cases ... --runs 3 --out ...`."""
    argv = ["--repo", "acme/p", "--models", "a", "--cases", "e1", "--runs", "1", "--dry-run"]
    with pytest.raises(SystemExit, match="no PR number for case e1"):
        ev.main([*argv, "--raw-dir", str(tmp_path)])  # no --prs: numbers from the keys
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps({"repo": "acme/p", "cases": {"e1": {"number": 5}}}))
    assert ev.main([*argv, "--prs", str(prs), "--raw-dir", str(tmp_path)]) == 0
    (line,) = capsys.readouterr().out.strip().splitlines()
    assert "https://github.com/acme/p/pull/5" in line
    assert f"--out {tmp_path / 'model_a' / 'e1' / 'run-1-reports'}" in line
    with pytest.raises(SystemExit, match="--resume needs --raw-dir"):
        ev.main(["run", "--prs", str(prs), "--models", "a", "--resume"])
    with pytest.raises(SystemExit, match="needs --prs"):
        ev.main(["run", "--models", "a"])
