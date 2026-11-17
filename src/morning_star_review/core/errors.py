"""Exception classes and pure helpers that describe failures.

Our errors are plain Python exceptions so that ``core`` never imports temporalio at
module level. Inside a Workflows activity, ``for_activity`` turns a non-retryable
error into ``temporalio.exceptions.ApplicationError(..., non_retryable=True)``; in local
in-process mode the raw exception propagates (DESIGN 5.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from morning_star_review.core.redact import redact

if TYPE_CHECKING:  # core must not import temporalio at module level
    from temporalio.exceptions import ApplicationError

__all__ = [
    "FAILURE_TEXT_LIMIT",
    "MsreviewBadRequest",
    "MsreviewDigestMismatch",
    "MsreviewError",
    "MsreviewForeignTask",
    "MsreviewGitError",
    "MsreviewGitHubLasting",
    "MsreviewNoRunContext",
    "MsreviewPinRejected",
    "MsreviewRunDirMissing",
    "MsreviewSealInvalid",
    "MsreviewSecretCommandError",
    "MsreviewTooLarge",
    "failure_text",
    "failure_type",
    "for_activity",
    "non_retryable",
]

FAILURE_TEXT_LIMIT = 2000  # FailureInput.error_text bound
_TYPE_LIMIT = 200
_MAX_CHAIN = 8

# Temporal wrappers whose own message says nothing about the root cause.
_WRAPPER_NAMES = frozenset({"ActivityError", "ChildWorkflowError", "WorkflowFailureError"})


class MsreviewError(Exception):
    """Base class. ``non_retryable`` decides how an activity reports it."""

    non_retryable: ClassVar[bool] = False


class MsreviewPinRejected(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """The task does not belong to this run (foreign execution id or request hash)."""

    non_retryable = True


class MsreviewForeignTask(MsreviewPinRejected):
    """The task belongs to another execution served from the same task queue.

    Retryable: two runs for one PR can briefly share a deployment (both chose it before
    either worker registered), and Temporal hands a task to either worker. Rejected
    here, it is retried, and the other run's worker can take it (security review PL-1).
    """

    non_retryable = False


class MsreviewTooLarge(MsreviewError):  # noqa: N818 - shown as the failure type
    """The pull request's file list would not fit in a workflow payload (~2 MB)."""

    non_retryable = True


class MsreviewSealInvalid(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """A sealed payload was not sealed by this process, or was changed after sealing."""

    non_retryable = True


class MsreviewRunDirMissing(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """The run directory or a file in it is gone (task served by another process)."""

    non_retryable = True


class MsreviewDigestMismatch(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """A run-directory file does not match the digest carried in the payload."""

    non_retryable = True


class MsreviewBadRequest(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """Invalid request: bad PR URL, refused model, inconsistent options."""

    non_retryable = True


class MsreviewGitHubLasting(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """A GitHub 4xx that retrying will not fix (not 408/409/425/429, not rate limiting)."""

    non_retryable = True


class MsreviewNoRunContext(MsreviewError):  # noqa: N818 - DESIGN name; shown as the failure type
    """``run_context()`` was called before the driver set one."""

    non_retryable = True


class MsreviewGitError(MsreviewError):
    """A git command failed (retryable: network and lock problems are transient)."""


class MsreviewSecretCommandError(MsreviewError):
    """A key command (``*_command`` setting) failed or printed nothing."""

    non_retryable = True


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` followed by its causes, outermost first, without cycles.

    Temporal failures expose the cause as ``.cause``; plain exceptions use
    ``__cause__`` (explicit ``raise ... from``) or ``__context__``.
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(out) < _MAX_CHAIN:
        out.append(cur)
        seen.add(id(cur))
        nxt = getattr(cur, "cause", None)
        if not isinstance(nxt, BaseException):
            nxt = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
        cur = nxt
    return out


def _type_of(exc: BaseException) -> str:
    # ApplicationError carries the original error class name in ``.type``; other
    # Temporal errors use ``.type`` for enums (TimeoutType), so only strings count.
    declared = getattr(exc, "type", None)
    if isinstance(declared, str) and declared:
        return declared
    return type(exc).__name__


def failure_type(exc: BaseException) -> str:
    """Name of the error that caused the failure.

    Skips Temporal wrapper errors (``ActivityError`` and friends) and returns the
    ``ApplicationError.type`` of the cause when there is one, else the class name.
    """
    for link in _chain(exc):
        if type(link).__name__ in _WRAPPER_NAMES:
            continue
        return _cap(_type_of(link), _TYPE_LIMIT)
    return _cap(_type_of(exc), _TYPE_LIMIT)


def _message_of(exc: BaseException) -> str:
    message = getattr(exc, "message", None)  # Temporal FailureError
    if isinstance(message, str):
        return message
    return str(exc)


def failure_text(
    exc: BaseException,
    *,
    limit: int = FAILURE_TEXT_LIMIT,
    extra_secrets: list[str] | None = None,
) -> str:
    """One redacted, capped line describing ``exc`` and its causes."""
    parts: list[str] = []
    for link in _chain(exc):
        if type(link).__name__ in _WRAPPER_NAMES:
            continue
        message = " ".join(_message_of(link).split())
        part = f"{_type_of(link)}: {message}" if message else _type_of(link)
        if part not in parts:
            parts.append(part)
    if not parts:
        parts.append(_type_of(exc))
    text = redact(" <- ".join(parts), extra_secrets or [])
    return _cap(text, limit)


def non_retryable(exc_type: str | type[BaseException], msg: str) -> ApplicationError:
    """A Temporal ``ApplicationError`` with ``non_retryable=True`` (raise it).

    ``msg`` is redacted (pattern-based) and capped. Imports temporalio lazily.
    """
    from temporalio.exceptions import ApplicationError

    name = exc_type if isinstance(exc_type, str) else exc_type.__name__
    return ApplicationError(
        _cap(redact(msg, []), FAILURE_TEXT_LIMIT), type=name, non_retryable=True
    )


def for_activity(exc: BaseException) -> BaseException:
    """The exception an activity body should raise for ``exc``.

    Inside a Temporal activity, a non-retryable ``MsreviewError`` becomes a
    non-retryable ``ApplicationError`` whose ``type`` is the class name. Everything
    else (local mode, retryable errors, foreign exceptions) is returned unchanged.
    """
    if not isinstance(exc, MsreviewError) or not exc.non_retryable:
        return exc
    try:
        from temporalio import activity
    except ImportError:  # pragma: no cover - temporalio is a worker dependency
        return exc
    if not activity.in_activity():
        return exc
    converted = non_retryable(type(exc), str(exc))
    converted.__cause__ = exc
    return converted
