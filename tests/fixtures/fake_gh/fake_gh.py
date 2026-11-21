"""A stand-in for the GitHub CLI (``gh``) used by tests/unit/test_enable*.py.

Run as ``python fake_gh.py ARGS...`` (tests put a ``gh`` wrapper on PATH). Every call
is appended to ``$FAKE_GH_LOG`` (JSON lines: argv and standard input), and the fake
GitHub lives in ``$FAKE_GH_STATE`` (a JSON file the calls read and update).
Only the commands msreview uses are served: ``auth status``, ``api``, ``secret
set|delete`` and ``variable set|delete``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import fake_api


def fail(message: str, code: int = 1) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def auth_status(state: dict[str, Any], argv: list[str]) -> None:
    auth = state.get("auth", {})
    if "--json" in argv:
        if not auth.get("json", True):
            fail("unknown flag: --json")
        entry = {
            "state": auth.get("state", "success"),
            "active": True,
            "host": "github.com",
            "login": auth.get("login", "octo"),
            "tokenSource": auth.get("tokenSource", "keyring"),
            "scopes": auth.get("scopes", "repo, workflow"),
            "gitProtocol": "https",
        }
        print(json.dumps({"hosts": {"github.com": [entry]}}))
        return
    if auth.get("state", "success") != "success":
        fail("You are not logged into any GitHub hosts. To log in, run: gh auth login")
    scopes = ", ".join(f"'{s.strip()}'" for s in auth.get("scopes", "").split(",") if s.strip())
    print("github.com")
    print(f"  Logged in to github.com account {auth.get('login', 'octo')} (keyring)")
    print(f"  - Token scopes: {scopes}")


def repo_of(state: dict[str, Any], argv: list[str]) -> dict[str, Any]:
    full = argv[argv.index("--repo") + 1]
    repo = state["repos"].get(full)
    if repo is None:
        fail(f"HTTP 404: Not Found (https://api.github.com/repos/{full})")
    return repo


def secrets_and_variables(state: dict[str, Any], argv: list[str], stdin: str) -> None:
    kind, action, name = argv[0], argv[1], argv[2]
    if state.get("fail", {}).get(f"{kind} {action}"):
        fail(f"failed to {action} {kind} {name}: HTTP 403: Resource not accessible")
    if "--body" in argv or "-b" in argv:
        fail("the fake refuses values on the command line")
    repo = repo_of(state, argv)
    store = repo.setdefault("secrets" if kind == "secret" else "variables", {})
    if action == "set":
        if not stdin:
            fail("no value on standard input")
        store[name] = stdin
        print(f"Set {kind} {name} for {argv[argv.index('--repo') + 1]}", file=sys.stderr)
    elif action == "delete":
        if name not in store:
            fail(f"failed to delete {kind} {name}: HTTP 404: Not Found")
        del store[name]
    else:
        fail(f"unknown command {kind} {action}")


def main() -> None:
    argv = sys.argv[1:]
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as f:
            leaked = sorted(
                k for k in os.environ if k.startswith("MSREVIEW_") or k == "MISTRAL_API_KEY"
            )
            f.write(json.dumps({"argv": argv, "stdin": stdin, "msreview_env": leaked}) + "\n")
    state_path = Path(os.environ["FAKE_GH_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    try:
        if argv[:2] == ["auth", "status"]:
            auth_status(state, argv)
        elif argv[:1] == ["api"]:
            fake_api.handle(state, argv, stdin)
        elif argv[:1] in (["secret"], ["variable"]):
            secrets_and_variables(state, argv, stdin)
        else:
            fail(f"fake gh: unsupported command {argv[:2]}")
    finally:
        state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
