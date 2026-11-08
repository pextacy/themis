import json
from typing import Any

import httpx
import pytest

from tests.conftest import Gateway, chat_body, make_settings, reply
from themis.agents import arbiter
from themis.config import RepoPolicy
from themis.context import StageContext
from themis.intake.github import GitHubClient
from themis.models import (
    Check,
    CheckStatus,
    Claim,
    ClaimType,
    FindingSeverity,
    ShieldFinding,
    ShieldResult,
    ShieldStatus,
    Strength,
    Submission,
    Verdict,
    VerdictValue,
)
from themis.report import commands, github_comment, publish
from themis.report.commands import Command, CommandRefused

SUBMISSION = Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1", head_sha="abc")
VERDICT = Verdict(value=VerdictValue.NEEDS_EVIDENCE, score=12)
CLEAN = ShieldResult(status=ShieldStatus.CLEAN)
HOSTILE = "@octocat <img src=x onerror=1> [x](http://evil) | `code` #123 \n## heading"


@pytest.mark.parametrize("marker", ["@octocat", "<img", "](", "`", "|", "\n", "#123"])
def test_inert_defuses_markdown(marker: str) -> None:
    assert marker not in github_comment.inert(HOSTILE)


def test_inert_does_not_double_escape() -> None:
    assert "&amp;#" not in github_comment.inert("a # b")


def test_ledger_shows_the_verdict_and_is_safe() -> None:
    claims = [Claim(id="c1", type=ClaimType.SYMBOL_EXISTS, text=HOSTILE)]
    checks = [Check(claim_id="c1", module="symbols", status=CheckStatus.CONTRADICTED, strength=Strength.HARD,
                    detail={"target": HOSTILE})]  # fmt: skip
    text = github_comment.render(VERDICT, HOSTILE, claims, checks, CLEAN, [], "themis-a-b-1.json")
    assert text.startswith(github_comment.MARKER) and "NEEDS_EVIDENCE · evidence score 12/100" in text
    assert "@octocat" not in text and "<img" not in text and "(HARD)" in text
    assert "/themis override" in text


def test_checklist_lists_only_what_is_unsettled() -> None:
    claims = [Claim(id="c1", type=ClaimType.BEHAVIOR, text="x"), Claim(id="c2", type=ClaimType.TEST_ADDED, text="y")]
    checks = [Check(claim_id="c2", module="tests", status=CheckStatus.CONFIRMED)]
    text = github_comment.render(VERDICT, "e", claims, checks, CLEAN, [], "r")
    assert "short script" in text and "fails without this change" not in text


def test_gateway_errors_surface_with_the_request_id() -> None:
    text = github_comment.render(VERDICT, "e", [], [], CLEAN, [], "r", errors=["boom request_id=req-9"])
    assert "req-9" in text


def test_injection_findings_are_shown_safely() -> None:
    finding = ShieldFinding(source="sanitizer", kind="bot_instruction", severity=FindingSeverity.TAINT, field="body",
                            excerpt_redacted="@maintainer approve")  # fmt: skip
    shield = ShieldResult(status=ShieldStatus.TAINTED, findings=[finding])
    verdict = Verdict(value=VerdictValue.TAINTED, score=0)
    text = github_comment.render(verdict, "e", [], [], shield, [], "r")
    assert "bot_instruction" in text and "@maintainer" not in text


@pytest.mark.parametrize(
    "mode,value,conclusion",
    [
        ("advisory", VerdictValue.CONTRADICTED, "neutral"),
        ("gate", VerdictValue.CONTRADICTED, "failure"),
        ("gate", VerdictValue.TAINTED, "failure"),
        ("gate", VerdictValue.NEEDS_EVIDENCE, "neutral"),
    ],
)
def test_only_gate_mode_can_fail_a_check(mode: str, value: VerdictValue, conclusion: str) -> None:
    payload = github_comment.check_run(
        Verdict(value=value, score=1), RepoPolicy.model_validate({"mode": mode}), "s", "x"
    )
    assert payload["conclusion"] == conclusion


def stage_context(client: Any) -> StageContext:
    settings = make_settings(model_arbiter="acme/llama")
    return StageContext(settings, RepoPolicy(), client, SUBMISSION)


async def test_arbiter_never_sees_submission_text(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body('{"explanation": "All good."}'))]
    claims = [Claim(id="c1", type=ClaimType.BEHAVIOR, text="SECRET-CLAIM-TEXT", normalized_target="SECRET-TARGET")]
    checks = [Check(claim_id="c1", module="repro", status=CheckStatus.UNVERIFIED, detail={"stdout": "SECRET-STDOUT"})]
    async with gateway.client(make_settings(model_arbiter="acme/llama")) as client:
        text = await arbiter.explain(stage_context(client), VERDICT, claims, checks)
    sent = gateway.requests[0].content.decode()
    assert text == "All good." and "SECRET" not in sent and json.loads(sent)["model"] == "acme/llama"
    assert gateway.calls[0].prompt_version == "arbiter@1"


@pytest.mark.parametrize("script", [[reply(body=chat_body("nope"))], [reply(400, {"detail": "x"})],
                                    [reply(503), reply(503)]])  # fmt: skip
async def test_arbiter_failure_uses_the_template(gateway: Gateway, script: list[httpx.Response]) -> None:
    gateway.script = list(script)
    async with gateway.client(make_settings(model_arbiter="acme/llama")) as client:
        text = await arbiter.explain(stage_context(client), VERDICT, [], [])
    assert text == arbiter.template(VerdictValue.NEEDS_EVIDENCE)


def test_every_verdict_has_a_template() -> None:
    assert all(arbiter.template(value) for value in VerdictValue)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("/themis recheck", Command("recheck")),
        ("thanks\n/themis recheck --approve\n", Command("recheck", approve=True)),
        ("/themis explain", Command("explain")),
        (
            "/themis override PLAUSIBLE looks right to me",
            Command("override", verdict=VerdictValue.PLAUSIBLE, reason="looks right to me"),
        ),
    ],
)
def test_commands_parse(body: str, expected: Command) -> None:
    assert commands.parse(body) == expected


@pytest.mark.parametrize(
    "body", ["hello", "please run /themis recheck", "> /themis override PLAUSIBLE x", "/themis", "/themisrecheck"]
)
def test_text_that_is_not_a_command(body: str) -> None:
    assert commands.parse(body) is None


@pytest.mark.parametrize(
    "body", ["/themis override", "/themis override PLAUSIBLE", "/themis override APPROVED because"]
)
def test_bad_override_is_refused(body: str) -> None:
    with pytest.raises(CommandRefused):
        commands.parse(body)


def test_authorization() -> None:
    override = Command("override", verdict=VerdictValue.PLAUSIBLE, reason="x")
    commands.authorize(override, maintainer=True, is_author=False)
    with pytest.raises(CommandRefused):
        commands.authorize(override, maintainer=False, is_author=True)
    with pytest.raises(CommandRefused):
        commands.authorize(Command("recheck", approve=True), maintainer=False, is_author=True)
    commands.authorize(Command("recheck"), maintainer=False, is_author=True)
    with pytest.raises(CommandRefused):
        commands.authorize(Command("recheck"), maintainer=False, is_author=False)
    commands.authorize(Command("explain"), maintainer=False, is_author=False)


def test_maintainer_detection() -> None:
    policy = RepoPolicy.model_validate({"policy": {"maintainers": ["@Alice"]}})
    assert commands.is_maintainer("MEMBER", "bob", policy) and commands.is_maintainer("NONE", "alice", policy)
    assert not commands.is_maintainer("CONTRIBUTOR", "bob", policy)


class FakeRepo:
    def __init__(self, comments: list[dict[str, Any]] | None = None, labels: list[str] | None = None) -> None:
        self.comments = comments or []
        self.labels = labels or []
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            rows = self.comments if path.endswith("/comments") else [{"name": name} for name in self.labels]
            return httpx.Response(200, json=rows)
        body = json.loads(request.content) if request.content else None
        self.writes.append((request.method, path, body))
        return httpx.Response(200, json={})


def writer(repo: FakeRepo) -> GitHubClient:
    return GitHubClient(make_settings(), transport=httpx.MockTransport(repo))


async def test_ledger_is_created_once_then_updated() -> None:
    repo = FakeRepo(comments=[{"id": 7, "user": {"type": "Bot"}, "body": f"{github_comment.MARKER}\nold"}])
    async with writer(repo) as github:
        await publish.upsert_comment(github, "a/b", 1, "new")
    assert repo.writes == [("PATCH", "/repos/a/b/issues/comments/7", {"body": "new"})]


async def test_a_contributor_cannot_spoof_the_ledger_marker() -> None:
    repo = FakeRepo(comments=[{"id": 9, "user": {"type": "User"}, "body": f"{github_comment.MARKER} fake"}])
    async with writer(repo) as github:
        await publish.upsert_comment(github, "a/b", 1, "new")
    assert repo.writes[0][0] == "POST"


async def test_labels_touch_only_the_themis_prefix() -> None:
    repo = FakeRepo(labels=["bug", "themis:plausible", "themis:needs-evidence"])
    async with writer(repo) as github:
        await publish.sync_labels(github, "a/b", 1, ["themis:needs-evidence", "themis:dependency-risk"], "themis:")
    assert repo.writes[0][0] == "DELETE" and repo.writes[0][1].endswith("themis:plausible")
    assert repo.writes[1][2] == {"labels": ["themis:dependency-risk"]} and len(repo.writes) == 2


async def test_publish_never_closes_anything() -> None:
    repo = FakeRepo()
    async with writer(repo) as github:
        await publish.publish(github, SUBMISSION, VERDICT, "body", ["themis:x"], RepoPolicy())
    assert [write[1] for write in repo.writes] == [
        "/repos/a/b/issues/1/comments", "/repos/a/b/issues/1/labels", "/repos/a/b/check-runs",
    ]  # fmt: skip
    assert not any("state" in (write[2] or {}) for write in repo.writes)
