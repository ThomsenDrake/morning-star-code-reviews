"""``gh api`` of the fake GitHub CLI: a few REST routes over a JSON state."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ACTION = "ThomsenDrake/morning-star-code-reviews"
_REASONS = {404: "Not Found", 409: "Conflict", 422: "Reference already exists"}


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def respond(data: Any) -> None:
    print(json.dumps(data))


def new_sha(repo: dict[str, Any]) -> str:
    repo["counter"] = repo.get("counter", 0) + 1
    return hashlib.sha1(f"{repo['full_name']}-{repo['counter']}".encode()).hexdigest()  # noqa: S324


def branch(repo: dict[str, Any], name: str) -> dict[str, Any]:
    found = repo["branches"].get(name)
    if found is None:
        raise HttpError(404)
    return found


def contents(repo: dict[str, Any], method: str, path: str, query: dict, body: Any) -> Any:
    if method == "GET":
        files = branch(repo, query.get("ref", [repo["default_branch"]])[0])["files"]
        if path not in files:
            raise HttpError(404)
        text = files[path].encode()
        sha = hashlib.sha1(b"blob %d\0" % len(text) + text).hexdigest()  # noqa: S324
        return {"type": "file", "sha": sha, "content": base64.encodebytes(text).decode()}
    name = body.get("branch") or repo["default_branch"]
    if name in repo.get("protected", []):
        raise HttpError(409)
    br = branch(repo, name)
    current = br["files"].get(path)
    if current is not None:
        text = current.encode()
        if body.get("sha") != hashlib.sha1(b"blob %d\0" % len(text) + text).hexdigest():  # noqa: S324
            raise HttpError(409)
    elif method == "DELETE" or body.get("sha"):
        raise HttpError(404 if method == "DELETE" else 409)
    if method == "PUT":
        br["files"][path] = base64.b64decode(body["content"]).decode()
    else:
        del br["files"][path]
    br["sha"] = new_sha(repo)
    repo.setdefault("commits", []).append(
        {"branch": body.get("branch"), "message": body["message"], "path": path, "op": method}
    )
    return {"commit": {"sha": br["sha"]}, "content": None}


def repo_routes(repo: dict[str, Any], method: str, rest: str, query: dict, body: Any) -> Any:
    if rest == "" and method == "GET":
        return {"full_name": repo["full_name"], "default_branch": repo["default_branch"]}
    if rest.startswith("/contents/"):
        return contents(repo, method, unquote(rest[len("/contents/") :]), query, body)
    m = re.fullmatch(r"/git/refs?/heads/(.+)", rest)
    if m and method == "GET":
        return {"object": {"type": "commit", "sha": branch(repo, unquote(m.group(1)))["sha"]}}
    if m and method == "PATCH":
        br = branch(repo, unquote(m.group(1)))
        source = next(b for b in repo["branches"].values() if b["sha"] == body["sha"])
        br.update(sha=body["sha"], files=dict(source["files"]))
        return {"object": {"sha": body["sha"]}}
    if rest == "/git/refs" and method == "POST":
        name = body["ref"].removeprefix("refs/heads/")
        if name in repo["branches"]:
            raise HttpError(422)
        source = next(b for b in repo["branches"].values() if b["sha"] == body["sha"])
        repo["branches"][name] = {"sha": body["sha"], "files": dict(source["files"])}
        return {"ref": body["ref"]}
    if rest == "/pulls" and method == "GET":
        head = query.get("head", [""])[0]
        return [p for p in repo.get("pulls", []) if f"{repo['owner']}:{p['head']}" == head]
    if rest == "/pulls" and method == "POST":
        pulls = repo.setdefault("pulls", [])
        number = len(pulls) + 1
        url = f"https://github.com/{repo['full_name']}/pull/{number}"
        pulls.append({"head": body["head"], "base": body["base"], "title": body["title"],
                      "html_url": url})  # fmt: skip
        return {"number": number, "html_url": url}
    if rest == "/actions/secrets":
        return {"secrets": [{"name": n} for n in sorted(repo.get("secrets", {}))]}
    if rest == "/actions/variables":
        variables = repo.get("variables", {})
        return {"variables": [{"name": n, "value": v} for n, v in sorted(variables.items())]}
    m = re.fullmatch(r"/actions/workflows/([^/]+)/runs", rest)
    if m:
        files = repo["branches"][repo["default_branch"]]["files"]
        if f".github/workflows/{m.group(1)}" not in files:
            raise HttpError(404)
        return {"workflow_runs": repo.get("runs", [])}
    raise HttpError(404)


def route(state: dict[str, Any], method: str, url: str, body: Any) -> Any:
    parts = urlsplit(url)
    path, query = parts.path.lstrip("/"), parse_qs(parts.query)
    if int(query.get("page", ["1"])[0]) > 1:
        key = path.rsplit("/", 1)[-1]
        return {key: []}
    if path.startswith(f"repos/{ACTION}/git/"):
        rest = path[len(f"repos/{ACTION}/git/") :]
        if rest.startswith("ref/tags/") and rest[9:] in state.get("tags", {}):
            return {"object": state["tags"][rest[9:]]}
        if rest.startswith("tags/") and rest[5:] in state.get("tag_objects", {}):
            return {"object": state["tag_objects"][rest[5:]]}
        raise HttpError(404)
    m = re.fullmatch(r"repos/([^/]+)/([^/]+)(/.*)?", path)
    if m:
        full = f"{unquote(m.group(1))}/{unquote(m.group(2))}"
        repo = state["repos"].get(full)
        if repo is None:
            raise HttpError(404)
        repo.setdefault("full_name", full)
        repo.setdefault("owner", full.split("/")[0])
        return repo_routes(repo, method, m.group(3) or "", query, body)
    raise HttpError(404)


def handle(state: dict[str, Any], argv: list[str], stdin: str) -> None:
    method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
    url = next(a for a in argv[1:] if re.match(r"^/?repos/", a))
    body = json.loads(stdin) if "--input" in argv else None
    try:
        respond(route(state, method, url, body))
    except HttpError as exc:
        reason = _REASONS.get(exc.status, "Error")
        respond({"message": reason})
        sys.stderr.write(f"gh: {reason} (HTTP {exc.status})\n")
        sys.exit(1)
