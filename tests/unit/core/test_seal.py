"""Seals, canonical JSON, request pins and the run context (DESIGN 4.3, 5.3)."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from morning_star_review.core import runctx
from morning_star_review.core.contract import (
    DraftFinding,
    OperatorPolicy,
    ReviewDraft,
    ReviewRequest,
    Sealed,
    Usage,
    VerifyResult,
)
from morning_star_review.core.errors import MsreviewNoRunContext
from morning_star_review.core.runctx import (
    RunContext,
    clear_run_context,
    run_context,
    set_run_context,
)
from morning_star_review.core.seal import canonical_json, compute_seal, request_digest, seal, verify


@pytest.fixture(autouse=True)
def _no_global_context() -> Iterator[None]:
    clear_run_context()
    yield
    clear_run_context()


def make_request(**changes: object) -> ReviewRequest:
    data: dict[str, object] = {
        "pr_url": "https://github.com/acme/widgets/pull/7",
        "repo": "acme/widgets",
        "mode": "cli",
        "policy": OperatorPolicy(trust_repo_config_models=False),
        "run_label": "cli:abc123",
    }
    data.update(changes)
    return ReviewRequest.model_validate(data)


def make_ctx(req: ReviewRequest | None = None, **changes: Any) -> RunContext:
    fields: dict[str, object] = {
        "execution_id": "msreview-cli-acme-widgets-pr7-latest-1a2b3c4d",
        "request_sha256": request_digest(req or make_request()),
        "seal_key": secrets.token_bytes(32),
        "run_root": "/tmp/msreview-runs",
        "mode": "cli",
        "on_worker": False,
        "github_token": "ghs_installationtoken000000000000000",
        "read_token": None,
        "bot_login": "morning-star-code-reviews[bot]",
        "mistral_api_key": "mistral-key-for-tests-0000000000",
        "agent_python": "/usr/bin/python3",
        "cannot_post_reason": None,
    }
    fields.update(changes)
    return RunContext(**fields)  # type: ignore[arg-type]


def make_draft(summary: str = "Two problems.") -> ReviewDraft:
    finding = DraftFinding(
        path="ledger/db.py",
        line=136,
        severity="high",
        category="security",
        title="SQL injection",
        evidence="cur.execute(q)",
        explanation="Interpolated input.",
    )
    return ReviewDraft(
        findings=[finding],
        prior_status=[],
        summary=summary,
        source="parser",
        marker_count=1,
        formatter_usage=Usage(model="mistral-large-4", cost_usd=0.1),
    )


# --------------------------------------------------------------------------------------
# canonical_json and request_digest
# --------------------------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_and_excludes_the_seal() -> None:
    draft = make_draft().model_copy(update={"seal": "abc"})
    text = canonical_json(draft)
    data = json.loads(text)
    assert "seal" not in data
    assert text == json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_canonical_json_survives_a_json_round_trip() -> None:
    draft = make_draft("Résumé of the review 🙂")
    again = ReviewDraft.model_validate(json.loads(json.dumps(draft.model_dump(mode="json"))))
    assert canonical_json(again) == canonical_json(draft)
    assert ReviewDraft.model_validate_json(draft.model_dump_json()) == draft


def test_request_digest_is_sha256_of_canonical_json_and_changes_with_any_field() -> None:
    req = make_request()
    assert request_digest(req) == hashlib.sha256(canonical_json(req).encode()).hexdigest()
    assert request_digest(make_request()) == request_digest(req)
    assert request_digest(make_request(force=True)) != request_digest(req)
    assert request_digest(make_request(run_label="cli:other")) != request_digest(req)


# --------------------------------------------------------------------------------------
# seal / verify
# --------------------------------------------------------------------------------------


def test_seal_round_trip_through_json() -> None:
    ctx = make_ctx()
    sealed = seal(make_draft(), ctx)
    assert len(sealed.seal) == 64
    assert verify(sealed, ctx)
    wire = sealed.model_dump(mode="json")
    assert verify(ReviewDraft.model_validate(wire), ctx)
    assert verify(ReviewDraft.model_validate_json(sealed.model_dump_json()), ctx)


def test_seal_returns_a_copy() -> None:
    draft = make_draft()
    sealed = seal(draft, make_ctx())
    assert draft.seal == "" and sealed.seal != ""
    assert sealed.model_copy(update={"seal": ""}) == draft


def test_any_field_change_breaks_the_seal() -> None:
    ctx = make_ctx()
    sealed = seal(make_draft(), ctx)
    assert not verify(sealed.model_copy(update={"summary": "Two problems!"}), ctx)
    assert not verify(sealed.model_copy(update={"marker_count": 2}), ctx)
    finding = sealed.findings[0].model_copy(update={"line": 137})
    assert not verify(sealed.model_copy(update={"findings": [finding]}), ctx)
    usage = Usage(model="mistral-large-4", cost_usd=0.2)
    assert not verify(sealed.model_copy(update={"formatter_usage": usage}), ctx)


def test_seal_is_bound_to_execution_id_and_key() -> None:
    ctx = make_ctx()
    sealed = seal(make_draft(), ctx)
    assert not verify(sealed, make_ctx(execution_id="msreview-cli-other-pr7-latest-ffff0000"))
    assert not verify(sealed, make_ctx(seal_key=secrets.token_bytes(32)))
    assert verify(sealed, make_ctx(seal_key=ctx.seal_key))


def test_seal_is_bound_to_the_type_name() -> None:
    class First(Sealed):
        value: int

    class Second(Sealed):
        value: int

    ctx = make_ctx()
    one = seal(First(value=1), ctx)
    forged = Second(value=1, seal=one.seal)
    assert verify(one, ctx)
    assert not verify(forged, ctx)


def test_missing_or_garbage_seal_does_not_verify() -> None:
    ctx = make_ctx()
    assert not verify(make_draft(), ctx)
    assert not verify(make_draft().model_copy(update={"seal": "zz" * 32}), ctx)
    assert not verify(make_draft().model_copy(update={"seal": "é" * 10}), ctx)


def test_outer_seal_covers_nested_payloads() -> None:
    ctx = make_ctx()
    usage = Usage(model="zai-glm-5-3")
    result = VerifyResult(
        entries=[],
        source="empty",
        state="Completed",
        stop_reason=None,
        rescued=False,
        complete=True,
        budget_exhausted=False,
        opened_paths=["a.py"],
        usage=usage,
    )
    sealed = seal(result, ctx)
    changed = sealed.model_copy(update={"usage": usage.model_copy(update={"llm_calls": 9})})
    assert verify(sealed, ctx) and not verify(changed, ctx)


def test_compute_seal_matches_the_documented_formula() -> None:
    import hmac

    ctx = make_ctx()
    draft = make_draft()
    msg = f"{ctx.execution_id}\x00ReviewDraft\x00{canonical_json(draft)}".encode()
    expected = hmac.new(ctx.seal_key, msg, hashlib.sha256).hexdigest()
    assert compute_seal(draft, ctx.seal_key, ctx.execution_id) == expected


def test_seal_and_verify_default_to_the_process_context() -> None:
    ctx = make_ctx()
    set_run_context(ctx)
    sealed = seal(make_draft())
    assert verify(sealed) and verify(sealed, ctx)


# --------------------------------------------------------------------------------------
# RunContext
# --------------------------------------------------------------------------------------


def test_run_context_unset_raises() -> None:
    with pytest.raises(MsreviewNoRunContext):
        run_context()
    with pytest.raises(MsreviewNoRunContext):
        seal(make_draft())


def test_set_run_context_is_idempotent_but_refuses_a_different_context() -> None:
    ctx = make_ctx()
    set_run_context(ctx)
    set_run_context(ctx)
    assert run_context() is ctx
    with pytest.raises(RuntimeError, match="different run context"):
        set_run_context(make_ctx())
    clear_run_context()
    set_run_context(make_ctx())  # allowed after clearing


def test_repr_hides_secrets() -> None:
    ctx = make_ctx(read_token="readtoken-000000000")
    text = repr(ctx)
    for secret in (ctx.github_token, ctx.read_token, ctx.mistral_api_key, repr(ctx.seal_key)):
        assert secret is not None and secret not in text
    assert ctx.execution_id in text


def test_secrets_and_run_dir() -> None:
    ctx = make_ctx(read_token="readtoken-000000000")
    assert ctx.secrets() == [ctx.github_token, "readtoken-000000000", ctx.mistral_api_key]
    assert make_ctx(github_token=None).secrets() == [make_ctx().mistral_api_key]
    assert ctx.run_dir == Path("/tmp/msreview-runs") / ctx.execution_id


@pytest.mark.parametrize(
    "changes",
    [{"seal_key": b"short"}, {"execution_id": ""}, {"request_sha256": "abc"}],
)
def test_run_context_validation(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_ctx(**changes)


def test_run_context_is_frozen() -> None:
    ctx = make_ctx()
    with pytest.raises(AttributeError):
        ctx.on_worker = True  # type: ignore[misc]
    assert runctx.MIN_SEAL_KEY_BYTES == 32
