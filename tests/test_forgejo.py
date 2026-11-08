"""Everything here is a TEST DOUBLE. No request leaves the process."""

from typing import Any

import httpx
import pytest

from tests.conftest import make_settings
from themis.evidence.snapshot import from_tarball
from themis.intake import forge
from themis.intake.forgejo import ForgejoClient, ForgejoHostRefused, forge_contributor_key
from themis.intake.github import GitHubClient, GitHubError, GitHubRateLimited, Target
from themis.otari.budgets import check_contributor_key

ISSUE = {"title": "Bug", "body": "text", "user": {"id": 7, "login": "mira"}, "pull_request": None}
PULL_ISSUE = {**ISSUE, "pull_request": {"merged": False}}
PULL = {"title": "Fix", "body": "Fixes #1", "user": {"id": 7}, "head": {"sha": "abc123"}}
DIFF = (
    "diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/img.png b/img.png\nBinary files differ\n"
)
FILES = [{"filename": "src/a.py", "status": "changed", "additions": 1, "deletions": 1}, {"filename": "img.png"}]


def transport(seen: list[httpx.Request], overrides: dict[str, httpx.Response] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/api/v1")
        routes: dict[str, Any] = {
            "/repos/o/r": {"default_branch": "trunk"},
            "/repos/o/r/tags": [{"name": "v1"}],
            "/repos/o/r/issues/1": ISSUE,
            "/repos/o/r/issues/2": PULL_ISSUE,
            "/repos/o/r/issues/1/comments": [{"body": "hi"}],
            "/repos/o/r/issues/2/comments": [],
            "/repos/o/r/issues/1/labels": [{"name": "bug"}],
            "/repos/o/r/pulls/2": PULL,
            "/repos/o/r/pulls/2/files": FILES,
            "/repos/o/r/pulls": [{"number": 2, "head": {"sha": "abc123"}}],
            "/repos/o/r/issues": [{"number": 5, "title": "x"}],
        }
        if overrides and path in overrides:
            return overrides[path]
        if path == "/repos/o/r/pulls/2.diff":
            return httpx.Response(200, text=DIFF)
        if path == "/repos/o/r/raw/.github/themis.yml":
            return httpx.Response(200, text="version: 1\n")
        return httpx.Response(200, json=routes[path]) if path in routes else httpx.Response(404)

    return httpx.MockTransport(handle)


def client(
    seen: list[httpx.Request], overrides: dict[str, httpx.Response] | None = None, **settings: Any
) -> ForgejoClient:
    return ForgejoClient(make_settings(**settings), "codeberg.org", transport=transport(seen, overrides))


async def test_issue_becomes_a_submission() -> None:
    seen: list[httpx.Request] = []
    async with client(seen) as api:
        item = await api.fetch(Target("o", "r", 1))
    assert item.id == "codeberg-org-o-r-1" and not item.is_pull_request and item.comments == ["hi"]
    assert item.repo == "o/r" and item.contributor_key == forge_contributor_key("codeberg.org", 7)


async def test_pull_gets_patches_from_the_raw_diff() -> None:
    async with client([]) as api:
        item = await api.fetch(Target("o", "r", 2))
    assert item.is_pull_request and item.head_sha == "abc123"
    assert item.changed_files == ["src/a.py", "img.png"]
    assert "--- src/a.py\n@@ -1 +1 @@\n-old\n+new" in item.diff_excerpt and "img.png" not in item.diff_excerpt


def test_contributor_key_is_valid_numeric_and_never_a_github_id() -> None:
    key = forge_contributor_key("codeberg.org", 7)
    assert check_contributor_key(key) == key
    assert key != "gh:7" and int(key[3:]) > 10**12
    assert key != forge_contributor_key("git.example.org", 7)
    assert key == forge_contributor_key("CODEBERG.org", 7)


async def test_read_surface() -> None:
    seen: list[httpx.Request] = []
    async with client(seen) as api:
        assert await api.default_branch("o/r") == "trunk"
        assert await api.tags("o/r") == ["v1"]
        assert await api.issue_exists("o/r", 1) is True
        assert await api.issue_exists("o/r", 404) is False
        assert await api.issue_labels("o/r", 1) == ["bug"]
        assert await api.file_text("o/r", ".github/themis.yml") == "version: 1\n"
        assert await api.file_text("o/r", "missing.yml") is None
        assert await api.pull_for_commit("o/r", "abc123") == 2
        assert await api.pull_for_commit("o/r", "nope") is None
    assert all(request.url.host == "codeberg.org" for request in seen)


async def test_search_takes_the_github_style_query() -> None:
    seen: list[httpx.Request] = []
    async with client(seen) as api:
        rows = await api.search_issues("repo:o/r type:pr parse range")
        with pytest.raises(GitHubError):
            await api.search_issues("no qualifier")
    assert rows == [{"number": 5, "title": "x"}]
    assert seen[0].url.params["q"] == "parse range"


async def test_rate_limit_and_outage_are_told_apart() -> None:
    overrides = {"/repos/o/r/issues/1": httpx.Response(429), "/repos/o/r/issues/3": httpx.Response(500)}
    async with client([], overrides) as api:
        assert await api.issue_exists("o/r", 1) is None
        assert await api.issue_exists("o/r", 3) is None
        with pytest.raises(GitHubRateLimited):
            await api.fetch(Target("o", "r", 1))


async def test_tarball_reads_into_a_snapshot() -> None:
    from tests.evidence_helpers import tarball

    archive = tarball({"src/a.py": "x = 1\n"})
    async with client([], {"/repos/o/r/archive/v1.tar.gz": httpx.Response(200, content=archive)}) as api:
        snapshot = from_tarball("v1", await api.tarball("o/r", "v1"), "r")
    assert snapshot.has_path("src/a.py")


async def test_tarball_over_the_cap_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("themis.intake.forgejo.MAX_TARBALL_BYTES", 3)
    async with client([], {"/repos/o/r/archive/v1.tar.gz": httpx.Response(200, content=b"12345")}) as api:
        assert await api.tarball("o/r", "v1") is None


async def test_ref_cannot_escape_the_archive_path() -> None:
    seen: list[httpx.Request] = []
    async with client(seen) as api:
        with pytest.raises(GitHubError):
            await api.tarball("o/r", "../../admin")
    assert b"..%2F..%2Fadmin.tar.gz" in seen[0].url.raw_path


async def test_a_redirect_off_the_host_is_refused_and_keeps_the_token() -> None:
    seen: list[httpx.Request] = []
    moved = httpx.Response(302, headers={"location": "https://evil.example/a.tar.gz"})
    async with client(seen, {"/repos/o/r/archive/v1.tar.gz": moved}, forgejo_token="fj-secret") as api:
        with pytest.raises(GitHubError, match="redirect"):
            await api.tarball("o/r", "v1")
    assert [request.url.host for request in seen] == ["codeberg.org"]
    assert seen[0].headers["authorization"] == "token fj-secret"


async def test_a_redirect_on_the_same_host_is_followed() -> None:
    from tests.evidence_helpers import tarball

    archive = tarball({"a.py": "x\n"})
    moved = httpx.Response(302, headers={"location": "https://codeberg.org/attachments/a.tar.gz"})
    overrides = {"/repos/o/r/archive/v1.tar.gz": moved, "/attachments/a.tar.gz": httpx.Response(200, content=archive)}
    async with client([], overrides) as api:
        assert await api.tarball("o/r", "v1") == archive


async def test_writes_are_refused() -> None:
    seen: list[httpx.Request] = []
    async with client(seen) as api:
        for call in (api.post("/x", {}), api.patch("/x", {}), api.delete("/x")):
            with pytest.raises(GitHubError, match="post=False"):
                await call
    assert seen == []


def test_token_never_goes_to_an_unlisted_host() -> None:
    with pytest.raises(ForgejoHostRefused):
        ForgejoClient(make_settings(forgejo_token="fj-secret"), "evil.example")


def test_routing() -> None:
    settings = make_settings(forgejo_hosts=["codeberg.org", "git.example.org"])
    assert forge.parse_any_url("https://github.com/o/r/pull/3", settings) == ("github", Target("o", "r", 3))
    assert forge.parse_any_url("https://codeberg.org/o/r/pulls/3", settings) == ("codeberg.org", Target("o", "r", 3))
    assert forge.parse_any_url("https://git.example.org/o/r/issues/4#x", settings)[0] == "git.example.org"


def test_routing_refuses_what_it_should() -> None:
    settings = make_settings()
    with pytest.raises(ForgejoHostRefused):
        forge.parse_any_url("https://evil.example/o/r/issues/1", settings)
    with pytest.raises(ValueError):
        forge.parse_any_url("https://codeberg.org/../r/issues/1", settings)
    with pytest.raises(ValueError):
        forge.parse_any_url("http://codeberg.org/o/r/issues/1", settings)
    with pytest.raises(ValueError):
        forge.parse_any_url("not a url", settings)


async def test_open_client_picks_by_forge() -> None:
    settings = make_settings()
    async with forge.open_client(settings, "github") as github, forge.open_client(settings, "codeberg.org") as forgejo:
        assert isinstance(github, GitHubClient) and isinstance(forgejo, ForgejoClient)


def test_forgejo_client_has_the_read_surface_of_the_pipeline() -> None:
    names = [name for name in vars(forge.ForgeClient) if not name.startswith("_")]
    assert names and all(callable(getattr(ForgejoClient, name)) for name in names)
    assert all(callable(getattr(GitHubClient, name)) for name in names)
