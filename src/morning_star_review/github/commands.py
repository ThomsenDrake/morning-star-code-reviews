"""``/msreview`` commands in pull request comments.

One command exists: ``/msreview dismiss <finding id> [<finding id> ...] [reason]``. It
dismisses findings as false positives (or as accepted), like resolving their threads,
and it works for every finding, also one without an inline comment. A command counts
only when its author may dismiss findings (``permissions.can_dismiss``: write access;
the PR author only while ``review.author_may_dismiss`` allows it). Our bot's own
comments never count, and neither do lines inside fenced code blocks or quotes.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from morning_star_review.core.contract import DismissCommand
from morning_star_review.github.permissions import can_dismiss
from morning_star_review.github.state import is_bot_comment

__all__ = [
    "DISMISS_HINT",
    "MAX_COMMANDS",
    "Commands",
    "DismissLine",
    "collect_commands",
    "parse_dismiss_lines",
]

DISMISS_HINT = "/msreview dismiss <id> <reason>"
MAX_COMMANDS = 100
_MAX_NOTES = 20
_REASON_MAX = 200

_COMMAND = re.compile(r"^/msreview[ \t]+dismiss\b(?P<rest>.*)$", re.IGNORECASE)
_ID = re.compile(r"`?(f[0-9a-f]{6,32})`?[,;:]?", re.IGNORECASE)
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_LEADING = " \t:;,.-–—"  # noqa: RUF001 - separators people type between the ids and the reason


@dataclass(frozen=True)
class DismissLine:
    """One ``/msreview dismiss`` line: the finding ids it names and the reason."""

    ids: tuple[str, ...]
    reason: str


def _command_lines(body: str) -> list[str]:
    """Lines outside fenced code blocks that start with ``/msreview``."""
    out: list[str] = []
    fence: str | None = None
    for raw in (body or "").splitlines():
        line = raw.strip()
        marker = _FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0] * 3
            elif line.startswith(fence):
                fence = None
            continue
        if fence is None and line.lower().startswith("/msreview"):
            out.append(line)
    return out


def parse_dismiss_lines(body: str) -> list[DismissLine]:
    """The ``dismiss`` commands in a comment body: finding ids and the reason."""
    found: list[DismissLine] = []
    for line in _command_lines(body):
        match = _COMMAND.match(line)
        if match is None:
            continue
        words = match.group("rest").split()
        ids: list[str] = []
        while words and (m := _ID.fullmatch(words[0])):
            ids.append(m.group(1).lower())
            words.pop(0)
        reason = " ".join(words).lstrip(_LEADING).strip()
        found.append(DismissLine(tuple(dict.fromkeys(ids)), reason[:_REASON_MAX]))
    return found


@dataclass
class Commands:
    """The authorised dismissals (first command per id wins) and why others were ignored."""

    dismissals: list[DismissCommand] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def _sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    return str(comment.get("created_at") or ""), int(comment.get("id") or 0)


async def collect_commands(
    comments: Sequence[Any],
    *,
    bot_login: str | None,
    pr_author: str,
    author_may_dismiss: bool,
    permission: Callable[[str], Awaitable[str]],
) -> Commands:
    """Dismiss commands from the PR's issue comments, checked against who wrote them.

    ``permission(login)`` returns the commenter's role on the repository.
    """
    out = Commands()
    seen: set[str] = set()
    who = "people with write access"
    if not author_may_dismiss:
        who += " other than the PR author"
    ordered = sorted((c for c in comments if isinstance(c, dict)), key=_sort_key)
    for comment in ordered:
        if is_bot_comment(comment, bot_login):
            continue
        lines = parse_dismiss_lines(str(comment.get("body") or ""))
        if not lines:
            continue
        user = comment.get("user")
        login = str(user.get("login") or "") if isinstance(user, dict) else ""
        comment_id = int(comment.get("id") or 0)
        if not login or comment_id < 1:
            continue
        if not can_dismiss(await permission(login), login, pr_author, author_may_dismiss):
            out.ignored.append(
                f"a dismiss command by {login} was ignored: only {who} can dismiss findings"
            )
            continue
        for line in lines:
            if not line.ids:
                out.ignored.append(f"a dismiss command by {login} names no finding id")
            for fid in line.ids:
                if fid in seen or len(out.dismissals) >= MAX_COMMANDS:
                    continue
                seen.add(fid)
                out.dismissals.append(
                    DismissCommand(
                        id=fid, by=login[:100], reason=line.reason, comment_id=comment_id
                    )
                )
    out.ignored = list(dict.fromkeys(out.ignored))[:_MAX_NOTES]
    return out
