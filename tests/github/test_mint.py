"""App JWT, installation lookup, token minting and the mint child."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from morning_star_review.github import app_auth
from morning_star_review.github import http as gh_http
from morning_star_review.github import mint_child as mc
from morning_star_review.github.http import GitHubHttp

CLIENT_ID = "Iv23liTESTCLIENT"
MINTED = "ghs_mintedtoken0123456789abcdef"


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return pem, key.public_key()


class FakeAppApi:
    def __init__(self, *, installed: bool = True, perms: dict[str, str] | None = None) -> None:
        self.installed = installed
        self.perms = perms or {
            "contents": "read",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        }
        self.requests: list[httpx.Request] = []
        self.token_body: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"slug": "msr-test-app", "name": "MSR Test"})
        if path == "/repos/octo-org/widgets/installation":
            if not self.installed:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"id": 42, "account": {"login": "octo-org"}, "permissions": self.perms}
            )
        if path == "/app/installations/42/access_tokens":
            self.token_body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "token": MINTED,
                    "expires_at": "2026-01-01T01:00:00Z",
                    "permissions": self.token_body["permissions"],
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})


def write_key(tmp_path: Path, pem: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    path.chmod(mode)
    return path


def test_make_app_jwt_claims(keypair: tuple[bytes, Any]) -> None:
    pem, public = keypair
    token = app_auth.make_app_jwt(CLIENT_ID, pem, now=1_000_000)
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": CLIENT_ID}
    with pytest.raises(ValueError):
        app_auth.make_app_jwt(" ", pem, now=1)


def test_requested_and_missing_permissions() -> None:
    full = {"contents": "read", "pull_requests": "write", "issues": "write", "metadata": "read"}
    assert app_auth.requested_permissions(full) == full
    no_issues = {"contents": "read", "pull_requests": "write", "metadata": "read"}
    assert "issues" not in app_auth.requested_permissions(no_issues)
    assert app_auth.missing_permissions(full) == []
    assert app_auth.missing_permissions({"contents": "read", "pull_requests": "read"}) == [
        "pull_requests: write"
    ]
    assert app_auth.has_level({"contents": "write"}, "contents", "read")


async def test_find_installation_and_mint() -> None:
    api = FakeAppApi()
    async with GitHubHttp("jwt", transport=httpx.MockTransport(api)) as gh:
        app = await app_auth.get_app(gh)
        assert app.bot_login == "msr-test-app[bot]"
        inst = await app_auth.find_installation(gh, "octo-org", "widgets")
        assert inst is not None and inst.id == 42
        token = await app_auth.mint_installation_token(
            gh, inst.id, "widgets", app_auth.requested_permissions(inst.permissions)
        )
    assert token.token == MINTED
    assert MINTED not in repr(token)
    assert api.token_body == {
        "repositories": ["widgets"],
        "permissions": {
            "contents": "read",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        },
    }
    api.installed = False
    async with GitHubHttp("jwt", transport=httpx.MockTransport(api)) as gh:
        assert await app_auth.find_installation(gh, "octo-org", "widgets") is None


def test_settings_precedence(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[app]\nclient_id = "from-file"\nprivate_key_path = "~/k.pem"\n'
        'private_key_command = "echo file-cmd"\n'
    )
    s = mc.load_app_settings({}, cfg)
    assert (s.client_id, s.private_key_path, s.private_key_command) == (
        "from-file",
        None,
        "echo file-cmd",
    )
    env = {mc.ENV_CLIENT_ID: "from-env", mc.ENV_KEY_PATH: "/env/k.pem"}
    s = mc.load_app_settings(env, cfg)
    assert (s.client_id, s.private_key_path, s.private_key_command) == (
        "from-env",
        "/env/k.pem",
        None,
    )
    assert "echo" not in repr(mc.load_app_settings({mc.ENV_KEY_COMMAND: "echo x"}, None))
    assert not mc.load_app_settings({}, tmp_path / "missing.toml").complete
    bad = tmp_path / "bad.toml"
    bad.write_text("[app\n")
    with pytest.raises(mc.MintError):
        mc.load_app_settings({}, bad)


def test_key_file_permissions(tmp_path: Path, keypair: tuple[bytes, Any]) -> None:
    pem, _ = keypair
    loose = write_key(tmp_path, pem, 0o644)
    with pytest.raises(mc.MintError) as info:
        mc.read_private_key(mc.AppSettings(CLIENT_ID, str(loose)))
    assert "chmod 600" in info.value.reason
    assert pem.decode()[40:80] not in info.value.reason
    loose.chmod(0o600)
    assert mc.read_private_key(mc.AppSettings(CLIENT_ID, str(loose))) == pem
    with pytest.raises(mc.MintError):
        mc.read_private_key(mc.AppSettings(CLIENT_ID, str(tmp_path / "nope.pem")))


def test_key_command(tmp_path: Path, keypair: tuple[bytes, Any]) -> None:
    pem, _ = keypair
    # The command reads a file the key check would refuse: commands are not mode-checked.
    src = write_key(tmp_path, pem, 0o644)
    settings = mc.AppSettings(CLIENT_ID, None, f"cat '{src}'")
    assert mc.read_private_key(settings).strip() == pem.strip()
    failing = mc.AppSettings(CLIENT_ID, None, "echo oops >&2; exit 3")
    with pytest.raises(mc.MintError) as info:
        mc.read_private_key(failing)
    assert "exit code 3" in info.value.reason


async def test_mint_statuses(tmp_path: Path, keypair: tuple[bytes, Any]) -> None:
    pem, _ = keypair
    settings = mc.AppSettings(CLIENT_ID, str(write_key(tmp_path, pem)))
    api = FakeAppApi()
    gh_http.set_test_transport(httpx.MockTransport(api))
    ok = await mc.mint("octo-org/widgets", settings)
    assert ok.status == "ok" and ok.token == MINTED and ok.bot_login == "msr-test-app[bot]"
    assert ok.can_label
    check = await mc.mint("octo-org/widgets", settings, check_only=True)
    assert check.status == "ok" and check.token is None and check.permissions
    api.installed = False
    missing = await mc.mint("octo-org/widgets", settings)
    assert missing.status == "not_installed"
    assert "returned 404" in (missing.reason or "")
    api.installed = True
    api.perms = {"contents": "read", "pull_requests": "read"}
    weak = await mc.mint("octo-org/widgets", settings)
    assert weak.status == "insufficient" and "pull_requests: write" in (weak.reason or "")
    none = await mc.mint("octo-org/widgets", mc.AppSettings(None, None))
    assert none.status == "no_credentials"
    assert (await mc.mint("not a repo", settings)).status == "error"
    garbage = write_key(
        tmp_path, b"-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n"
    )
    bad = await mc.mint("octo-org/widgets", mc.AppSettings(CLIENT_ID, str(garbage)))
    assert bad.status == "error" and "not a valid PEM" in (bad.reason or "")
    assert "AAAA" not in (bad.reason or "")


def test_main_prints_one_json_line_without_the_key(
    tmp_path: Path,
    keypair: tuple[bytes, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pem, _ = keypair
    src = write_key(tmp_path, pem)
    monkeypatch.setenv(mc.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(mc.ENV_KEY_COMMAND, f"cat '{src}'")
    monkeypatch.delenv(mc.ENV_KEY_PATH, raising=False)
    gh_http.set_test_transport(httpx.MockTransport(FakeAppApi()))
    assert mc.main(["--repo", "octo-org/widgets", "--config", str(tmp_path / "none.toml")]) == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["status"] == "ok" and data["token"] == MINTED
    body = pem.decode().splitlines()[1]
    assert body not in out and body not in err


async def test_mint_via_child_parses_last_line(tmp_path: Path) -> None:
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import json, sys\n"
        "print('noise')\n"
        "print(json.dumps({'status': 'ok', 'token': 'ghs_x', 'slug': 's', "
        "'permissions': {'issues': 'write'}}))\n"
    )
    shim = tmp_path / "python"
    shim.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{stub}'\n")
    shim.chmod(0o755)
    result = await mc.mint_via_child("o/r", python=str(shim), env={"PATH": os.defpath})
    assert result.status == "ok" and result.token == "ghs_x" and result.can_label
    assert "ghs_x" not in repr(result)
    shim.write_text("#!/bin/sh\necho 'not json'\n")
    bad = await mc.mint_via_child("o/r", python=str(shim), env={"PATH": os.defpath})
    assert bad.status == "error"
    shim.write_text("#!/bin/sh\nexit 4\n")
    empty = await mc.mint_via_child("o/r", python=str(shim), env={"PATH": os.defpath})
    assert empty.status == "error" and "exit code 4" in (empty.reason or "")


async def test_mint_via_real_child_without_credentials(tmp_path: Path) -> None:
    # The real child with no credentials answers no_credentials without any network use.
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path)}
    result = await mc.mint_via_child("octo-org/widgets", env=env, timeout_s=60)
    assert result.status == "no_credentials"


def test_from_json_rejects_odd_payloads() -> None:
    assert mc.MintResult.from_json([1]).status == "error"
    assert mc.MintResult.from_json({"status": "weird"}).status == "error"
    assert mc.MintResult.from_json({"status": "ok"}).status == "error"  # ok without token
    assert mc.MintResult.from_json({"status": "ok", "check": True}).status == "ok"


async def test_mint_via_child_timeout_kills_the_key_command(tmp_path: Path) -> None:
    # The child starts a long "key command"; a timeout must kill both.
    pid_file = tmp_path / "grandchild.pid"
    shim = tmp_path / "python"
    shim.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > '{pid_file}'\nwait\n")
    shim.chmod(0o755)
    started = time.monotonic()
    result = await mc.mint_via_child(
        "o/r", python=str(shim), env={"PATH": os.defpath}, timeout_s=1.5
    )
    # Killing only the child would leave the key command holding the pipes, and
    # waiting for the child would then last as long as the key command does.
    assert time.monotonic() - started < 10
    assert result.status == "error" and "timed out" in (result.reason or "")
    grandchild = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(grandchild, 9)
        pytest.fail("the key command outlived the mint child")


async def test_mint_via_child_requires_a_token_unless_checking(tmp_path: Path) -> None:
    shim = tmp_path / "python"
    shim.write_text('#!/bin/sh\necho \'{"status": "ok", "slug": "s", "check": true}\'\n')
    shim.chmod(0o755)
    env = {"PATH": os.defpath}
    assert (await mc.mint_via_child("o/r", python=str(shim), env=env)).status == "error"
    checked = await mc.mint_via_child("o/r", python=str(shim), env=env, check_only=True)
    assert checked.status == "ok" and checked.slug == "s"


async def test_mint_child_ignores_modules_in_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Security review SEC-CI-2: `msreview app-check` run inside a cloned repository.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = tmp_path / "PWNED"
    for module in ("json.py", "argparse.py", "tomllib.py"):
        (checkout / module).write_text(f"open({str(marker)!r}, 'a').write('{module}\\n')\n")
    pkg = checkout / "morning_star_review"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(f"open({str(marker)!r}, 'a').write('pkg\\n')\n")
    monkeypatch.chdir(checkout)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "BWS_ACCESS_TOKEN": "fake-machine-token",
        "PYTHONPATH": str(checkout),  # ignored in isolated mode as well
    }
    result = await mc.mint_via_child("octo-org/widgets", env=env, timeout_s=60, check_only=True)
    assert result.status == "no_credentials"
    assert not marker.exists()


async def test_mint_child_reads_app_settings_from_msreview_config(tmp_path: Path) -> None:
    # Security review SEC-CI-8: the same config file as the driver.
    custom = tmp_path / "custom.toml"
    custom.write_text('[app]\nclient_id = "Iv1.custom"\nprivate_key_command = "exit 3"\n')
    home = tmp_path / "home"
    home.mkdir()
    assert mc.default_config_path({"MSREVIEW_CONFIG": str(custom), "HOME": str(home)}) == custom
    assert mc.child_env("mint", source={"MSREVIEW_CONFIG": str(custom)})["MSREVIEW_CONFIG"] == str(
        custom
    )
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home)}
    without = await mc.mint_via_child("o/r", env=env, timeout_s=60, check_only=True)
    assert without.status == "no_credentials"
    env["MSREVIEW_CONFIG"] = str(custom)
    used = await mc.mint_via_child("o/r", env=env, timeout_s=60, check_only=True)
    assert used.status != "no_credentials"  # the key command from custom.toml ran (and failed)
    assert "exit code 3" in (used.reason or "")
