"""Redactor for error text and logs (DESIGN 13.2 "Log leakage")."""

from __future__ import annotations

import pytest

from morning_star_review.core.redact import REDACTED, redact, secret_env_values

# Built at run time so that no token-shaped literal sits in the repository.
GHS = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
JWT = "eyJ" + "hbGciOiJSUzI1NiJ9" + ".eyJ" + "pc3MiOiIxMjM0NSJ9" + ".c2lnbmF0dXJlLXZhbHVl"
PEM = (
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow"
    "IBAAKCAQEA0123\nabcdef==\n-----END RSA " + "PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"token {GHS} rejected", GHS),
        (f"pat={PAT}", PAT),
        (f"jwt {JWT}", JWT),
        (f"key:\n{PEM}\nafter", "MIIEow"),
        ("Authorization: Bearer abc.def-123456789", "abc.def-123456789"),
        ("AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2hzX3h4eA==", "eC1hY2Nlc3M"),
        ("header Authorization=token 0123456789abcdef", "0123456789abcdef"),
        ("https://x-access-token:s3cr3tvalue@github.com/o/r.git", "s3cr3tvalue"),
        ("https://user@github.com/o/r", "user@"),
        ("MISTRAL_API_KEY=abcd1234efgh5678", "abcd1234efgh5678"),
        ("GITHUB_TOKEN: 'quoted value 1'", "quoted value 1"),
        ('client_secret="xyz987654321"', "xyz987654321"),
        ("x-api-key: k3y-v4lue-0001", "k3y-v4lue-0001"),
        ("bearer AbCdEf0123456789xyz", "AbCdEf0123456789xyz"),
    ],
)
def test_patterns_are_redacted(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert "REDACTED" in out


def test_unterminated_pem_is_redacted_to_the_end() -> None:
    out = redact("-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\n(truncated")
    assert "MIIEvQ" not in out and out.startswith("[REDACTED PEM]")


def test_known_secrets_are_replaced_longest_first() -> None:
    out = redact("key abcdefgh12345678 and its prefix abcdefgh", ["abcdefgh", "abcdefgh12345678"])
    assert out == f"key {REDACTED} and its prefix {REDACTED}"


def test_short_or_empty_known_secrets_are_ignored() -> None:
    assert redact("a b c", ["a", "", "  "]) == "a b c"


def test_known_secret_with_surrounding_whitespace() -> None:
    assert redact("value=zzzzzzzzzzzz", ["  zzzzzzzzzzzz\n"]) == f"value={REDACTED}"


@pytest.mark.parametrize(
    "text",
    [
        "input_tokens=1234 output_tokens=56",
        "max_tokens: 4096",
        "token_count=5",
        "the token expired yesterday",
        "tokens are counted per call",
        "git fetch failed: could not resolve host",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert redact(text) == text


def test_redact_is_idempotent() -> None:
    text = f"{GHS} {PAT} {JWT} Authorization: Bearer abc123456789 GITHUB_TOKEN=xyz12345678"
    once = redact(text, ["xyz12345678"])
    assert redact(once, ["xyz12345678"]) == once


def test_secret_env_values() -> None:
    env = {
        "MISTRAL_API_KEY": "k" * 32,
        "GITHUB_TOKEN": "t" * 20,
        "MY_SECRET": "s" * 10,
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/me",
        "SHORT_TOKEN": "abc",
    }
    assert sorted(secret_env_values(env)) == sorted(["k" * 32, "t" * 20, "s" * 10])


@pytest.mark.parametrize(
    "text",
    ["a-" * 30000, "A" * 60000, "https://" + "a" * 60000, "x-api-key-" * 6000, "eyJ" * 20000],
)
def test_redact_is_fast_on_adversarial_input(text: str) -> None:
    import time

    start = time.monotonic()
    redact(text)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"api_key": "sk1234567890abcdef"}', '{"api_key": [REDACTED]}'),
        ('{"token":"abcd1234efgh5678"}', '{"token":[REDACTED]}'),
        ("{'api_key': 'abcdefgh1234'}", "{'api_key': [REDACTED]}"),
        ('{"max_tokens": 4000, "input_tokens": 12}', '{"max_tokens": 4000, "input_tokens": 12}'),
    ],
)
def test_quoted_json_and_repr_keys(text: str, expected: str) -> None:
    assert redact(text) == expected
