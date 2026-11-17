"""A small stateful fake of the GitHub REST and GraphQL APIs for publish/fetch tests.

It keeps PR comments, reviews, threads and labels in memory and records every
request, so tests can assert on the resulting GitHub state and on which writes were
made. Names and ids are synthetic.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "github"
OWNER, REPO, NUMBER = "octo-org", "widgets", 7
BOT = "msr-test-app[bot]"
HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40

Rule = Callable[[httpx.Request], httpx.Response | None]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeGitHub:
    owner: str = OWNER
    repo: str = REPO
    number: int = NUMBER
    pr: dict[str, Any] = field(default_factory=lambda: load_fixture("pull.json"))
    files: list[dict[str, Any]] = field(default_factory=lambda: load_fixture("files.json"))
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    review_comments: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    # Comments of PENDING reviews by review id; visible only once the review is submitted.
    pending: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    repo_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    pr_labels: list[str] = field(default_factory=list)
    collaborators: dict[str, str] = field(default_factory=dict)  # login -> role_name
    resolved: dict[str, str | None] = field(default_factory=dict)  # thread id -> resolver
    minimized: set[str] = field(default_factory=set)  # node ids
    config_text: str | None = None
    compare: dict[str, dict[str, Any]] = field(default_factory=dict)  # "a...b" -> response
    invalid_lines: set[tuple[str, int]] = field(default_factory=set)  # 422 on these
    resolve_forbidden: bool = True
    rules: list[Rule] = field(default_factory=list)  # first non-None response wins
    requests: list[tuple[str, str]] = field(default_factory=list)
    bodies: list[tuple[str, str, Any]] = field(default_factory=list)  # (method, path, json)
    next_id: int = 1000
    per_page_cap: int = 100

    # ------------------------------------------------------------------ helpers

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [
            (m, p)
            for m, p in self.requests
            if (m != "GET" and not p.startswith("/graphql")) or p == "/graphql:mutation"
        ]

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if p.startswith("/graphql:mutation")]

    @property
    def mutation_names(self) -> list[str]:
        """The GraphQL mutations sent, by field name (``resolveReviewThread``, ...)."""
        names: list[str] = []
        for _, label, body in self.bodies:
            if label == "/graphql:mutation":
                found = re.search(r"\{\s*(\w+)\s*\(", str((body or {}).get("query", "")))
                names.append(found.group(1) if found else "?")
        return names

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def snapshot(self) -> dict[str, Any]:
        """Everything a user could see on the PR (for idempotency comparisons)."""
        strip = ("updated_at",)
        return copy.deepcopy(
            {
                "issue_comments": [
                    {k: v for k, v in c.items() if k not in strip} for c in self.issue_comments
                ],
                "review_comments": self.review_comments,
                "reviews": self.reviews,
                "pr_labels": sorted(self.pr_labels),
                "repo_labels": self.repo_labels,
                "resolved": self.resolved,
                "minimized": sorted(self.minimized),
            }
        )

    def add_issue_comment(self, body: str, login: str = BOT) -> dict[str, Any]:
        cid = self._id()
        c = {
            "id": cid,
            "node_id": f"IC_{cid}",
            "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "body": body,
            "html_url": f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}#issuecomment-{cid}",
            "updated_at": f"2026-01-01T00:00:{len(self.issue_comments):02d}Z",
        }
        self.issue_comments.append(c)
        return c

    def add_review_comment(
        self,
        path: str,
        line: int,
        body: str,
        login: str = BOT,
        in_reply_to: int | None = None,
    ) -> dict[str, Any]:
        cid = self._id()
        c = {
            "id": cid,
            "node_id": f"PRRC_{cid}",
            "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "path": path,
            "line": line,
            "body": body,
            "in_reply_to_id": in_reply_to,
            "html_url": f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}#discussion_r{cid}",
        }
        self.review_comments.append(c)
        return c

    def posted(self, method: str, suffix: str) -> list[Any]:
        """JSON bodies of ``method`` requests whose path ends with ``suffix``."""
        return [b for m, p, b in self.bodies if m == method and p.endswith(suffix)]

    def thread_id_of(self, comment_id: int) -> str:
        return f"PRRT_{comment_id}"

    # ------------------------------------------------------------------ routing

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        body = json.loads(request.content) if request.content else None
        label = path
        if path == "/graphql" and isinstance(body, dict):
            kind = (
                "mutation"
                if str(body.get("query", "")).lstrip().startswith("mutation")
                else "query"
            )
            label = f"/graphql:{kind}"
        self.requests.append((request.method, label))
        self.bodies.append((request.method, label, body))
        for rule in self.rules:
            resp = rule(request)
            if resp is not None:
                return resp
        if path == "/graphql":
            return self._graphql(request, body or {})
        return self._rest(request, path, body)

    def _page(self, request: httpx.Request, items: list[Any]) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        per_page = min(int(query.get("per_page", ["30"])[0]), self.per_page_cap)
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = request.url.copy_merge_params({"page": page + 1, "per_page": per_page})
            headers["link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=copy.deepcopy(chunk), headers=headers)

    def _rest(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:
        m = request.method
        repo = f"/repos/{self.owner}/{self.repo}"
        n = self.number
        if not path.startswith(repo):
            return httpx.Response(404, json={"message": "Not Found"})
        sub = path[len(repo) :]
        if m == "GET" and sub == f"/pulls/{n}":
            return httpx.Response(200, json=self.pr)
        if m == "GET" and sub == f"/pulls/{n}/files":
            return self._page(request, self.files)
        if m == "GET" and sub.startswith("/compare/"):
            key = sub[len("/compare/") :]
            if key in self.compare:
                return httpx.Response(200, json=self.compare[key])
            if key == f"{BASE}...{HEAD}":
                return httpx.Response(
                    200,
                    json={
                        "status": "ahead",
                        "merge_base_commit": {"sha": MERGE_BASE},
                        "commits": [],
                        "total_commits": 0,
                    },
                )
            return httpx.Response(404, json={"message": "Not Found"})
        if m == "GET" and sub == "/contents/.morning-star-review.yml":
            if self.config_text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            raw = self.config_text.encode()
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "size": len(raw),
                    "content": base64.b64encode(raw).decode(),
                },
            )
        if sub == f"/issues/{n}/comments":
            if m == "GET":
                return self._page(request, self.issue_comments)
            if m == "POST":
                return httpx.Response(201, json=self.add_issue_comment(body["body"]))
        cm = re.fullmatch(r"/issues/comments/(\d+)", sub)
        if cm and m == "PATCH":
            for c in self.issue_comments:
                if c["id"] == int(cm.group(1)):
                    if c["user"]["login"] != BOT:
                        return httpx.Response(403, json={"message": "not yours"})
                    c["body"] = body["body"]
                    c["updated_at"] = "2026-01-02T00:00:00Z"
                    return httpx.Response(200, json=c)
            return httpx.Response(404, json={"message": "Not Found"})
        if sub == f"/pulls/{n}/comments" and m == "GET":
            return self._page(request, self.review_comments)
        rm = re.fullmatch(rf"/pulls/{n}/comments/(\d+)/replies", sub)
        if rm and m == "POST":
            parent = next((c for c in self.review_comments if c["id"] == int(rm.group(1))), None)
            if parent is None:
                return httpx.Response(404, json={"message": "Not Found"})
            # Like GitHub: a REST reply is a submitted review of its own.
            rid = self._id()
            self.reviews.append(
                {
                    "id": rid,
                    "node_id": f"PRR_{rid}",
                    "state": "COMMENTED",
                    "body": "",
                    "html_url": f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}#pullrequestreview-{rid}",
                    "user": {"login": BOT, "type": "Bot"},
                }
            )
            reply = self.add_review_comment(
                parent["path"], parent["line"], body["body"], in_reply_to=parent["id"]
            )
            return httpx.Response(201, json=reply)
        if sub == f"/pulls/{n}/reviews":
            if m == "POST":
                return self._post_review(body)
            if m == "GET":
                return self._page(request, self.reviews)
        sm = re.fullmatch(rf"/pulls/{n}/reviews/(\d+)/events", sub)
        if sm and m == "POST":
            return self._submit_review(int(sm.group(1)), body or {})
        dm = re.fullmatch(rf"/pulls/{n}/reviews/(\d+)", sub)
        if dm and m == "DELETE":
            review = self._review(int(dm.group(1)))
            if review is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if review["state"] != "PENDING":
                return httpx.Response(
                    422, json={"message": "Can not delete a non-pending pull request review"}
                )
            self.reviews.remove(review)
            self.pending.pop(review["id"], None)
            return httpx.Response(200, json=review)
        if sub == f"/issues/{n}/labels":
            if m == "GET":
                return self._page(request, [{"name": x} for x in self.pr_labels])
            if m == "POST":
                for name in body["labels"]:
                    self.repo_labels.setdefault(name, {"name": name, "color": "ededed"})
                    if name not in self.pr_labels:
                        self.pr_labels.append(name)
                return httpx.Response(200, json=[{"name": x} for x in self.pr_labels])
        lm = re.fullmatch(rf"/issues/{n}/labels/(.+)", sub)
        if lm and m == "DELETE":
            name = lm.group(1)
            if name not in self.pr_labels:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.pr_labels.remove(name)
            return httpx.Response(200, json=[])
        gm = re.fullmatch(r"/labels/(.+)", sub)
        if gm and m == "GET":
            label = self.repo_labels.get(gm.group(1))
            return (
                httpx.Response(200, json=label)
                if label
                else httpx.Response(404, json={"message": "Not Found"})
            )
        if sub == "/labels" and m == "POST":
            if body["name"] in self.repo_labels:
                return httpx.Response(
                    422,
                    json={"message": "Validation Failed", "errors": [{"code": "already_exists"}]},
                )
            self.repo_labels[body["name"]] = dict(body)
            return httpx.Response(201, json=body)
        pm = re.fullmatch(r"/collaborators/([^/]+)/permission", sub)
        if pm and m == "GET":
            role = self.collaborators.get(pm.group(1))
            if role is None:
                return httpx.Response(404, json={"message": "Not Found"})
            legacy = {"maintain": "write", "triage": "read"}.get(role, role)
            return httpx.Response(200, json={"permission": legacy, "role_name": role})
        return httpx.Response(404, json={"message": f"fake: no route {m} {sub}"})

    def _review(self, review_id: int) -> dict[str, Any] | None:
        return next((r for r in self.reviews if r["id"] == review_id), None)

    def _pending_review(self, login: str = BOT) -> dict[str, Any] | None:
        """``login``'s pending review (GitHub allows one per user and PR)."""
        return next(
            (r for r in self.reviews if r["state"] == "PENDING" and r["user"]["login"] == login),
            None,
        )

    def _post_review(self, body: dict[str, Any]) -> httpx.Response:
        """Create a review: submitted with ``event: COMMENT``, PENDING without an event."""
        event = body.get("event")
        if event not in (None, "COMMENT"):
            return httpx.Response(422, json={"message": "only COMMENT expected in tests"})
        if body.get("commit_id") != self.pr["head"]["sha"]:
            return httpx.Response(
                422, json={"message": "commit_id is not part of the pull request"}
            )
        if self._pending_review() is not None:
            return httpx.Response(
                422,
                json={
                    "message": "Unprocessable Entity",
                    "errors": ["User can only have one pending review per pull request"],
                },
            )
        comments = body.get("comments") or []
        for c in comments:
            if (c["path"], c["line"]) in self.invalid_lines:
                return httpx.Response(
                    422,
                    json={
                        "message": "Unprocessable Entity",
                        "errors": ["Line could not be resolved"],
                    },
                )
        if event == "COMMENT" and not comments and not body.get("body"):
            return httpx.Response(422, json={"message": "Body can't be blank"})
        rid = self._id()
        review = {
            "id": rid,
            "node_id": f"PRR_{rid}",
            "state": "PENDING" if event is None else "COMMENTED",
            "body": body.get("body", ""),
            "html_url": f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}#pullrequestreview-{rid}",
            "user": {"login": BOT, "type": "Bot"},
        }
        self.reviews.append(review)
        new = [
            {"path": c["path"], "line": c["line"], "body": c["body"], "in_reply_to": None}
            for c in comments
        ]
        if event is None:
            self.pending[rid] = new
        else:
            self._materialize(new)
        return httpx.Response(200, json=review)

    def _materialize(self, comments: list[dict[str, Any]]) -> None:
        for c in comments:
            self.add_review_comment(c["path"], c["line"], c["body"], in_reply_to=c["in_reply_to"])

    def _submit_review(self, review_id: int, body: dict[str, Any]) -> httpx.Response:
        review = self._review(review_id)
        if review is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if review["state"] != "PENDING":
            return httpx.Response(422, json={"message": "Review has already been submitted"})
        if body.get("event") != "COMMENT":
            return httpx.Response(422, json={"message": "only COMMENT expected in tests"})
        comments = self.pending.pop(review_id, [])
        if not comments and not body.get("body"):
            self.pending[review_id] = comments
            return httpx.Response(422, json={"message": "Body can't be blank"})
        review.update(state="COMMENTED", body=body.get("body", review["body"]))
        self._materialize(comments)
        return httpx.Response(200, json=review)

    def _add_reply(self, variables: dict[str, Any]) -> httpx.Response:
        """GraphQL ``addPullRequestReviewThreadReply`` into a pending review."""

        def not_found(what: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {"addPullRequestReviewThreadReply": None},
                    "errors": [{"type": "NOT_FOUND", "message": f"Could not resolve {what}"}],
                },
            )

        review = next((r for r in self.reviews if r["node_id"] == variables.get("reviewId")), None)
        if review is None or review["state"] != "PENDING":
            return not_found("the pending review")
        parent = next(
            (
                c
                for c in self.review_comments
                if c["in_reply_to_id"] is None
                and self.thread_id_of(c["id"]) == variables.get("threadId")
            ),
            None,
        )
        if parent is None:
            return not_found("the thread")
        self.pending[review["id"]].append(
            {
                "path": parent["path"],
                "line": parent["line"],
                "body": variables["body"],
                "in_reply_to": parent["id"],
            }
        )
        return httpx.Response(
            200,
            json={"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "PRRC_new"}}}},
        )

    # ------------------------------------------------------------------ GraphQL

    def _threads(self) -> list[dict[str, Any]]:
        nodes = []
        for c in self.review_comments:
            if c["in_reply_to_id"] is not None:
                continue
            tid = self.thread_id_of(c["id"])
            login = c["user"]["login"]
            is_bot = login.endswith("[bot]")
            resolver = self.resolved.get(tid)
            nodes.append(
                {
                    "id": tid,
                    "isResolved": tid in self.resolved,
                    "isOutdated": False,
                    "path": c["path"],
                    "line": c["line"],
                    "resolvedBy": {"login": resolver} if resolver else None,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": c["id"],
                                "author": {
                                    "__typename": "Bot" if is_bot else "User",
                                    "login": login[: -len("[bot]")] if is_bot else login,
                                },
                                "body": c["body"],
                            }
                        ]
                    },
                }
            )
        return nodes

    def _graphql(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        query = str(body.get("query", ""))
        variables = body.get("variables") or {}
        if "reviewThreads" in query:
            nodes = self._threads()
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": nodes,
                                }
                            }
                        }
                    }
                },
            )
        if "unresolveReviewThread" in query or "resolveReviewThread" in query:
            if self.resolve_forbidden:
                return httpx.Response(
                    200,
                    json={
                        "data": {"resolveReviewThread": None},
                        "errors": [
                            {
                                "type": "FORBIDDEN",
                                "message": "Resource not accessible by integration",
                            }
                        ],
                    },
                )
            tid = variables["threadId"]
            if "unresolve" in query:
                self.resolved.pop(tid, None)
            else:
                self.resolved[tid] = BOT[: -len("[bot]")]
            return httpx.Response(200, json={"data": {"x": {"thread": {"id": tid}}}})
        if "addPullRequestReviewThreadReply" in query:
            return self._add_reply(variables)
        if "minimizeComment" in query:
            self.minimized.add(variables["subjectId"])
            return httpx.Response(
                200,
                json={
                    "data": {
                        "minimizeComment": {
                            "minimizedComment": {"isMinimized": True, "minimizedReason": "resolved"}
                        }
                    }
                },
            )
        return httpx.Response(
            200, json={"errors": [{"type": "INTERNAL", "message": "fake: unknown query"}]}
        )
