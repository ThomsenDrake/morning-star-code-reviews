"""Error classes and the failure helpers used by the workflow."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from morning_star_review.core import errors
from morning_star_review.core.errors import (
    MsreviewBadRequest,
    MsreviewDigestMismatch,
    MsreviewError,
    MsreviewGitError,
    MsreviewGitHubLasting,
    MsreviewNoRunContext,
    MsreviewPinRejected,
    MsreviewRunDirMissing,
    MsreviewSealInvalid,
    MsreviewSecretCommandError,
    failure_text,
    failure_type,
    for_activity,
    non_retryable,
)


def _activity_error(cause: BaseException) -> ActivityError:
    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=5,
        started_event_id=6,
        identity="worker",
        activity_type="msreview_explore",
        activity_id="1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    err.__cause__ = cause
    return err


@pytest.mark.parametrize(
    ("cls", "retryable"),
    [
        (MsreviewPinRejected, False),
        (MsreviewSealInvalid, False),
        (MsreviewRunDirMissing, False),
        (MsreviewDigestMismatch, False),
        (MsreviewBadRequest, False),
        (MsreviewGitHubLasting, False),
        (MsreviewNoRunContext, False),
        (MsreviewSecretCommandError, False),
        (MsreviewGitError, True),
    ],
)
def test_retryability(cls: type[MsreviewError], retryable: bool) -> None:
    assert issubclass(cls, MsreviewError)
    assert cls.non_retryable is not retryable


def test_failure_type_of_raw_exceptions() -> None:
    assert failure_type(MsreviewSealInvalid("x")) == "MsreviewSealInvalid"
    assert failure_type(ValueError("x")) == "ValueError"


def test_failure_type_walks_activity_error_to_application_error() -> None:
    app = ApplicationError("pin", type="MsreviewPinRejected", non_retryable=True)
    assert failure_type(_activity_error(app)) == "MsreviewPinRejected"


def test_failure_type_uses_class_name_when_type_is_not_a_string() -> None:
    class OddError(Exception):
        type = 3

    assert failure_type(_activity_error(OddError("x"))) == "OddError"


def test_failure_text_joins_the_chain_and_skips_wrappers() -> None:
    app = ApplicationError("git fetch failed: timeout", type="MsreviewGitError")
    text = failure_text(_activity_error(app))
    assert text == "MsreviewGitError: git fetch failed: timeout"


def test_failure_text_follows_explicit_causes() -> None:
    try:
        try:
            raise OSError("disk full")
        except OSError as inner:
            raise MsreviewRunDirMissing("run dir gone") from inner
    except MsreviewRunDirMissing as exc:
        caught = exc
    assert failure_text(caught) == "MsreviewRunDirMissing: run dir gone <- OSError: disk full"


def test_failure_text_is_redacted_and_capped() -> None:
    token = "ghs_" + "x" * 36
    exc = RuntimeError(f"bad token {token} " + "y" * 5000)
    text = failure_text(exc, extra_secrets=["yyyyyyyy" * 10])
    assert token not in text
    assert len(text) <= errors.FAILURE_TEXT_LIMIT
    assert len(failure_text(exc, limit=50)) == 50


def test_failure_text_survives_cycles() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert failure_text(a) == "RuntimeError: a <- RuntimeError: b"


def test_failure_text_of_empty_message() -> None:
    assert failure_text(MsreviewBadRequest()) == "MsreviewBadRequest"


def test_non_retryable_builds_application_error() -> None:
    err = non_retryable(MsreviewPinRejected, "foreign execution")
    assert isinstance(err, ApplicationError)
    assert err.type == "MsreviewPinRejected"
    assert err.non_retryable
    assert err.message == "foreign execution"
    assert non_retryable("Custom", "m").type == "Custom"


def test_non_retryable_redacts_message() -> None:
    token = "ghs_" + "z" * 36
    err = non_retryable("X", f"leaked {token}")
    assert token not in str(err)


def test_for_activity_outside_an_activity_returns_the_exception() -> None:
    exc = MsreviewSealInvalid("x")
    assert for_activity(exc) is exc
    other = ValueError("y")
    assert for_activity(other) is other


def test_for_activity_inside_an_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    from temporalio import activity

    monkeypatch.setattr(activity, "in_activity", lambda: True)
    exc = MsreviewSealInvalid("tampered")
    converted = for_activity(exc)
    assert isinstance(converted, ApplicationError)
    assert converted.type == "MsreviewSealInvalid" and converted.non_retryable
    assert converted.__cause__ is exc
    retryable = MsreviewGitError("transient")
    assert for_activity(retryable) is retryable
