import json
from typing import Any

import httpx
import pytest

from tests.conftest import make_settings
from themis.intake.github import (
    MAX_DIFF_CHARS,
    GitHubClient,
    GitHubError,
    GitHubRateLimited,
    Target,
    from_issue_payload,
    is_first_time,
    parse_url,
)

ISSUE = {"title": "t", "body": "b", "user": {"id": 42, "login": "octocat"}, "author_association": "NONE"}
PULL = {**ISSUE, "head": {"sha": "abc"}, "pull_request": {}}
FILES = [{"filename": "a.py", "status": "modified", "additions": 2, "deletions": 1, "patch": "@@ x"}]


def routes(table: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=table[request.url.path])

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "url,number",
    [
        ("https://github.com/a/b/issues/7", 7),
        ("https://github.com/a/b.c/pull/8/files", 8),
        ("https://github.com/a/b/pull/9#discussion_r1", 9),
    ],
)
def test_parse_url(url: str, number: int) -> None:
    assert parse_url(url).number == number


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b/issues/1", "https://github.com/a/b", "nonsense"])
def test_parse_url_rejects_others(url: str) -> None:
    with pytest.raises(ValueError):
        parse_url(url)


def test_first_time_flag() -> None:
    assert is_first_time("FIRST_TIME_CONTRIBUTOR") and is_first_time("NONE")
    assert not is_first_time("MEMBER") and not is_first_time(None)


def test_submission_has_the_numeric_id_and_no_login() -> None:
    item = from_issue_payload(Target("a", "b", 1), ISSUE)
    assert item.contributor_key == "gh:42" and "octocat" not in json.dumps(item.model_dump())


def test_a_payload_without_a_numeric_id_is_an_error() -> None:
    with pytest.raises(GitHubError):
        from_issue_payload(Target("a", "b", 1), {"title": "t", "user": {"login": "x"}})


def test_null_body_becomes_empty() -> None:
    assert from_issue_payload(Target("a", "b", 1), {**ISSUE, "body": None}).body == ""


async def test_fetch_issue_with_comments() -> None:
    table = {"/repos/a/b/issues/1": ISSUE, "/repos/a/b/issues/1/comments": [{"body": "hello"}, {"body": None}]}
    async with GitHubClient(make_settings(), transport=routes(table)) as github:
        item = await github.fetch(Target("a", "b", 1))
    assert not item.is_pull_request and item.comments == ["hello", ""]


async def test_an_issues_url_can_name_a_pull_request() -> None:
    table = {
        "/repos/a/b/issues/2": PULL,
        "/repos/a/b/issues/2/comments": [],
        "/repos/a/b/pulls/2": PULL,
        "/repos/a/b/pulls/2/files": FILES,
    }
    async with GitHubClient(make_settings(), transport=routes(table)) as github:
        item = await github.fetch(parse_url("https://github.com/a/b/issues/2"))
    assert item.is_pull_request and item.head_sha == "abc" and item.changed_files == ["a.py"]
    assert item.diff_summary == "a.py (modified +2 -1)" and "@@ x" in item.diff_excerpt


async def test_large_diff_is_capped() -> None:
    big = [{"filename": f"{n}.py", "patch": "x" * 9000} for n in range(4)]
    table = {
        "/repos/a/b/issues/2": PULL,
        "/repos/a/b/issues/2/comments": [],
        "/repos/a/b/pulls/2": PULL,
        "/repos/a/b/pulls/2/files": big,
    }
    async with GitHubClient(make_settings(), transport=routes(table)) as github:
        item = await github.fetch(Target("a", "b", 2))
    assert len(item.diff_excerpt) <= MAX_DIFF_CHARS and len(item.changed_files) == 4


async def test_pagination_follows_next_links_and_stops_at_the_cap() -> None:
    seen: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1"):
            return httpx.Response(200, json=ISSUE)
        page = int(request.url.params["page"])
        seen.append(page)
        headers = {"Link": f'<https://api.github.com/x?page={page + 1}>; rel="next"'}
        return httpx.Response(200, json=[{"body": "c"}] * 100, headers=headers)

    async with GitHubClient(make_settings(), transport=httpx.MockTransport(handle)) as github:
        item = await github.fetch(Target("a", "b", 1))
    assert seen == [1] and len(item.comments) == 30


async def test_token_is_sent_when_configured() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=ISSUE if request.url.path.endswith("/1") else [])

    settings = make_settings(github_token="ghp_exampletoken0000")
    async with GitHubClient(settings, transport=httpx.MockTransport(handle)) as github:
        await github.fetch(Target("a", "b", 1))
    assert captured[0].headers["authorization"] == "Bearer ghp_exampletoken0000"


async def test_rate_limit_and_not_found_differ() -> None:
    limited = httpx.MockTransport(lambda _: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    async with GitHubClient(make_settings(), transport=limited) as github:
        with pytest.raises(GitHubRateLimited):
            await github.fetch(Target("a", "b", 1))
    missing = httpx.MockTransport(lambda _: httpx.Response(404))
    async with GitHubClient(make_settings(), transport=missing) as github:
        with pytest.raises(GitHubError) as caught:
            await github.fetch(Target("a", "b", 1))
    assert caught.value.status == 404


def test_labels_reach_the_submission() -> None:
    issue = {"title": "t", "user": {"id": 1}, "labels": [{"name": "security"}, {"color": "red"}, "bad"]}
    assert from_issue_payload(Target("a", "b", 1), issue).labels == ["security"]
