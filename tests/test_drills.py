"""Failure drills: each outage ends in an honest state, never a false verdict."""

import json

import httpx

from tests.conftest import Gateway, chat_body, make_settings, reply
from tests.test_pipeline import EVALUATED, POLICY, github_double, submission
from themis import pipeline
from themis.evidence.dependencies import RegistryClient
from themis.intake.github import GitHubClient, GitHubRateLimited
from themis.models import VerdictValue
from themis.report import publish

CLERK = {"kind": "bug_report", "claims": [{"type": "symbol_exists", "text": "x", "quote": "`Ghost.run`"}]}


async def drill(gateway: Gateway, respond, github: GitHubClient | None = None) -> pipeline.Outcome:  # type: ignore[no-untyped-def]
    gateway.respond = respond
    settings = make_settings(model_arbiter="acme/llama", model_prover="acme/deepseek")
    async with gateway.client(settings) as client, github or github_double() as repo, RegistryClient() as registry:
        return await pipeline.analyze(
            settings, POLICY, submission("`Ghost.run` crashes."), client, repo, registry, calls=gateway.calls
        )


def healthy(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return reply(body={"data": []})
    body = json.loads(request.content)
    if body.get("guardrails"):
        return reply(body=chat_body("OK"), headers={"Otari-Guardrails": EVALUATED})
    if body["messages"][0]["content"].startswith("You explain"):
        return reply(body=chat_body('{"explanation": "ok"}'))
    return reply(body=chat_body(json.dumps(CLERK)))


async def test_gateway_5xx_on_the_clerk_degrades_without_a_verdict_about_the_author(gateway: Gateway) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and not json.loads(request.content).get("guardrails"):
            return reply(503, {"detail": "down"})
        return healthy(request)

    outcome = await drill(gateway, respond)
    assert outcome.verdict.value is VerdictValue.NEEDS_EVIDENCE and "req-1" in outcome.ledger


async def test_guardrail_unavailable_stops_for_a_human(gateway: Gateway) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return reply(502, {"detail": "down"}) if request.method == "POST" else healthy(request)

    outcome = await drill(gateway, respond)
    assert outcome.verdict.value is VerdictValue.TAINTED_UNCHECKED and outcome.checks == []
    assert "clerk" not in {call.stage for call in outcome.calls}


async def test_budget_exhausted_defers(gateway: Gateway) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return reply(403, {"code": "budget_exceeded"}) if request.method == "POST" else healthy(request)

    assert (await drill(gateway, respond)).verdict.value is VerdictValue.DEFERRED


async def test_invalid_json_degrades_to_the_fallback_and_no_contradiction(gateway: Gateway) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and not json.loads(request.content).get("guardrails"):
            return reply(body=chat_body("not json"))
        return healthy(request)

    outcome = await drill(gateway, respond)
    assert outcome.verdict.value is VerdictValue.NEEDS_EVIDENCE
    assert outcome.claims.degraded and "invalid output" in str(outcome.claims.reason)
    assert all(check.strength is None for check in outcome.checks)


async def test_github_rate_limit_is_unverified_not_contradicted(gateway: Gateway) -> None:
    limited = httpx.MockTransport(lambda _: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    outcome = await drill(gateway, healthy, GitHubClient(make_settings(), transport=limited))
    assert outcome.verdict.value is VerdictValue.NEEDS_EVIDENCE and outcome.checks[0].strength is None


async def test_ledger_survives_a_reporting_failure() -> None:
    limited = httpx.MockTransport(lambda _: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    async with GitHubClient(make_settings(), transport=limited) as github:
        try:
            await publish.find_ledger(github, "a/b", 1)
        except GitHubRateLimited as error:
            assert error.status == 403
