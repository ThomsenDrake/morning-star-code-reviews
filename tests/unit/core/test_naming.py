"""PR URL parsing, execution ids and deployment names."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from morning_star_review.core import naming as n

EXEC_ID = re.compile(r"^[a-z0-9-]+$")
DEPLOYMENT = re.compile(r"^[A-Za-z0-9._-]+$")
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/Octo-Org/my.repo/pull/42",
        "http://github.com/Octo-Org/my.repo/pull/42",
        "https://github.com/Octo-Org/my.repo/pull/42/",
        "https://github.com/Octo-Org/my.repo/pull/42/files",
        "https://github.com/Octo-Org/my.repo/pull/42/commits/abc",
        "https://github.com/Octo-Org/my.repo/pull/42?diff=split",
        "https://github.com/Octo-Org/my.repo/pull/42#discussion_r1",
        "https://www.github.com/Octo-Org/my.repo/pull/42",
        "github.com/Octo-Org/my.repo/pull/42",
        "  https://github.com/Octo-Org/my.repo/pull/42\n",
        "https://api.github.com/repos/Octo-Org/my.repo/pulls/42",
        "Octo-Org/my.repo#42",
        "Octo-Org/my.repo/pull/42",
        "HTTPS://GITHUB.COM/Octo-Org/my.repo/pull/42",
    ],
)
def test_parse_variants(text: str) -> None:
    ref = n.parse_pr_url(text)
    assert (ref.owner, ref.repo, ref.number) == ("Octo-Org", "my.repo", 42)
    assert ref.canonical_url == "https://github.com/Octo-Org/my.repo/pull/42"
    assert ref.full_name == "Octo-Org/my.repo"


def test_parse_strips_git_suffix() -> None:
    assert n.parse_pr_url("o/r.git#1").repo == "r"


def test_parse_accepts_managed_user_owner() -> None:
    # Enterprise managed users have logins like "handle_shortcode".
    assert n.parse_pr_url("octo_acme/r#3").owner == "octo_acme"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://gitlab.com/o/r/pull/1",
        "https://github.com.evil.example/o/r/pull/1",
        "https://user:pw@github.com/o/r/pull/1",
        "https://github.com:8443/o/r/pull/1",
        "ftp://github.com/o/r/pull/1",
        "https://github.com/o/r/issues/1",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pull/abc",
        "https://github.com/o/r/pull/-1",
        "https://github.com/o/r",
        "https://github.com/-bad/r/pull/1",
        "https://github.com/o/../pull/1",
        "https://github.com/o.o/r/pull/1",
        "https://api.github.com/repos/o/r/issues/1",
        "o/r#x",
        "o#1",
        "https://github.com/o/r/pull/12345678901",
        "x" * 600,
    ],
)
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        n.parse_pr_url(text)


def test_slug() -> None:
    assert n.slug("Octo-Org/My.Repo__x") == "octo-org-my-repo-x"
    assert n.slug("--A--") == "a"


def test_execution_id_shape() -> None:
    ref = n.parse_pr_url("Octo-Org/My.Repo#7")
    eid = n.execution_id("ci", ref, SHA.upper(), "1a2b3c4d")
    assert eid == "msreview-ci-octo-org-my-repo-pr7-0123456-1a2b3c4d"
    assert EXEC_ID.match(eid)
    assert eid.startswith(n.execution_id_prefix("ci", ref))
    assert n.execution_id("cli", ref, None, "ff").endswith("-pr7-latest-ff")


def test_execution_id_prefix_does_not_match_other_prs() -> None:
    ref12 = n.parse_pr_url("o/r#12")
    ref123 = n.parse_pr_url("o/r#123")
    eid = n.execution_id("ci", ref123, SHA, "aa")
    assert not eid.startswith(n.execution_id_prefix("ci", ref12))


@pytest.mark.parametrize("rand", ["", "ABC", "a-b", "a" * 33, "zz zz"])
def test_execution_id_rejects_bad_rand(rand: str) -> None:
    with pytest.raises(ValueError):
        n.execution_id("ci", n.parse_pr_url("o/r#1"), SHA, rand)


def test_execution_id_rejects_bad_sha_and_mode() -> None:
    ref = n.parse_pr_url("o/r#1")
    with pytest.raises(ValueError):
        n.execution_id("ci", ref, "not-a-sha", "aa")
    with pytest.raises(ValueError):
        n.execution_id("prod", ref, SHA, "aa")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        n.deployment_name("prod", ref)  # type: ignore[arg-type]


def test_deployment_name_shape_and_stability() -> None:
    ref = n.parse_pr_url("Octo-Org/My.Repo#7")
    name = n.deployment_name("ci", ref)
    assert re.fullmatch(r"msreview-ci-octo-org-my-repo-[0-9a-f]{6}-pr7", name)
    # GitHub names are case-insensitive: same deployment for any casing.
    assert n.deployment_name("ci", n.parse_pr_url("octo-org/my.repo#7")) == name
    assert n.deployment_name("cli", ref) != name
    assert n.deployment_name("ci", ref, suffix="x1f2a") == name + "-x1f2a"
    with pytest.raises(ValueError):
        n.deployment_name("ci", ref, suffix="X!")


def test_deployment_names_differ_when_slugs_collide() -> None:
    a = n.parse_pr_url("a-b/c#1")
    b = n.parse_pr_url("a/b-c#1")
    assert n.slug(f"{a.owner}-{a.repo}") == n.slug(f"{b.owner}-{b.repo}")
    assert n.deployment_name("ci", a) != n.deployment_name("ci", b)


def test_longest_names_fit_limits() -> None:
    ref = n.parse_pr_url(f"{'o' * 39}/{'r' * 100}#9999999999")
    eid = n.execution_id("local", ref, SHA, "f" * 32)
    dep = n.deployment_name("local", ref, suffix="x" * 32)
    assert EXEC_ID.match(eid) and len(eid) <= 128
    assert DEPLOYMENT.match(dep) and len(dep) <= 128


def test_workflow_name() -> None:
    assert n.WORKFLOW_NAME == "morning-star-code-review"


_owner = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", fullmatch=True)
_repo = st.from_regex(r"[A-Za-z0-9._-]{1,100}", fullmatch=True).filter(
    lambda r: r not in {".", ".."} and not r.endswith(".git")
)


@given(
    owner=_owner,
    repo=_repo,
    number=st.integers(1, 10**9),
    rand=st.from_regex(r"[a-z0-9]{8}", fullmatch=True),
)
def test_names_charset_property(owner: str, repo: str, number: int, rand: str) -> None:
    ref = n.parse_pr_url(f"https://github.com/{owner}/{repo}/pull/{number}")
    assert (ref.owner, ref.repo, ref.number) == (owner, repo, number)
    for mode in ("ci", "cli", "local"):
        eid = n.execution_id(mode, ref, SHA, rand)
        dep = n.deployment_name(mode, ref)
        assert EXEC_ID.match(eid) and len(eid) <= 128
        assert DEPLOYMENT.match(dep) and len(dep) <= 128
        assert "--" not in dep and "--" not in eid
