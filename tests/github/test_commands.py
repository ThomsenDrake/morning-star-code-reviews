"""``/msreview dismiss`` commands: parsing, and who may dismiss."""

from __future__ import annotations

from typing import Any

import pytest
from fakegh import BOT

from morning_star_review.github.commands import collect_commands, parse_dismiss_lines

A, B = "f0123456789ab", "f00000000000b"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"/msreview dismiss {A} intended: by design", [((A,), "intended: by design")]),
        (f"/msreview dismiss `{A}` - not a bug", [((A,), "not a bug")]),
        (f"/msreview dismiss {A}: false positive", [((A,), "false positive")]),
        (f"/msreview dismiss {A}, {B} both fine", [((A, B), "both fine")]),
        (f"/msreview dismiss {A} {A}", [((A,), "")]),
        (f"/MSReview Dismiss {A.upper()}", [((A,), "")]),
        (f"  /msreview dismiss {A} — by design", [((A,), "by design")]),
        ("/msreview dismiss because I said so", [((), "because I said so")]),
        (f"Thanks!\n/msreview dismiss {A} ok\nmore text\n/msreview dismiss {B}", [
            ((A,), "ok"),
            ((B,), ""),
        ]),
        (f"please run /msreview dismiss {A}", []),  # not at the start of a line
        (f"> /msreview dismiss {A} quoted", []),  # a quote of someone else's command
        (f"```\n/msreview dismiss {A} in a code block\n```", []),
        (f"~~~text\n/msreview dismiss {A}\n~~~\n/msreview dismiss {B}", [((B,), "")]),
        (f"/msreview review {A}", []),  # no other command exists
        (f"/msreview dismissal {A}", []),
    ],
)  # fmt: skip
def test_parse_dismiss_lines(body: str, expected: list[tuple[tuple[str, ...], str]]) -> None:
    assert [(line.ids, line.reason) for line in parse_dismiss_lines(body)] == expected


def test_reason_is_capped() -> None:
    [line] = parse_dismiss_lines(f"/msreview dismiss {A} " + "x" * 500)
    assert len(line.reason) == 200


def comment(cid: int, body: str, login: str, created: str = "2026-11-24T10:00:00Z") -> Any:
    return {"id": cid, "body": body, "user": {"login": login}, "created_at": created}


ROLES = {"maintainer": "admin", "author": "write", "reader": "read", "outsider": "none"}


async def collect(comments: list[Any], *, author_may_dismiss: bool = True) -> Any:
    async def permission(login: str) -> str:
        return ROLES.get(login, "none")

    return await collect_commands(
        comments,
        bot_login=BOT,
        pr_author="author",
        author_may_dismiss=author_may_dismiss,
        permission=permission,
    )


async def test_only_people_who_may_dismiss_count() -> None:
    got = await collect(
        [
            comment(3, f"/msreview dismiss {A} later", "maintainer", "2026-11-24T12:00:00Z"),
            comment(1, f"/msreview dismiss {A} first", "author", "2026-11-24T11:00:00Z"),
            comment(2, f"/msreview dismiss {B} drive-by", "reader"),
            comment(4, f"/msreview dismiss {B} bot", BOT),  # our own text never counts
            comment(5, "/msreview dismiss no id here", "maintainer", "2026-11-24T13:00:00Z"),
            comment(6, "just a comment", "outsider"),
        ]
    )
    # The oldest command per finding wins; the author has write access.
    assert [(c.id, c.by, c.reason, c.comment_id) for c in got.dismissals] == [
        (A, "author", "first", 1)
    ]
    assert got.ignored == [
        "a dismiss command by reader was ignored: only people with write access can dismiss "
        "findings",
        "a dismiss command by maintainer names no finding id",
    ]


async def test_author_may_not_dismiss_when_the_config_says_so() -> None:
    got = await collect(
        [
            comment(1, f"/msreview dismiss {A}", "author"),
            comment(2, f"/msreview dismiss {B}", "maintainer"),
        ],
        author_may_dismiss=False,
    )
    assert [c.id for c in got.dismissals] == [B]
    assert got.ignored == [
        "a dismiss command by author was ignored: only people with write access other than "
        "the PR author can dismiss findings"
    ]
