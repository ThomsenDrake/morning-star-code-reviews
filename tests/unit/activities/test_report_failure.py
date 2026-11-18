"""The ``failed`` outcome names the stage and the error type once."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "github"))

from builders import EXEC_ID, make_req  # noqa: E402

from morning_star_review.activities.report_failure import (  # noqa: E402
    failed_outcome,
    failure_reason,
)
from morning_star_review.core.contract import FailureInput  # noqa: E402
from morning_star_review.core.errors import (  # noqa: E402
    MsreviewBadRequest,
    failure_text,
    failure_type,
)

NO_MODEL = "the operator's allowed models leave no usable model"


@pytest.mark.parametrize(
    ("error_type", "error_text", "reason"),
    [
        # failure_text already starts with the type: it is not repeated.
        (
            "MsreviewBadRequest",
            f"MsreviewBadRequest: {NO_MODEL}",
            f"fetch_pr failed: MsreviewBadRequest: {NO_MODEL}",
        ),
        ("MsreviewBadRequest", "MsreviewBadRequest", "fetch_pr failed: MsreviewBadRequest"),
        # A text without the type (the driver's deadline backstop) gets it in front.
        (
            "MsreviewDeadline",
            "the run reached its hard deadline",
            "fetch_pr failed: MsreviewDeadline: the run reached its hard deadline",
        ),
        ("MsreviewDeadline", "", "fetch_pr failed: MsreviewDeadline"),
        # Another type that only starts like the declared one is not mistaken for it.
        ("E", "Error: boom", "fetch_pr failed: E: Error: boom"),
    ],
)
def test_failure_reason_names_the_type_once(error_type: str, error_text: str, reason: str) -> None:
    assert failure_reason("fetch_pr", error_type, error_text) == reason


def test_failed_outcome_of_a_real_error_does_not_repeat_its_type() -> None:
    exc = MsreviewBadRequest(NO_MODEL)
    inp = FailureInput(
        req=make_req(mode="ci"),
        snap=None,
        stage="fetch_pr",
        error_type=failure_type(exc),
        error_text=failure_text(exc),
    )
    outcome = failed_outcome(inp, None, EXEC_ID, [])
    assert outcome.status == "failed"
    assert outcome.reason == f"fetch_pr failed: MsreviewBadRequest: {NO_MODEL}"
    assert (outcome.reason or "").count("MsreviewBadRequest") == 1
