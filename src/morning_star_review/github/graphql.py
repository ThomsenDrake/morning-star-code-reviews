"""GraphQL queries and mutations for review threads.

REST has no notion of review threads or their resolution; GraphQL does. Thread
first comments carry ``databaseId`` (the REST comment id) so the two worlds join.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from morning_star_review.github.http import GitHubGraphQLError, GitHubHttp
from morning_star_review.github.state import same_login

__all__ = [
    "MINIMIZE_MUTATION",
    "RESOLVE_MUTATION",
    "THREADS_QUERY",
    "UNRESOLVE_MUTATION",
    "ReviewThread",
    "list_review_threads",
    "minimize_comment",
    "resolve_thread",
    "unresolve_thread",
]

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          resolvedBy { login }
          comments(first: 1) { nodes { databaseId author { __typename login } body } }
        }
      }
    }
  }
}
"""

RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}
"""

UNRESOLVE_MUTATION = """
mutation($threadId: ID!) {
  unresolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}
"""

MINIMIZE_MUTATION = """
mutation($subjectId: ID!, $classifier: ReportedContentClassifiers!) {
  minimizeComment(input: {subjectId: $subjectId, classifier: $classifier}) {
    minimizedComment { isMinimized minimizedReason }
  }
}
"""


@dataclass(frozen=True)
class ReviewThread:
    id: str
    is_resolved: bool
    is_outdated: bool
    path: str
    line: int | None
    resolved_by: str | None
    first_comment_id: int | None
    first_author: str | None
    first_body: str
    first_author_type: str | None = None  # GraphQL __typename: "Bot", "User", ...

    def started_by(self, bot_login: str | None) -> bool:
        """True when the first comment is by our bot (an App shows as ``Bot`` ``<slug>``)."""
        return self.first_author_type == "Bot" and same_login(self.first_author, bot_login)


def _thread(node: Any) -> ReviewThread | None:
    if not isinstance(node, dict) or not isinstance(node.get("id"), str):
        return None
    comments = ((node.get("comments") or {}).get("nodes")) or []
    first = comments[0] if comments and isinstance(comments[0], dict) else {}
    author = first.get("author") if isinstance(first.get("author"), dict) else None
    resolver = node.get("resolvedBy") if isinstance(node.get("resolvedBy"), dict) else None
    line = node.get("line")
    database_id = first.get("databaseId")
    return ReviewThread(
        id=node["id"],
        is_resolved=bool(node.get("isResolved")),
        is_outdated=bool(node.get("isOutdated")),
        path=str(node.get("path") or ""),
        line=line if isinstance(line, int) else None,
        resolved_by=str(resolver["login"]) if resolver and resolver.get("login") else None,
        first_comment_id=database_id if isinstance(database_id, int) else None,
        first_author=str(author["login"]) if author and author.get("login") else None,
        first_body=str(first.get("body") or ""),
        first_author_type=str(author["__typename"])
        if author and author.get("__typename")
        else None,
    )


async def list_review_threads(
    gh: GitHubHttp, owner: str, repo: str, number: int, *, max_pages: int = 10
) -> list[ReviewThread]:
    """All review threads of a PR (100 per page, at most ``max_pages`` pages)."""
    threads: list[ReviewThread] = []
    cursor: str | None = None
    for _ in range(max_pages):
        data = await gh.graphql(
            THREADS_QUERY, {"owner": owner, "repo": repo, "number": number, "cursor": cursor}
        )
        pr = ((data.get("repository") or {}).get("pullRequest")) or {}
        conn = pr.get("reviewThreads") or {}
        for node in conn.get("nodes") or []:
            thread = _thread(node)
            if thread is not None:
                threads.append(thread)
        info = conn.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if not info.get("hasNextPage") or not cursor:
            break
    return threads


async def resolve_thread(gh: GitHubHttp, thread_id: str) -> bool:
    """Resolve a thread. Returns False when GitHub refuses (``FORBIDDEN``); other
    GraphQL errors propagate."""
    try:
        await gh.graphql(RESOLVE_MUTATION, {"threadId": thread_id})
    except GitHubGraphQLError as exc:
        if exc.forbidden:
            return False
        raise
    return True


async def unresolve_thread(gh: GitHubHttp, thread_id: str) -> bool:
    """Unresolve a thread (same error handling as ``resolve_thread``)."""
    try:
        await gh.graphql(UNRESOLVE_MUTATION, {"threadId": thread_id})
    except GitHubGraphQLError as exc:
        if exc.forbidden:
            return False
        raise
    return True


async def minimize_comment(gh: GitHubHttp, subject_id: str, classifier: str = "RESOLVED") -> bool:
    """Hide a comment or review (node id) with ``classifier``; True when minimized."""
    data = await gh.graphql(MINIMIZE_MUTATION, {"subjectId": subject_id, "classifier": classifier})
    minimized = ((data.get("minimizeComment") or {}).get("minimizedComment")) or {}
    return bool(minimized.get("isMinimized"))
