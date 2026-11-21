"""Test harness around the fake ``gh``: state, call log and a runner for the commands.

Loaded by tests/unit/test_enable*.py with ``load_harness()`` (tests are not a package).
"""

from __future__ import annotations

import io
import json
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from morning_star_review import cli
from morning_star_review.enable.commands import Deps, run
from morning_star_review.enable.gh import GhError
from morning_star_review.enable.values import AppIdentity, EnableUsageError, Prompts

FAKE = Path(__file__).resolve().parent / "fake_gh.py"
CLIENT_ID = "Iv23liFakeClient0001"
SLUG = "octo-review"
SHA_020 = "0123456789abcdef0123456789abcdef01234567"
SHA_011 = "89abcdef0123456789abcdef0123456789abcdef"
TAG_OBJECT = "f" * 40
MISTRAL_KEY = "mk-test-0123456789abcdefXYZ"
INSTALLED = ("octo/one", "octo/two", "octo/three")


def make_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")


PEM = make_pem()


OWN_KEY = "the-repositorys-own-key"  # a MISTRAL_API_KEY that other workflows use


def legacy_workflow(version: str, sha: str, preset: str | None = None) -> str:
    """A workflow as 0.1.x documented it: the Mistral key in ``secrets.MISTRAL_API_KEY``."""
    from morning_star_review.enable import template as tpl

    text = tpl.render(version, sha, preset)
    return text.replace("secrets.MSREVIEW_MISTRAL_API_KEY", "secrets.MISTRAL_API_KEY")


def new_repo(files: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "default_branch": "main",
        "branches": {"main": {"sha": "b" * 40, "files": {"README.md": "hi\n", **(files or {})}}},
        "secrets": {},
        "variables": {},
    }


def default_state() -> dict[str, Any]:
    return {
        "auth": {"login": "octo", "scopes": "gist, read:org, repo, workflow"},
        "installations": [
            {
                "id": 11,
                "app_slug": SLUG,
                "app_id": 7,
                "client_id": CLIENT_ID,
                "account": {"login": "octo", "type": "User"},
                "repository_selection": "selected",
            },
            {
                "id": 12,
                "app_slug": "someone-elses-app",
                "app_id": 99,
                "client_id": "Iv23liAnotherApp0002",
                "account": {"login": "octo", "type": "User"},
                "repository_selection": "all",
            },
        ],
        "installation_repos": {"11": list(INSTALLED), "12": ["octo/one", "octo/other"]},
        "tags": {
            "v0.2.0": {"type": "commit", "sha": SHA_020},
            "v0.1.1": {"type": "tag", "sha": TAG_OBJECT},
        },
        "tag_objects": {TAG_OBJECT: {"type": "commit", "sha": SHA_011}},
        "repos": {name: new_repo() for name in (*INSTALLED, "octo/other", "acme/tool")},
    }


class FakeVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def mistral(self, key: bytes) -> None:
        self.calls.append(("mistral", key.decode()))


class FakeAppApi:
    """The App's view (JWT calls) of the same fake state: only this App's installations."""

    def __init__(self, github: FakeGitHub, client_id: str, key: bytes) -> None:
        self.github = github
        self.client_id = client_id
        self.key = key
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False
        github.app_apis.append(self)

    def _state(self) -> dict[str, Any]:
        state = self.github.state()
        if state.get("app_api_down"):
            raise GhError("GET /app/installations: HTTP 502", 502)
        return state

    def app(self) -> AppIdentity:
        self.calls.append(("app",))
        if self._state().get("app_refused"):
            raise EnableUsageError("GitHub refused the App credentials (HTTP 401)")
        return AppIdentity(client_id=self.client_id, slug=SLUG, app_id=7, name="Octo Review")

    def installations(self) -> list[dict[str, Any]]:
        self.calls.append(("installations",))
        return [i for i in self._state()["installations"] if i["app_slug"] == SLUG]

    def installation_repos(self, installation_id: int) -> list[str]:
        self.calls.append(("installation_repos", installation_id))
        return list(self._state()["installation_repos"][str(installation_id)])

    def repo_installation(self, owner: str, repo: str) -> dict[str, Any] | None:
        self.calls.append(("repo_installation", f"{owner}/{repo}"))
        state = self._state()
        for inst in state["installations"]:
            if inst["app_slug"] != SLUG or inst["account"]["login"] != owner:
                continue
            names = state["installation_repos"][str(inst["id"])]
            if inst["repository_selection"] == "all" or f"{owner}/{repo}" in names:
                return dict(inst)
        return None

    def close(self) -> None:
        self.closed = True


@dataclass
class CountingPrompts:
    secret_answer: str = MISTRAL_KEY
    text_answer: str = CLIENT_ID
    asked: list[str] = field(default_factory=list)

    def prompts(self, interactive: bool = True) -> Prompts:
        def secret(prompt: str) -> str:
            self.asked.append("secret")
            return self.secret_answer

        def text(prompt: str) -> str:
            self.asked.append("text")
            return self.text_answer

        return Prompts(secret=secret, text=text, interactive=interactive)


@dataclass
class Outcome:
    code: int
    out: str
    err: str


class FakeGitHub:
    """One fake GitHub (state file + call log) and a ``gh`` on PATH that serves it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S -E "{FAKE}" "$@"\n')
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.state_path = tmp / "state.json"
        self.log_path = tmp / "calls.jsonl"
        self.save(default_state())
        self.key_file = tmp / "app.pem"
        self.key_file.write_text(PEM)
        self.key_file.chmod(0o600)
        self.env: dict[str, str] = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp / "home"),
            "MSREVIEW_CONFIG": str(tmp / "no-config.toml"),
            "FAKE_GH_STATE": str(self.state_path),
            "FAKE_GH_LOG": str(self.log_path),
        }
        self.verifier = FakeVerifier()
        self.app_apis: list[FakeAppApi] = []

    def state(self) -> dict[str, Any]:
        return dict(json.loads(self.state_path.read_text()))

    def save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=1))

    def edit(self, **changes: Any) -> None:
        state = self.state()
        state.update(changes)
        self.save(state)

    def repo(self, name: str) -> dict[str, Any]:
        return dict(self.state()["repos"][name])

    def put(self, name: str, *, files: dict[str, str] | None = None, **secrets: str) -> None:
        """Add files (on main) and secrets to the repository ``name``."""
        state = self.state()
        repo = state["repos"][name]
        repo["branches"]["main"]["files"].update(files or {})
        repo["secrets"].update(secrets)
        self.save(state)

    def file(self, name: str, path: str, branch: str = "main") -> str | None:
        files = self.repo(name)["branches"][branch]["files"]
        return files.get(path)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def run(
        self, *argv: str, prompts: Prompts | None = None, env: dict[str, str] | None = None
    ) -> Outcome:
        args = cli.build_parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        deps = Deps(
            env={**self.env, **(env or {})},
            out=out,
            err=err,
            prompts=prompts or Prompts(secret=_no_prompt, text=_no_prompt, interactive=False),
            verifier=self.verifier,
            app_api=lambda client_id, key: FakeAppApi(self, client_id, key),
        )
        code = run(args, deps)
        assert all(api.closed for api in self.app_apis), "an App API was not closed"
        return Outcome(code, out.getvalue(), err.getvalue())

    def enable(self, *argv: str, **kw: Any) -> Outcome:
        env = {
            "MSREVIEW_ENABLE_MISTRAL_API_KEY": MISTRAL_KEY,
            "MSREVIEW_ENABLE_APP_CLIENT_ID": CLIENT_ID,
        }
        env.update(kw.pop("env", {}))
        return self.run("enable", *argv, "--app-key-file", str(self.key_file), env=env, **kw)


def _no_prompt(prompt: str) -> str:
    raise AssertionError(f"unexpected prompt: {prompt}")
