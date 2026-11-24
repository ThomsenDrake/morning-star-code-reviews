"""The workflow template, its parsing, and the checks of the values ``enable`` stores."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from morning_star_review._version import __version__
from morning_star_review.enable import template as tpl
from morning_star_review.enable.appapi import HttpAppApi
from morning_star_review.enable.ops import parse_repo
from morning_star_review.enable.values import (
    EnableUsageError,
    Prompts,
    Values,
    check_client_id,
    check_mistral_key,
    check_private_key,
    read_key_file,
)
from morning_star_review.enable.verify import HttpVerifier

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "workflows" / "morning-star-code-review.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_the_example_is_the_template_rendered_for_this_release() -> None:
    assert EXAMPLE.read_text() == tpl.render(__version__), (
        'regenerate it: python -c "from pathlib import Path; from morning_star_review.'
        "_version import __version__ as v; from morning_star_review.enable.template import "
        f"render; Path('{EXAMPLE.relative_to(ROOT)}').write_text(render(v))\""
    )


def test_rendered_workflow_pins_the_commit_and_sets_the_preset() -> None:
    text = tpl.render("v0.2.0", SHA, "balanced")
    assert f"      - uses: {tpl.ACTION_REPO}@{SHA} # v0.2.0\n" in text
    assert "__MSREVIEW" not in text
    doc = yaml.safe_load(text)
    job = doc["jobs"]["review"]
    assert job["timeout-minutes"] == 110
    step = job["steps"][0]
    assert step["uses"] == f"{tpl.ACTION_REPO}@{SHA}"
    assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"])
    assert step["with"]["preset"] == "balanced"
    assert step["with"]["client-id"] == "${{ vars.MSREVIEW_APP_CLIENT_ID }}"
    plain = yaml.safe_load(tpl.render("v0.2.0", SHA))["jobs"]["review"]["steps"][0]
    assert "preset" not in plain["with"]
    assert "# preset: best              # best | balanced" in tpl.render("0.2.0")


@pytest.mark.parametrize(
    ("version", "sha", "preset"),
    [("main", None, None), ("v0.2.0", "abc123", None), ("v0.2.0", SHA, "fastest")],
)
def test_render_rejects_bad_values(version: str, sha: str | None, preset: str | None) -> None:
    with pytest.raises(ValueError):
        tpl.render(version, sha, preset)


def test_pin_preset_and_repin_round_trip() -> None:
    text = tpl.render("v0.1.1", SHA, "fast")
    pin = tpl.find_pin(text)
    assert pin == tpl.Pin(ref=SHA, version="v0.1.1")
    assert pin.describe() == "v0.1.1 (0123456)"
    assert tpl.find_preset(text) == "fast"
    assert tpl.find_preset(tpl.render("v0.1.1", SHA)) is None
    other = "f" * 40
    moved = tpl.repin(text, "v0.2.0", other)
    assert moved == tpl.render("v0.2.0", other, "fast")
    tag_pin = tpl.find_pin(EXAMPLE.read_text())
    assert tag_pin is not None and tag_pin.sha is None
    assert tag_pin.describe() == f"v{__version__} (not pinned to a commit)"
    assert tpl.repin("name: x\n", "v0.2.0", other) is None
    assert tpl.find_pin("steps:\n  - uses: actions/checkout@v4\n") is None


FORKS = ROOT / "examples" / "workflows" / "morning-star-code-review-forks.yml"
SECRET_INPUTS = {
    "mistral-api-key": "${{ secrets.MSREVIEW_MISTRAL_API_KEY }}",
    "client-id": "${{ vars.MSREVIEW_APP_CLIENT_ID }}",
    "app-private-key": "${{ secrets.MSREVIEW_APP_PRIVATE_KEY }}",
}


def _action_inputs(text: str) -> dict[str, str]:
    for job in yaml.safe_load(text)["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith(f"{tpl.ACTION_REPO}@"):
                return {k: v for k, v in step["with"].items() if k in SECRET_INPUTS}
    raise AssertionError("no step uses the Action")


def test_template_and_examples_use_msreviews_own_secret_names() -> None:
    # MSREVIEW_ names never collide with a repository's own MISTRAL_API_KEY.
    for text in (tpl.render("v0.2.0", SHA), EXAMPLE.read_text(), FORKS.read_text()):
        assert _action_inputs(text) == SECRET_INPUTS
        assert not tpl.uses_legacy_secret(text)
        assert "secrets.MISTRAL_API_KEY" not in text
    assert EXAMPLE.read_text() == tpl.render(__version__)


@pytest.mark.parametrize(
    ("text", "legacy"),
    [
        ("mistral-api-key: ${{ secrets.MISTRAL_API_KEY }}", True),
        ("k: ${{secrets.mistral_api_key}}", True),
        ("k: ${{ secrets['MISTRAL_API_KEY'] }}", True),
        ("k: ${{ secrets.MSREVIEW_MISTRAL_API_KEY }}", False),
        ("k: ${{ secrets.MISTRAL_API_KEY_2 }}", False),
        ("k: ${{ vars.MISTRAL_API_KEY }}", False),
        ("# k: ${{ secrets.MISTRAL_API_KEY }}", False),
        ("k: 1  # was ${{ secrets.MISTRAL_API_KEY }}", False),
    ],
)
def test_uses_legacy_secret(text: str, legacy: bool) -> None:
    assert tpl.uses_legacy_secret(text) is legacy


def test_diff_summary_is_short_and_safe() -> None:
    old = "\n".join(f"line {i}" for i in range(20)) + "\n\x1b]52;c;evil\x07\n"
    lines = tpl.diff_summary(old, "new\n", limit=3)
    assert lines[0].startswith("differences")
    assert len(lines) == 5 and lines[-1].startswith("    ... and ")
    assert all("\x1b" not in line for line in tpl.diff_summary(old, "new\n", limit=30))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("octo/one", "octo/one"),
        (" https://github.com/octo/one.git/ ", "octo/one"),
        ("github.com/Octo-Org/my.repo", "Octo-Org/my.repo"),
    ],
)
def test_parse_repo(text: str, expected: str) -> None:
    assert parse_repo(text) == expected


@pytest.mark.parametrize("text", ["octo", "octo/one/two", "-x/y", "octo/..", "o/r;rm", ""])
def test_parse_repo_rejects(text: str) -> None:
    with pytest.raises(EnableUsageError):
        parse_repo(text)


def pem_of(key: Any) -> bytes:
    return bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def test_value_checks() -> None:
    assert check_client_id(" Iv23liAbCdEf0123 ") == "Iv23liAbCdEf0123"
    for bad in ("123456", "Iv1", "Iv23 li", ""):
        with pytest.raises(EnableUsageError, match="client id"):
            check_client_id(bad)
    assert check_mistral_key(b"  abcdefghijk\n") == bytearray(b"abcdefghijk")
    for bad_key in (b"", b"short", b"has space inside", b"tab\tinside-key"):
        with pytest.raises(EnableUsageError, match="Mistral API key"):
            check_mistral_key(bad_key)
    rsa_pem = pem_of(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert check_private_key(b"\n" + rsa_pem + b"\n\n") == bytearray(rsa_pem.strip() + b"\n")
    ec_pem = pem_of(ec.generate_private_key(ec.SECP256R1()))
    for bad_pem in (b"not a key", ec_pem, rsa_pem[:200]):
        with pytest.raises(EnableUsageError, match="PEM RSA private key") as info:
            check_private_key(bad_pem)
        assert "BEGIN" not in str(info.value)


def test_terminal_prompts_hide_the_key() -> None:
    import builtins
    import getpass

    prompts = Prompts.terminal()
    assert prompts.secret is getpass.getpass  # the Mistral key is never echoed
    assert prompts.text is builtins.input  # the client id is not a secret


def test_key_file_mode_warning_and_clear(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_bytes(b"pem bytes")
    key.chmod(0o644)
    warnings: list[str] = []
    data = read_key_file(str(key), warnings.append)
    assert data == bytearray(b"pem bytes")
    assert warnings and "chmod 600" in warnings[0] and "0644" in warnings[0]
    key.chmod(0o600)
    warnings.clear()
    read_key_file(str(key), warnings.append)
    assert warnings == []
    with pytest.raises(EnableUsageError, match="cannot read"):
        read_key_file(str(tmp_path / "missing.pem"), warnings.append)
    with pytest.raises(EnableUsageError, match="not a regular file"):
        read_key_file(str(tmp_path), warnings.append)
    mistral, pem = bytearray(b"secret-key"), bytearray(b"secret-pem")
    values = Values(client_id="Iv23liAbCdEf0123", mistral_key=mistral, private_key=pem)
    assert "secret" not in repr(values)
    values.clear()
    assert mistral == bytearray(len(b"secret-key")) and pem == bytearray(len(b"secret-pem"))
    assert values.mistral_key is None and values.private_key is None


def test_http_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    answers: dict[str, httpx.Response] = {}

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        seen.append((url, headers["Authorization"]))
        return answers[url.rsplit("/", 1)[-1]]

    monkeypatch.setattr(httpx, "get", fake_get)
    verifier = HttpVerifier()
    answers["whoami"] = httpx.Response(200, json={"namespace": "ns"})
    verifier.mistral(b"mk-abcdefgh")
    assert seen[-1] == ("https://api.mistral.ai/v1/workflows/workers/whoami", "Bearer mk-abcdefgh")
    answers["whoami"] = httpx.Response(401)
    with pytest.raises(EnableUsageError, match=r"refused the API key \(HTTP 401\)"):
        verifier.mistral(b"mk-abcdefgh")


def test_http_app_api_asks_as_the_app_and_revokes_its_token() -> None:
    seen: list[tuple[str, str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"]
        seen.append((request.method, request.url.path, auth, request.content))
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": 7, "slug": "octo-review", "name": "Octo"})
        if path == "/repos/octo/one/installation":
            return httpx.Response(200, json={"id": 11})
        if path == "/repos/octo/other/installation":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": 11, "account": {"login": "octo"}}])
        if path == "/app/installations/11/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation"})
        if path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [{"full_name": "octo/one"}]})
        if path == "/installation/token":
            return httpx.Response(204)
        return httpx.Response(500)

    pem = pem_of(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    api = HttpAppApi("Iv23liAbCdEf0123", pem, transport=httpx.MockTransport(handler))
    assert "BEGIN" not in repr(api)
    app = api.app()
    assert (app.slug, app.app_id, app.name) == ("octo-review", 7, "Octo")
    assert api.repo_installation("octo", "one") == {"id": 11}
    assert api.repo_installation("octo", "other") is None
    assert api.installations() == [{"id": 11, "account": {"login": "octo"}}]
    assert api.installation_repos(11) == ["octo/one"]
    jwt_calls = [s for s in seen if s[2].startswith("Bearer ey")]
    assert {s[1] for s in jwt_calls} >= {"/app", "/app/installations/11/access_tokens"}
    post = next(s for s in seen if s[0] == "POST")
    assert json.loads(post[3]) == {"permissions": {"metadata": "read"}}  # read-only token
    assert seen[-1][:3] == ("DELETE", "/installation/token", "Bearer ghs_installation")
    api.close()
    refused = HttpAppApi(
        "Iv23liAbCdEf0123", pem, transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    with pytest.raises(EnableUsageError, match="do not belong to the same App"):
        refused.app()
    refused.close()


def test_has_command_trigger() -> None:
    text = tpl.render("v1.2.3")
    assert tpl.has_command_trigger(text)
    assert not tpl.has_command_trigger(text.replace("  issue_comment:", "  # issue_comment:"))
    assert not tpl.has_command_trigger("on:\n  pull_request:\n# issue_comment: later\n")
