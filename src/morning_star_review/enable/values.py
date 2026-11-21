"""The values ``msreview enable`` stores: collected once, checked, kept in memory only.

The Mistral key and the App private key live in ``bytearray`` buffers that ``clear``
overwrites; they are never printed, logged or written to a file by msreview.
"""

from __future__ import annotations

import re
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from morning_star_review.core.errors import MsreviewError

__all__ = [
    "ENV_APP_PRIVATE_KEY",
    "ENV_CLIENT_ID",
    "ENV_MISTRAL_KEY",
    "AppIdentity",
    "EnableUsageError",
    "Prompts",
    "Values",
    "check_client_id",
    "check_mistral_key",
    "check_private_key",
    "configured_client_id",
    "read_key_file",
]

ENV_MISTRAL_KEY = "MSREVIEW_ENABLE_MISTRAL_API_KEY"
ENV_CLIENT_ID = "MSREVIEW_ENABLE_APP_CLIENT_ID"
ENV_APP_PRIVATE_KEY = "MSREVIEW_ENABLE_APP_PRIVATE_KEY"
_CLIENT_ID_RE = re.compile(r"^Iv[0-9A-Za-z._-]{6,62}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{8,512}$")  # printable ASCII, no spaces
_MAX_KEY_FILE = 64 * 1024


class EnableUsageError(MsreviewError):
    """Bad flags, input or environment (exit code 2; the message is safe to show)."""


@dataclass
class Prompts:
    """How values are asked for: hidden for secrets, echoed for the client id."""

    secret: Callable[[str], str]
    text: Callable[[str], str]
    interactive: bool

    @classmethod
    def terminal(cls) -> Prompts:
        import getpass

        return cls(secret=getpass.getpass, text=input, interactive=sys.stdin.isatty())


@dataclass(frozen=True)
class AppIdentity:
    """The GitHub App (``GET /app``)."""

    client_id: str
    slug: str | None = None
    app_id: int | None = None
    name: str | None = None


@dataclass
class Values:
    """What one ``msreview enable`` run stores in every repository."""

    client_id: str  # "" when not needed (--no-secrets without a key)
    mistral_key: bytearray | None = field(default=None, repr=False)
    private_key: bytearray | None = field(default=None, repr=False)

    def clear(self) -> None:
        """Overwrite and drop the secret buffers."""
        for buf in (self.mistral_key, self.private_key):
            if buf is not None:
                buf[:] = b"\0" * len(buf)
        self.mistral_key = None
        self.private_key = None


def check_client_id(value: str) -> str:
    text = value.strip()
    if not _CLIENT_ID_RE.match(text):
        raise EnableUsageError(
            "the App client id should look like Iv23li... (GitHub: the App's settings, "
            "General, Client ID); the numeric App ID is not the client id"
        )
    return text


def check_mistral_key(value: bytes | bytearray) -> bytearray:
    text = bytes(value).strip()
    if not _KEY_RE.match(text.decode("latin-1")):
        raise EnableUsageError(
            "the Mistral API key looks wrong (empty, too short, or it has spaces or "
            "control characters)"
        )
    return bytearray(text)


def check_private_key(pem: bytes | bytearray) -> bytearray:
    """``pem`` stripped, with one final newline, when it is a PEM RSA private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    data = bytes(pem).strip() + b"\n"
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # any parse failure: the message below says what to do
        key = None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise EnableUsageError(
            "the App private key is not an unencrypted PEM RSA private key (download a "
            "new key from the App's settings page)"
        )
    return bytearray(data)


def read_key_file(path: str, warn: Callable[[str], None]) -> bytearray:
    """Read the App's private key file once; warn when other users can read it."""
    p = Path(path).expanduser()
    try:
        st = p.stat()
    except OSError as exc:
        raise EnableUsageError(f"cannot read {p}: {exc.strerror or 'error'}") from None
    if not stat.S_ISREG(st.st_mode):
        raise EnableUsageError(f"{p} is not a regular file")
    if st.st_size > _MAX_KEY_FILE:
        raise EnableUsageError(f"{p} is too large for a private key")
    if st.st_mode & 0o077:
        warn(
            f"{p} can be read by other users (mode {stat.S_IMODE(st.st_mode):04o}); "
            f"run `chmod 600 {p}`, or delete it once the key is stored in GitHub"
        )
    try:
        return bytearray(p.read_bytes())
    except OSError as exc:
        raise EnableUsageError(f"cannot read {p}: {exc.strerror or 'error'}") from None


def configured_client_id(env: Mapping[str, str]) -> tuple[str | None, str]:
    """The client id the CLI itself uses (``MSREVIEW_APP_CLIENT_ID`` or the config
    file's ``[app] client_id``) and where it came from; ``(None, "")`` if none."""
    from morning_star_review.github import mint_child

    try:
        settings = mint_child.load_app_settings(env, mint_child.default_config_path(env))
    except Exception:  # an unreadable config file only means: not configured here
        return None, ""
    if not settings.client_id:
        return None, ""
    from_env = env.get(mint_child.ENV_CLIENT_ID, "").strip()
    return settings.client_id, mint_child.ENV_CLIENT_ID if from_env else "the config file"
