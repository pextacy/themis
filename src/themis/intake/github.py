"""Read-only GitHub intake. Reads through the API and never checks out PR code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from themis.config import Settings
from themis.models import Submission
from themis.otari.budgets import contributor_key

API_URL = "https://api.github.com"
MAX_PAGES = 3
PER_PAGE = 100
MAX_DIFF_CHARS = 16_000
MAX_COMMENTS = 30
MAX_TARBALL_BYTES = 100 * 1024 * 1024
MAX_TAGS = 300
FIRST_TIME_ASSOCIATIONS = frozenset({"FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE"})
URL_PATTERN = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)(?:[/?#].*)?")


class GitHubError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubRateLimited(GitHubError):
    """The API refused because the rate limit ran out."""


@dataclass(frozen=True)
class Target:
    owner: str
    repo: str
    number: int


def parse_url(url: str) -> Target:
    match = URL_PATTERN.fullmatch(url.strip())
    if match is None:
        raise ValueError(f"not a GitHub issue or pull request URL: {url!r}")
    return Target(match[1], match[2], int(match[4]))


def is_first_time(association: str | None) -> bool:
    """Only ever raises priority. Never an input to a verdict."""
    return association in FIRST_TIME_ASSOCIATIONS


def _submission_id(target: Target) -> str:
    return f"{target.owner}-{target.repo}-{target.number}"


def _diff_summary(files: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{item['filename']} ({item.get('status', 'modified')} +{item.get('additions', 0)} -{item.get('deletions', 0)})"
        for item in files
    )


def _diff_excerpt(files: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    used = 0
    for item in files:
        patch = item.get("patch")
        if not patch:
            continue
        part = f"--- {item['filename']}\n{patch}"
        if used + len(part) > MAX_DIFF_CHARS:
            break
        parts.append(part)
        used += len(part)
    return "\n".join(parts)


def _author(payload: dict[str, Any]) -> str:
    user = payload.get("user") or {}
    if not isinstance(user.get("id"), int):
        raise GitHubError("payload has no numeric author id")
    return contributor_key(user["id"])


def _labels(item: dict[str, Any]) -> list[str]:
    return [str(row["name"]) for row in item.get("labels") or [] if isinstance(row, dict) and row.get("name")]


def from_issue_payload(target: Target, issue: dict[str, Any], comments: list[str] | None = None) -> Submission:
    return Submission(
        id=_submission_id(target),
        repo=f"{target.owner}/{target.repo}",
        number=target.number,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        contributor_key=_author(issue),
        first_time=is_first_time(issue.get("author_association")),
        comments=comments or [],
        labels=_labels(issue),
    )


def from_pull_payload(
    target: Target, pull: dict[str, Any], files: list[dict[str, Any]], comments: list[str] | None = None
) -> Submission:
    return Submission(
        id=_submission_id(target),
        repo=f"{target.owner}/{target.repo}",
        number=target.number,
        title=pull.get("title") or "",
        body=pull.get("body") or "",
        contributor_key=_author(pull),
        first_time=is_first_time(pull.get("author_association")),
        is_pull_request=True,
        head_sha=(pull.get("head") or {}).get("sha"),
        changed_files=[item["filename"] for item in files],
        diff_summary=_diff_summary(files),
        diff_excerpt=_diff_excerpt(files),
        comments=comments or [],
        labels=_labels(pull),
    )


class GitHubClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token is not None:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        self._http = httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=30, transport=transport)

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: int) -> httpx.Response:
        try:
            reply = await self._http.get(path, params=params)
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach GitHub: {error}") from error
        if reply.status_code in (403, 429) and reply.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubRateLimited("GitHub rate limit exhausted", status=reply.status_code)
        if not reply.is_success:
            raise GitHubError(f"GitHub answered {reply.status_code} for {path}", status=reply.status_code)
        return reply

    async def _object(self, path: str) -> dict[str, Any]:
        data = (await self._get(path)).json()
        if not isinstance(data, dict):
            raise GitHubError(f"expected an object from {path}")
        return data

    async def _list(self, path: str, limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            reply = await self._get(path, per_page=PER_PAGE, page=page)
            items.extend(reply.json())
            if "next" not in reply.links or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    async def _comments(self, base: str, number: int) -> list[str]:
        rows = await self._list(f"{base}/issues/{number}/comments", MAX_COMMENTS)
        return [str(row.get("body") or "") for row in rows]

    async def default_branch(self, repo: str) -> str:
        return str((await self._object(f"/repos/{repo}"))["default_branch"])

    async def tags(self, repo: str) -> list[str]:
        """Newest first, as GitHub lists them."""
        return [str(row["name"]) for row in await self._list(f"/repos/{repo}/tags", MAX_TAGS)]

    async def issue_exists(self, repo: str, number: int) -> bool | None:
        """None when GitHub could not say."""
        try:
            await self._get(f"/repos/{repo}/issues/{number}")
        except GitHubRateLimited:
            return None
        except GitHubError as error:
            return False if error.status in (404, 410) else None
        return True

    async def search_issues(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            reply = await self._http.get("/search/issues", params={"q": query, "per_page": limit})
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach GitHub: {error}") from error
        if not reply.is_success:
            raise GitHubError(f"GitHub search answered {reply.status_code}", status=reply.status_code)
        return list(reply.json().get("items", []))

    async def tarball(self, repo: str, ref: str) -> bytes | None:
        """The archive at ref, held in memory only. None when it is larger than the cap."""
        chunks: list[bytes] = []
        size = 0
        try:
            async with self._http.stream("GET", f"/repos/{repo}/tarball/{ref}", follow_redirects=True) as reply:
                if not reply.is_success:
                    raise GitHubError(f"GitHub answered {reply.status_code} for a tarball", status=reply.status_code)
                async for chunk in reply.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_TARBALL_BYTES:
                        return None
                    chunks.append(chunk)
        except httpx.TransportError as error:
            raise GitHubError(f"could not download the tarball: {error}") from error
        return b"".join(chunks)

    async def file_text(self, repo: str, path: str) -> str | None:
        """A file on the default branch, so a pull request cannot change what is read."""
        try:
            reply = await self._http.get(
                f"/repos/{repo}/contents/{path}", headers={"Accept": "application/vnd.github.raw+json"}
            )
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach GitHub: {error}") from error
        if reply.status_code == 404:
            return None
        if not reply.is_success:
            raise GitHubError(f"GitHub answered {reply.status_code} for {path}", status=reply.status_code)
        return reply.text

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._write("POST", path, body)

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._write("PATCH", path, body)

    async def delete(self, path: str) -> None:
        """Deleting something already gone is fine."""
        try:
            reply = await self._http.delete(path)
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach GitHub: {error}") from error
        if not reply.is_success and reply.status_code != 404:
            raise GitHubError(f"GitHub answered {reply.status_code} for {path}", status=reply.status_code)

    async def _write(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = await self._http.request(method, path, json=body)
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach GitHub: {error}") from error
        if not reply.is_success:
            raise GitHubError(f"GitHub answered {reply.status_code} for {path}", status=reply.status_code)
        data = reply.json()
        return data if isinstance(data, dict) else {}

    async def pull_for_commit(self, repo: str, sha: str) -> int | None:
        """A fork's workflow_run lists no pull requests, so find it by its head commit."""
        for item in await self.search_issues(f"repo:{repo} type:pr {sha}", limit=1):
            return int(item["number"])
        return None

    async def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._list(f"/repos/{repo}/issues/{number}/comments")

    async def issue_labels(self, repo: str, number: int) -> list[str]:
        rows = await self._list(f"/repos/{repo}/issues/{number}/labels")
        return [str(row["name"]) for row in rows]

    async def fetch(self, target: Target) -> Submission:
        """An /issues/ URL can name a pull request, so the payload decides."""
        base = f"/repos/{target.owner}/{target.repo}"
        issue = await self._object(f"{base}/issues/{target.number}")
        comments = await self._comments(base, target.number)
        if "pull_request" not in issue:
            return from_issue_payload(target, issue, comments)
        pull = await self._object(f"{base}/pulls/{target.number}")
        files = await self._list(f"{base}/pulls/{target.number}/files")
        return from_pull_payload(target, pull, files, comments)
