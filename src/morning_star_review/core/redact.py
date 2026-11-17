"""Remove secrets from text before it is logged, stored or shown.

Two layers: exact values the caller knows (``extra_secrets``: tokens, API keys) and
patterns for secrets we may not know (GitHub tokens, PEM blocks, JWTs, auth headers,
URL credentials, ``*_KEY=...`` assignments).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

__all__ = ["MIN_SECRET_LEN", "REDACTED", "redact", "secret_env_values"]

REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 8  # shorter "secrets" would mangle ordinary text

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}-----.*?(?:-----END [A-Z0-9 ]{0,40}-----|\Z)",
    re.DOTALL,
)
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# "Authorization: Bearer x", "AUTHORIZATION: basic x" (git extraheader), "token x" headers.
_AUTH_HEADER = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:(?:bearer|token|basic)\s+)?[^\s\"',;]+"
)
# Real tokens contain a digit; requiring one spares prose like "token expiration".
_BEARER = re.compile(r"(?i)\b(bearer|token)(\s+)(?=[A-Za-z._~+/=-]*[0-9])([A-Za-z0-9._~+/=-]{12,})")
# Bounded repetitions keep these linear on long runs of name-like characters.
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://)[^/\s:@]*(?::[^/\s@]*)?@")
_ASSIGNMENT = re.compile(
    # The name must END with the secret word: GITHUB_TOKEN yes, max_tokens / token_count no.
    r"(?i)\b([A-Z0-9_-]{0,64}(?:API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|SECRET|PASSWORD|PASSWD|TOKEN))\b"
    # A quote may close the name: {"api_key": "..."} and {'token': '...'} count too.
    r"([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;]+)"
)
_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PEM)")


def _without_known(text: str, secrets: Iterable[str]) -> str:
    values = {s.strip() for s in secrets if s and len(s.strip()) >= MIN_SECRET_LEN}
    # Longest first, so a secret that contains another is removed whole.
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def redact(text: str, extra_secrets: Iterable[str] | None = None) -> str:
    """Return ``text`` with known secret values and secret-looking strings replaced."""
    if not text:
        return text
    out = _without_known(text, extra_secrets or ())
    out = _PEM.sub("[REDACTED PEM]", out)
    out = _GITHUB_TOKEN.sub(REDACTED, out)
    out = _JWT.sub(REDACTED, out)
    out = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BEARER.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)
    out = _URL_CREDENTIALS.sub(lambda m: m.group(1) + REDACTED + "@", out)
    return _ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)


def secret_env_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Values of environment variables whose names look secret (for ``extra_secrets``)."""
    source = os.environ if env is None else env
    return [
        value
        for name, value in source.items()
        if _SECRET_ENV_NAME.search(name) and len(value.strip()) >= MIN_SECRET_LEN
    ]
