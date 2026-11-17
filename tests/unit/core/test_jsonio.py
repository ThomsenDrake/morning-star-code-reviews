"""Run-directory file helpers."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from morning_star_review.core.contract import Usage
from morning_star_review.core.errors import MsreviewDigestMismatch, MsreviewRunDirMissing
from morning_star_review.core.jsonio import (
    FileTooLarge,
    canonical_dumps,
    read_bytes,
    read_json,
    read_model,
    read_text,
    read_verified,
    sha256_bytes,
    sha256_file,
    sha256_text,
    write_bytes_atomic,
    write_json_atomic,
    write_model,
    write_text_atomic,
)


def test_canonical_dumps_is_sorted_compact_and_utf8() -> None:
    assert canonical_dumps({"b": 1, "a": [1, {"d": "é", "c": None}]}) == (
        '{"a":[1,{"c":null,"d":"é"}],"b":1}'
    )


def test_digests_agree() -> None:
    data = "héllo\n".encode()
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    assert sha256_text("héllo\n") == sha256_bytes(data)


def test_atomic_write_creates_parents_sets_mode_and_returns_digest(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.bin"
    digest = write_bytes_atomic(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert digest == sha256_bytes(b"payload") == sha256_file(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["file.bin"]  # no temp files left


def test_atomic_write_replaces_and_honours_mode(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    write_text_atomic(target, "one")
    write_text_atomic(target, "two", mode=0o644)
    assert target.read_text() == "two"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        write_bytes_atomic(tmp_path / "x", b"data")
    assert list(tmp_path.iterdir()) == []


def test_json_and_model_round_trip(tmp_path: Path) -> None:
    write_json_atomic(tmp_path / "d.json", {"z": 1, "a": "ü"})
    assert read_json(tmp_path / "d.json") == {"z": 1, "a": "ü"}
    assert json.loads((tmp_path / "d.json").read_text())["a"] == "ü"
    usage = Usage(model="zai-glm-5-3", llm_calls=2, input_tokens=10)
    digest = write_model(tmp_path / "u.json", usage)
    assert read_model(tmp_path / "u.json", Usage) == usage
    assert read_verified(tmp_path / "u.json", digest) == (tmp_path / "u.json").read_bytes()


def test_missing_file_is_run_dir_missing(tmp_path: Path) -> None:
    with pytest.raises(MsreviewRunDirMissing):
        read_bytes(tmp_path / "nope.json")
    with pytest.raises(MsreviewRunDirMissing):
        read_verified(tmp_path / "nope.json", "0" * 64)


def test_size_cap(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"x" * 101)
    with pytest.raises(FileTooLarge):
        read_bytes(tmp_path / "big", max_bytes=100)
    assert read_text(tmp_path / "big", max_bytes=101) == "x" * 101


def test_digest_mismatch(tmp_path: Path) -> None:
    digest = write_text_atomic(tmp_path / "notes.txt", "original")
    (tmp_path / "notes.txt").write_text("tampered")
    with pytest.raises(MsreviewDigestMismatch):
        read_verified(tmp_path / "notes.txt", digest)
    assert read_verified(tmp_path / "notes.txt", sha256_text("tampered").upper()) == b"tampered"


def test_invalid_utf8_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "b").write_bytes(b"ok \xff")
    assert read_text(tmp_path / "b") == "ok �"
