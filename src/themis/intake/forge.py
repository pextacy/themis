"""Pick the forge a URL belongs to. GitHub stays the default and the only one that can post."""

from __future__ import annotations

import re
from typing import Any, Protocol

from themis.config import Settings
from themis.intake.forgejo import ForgejoClient, ForgejoHostRefused
from themis.intake.github import GitHubClient, Target, parse_url

GITHUB = "github"
FORGE_URL = re.compile(r"https://([\w.-]+)/([\w.-]+)/([\w.-]+)/(issues|pulls)/(\d+)(?:[/?#].*)?")
RESERVED = frozenset({".", ".."})


class ForgeClient(Protocol):
    """The read surface the pipeline and the evidence modules use."""

    async def default_branch(self, repo: str) -> str: ...
    async def tags(self, repo: str) -> list[str]: ...
    async def issue_exists(self, repo: str, number: int) -> bool | None: ...
    async def search_issues(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
    async def tarball(self, repo: str, ref: str) -> bytes | None: ...
    async def file_text(self, repo: str, path: str) -> str | None: ...
    async def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def issue_labels(self, repo: str, number: int) -> list[str]: ...
    async def pull_for_commit(self, repo: str, sha: str) -> int | None: ...
    async def fetch(self, target: Target) -> Any: ...


def parse_any_url(url: str, settings: Settings) -> tuple[str, Target]:
    """Return ("github" or the Forgejo host, target). An unlisted host is refused, not guessed."""
    text = url.strip()
    if text.startswith("https://github.com/"):
        return GITHUB, parse_url(text)
    match = FORGE_URL.fullmatch(text)
    if match is None:
        raise ValueError(f"not an issue or pull request URL: {url!r}")
    host, owner, repo = match[1].lower(), match[2], match[3]
    if host not in {item.lower() for item in settings.forgejo_hosts}:
        raise ForgejoHostRefused(f"{host} is not in THEMIS_FORGEJO_HOSTS")
    if owner in RESERVED or repo in RESERVED:
        raise ValueError(f"not a repository: {owner}/{repo}")
    return host, Target(owner, repo, int(match[5]))


def open_client(settings: Settings, forge: str) -> GitHubClient | ForgejoClient:
    return GitHubClient(settings) if forge == GITHUB else ForgejoClient(settings, forge)
