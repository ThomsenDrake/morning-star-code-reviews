"""Scope decisions: full, incremental, carry_over (DESIGN 7.6)."""

from __future__ import annotations

from typing import Any

import pytest

from morning_star_review.core.contract import PriorState
from morning_star_review.diff.incremental import decide_scope, major_version

PRIOR_HEAD = "c" * 40
REVIEWABLE = ["src/a.py", "src/b.py", "src/c.py"]


def _prior(cfg, **kw: Any) -> PriorState:
    data: dict[str, Any] = {
        "version": 1,
        "tool_version": "0.1.0",
        "head_sha": PRIOR_HEAD,
        "merge_base_sha": "a" * 40,
        "complete": True,
        "verdict": "findings",
        "config_hash": cfg.config_hash,
        "reviewed_at": "2026-11-17T00:00:00Z",
    }
    data.update(kw)
    return PriorState(**data)


def _decide(cfg, prior, **kw: Any):
    args: dict[str, Any] = {
        "compare_status": "ahead",
        "merges": False,
        "changed_since_prior": ["src/b.py", "uv.lock"],
        "reviewable": REVIEWABLE,
        "force": False,
        "tool_version": "0.1.3",
    }
    args.update(kw)
    return decide_scope(
        prior,
        args.pop("compare_status"),
        args.pop("merges"),
        cfg,
        args.pop("changed_since_prior"),
        args.pop("reviewable"),
        args.pop("force"),
        **args,
    )


def test_incremental_when_every_condition_holds(h) -> None:
    cfg = h.make_cfg()
    d = _decide(cfg, _prior(cfg))
    assert d.scope == "incremental"
    assert d.since_sha == PRIOR_HEAD
    assert d.diff_paths == ["src/b.py"]  # uv.lock is not reviewable


def test_carry_over_when_no_reviewable_file_changed(h) -> None:
    cfg = h.make_cfg()
    d = _decide(cfg, _prior(cfg), changed_since_prior=["uv.lock"])
    assert d.scope == "carry_over"
    assert d.diff_paths == []
    assert d.reason == "no reviewable changes since ccccccc"


@pytest.mark.parametrize(
    ("change", "reason_part"),
    [
        ({"force": True}, "requested"),
        ({"compare_status": "diverged"}, "force push"),
        ({"compare_status": None}, "unknown"),
        ({"compare_status": "identical"}, "already reviewed"),
        ({"compare_status": "behind"}, "behind; force push"),
        ({"merges": True}, "merge commits"),
        ({"changed_since_prior": None}, "unknown"),
        ({"changed_since_prior": [f"f{i}" for i in range(301)]}, "300"),
        ({"prior_fetchable": False}, "no longer available"),
        ({"tool_version": "0.2.0"}, "version 0.1.0"),
    ],
)
def test_full_when_a_condition_fails(h, change: dict[str, Any], reason_part: str) -> None:
    cfg = h.make_cfg()
    d = _decide(cfg, _prior(cfg), **change)
    assert d.scope == "full"
    assert d.since_sha is None
    assert d.diff_paths == REVIEWABLE
    assert reason_part in d.reason


def test_full_without_prior_or_when_prior_incomplete(h) -> None:
    cfg = h.make_cfg()
    assert _decide(cfg, None).reason == "no previous review"
    d = _decide(cfg, _prior(cfg, complete=False))
    assert d.scope == "full" and "incomplete" in d.reason


def test_full_when_config_changed_or_incremental_off(h) -> None:
    cfg = h.make_cfg()
    prior = _prior(cfg)
    changed = h.make_cfg(instructions="new guidance")
    assert _decide(changed, prior).reason == "the review configuration changed"
    off = h.make_cfg(incremental=False)
    assert _decide(off, _prior(off)).reason == "incremental reviews are turned off"


def test_force_wins_over_carry_over(h) -> None:
    cfg = h.make_cfg()
    d = _decide(cfg, _prior(cfg), changed_since_prior=[], force=True)
    assert d.scope == "full"


@pytest.mark.parametrize(
    ("version", "key"),
    [("1.4.2", "1"), ("2.0", "2"), ("0.3.1", "0.3"), ("v1.0.0", "1"), ("", "")],
)
def test_major_version(version: str, key: str) -> None:
    assert major_version(version) == key
