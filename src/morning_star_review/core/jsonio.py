"""JSON and file helpers for the run directory: canonical dumps, digests, atomic writes,
size-capped and digest-checked reads."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from morning_star_review.core.errors import MsreviewDigestMismatch, MsreviewRunDirMissing

__all__ = [
    "DEFAULT_MAX_BYTES",
    "FileTooLarge",
    "canonical_dumps",
    "read_bytes",
    "read_json",
    "read_model",
    "read_text",
    "read_verified",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "write_bytes_atomic",
    "write_json_atomic",
    "write_model",
    "write_text_atomic",
]

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_CHUNK = 1024 * 1024


class FileTooLarge(ValueError):  # noqa: N818 - reads like the condition it reports
    """A file is larger than the caller's limit."""


def canonical_dumps(data: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept as is."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "surrogatepass"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: str | Path, data: bytes, *, mode: int = 0o600) -> str:
    """Write ``data`` via a temp file in the same directory and ``os.replace``.

    Readers never see a half-written file. Parent directories are created.
    Returns the sha256 of ``data``.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return sha256_bytes(data)


def write_text_atomic(path: str | Path, text: str, *, mode: int = 0o600) -> str:
    """Atomic UTF-8 text write. Returns the sha256 of the encoded bytes."""
    return write_bytes_atomic(path, text.encode("utf-8", "surrogatepass"), mode=mode)


def write_json_atomic(path: str | Path, data: Any, *, mode: int = 0o600) -> str:
    """Atomic JSON write (sorted keys, indented for humans). Returns the sha256."""
    text = json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    return write_text_atomic(path, text, mode=mode)


def write_model(path: str | Path, model: BaseModel, *, mode: int = 0o600) -> str:
    """Atomic write of a pydantic model as JSON. Returns the sha256."""
    return write_text_atomic(path, model.model_dump_json(indent=1) + "\n", mode=mode)


def read_bytes(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Read at most ``max_bytes``; raise ``FileTooLarge`` above that.

    A missing file raises ``MsreviewRunDirMissing`` (non-retryable): run-directory
    files only disappear when another process picked up the task.
    """
    p = Path(path)
    try:
        with p.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except FileNotFoundError as exc:
        raise MsreviewRunDirMissing(f"missing run file: {p.name}") from exc
    if len(data) > max_bytes:
        raise FileTooLarge(f"{p.name} is larger than {max_bytes} bytes")
    return data


def read_text(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    return read_bytes(path, max_bytes=max_bytes).decode("utf-8", "replace")


def read_json(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Any:
    return json.loads(read_bytes(path, max_bytes=max_bytes))


def read_model[M: BaseModel](
    path: str | Path, cls: type[M], *, max_bytes: int = DEFAULT_MAX_BYTES
) -> M:
    """Read and validate a pydantic model from a JSON file."""
    return cls.model_validate_json(read_bytes(path, max_bytes=max_bytes))


def read_verified(
    path: str | Path, expected_sha256: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> bytes:
    """Read a file and check it against the digest a payload carries.

    Raises ``MsreviewRunDirMissing`` if it is gone and ``MsreviewDigestMismatch`` if
    its content changed.
    """
    data = read_bytes(path, max_bytes=max_bytes)
    if sha256_bytes(data) != expected_sha256.lower():
        raise MsreviewDigestMismatch(f"digest mismatch for run file: {Path(path).name}")
    return data
