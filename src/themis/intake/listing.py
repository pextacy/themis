"""List a repository's issues and pull requests for a backfill. Read-only, API only."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from themis.intake.github import MAX_PAGES, PER_PAGE, GitHubClient, GitHubError, Target

REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
STATES = ("open", "closed", "all")
KINDS = ("issues", "pulls", "all")


@dataclass(frozen=True)
class ListedItem:
    number: int
    is_pull_request: bool
    updated_at: str


@dataclass
class Listing:
    items: list[ListedItem] = field(default_factory=list)
    truncated: bool = False


def parse_repo(repo: str) -> str:
    if REPO_NAME.fullmatch(repo) is None or ".." in repo:
        raise ValueError(f"not an owner/name repository: {repo!r}")
    return repo


def target_for(repo: str, number: int) -> Target:
    owner, name = repo.split("/", 1)
    return Target(owner, name, number)


def _matches(row: dict[str, Any], kind: str) -> bool:
    is_pull = "pull_request" in row
    return kind == "all" or is_pull == (kind == "pulls")


def _item(row: dict[str, Any]) -> ListedItem:
    return ListedItem(int(row["number"]), "pull_request" in row, str(row.get("updated_at") or ""))


class RepoLister(GitHubClient):
    async def _pages(self, path: str, query: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """`_list` cannot carry filters, because httpx drops a query string when params are set.

        The cast is the whole workaround: `_get` is typed for page numbers but sends any value.
        """
        params = cast(dict[str, int], query)
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            reply = await self._get(path, per_page=PER_PAGE, page=page, **params)
            data = reply.json()
            if not isinstance(data, list):
                raise GitHubError(f"expected a list from {path}")
            rows.extend(data)
            if "next" not in reply.links:
                return rows, False
        return rows, True

    async def list_items(
        self, repo: str, *, state: str = "open", kind: str = "all", since: datetime | None = None, limit: int = 50
    ) -> Listing:
        """Newest first. The issues route also lists pull requests, so one route covers all kinds."""
        if state not in STATES or kind not in KINDS:
            raise ValueError("state must be open, closed or all; kind must be issues, pulls or all")
        query = {"state": state, "sort": "created", "direction": "desc"}
        if since is not None:
            query["since"] = since.isoformat()
        rows, more = await self._pages(f"/repos/{parse_repo(repo)}/issues", query)
        items = [_item(row) for row in rows if _matches(row, kind)]
        return Listing(items[:limit], more or len(items) > limit)
