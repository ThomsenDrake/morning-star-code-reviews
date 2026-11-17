"""scripts/make_test_prs.py against a local bare repository and a fake `gh`."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from msreview_eval.cases import CaseKey

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_test_prs", SCRIPTS_DIR / "make_test_prs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mk = _load()

FAKE_GH = """\
#!{python}
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {{"prs": [], "calls": []}}
args = sys.argv[1:]
state["calls"].append(args[:2])
def opt(name):
    return args[args.index(name) + 1]
if args[:2] == ["pr", "list"]:
    head, base = opt("--head"), opt("--base")
    found = [p for p in state["prs"] if p["head"] == head and p["base"] == base and p["open"]]
    print(json.dumps([{{"number": p["number"], "url": p["url"], "headRefOid": ""}} for p in found]))
elif args[:2] == ["pr", "create"]:
    number = len(state["prs"]) + 1
    url = "https://github.com/" + opt("--repo") + "/pull/" + str(number)
    state["prs"].append({{"number": number, "url": url, "head": opt("--head"),
                         "base": opt("--base"), "title": opt("--title"),
                         "body": sys.stdin.read(), "open": True}})
    print(url)
else:
    sys.exit("unexpected gh call: " + " ".join(args))
json.dump(state, open(state_path, "w"))
"""


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    gh = tmp_path / "fake-gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "gh-state.json"
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    return bare, gh, state


def _main(
    bare: Path, gh: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict]:
    argv = ["--repo", "acme/playground", "--gh", str(gh), "--remote-url", bare.as_uri(), *extra]
    code = mk.main(argv)
    out = capsys.readouterr().out
    return code, (json.loads(out) if code == 0 else {})


def _remote_head(bare: Path, branch: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_branch_plan_actions() -> None:
    chain = ("a1", "a2", "a3")
    assert mk.BranchPlan("b", "a2", chain, None).action == "create"
    assert mk.BranchPlan("b", "a2", chain, "a2").action == "unchanged"
    assert mk.BranchPlan("b", "a2", chain, "a1").action == "fast_forward"
    assert mk.BranchPlan("b", "a2", chain, "a3").action == "ahead"
    assert mk.BranchPlan("b", "a2", chain, "zz").action == "diverged"
    assert not mk.BranchPlan("b", "a2", chain, "a3").needs_push
    with pytest.raises(mk.PlanError):
        mk.check_plans([mk.BranchPlan("main", "a1", ("a1",), None)], force=True)


def test_parse_stages_and_pr_number(cases: dict[str, CaseKey]) -> None:
    assert mk.parse_stages(["e2=2"], cases) == {"e2": 2}
    for bad in ("e2", "e2=3", "e2=x", "zz=1"):
        with pytest.raises(SystemExit):
            mk.parse_stages([bad], cases)
    assert mk.pr_number("https://github.com/a/b/pull/42\n") == 42
    assert mk.pr_number("nope") is None


def test_rejects_bad_repo_names(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        mk.main(["--repo", "not a repo", "--dry-run"])
    with pytest.raises(SystemExit):
        mk.main(["--repo", "a/b", "--base-branch", "main", "--dry-run"])


def test_create_is_idempotent_and_stages_advance(
    remote: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bare, gh, state_path = remote
    code, first = _main(bare, gh, capsys, "--cases", "e1,e2")
    assert code == 0
    assert first["base_action"] == "create"
    e1, e2 = first["cases"]["e1"], first["cases"]["e2"]
    assert (e1["number"], e1["pr"], e1["branch_action"]) == (1, "created", "create")
    assert (e2["number"], e2["stage"], e2["commits"]) == (2, 1, 2)
    assert e1["head_sha"] == e2["head_sha"] == _remote_head(bare, "msreview-eval/e1-planted")
    state = json.loads(state_path.read_text())
    assert state["prs"][0]["base"] == "msreview-eval/base"
    assert state["prs"][0]["title"] == "Plan changes and transaction search"
    assert "pro rata" in state["prs"][0]["body"]

    code, again = _main(bare, gh, capsys, "--cases", "e1,e2")
    assert code == 0
    assert again["base_action"] == "unchanged"
    assert {c["branch_action"] for c in again["cases"].values()} == {"unchanged"}
    assert {c["pr"] for c in again["cases"].values()} == {"existing"}
    assert len(json.loads(state_path.read_text())["prs"]) == 2

    code, advanced = _main(bare, gh, capsys, "--cases", "e2", "--stage", "e2=2")
    assert code == 0
    e2b = advanced["cases"]["e2"]
    assert (e2b["branch_action"], e2b["stage"], e2b["pr"]) == ("fast_forward", 2, "existing")
    assert _remote_head(bare, "msreview-eval/e2-incremental") == e2b["head_sha"]

    # Re-running with the default stage leaves the advanced branch alone.
    code, later = _main(bare, gh, capsys, "--cases", "e2")
    assert code == 0 and later["cases"]["e2"]["branch_action"] == "ahead"
    assert later["cases"]["e2"]["head_sha"] == e2b["head_sha"]
    assert later["cases"]["e2"]["stage"] == 2  # the stage of the branch as it is


def test_credential_helper_quotes_the_gh_path() -> None:
    args = mk._remote_git_args("/opt/my tools/gh")
    assert args == [
        "-c", "credential.helper=",
        "-c", "credential.helper=!'/opt/my tools/gh' auth git-credential",
    ]  # fmt: skip


def test_diverged_branch_needs_force(
    remote: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare, gh, _state = remote
    assert _main(bare, gh, capsys, "--cases", "e4")[0] == 0
    # Someone pushes an unrelated commit to the case branch.
    work = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", bare.as_uri(), str(work)], check=True)
    env = ["-c", "user.name=x", "-c", "user.email=x@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "msreview-eval/e4-clean-refactor"],
                   check=True)  # fmt: skip
    (work / "extra.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), *env, "commit", "-qm", "extra"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD"], check=True)

    code, _ = _main(bare, gh, capsys, "--cases", "e4")
    assert code == 2
    code, forced = _main(bare, gh, capsys, "--cases", "e4", "--force")
    assert code == 0 and forced["cases"]["e4"]["branch_action"] == "diverged"
    assert (
        _remote_head(bare, "msreview-eval/e4-clean-refactor") == forced["cases"]["e4"]["head_sha"]
    )


def test_dry_run_is_offline(capsys: pytest.CaptureFixture[str]) -> None:
    code = mk.main(["--repo", "acme/playground", "--dry-run", "--gh", "/nonexistent/gh"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert set(result["cases"]) == {"e1", "e2", "e4", "e5", "e6"}
    assert all(c["number"] is None for c in result["cases"].values())
