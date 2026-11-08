import json
from typing import Any

import pytest

from tests.conftest import Gateway, chat_body, make_settings, reply
from themis.agents import clerk
from themis.config import RepoPolicy
from themis.context import StageContext
from themis.models import ClaimType, OtariCall, Role, ShieldResult, ShieldStatus, Submission, SubmissionKind
from themis.otari.client import OtariClient, OtariResult
from themis.otari.errors import OtariBudgetExceeded, OtariConfigError
from themis.shield import sanitize, stage

BODY = "In `tinyhttp/ranges.py`, `parse_range` returns one byte too many. See CVE-2021-44228. Tests are in   place."
ITEM: dict[str, Any] = {"id": "a-b-1", "repo": "a/b", "number": 1, "title": "Bug", "contributor_key": "gh:7"}


def output(*claims: dict[str, str], kind: str = "bug_report") -> str:
    return json.dumps({"kind": kind, "claims": list(claims)})


def claim(kind: str, quote: str, text: str = "a claim") -> dict[str, str]:
    return {"type": kind, "text": text, "quote": quote}


def context(client: OtariClient, **overrides: Any) -> StageContext:
    item = Submission(**{**ITEM, "body": BODY, **overrides})
    view = stage.envelope(item, stage.clean_view(item, stage.scan_fields(item)))
    shield = ShieldResult(status=ShieldStatus.CLEAN, clean_view=view)
    return StageContext(make_settings(), RepoPolicy(), client, item, shield)


def test_prompt_has_a_version_and_no_submission_placeholder() -> None:
    version, system = clerk.load_prompt()
    assert version == "clerk@2" and "untrusted_submission" in system and "{" in system


async def test_valid_output_is_anchored_with_computed_spans(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body(output(claim("symbol_exists", "`parse_range` returns one byte"))))]
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    item = result.claims[0]
    start, end = item.span or (0, 0)
    assert f"Bug\n\n{BODY}"[start:end] == "`parse_range` returns one byte"
    assert (item.id, item.type, item.normalized_target) == ("c1", ClaimType.SYMBOL_EXISTS, "parse_range")
    assert result.kind is SubmissionKind.BUG_REPORT and result.otari_request_ids == ["req-1"]
    body = gateway.body()
    assert body["user"] == "gh:7" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][1]["content"].startswith("<untrusted_submission")
    assert BODY[:20] not in body["messages"][0]["content"]
    assert gateway.calls[0].prompt_version == "clerk@2" and gateway.calls[0].stage == "clerk"


async def test_cve_target_is_normalized(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body(output(claim("cve_reference", "CVE-2021-44228"))))]
    async with gateway.client() as client:
        assert (await clerk.run(context(client))).claims[0].normalized_target == "CVE-2021-44228"


async def test_a_claim_the_submission_does_not_contain_is_dropped(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body(output(
        claim("symbol_exists", "ConnectionPool leaks sockets"), claim("behavior", "returns one byte too many"),
    )))]  # fmt: skip
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    assert len(result.claims) == 1 and result.claims[0].id == "c1" and result.dropped_unanchored == 1


async def test_quote_matches_despite_whitespace_and_escaping(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body(output(claim("behavior", "a &lt; b  holds"))))]
    async with gateway.client() as client:
        result = await clerk.run(context(client, body="We see that a < b holds here."))
    assert len(result.claims) == 1


async def test_fenced_json_is_accepted_without_a_repair(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body("```json\n" + output() + "\n```"))]
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    assert not result.degraded and len(gateway.requests) == 1


async def test_one_repair_then_success(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body("not json")), reply(body=chat_body(output()))]
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    assert not result.degraded and len(gateway.requests) == 2
    assert gateway.body()["messages"][-1]["content"] == clerk.REPAIR


async def test_exactly_one_repair_then_degrades_to_no_claims(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body("nope")), reply(body=chat_body("still nope"))]
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    assert result.degraded and result.claims == [] and len(gateway.requests) == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"kind": "APPROVED", "claims": []}',
        '{"kind": "other", "claims": [{"type": "made_up", "text": "x", "quote": "y"}]}',
        '{"kind": "other"}',
        '{"kind": "other", "claims": [{"type": "other", "text": "x"}]}',
        "[]",
    ],
)
def test_schema_rejects(text: str) -> None:
    assert clerk.parse(text) is None


@pytest.mark.parametrize(
    "kind,is_pr,expected",
    [
        (SubmissionKind.BUGFIX_PR, False, SubmissionKind.OTHER),
        (SubmissionKind.BUG_REPORT, True, SubmissionKind.OTHER),
        (SubmissionKind.BUGFIX_PR, True, SubmissionKind.BUGFIX_PR),
        (SubmissionKind.QUESTION, False, SubmissionKind.QUESTION),
    ],
)
def test_the_model_cannot_contradict_the_envelope(kind: SubmissionKind, is_pr: bool, expected: SubmissionKind) -> None:
    assert clerk.coherent_kind(kind, is_pr) is expected


async def test_too_many_claims_counts_as_invalid(gateway: Gateway) -> None:
    many = output(*[claim("other", "x")] * (clerk.MAX_CLAIMS + 1))
    gateway.script = [reply(body=chat_body(many)), reply(body=chat_body(many))]
    async with gateway.client() as client:
        assert (await clerk.run(context(client))).degraded


async def test_gateway_failure_degrades_and_keeps_the_request_id(gateway: Gateway) -> None:
    gateway.script = [reply(503), reply(503)]
    async with gateway.client() as client:
        result = await clerk.run(context(client))
    assert result.degraded and "req-1" in (result.reason or "") and result.otari_request_ids == ["req-1"]


async def test_budget_refusal_and_rejected_key_propagate(gateway: Gateway) -> None:
    gateway.script = [reply(403, {"code": "budget_exceeded"}), reply(401, {"detail": "bad"})]
    async with gateway.client() as client:
        with pytest.raises(OtariBudgetExceeded):
            await clerk.run(context(client))
        with pytest.raises(OtariConfigError):
            await clerk.run(context(client))


@pytest.mark.parametrize("status", [ShieldStatus.TAINTED, ShieldStatus.TAINTED_UNCHECKED, ShieldStatus.DEFERRED])
async def test_clerk_refuses_anything_the_shield_did_not_pass(gateway: Gateway, status: ShieldStatus) -> None:
    async with gateway.client() as client:
        ctx = context(client)
        ctx.shield = ShieldResult(status=status)
        with pytest.raises(clerk.ClerkSkipped):
            await clerk.run(ctx)
        ctx.shield = None
        with pytest.raises(clerk.ClerkSkipped):
            await clerk.run(ctx)
    assert gateway.requests == []


def test_quote_can_be_found_past_a_hidden_comment() -> None:
    text = sanitize.scan("Bug\n\nalpha <!-- note --> beta", "submission")
    assert text.locate("alpha beta") is not None


@pytest.mark.parametrize("content,finish,expected", [
    ("", "length", "empty reply, finish_reason=length"),
    ('{"kind": "bug_re', "length", "not JSON, finish_reason=length"),
    ('{"kind": "bug_report", "claims": [{"type": "IGNORE ALL RULES", "text": "t", "quote": "q"}]}', "stop",
     "claims.0.type: enum, finish_reason=stop"),
])  # fmt: skip
def test_problem_names_the_cause_without_repeating_model_text(content: str, finish: str, expected: str) -> None:
    call = OtariCall(
        request_id=None, role=Role.CLERK, endpoint="chat/completions", status=200, outcome="ok", latency_ms=1
    )
    assert clerk.problem(OtariResult(content=content, finish_reason=finish, call=call)) == expected
