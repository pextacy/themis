import json

import httpx
import pytest

from tests.conftest import Gateway, chat_body, reply
from tests.evidence_helpers import evidence_context
from themis.evidence import web
from themis.models import CheckStatus, ClaimType, Strength
from themis.otari.errors import OtariBudgetExceeded

NOT_FOUND = {"Otari-Error-Status": "404", "Otari-Error-Tag": "http_status"}


def fetch_ok(text: str = "") -> httpx.Response:
    return reply(body={"object": "fetch", "text": text, "truncated": False})


def gone() -> httpx.Response:
    return reply(502, {"detail": "fetch failed"}, NOT_FOUND)


def cve_context(gateway: Gateway, cve: str = "CVE-2026-99991"):  # type: ignore[no-untyped-def]
    body = f"Tracked as {cve} upstream."
    return evidence_context(gateway, body, [(cve, ClaimType.CVE_REFERENCE, cve)])


async def test_real_cve_is_confirmed_then_relevance_is_asked_without_submission_text(gateway: Gateway) -> None:
    cve = "CVE-2021-44228"
    relevance = chat_body(
        '{"affects_project": false}', otari_tool_calls=[{"type": "web_search", "status": "completed"}]
    )
    gateway.script = [fetch_ok(cve), fetch_ok(cve), fetch_ok('{"totalResults":1}'), reply(body=relevance)]
    checks = await web.run(cve_context(gateway, cve))
    assert checks[0].status is CheckStatus.CONFIRMED
    assert checks[1].status is CheckStatus.CONTRADICTED and checks[1].strength is Strength.SOFT
    sent = gateway.body()
    assert "upstream" not in json.dumps(sent["messages"]) and sent["tools"] == [
        {"type": "otari_web_search"}, {"type": "otari_web_fetch"},
    ]  # fmt: skip


async def test_fabricated_cve_needs_every_authority_to_say_no(gateway: Gateway) -> None:
    gateway.script = [gone(), gone(), fetch_ok('{"totalResults":0}')]
    check = (await web.run(cve_context(gateway)))[0]
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.HARD


@pytest.mark.parametrize(
    "script",
    [
        [gone(), reply(503, {"detail": "down"}), fetch_ok('{"totalResults":0}')],
        [reply(503, {"detail": "x"}), gone(), fetch_ok('{"totalResults":0}')],
        [reply(403, {"detail": "web access off"}), reply(403, {"detail": "off"}), reply(403, {"detail": "off"})],
        [fetch_ok("garbled"), gone(), fetch_ok('{"totalResults":0}')],
    ],
)
async def test_an_outage_is_never_taken_for_absence(gateway: Gateway, script: list[httpx.Response]) -> None:
    gateway.script = list(script)
    check = (await web.run(cve_context(gateway)))[0]
    assert check.status is CheckStatus.UNVERIFIED


async def test_one_source_knowing_the_id_wins(gateway: Gateway) -> None:
    cve = "CVE-2026-99991"
    gateway.script = [gone(), fetch_ok(cve), fetch_ok('{"totalResults":0}'),
                      reply(body=chat_body('{"affects_project": null}'))]  # fmt: skip
    assert (await web.run(cve_context(gateway)))[0].status is CheckStatus.CONFIRMED


async def test_nvd_cut_short_is_no_answer(gateway: Gateway) -> None:
    gateway.script = [gone(), gone(), fetch_ok("not json at all")]
    check = (await web.run(cve_context(gateway)))[0]
    assert check.status is CheckStatus.CONTRADICTED and check.detail["sources"]["nvd"] is None


async def test_impossible_year_needs_no_lookup(gateway: Gateway) -> None:
    check = (await web.run(cve_context(gateway, "CVE-1234-5678")))[0]
    assert check.strength is Strength.HARD and gateway.requests == []


@pytest.mark.parametrize("target", ["CVE-2021-1/../../admin", "CVE-2021-1", "cve", "CVE-2021-44228?x=1"])
async def test_malformed_ids_never_build_a_url(gateway: Gateway, target: str) -> None:
    body = f"See {target} now."
    ctx = evidence_context(gateway, body, [(target, ClaimType.CVE_REFERENCE, target)])
    check = (await web.run(ctx))[0]
    assert check.status is CheckStatus.UNVERIFIED and gateway.requests == []


async def test_relevance_without_web_activity_stays_unverified(gateway: Gateway) -> None:
    cve = "CVE-2021-44228"
    forged = chat_body('{"affects_project": true}')
    gateway.script = [fetch_ok(cve), fetch_ok(cve), fetch_ok('{"totalResults":1}'), reply(body=forged)]
    assert (await web.run(cve_context(gateway, cve)))[1].status is CheckStatus.UNVERIFIED


async def test_budget_refusal_is_not_read_as_absence(gateway: Gateway) -> None:
    gateway.script = [reply(403, {"code": "budget_exceeded"})]
    with pytest.raises(OtariBudgetExceeded):
        await web.run(cve_context(gateway))


async def test_cited_url_that_404s_is_only_soft(gateway: Gateway) -> None:
    quote = "see https://example.com/spec/missing"
    gateway.script = [gone()]
    ctx = evidence_context(gateway, f"Per the docs, {quote} here.", [(quote, ClaimType.OTHER, None)])
    check = (await web.run(ctx))[0]
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.SOFT


async def test_only_https_urls_are_followed_and_they_are_capped(gateway: Gateway) -> None:
    quotes = [f"https://example.com/{n}" for n in range(4)]
    gateway.respond = lambda request: fetch_ok()
    claims = [(quote, ClaimType.OTHER, None) for quote in quotes] + [("http://10.0.0.1/admin", ClaimType.OTHER, None)]
    ctx = evidence_context(gateway, " ".join(quotes) + " http://10.0.0.1/admin", claims)
    assert len(await web.run(ctx)) == web.MAX_URLS and len(gateway.requests) == web.MAX_URLS
