"""Hidden HTML markers in our comments, and the review state marker.

Markers are only trusted on comments authored by our bot: anyone can type the same
HTML comment into a PR. Model text can never contain a marker because the
sanitiser strips ``<!--``, ``-->`` and ``morning-star-review:``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import zlib
from typing import Any, cast

from pydantic import ValidationError

from morning_star_review.core.contract import PriorFinding, PriorState

__all__ = [
    "MAX_DECODED_STATE_BYTES",
    "MAX_STATE_MARKER_CHARS",
    "SUMMARY_MARKER",
    "decode_state",
    "encode_state",
    "failure_marker",
    "find_summary_comment",
    "finding_ids_in",
    "finding_marker",
    "is_bot_comment",
    "is_summary_body",
    "reply_ids_in",
    "reply_marker",
    "same_login",
    "strip_state",
]

log = logging.getLogger(__name__)

_PREFIX = "morning-star-review"
SUMMARY_MARKER = f"<!-- {_PREFIX}:summary v1 -->"
_STATE_OPEN = f"<!-- {_PREFIX}:state "
_STATE_CLOSE = " -->"
MAX_STATE_MARKER_CHARS = 20_000  # the whole marker, as posted
MAX_DECODED_STATE_BYTES = 256 * 1024
_MAX_ENCODED_CHARS = 64 * 1024  # anything longer is not ours

_STATE_RE = re.compile(
    re.escape(_STATE_OPEN) + r"([A-Za-z0-9_=-]{1,65536})" + re.escape(_STATE_CLOSE)
)
_FINDING_ID = r"f[0-9a-f]{6,32}"  # "f" + 12 hex today; the parser is the authority
_FINDING_RE = re.compile(rf"<!-- {_PREFIX}:finding ({_FINDING_ID}) -->")
_REPLY_RE = re.compile(rf"<!-- {_PREFIX}:resolved ([A-Za-z0-9_-]{{1,40}}) -->")
_ID_OK = re.compile(r"[A-Za-z0-9_-]{1,40}")


def finding_marker(finding_id: str) -> str:
    """Marker that identifies the inline comment of ``finding_id``.

    Only ids that ``finding_ids_in`` reads back are accepted: a marker we could not
    recognise later would make a retried publish post the comment again.
    """
    if not re.fullmatch(_FINDING_ID, finding_id):
        raise ValueError("invalid finding id")
    return f"<!-- {_PREFIX}:finding {finding_id} -->"


def reply_marker(finding_id: str) -> str:
    """Marker on our "looks addressed" reply, so a retried publish does not reply twice."""
    if not _ID_OK.fullmatch(finding_id):
        raise ValueError("invalid finding id")
    return f"<!-- {_PREFIX}:resolved {finding_id} -->"


def failure_marker() -> str:
    """Marker on the failure banner that ``write_failure_header`` maintains."""
    return f"<!-- {_PREFIX}:failure -->"


def finding_ids_in(body: str) -> list[str]:
    """Finding ids named by finding markers in ``body`` (in order, unique)."""
    out: list[str] = []
    for match in _FINDING_RE.finditer(body or ""):
        if match.group(1) not in out:
            out.append(match.group(1))
    return out


def reply_ids_in(body: str) -> list[str]:
    """Finding ids named by "looks addressed" reply markers in ``body``."""
    return list(dict.fromkeys(m.group(1) for m in _REPLY_RE.finditer(body or "")))


def is_summary_body(body: str | None) -> bool:
    """True when ``body`` starts with the summary marker (leading whitespace allowed)."""
    return body is not None and body.lstrip().startswith(SUMMARY_MARKER)


def same_login(login: str | None, bot_login: str | None) -> bool:
    """Compare logins ignoring case and the ``[bot]`` suffix.

    REST reports an App as ``<slug>[bot]`` while GraphQL ``author.login`` reports
    ``<slug>``; both must match our bot.
    """
    if not login or not bot_login:
        return False

    def bare(value: str) -> str:
        value = value.strip().lower()
        return value[: -len("[bot]")] if value.endswith("[bot]") else value

    return bare(login) == bare(bot_login)


def is_bot_comment(comment: Any, bot_login: str | None) -> bool:
    """A REST comment authored by our bot (exact ``<slug>[bot]`` login)."""
    if not bot_login or not isinstance(comment, dict):
        return False
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return isinstance(login, str) and login.lower() == bot_login.lower()


def find_summary_comment(comments: list[Any], bot_login: str | None) -> dict[str, Any] | None:
    """Our summary comment: bot-authored and starting with the summary marker.

    If several exist (a crash between two writes), the most recently updated wins.
    """
    ours = [c for c in comments if is_bot_comment(c, bot_login) and is_summary_body(c.get("body"))]
    if not ours:
        return None
    return cast(
        dict[str, Any],
        max(ours, key=lambda c: (str(c.get("updated_at") or ""), int(c.get("id") or 0))),
    )


def _pack(state: PriorState) -> str:
    raw = json.dumps(state.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    packed = base64.urlsafe_b64encode(zlib.compress(raw.encode("utf-8"), 9)).decode("ascii")
    return f"{_STATE_OPEN}{packed}{_STATE_CLOSE}"


def _with_findings(state: PriorState, findings: list[PriorFinding]) -> PriorState:
    data = state.model_dump(mode="json")
    data["findings"] = [f.model_dump(mode="json") for f in findings]
    return PriorState.model_validate(data)


def encode_state(state: PriorState) -> str:
    """The state marker for ``state``: at most ``MAX_STATE_MARKER_CHARS`` characters.

    When too large, the oldest findings that are no longer open are dropped first,
    then open findings from the end, until it fits.
    """
    marker = _pack(state)
    if len(marker) <= MAX_STATE_MARKER_CHARS:
        return marker
    findings = list(state.findings)
    while findings:
        closed = [i for i, f in enumerate(findings) if f.state != "open"]
        findings.pop(closed[0] if closed else len(findings) - 1)
        marker = _pack(_with_findings(state, findings))
        if len(marker) <= MAX_STATE_MARKER_CHARS:
            log.warning(
                "review state too large; dropped %d finding(s)", len(state.findings) - len(findings)
            )
            return marker
    return _pack(_with_findings(state, []))


def _last_state_match(comment_body: str) -> re.Match[str] | None:
    matches = list(_STATE_RE.finditer(comment_body))
    return matches[-1] if matches else None


def decode_state(comment_body: str) -> PriorState | None:
    """The state in the LAST state marker of ``comment_body``, or ``None`` on any problem.

    ``publish`` always appends our marker at the very end of the summary, so only the
    last marker is trusted: an earlier one (text that slipped into the body above our
    own marker) is never read. The decompressed size is capped at
    ``MAX_DECODED_STATE_BYTES``.
    """
    if not comment_body or len(comment_body) > 2 * 1024 * 1024:
        return None
    match = _last_state_match(comment_body)
    if match is None or len(match.group(1)) > _MAX_ENCODED_CHARS:
        return None
    try:
        compressed = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_DECODED_STATE_BYTES)
        if inflater.unconsumed_tail or not inflater.eof:
            log.warning("ignoring an oversized or truncated review state marker")
            return None
        data = json.loads(raw.decode("utf-8"))
        return PriorState.model_validate(data)
    except (binascii.Error, ValueError, zlib.error, ValidationError, UnicodeDecodeError):
        log.warning("ignoring an unreadable review state marker")
        return None


def strip_state(body: str) -> str:
    """``body`` without its state marker (and without trailing whitespace)."""
    return _STATE_RE.sub("", body or "").rstrip()
