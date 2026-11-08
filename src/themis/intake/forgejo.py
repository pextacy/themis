"""Read-only Forgejo and Codeberg intake. The same read surface as GitHubClient, no writes.

Reads through the API and never checks out PR code. The token only goes to hosts listed in
`THEMIS_FORGEJO_HOSTS`, and redirects leaving that host are refused.
"""

from __future__ import annotations

import re
import zlib
from typing import Any
from urllib.parse import quote

import httpx

from themis.config import Settings
from themis.intake.diffs import split_unified_diff
from themis.intake.github import (
    MAX_COMMENTS,
    MAX_PAGES,
    MAX_TAGS,
    MAX_TARBALL_BYTES,
    PER_PAGE,
    GitHubError,
    GitHubRateLimited,
    Target,
    from_issue_payload,
    from_pull_payload,
)
from themis.models import Submission
from themis.otari.budgets import contributor_key

MAX_DIFF_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
PR_SCAN_LIMIT = 50
# Keys must stay `gh:<number>` for Otari, so each host gets a range no GitHub id reaches.
HOST_RANGE = 10**13
HOST_SLOTS = 90
QUALIFIER = re.compile(r"\b(?:repo|type|is|state):\S+")
NOT_SUPPORTED = "posting to Forgejo is not supported yet: run with post=False"


class ForgejoHostRefused(ValueError):
    """The host is not in THEMIS_FORGEJO_HOSTS, so no request was made."""


def forge_contributor_key(host: str, user_id: int) -> str:
    """A numeric key per host and user. Never a username, and never equal to a GitHub id."""
    slot = zlib.crc32(host.lower().encode()) % HOST_SLOTS + 1
    return contributor_key(slot * HOST_RANGE + user_id)


def _path_part(value: str) -> str:
    return quote(value, safe="")


class ForgejoClient:
    def __init__(self, settings: Settings, host: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        allowed = {item.lower() for item in settings.forgejo_hosts}
        if host.lower() not in allowed:
            raise ForgejoHostRefused(f"{host} is not in THEMIS_FORGEJO_HOSTS")
        self.host = host.lower()
        headers = {"Accept": "application/json"}
        if settings.forgejo_token is not None:
            headers["Authorization"] = f"token {settings.forgejo_token.get_secret_value()}"
        self._http = httpx.AsyncClient(
            base_url=f"https://{self.host}/api/v1", headers=headers, timeout=30, transport=transport
        )

    async def __aenter__(self) -> ForgejoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: int | str) -> httpx.Response:
        try:
            reply = await self._http.get(path, params=params)
        except httpx.TransportError as error:
            raise GitHubError(f"could not reach {self.host}: {error}") from error
        if reply.status_code == 429:
            raise GitHubRateLimited(f"{self.host} rate limit exhausted", status=429)
        if not reply.is_success:
            raise GitHubError(f"{self.host} answered {reply.status_code} for {path}", status=reply.status_code)
        return reply

    async def _object(self, path: str) -> dict[str, Any]:
        data = (await self._get(path)).json()
        if not isinstance(data, dict):
            raise GitHubError(f"expected an object from {path}")
        return data

    async def _list(self, path: str, limit: int | None = None, **params: int | str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            rows = (await self._get(path, limit=PER_PAGE, page=page, **params)).json()
            if not isinstance(rows, list) or not rows:
                break
            items.extend(rows)
            if len(rows) < PER_PAGE or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    async def default_branch(self, repo: str) -> str:
        return str((await self._object(f"/repos/{repo}"))["default_branch"])

    async def tags(self, repo: str) -> list[str]:
        return [str(row["name"]) for row in await self._list(f"/repos/{repo}/tags", MAX_TAGS)]

    async def issue_exists(self, repo: str, number: int) -> bool | None:
        """None when the host could not say."""
        try:
            await self._get(f"/repos/{repo}/issues/{number}")
        except GitHubRateLimited:
            return None
        except GitHubError as error:
            return False if error.status in (404, 410) else None
        return True

    async def search_issues(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Takes the GitHub-style query the evidence modules write, `repo:o/n words`."""
        repo = re.search(r"\brepo:(\S+)", query)
        if repo is None:
            raise GitHubError("a Forgejo search needs a repo: qualifier")
        terms = " ".join(QUALIFIER.sub(" ", query).split())
        reply = await self._get(f"/repos/{repo[1]}/issues", q=terms, type="all", state="all", limit=limit)
        rows = reply.json()
        return rows if isinstance(rows, list) else []

    async def _follow(self, url: str) -> httpx.Response:
        """Stream one archive request, following redirects only within this host."""
        for _ in range(MAX_REDIRECTS + 1):
            request = self._http.build_request("GET", url)
            reply = await self._http.send(request, stream=True)
            if not reply.is_redirect:
                return reply
            location = reply.headers.get("location", "")
            await reply.aclose()
            target = request.url.join(location)
            if target.host != self.host:
                raise GitHubError(f"refused a redirect away from {self.host}")
            url = str(target)
        raise GitHubError("too many redirects for an archive")

    async def tarball(self, repo: str, ref: str) -> bytes | None:
        """The archive at ref, held in memory only. None when it is larger than the cap."""
        size = 0
        chunks: list[bytes] = []
        try:
            reply = await self._follow(f"/repos/{repo}/archive/{_path_part(ref)}.tar.gz")
            try:
                if not reply.is_success:
                    raise GitHubError(
                        f"{self.host} answered {reply.status_code} for an archive", status=reply.status_code
                    )
                async for chunk in reply.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_TARBALL_BYTES:
                        return None
                    chunks.append(chunk)
            finally:
                await reply.aclose()
        except httpx.TransportError as error:
            raise GitHubError(f"could not download the archive: {error}") from error
        return b"".join(chunks)

    async def file_text(self, repo: str, path: str) -> str | None:
        """A file on the default branch, so a pull request cannot change what is read."""
        try:
            return (await self._get(f"/repos/{repo}/raw/{quote(path)}")).text
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    async def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._list(f"/repos/{repo}/issues/{number}/comments")

    async def issue_labels(self, repo: str, number: int) -> list[str]:
        rows = await self._list(f"/repos/{repo}/issues/{number}/labels")
        return [str(row["name"]) for row in rows]

    async def pull_for_commit(self, repo: str, sha: str) -> int | None:
        for row in await self._list(f"/repos/{repo}/pulls", PR_SCAN_LIMIT, state="all"):
            if (row.get("head") or {}).get("sha") == sha:
                return int(row["number"])
        return None

    async def _pull_files(self, base: str, number: int) -> list[dict[str, Any]]:
        """The file list has no patches here, so they come from the raw diff."""
        files = await self._list(f"{base}/pulls/{number}/files")
        reply = await self._get(f"{base}/pulls/{number}.diff")
        if len(reply.content) > MAX_DIFF_BYTES:
            return files
        patches = {item["filename"]: item.get("patch") for item in split_unified_diff(reply.text)}
        return [{**item, "patch": patches.get(item["filename"])} for item in files]

    def _localize(self, item: Submission, target: Target, author: dict[str, Any]) -> Submission:
        """A separate id space, so the same owner/name on two forges never shares a row."""
        user = author.get("user") or {}
        if not isinstance(user.get("id"), int):
            raise GitHubError("payload has no numeric author id")
        prefix = self.host.replace(".", "-")
        return item.model_copy(
            update={
                "id": f"{prefix}-{target.owner}-{target.repo}-{target.number}",
                "contributor_key": forge_contributor_key(self.host, user["id"]),
            }
        )

    async def fetch(self, target: Target) -> Submission:
        """Issues carry a `pull_request` key that is null unless they are one."""
        base = f"/repos/{target.owner}/{target.repo}"
        issue = await self._object(f"{base}/issues/{target.number}")
        rows = await self._list(f"{base}/issues/{target.number}/comments", MAX_COMMENTS)
        comments = [str(row.get("body") or "") for row in rows]
        if not issue.get("pull_request"):
            return self._localize(from_issue_payload(target, issue, comments), target, issue)
        pull = await self._object(f"{base}/pulls/{target.number}")
        files = await self._pull_files(base, target.number)
        return self._localize(from_pull_payload(target, pull, files, comments), target, pull)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise GitHubError(NOT_SUPPORTED)

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise GitHubError(NOT_SUPPORTED)

    async def delete(self, path: str) -> None:
        raise GitHubError(NOT_SUPPORTED)
