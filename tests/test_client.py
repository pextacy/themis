import json

import httpx
import pytest

from tests.conftest import API_KEY, Gateway, chat_body, make_settings, reply, sse
from themis.config import ConfigError
from themis.models import Role
from themis.otari import errors
from themis.otari.mcp import MCP_BETA, McpServer

USER = [{"role": "user", "content": "hi"}]
SERVER = [McpServer(name="deepwiki", url="https://mcp.example/mcp")]


async def test_chat_shape_and_receipt_fields(gateway: Gateway) -> None:
    headers = {
        "Otari-Provider": "deepinfra", "Otari-Model": "served-model", "Otari-Fallback": "true",
        "Otari-Attempt-Count": "2", "Set-Cookie": "session=abc",
    }  # fmt: skip
    gateway.script = [reply(body=chat_body("hello"), headers=headers)]
    async with gateway.client() as client:
        result = await client.chat(Role.CLERK, USER, user="gh:42", conversation_id="themis-1", prompt_version="clerk@1")
    request = gateway.requests[0]
    body = gateway.body()
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["otari-conversation-id"] == "themis-1"
    assert str(request.url) == "https://api.otari.ai/api/v1/chat/completions"
    assert body["model"] == "acme/qwen-small" and body["user"] == "gh:42"
    call = result.call
    assert (call.request_id, call.provider, call.effective_model) == ("req-1", "deepinfra", "served-model")
    assert (call.fallback, call.attempt_count, call.cost_usd) == (True, 2, 0.00012)
    assert (call.usage.prompt, call.usage.completion) == (10, 2)
    assert call.prompt_version == "clerk@1" and result.content == "hello"
    assert "set-cookie" not in result.raw_headers_redacted


async def test_unset_options_are_not_sent(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body())]
    async with gateway.client() as client:
        await client.chat(Role.CLERK, USER)
    assert not {"user", "tools", "guardrails", "response_format"} & gateway.body().keys()


async def test_missing_cost_stays_none(gateway: Gateway) -> None:
    body = chat_body()
    del body["usage"]["cost_usd"]
    gateway.script = [reply(body=body)]
    async with gateway.client() as client:
        assert (await client.chat(Role.CLERK, USER)).call.cost_usd is None


async def test_a_username_never_reaches_the_gateway(gateway: Gateway) -> None:
    async with gateway.client() as client:
        with pytest.raises(ValueError):
            await client.chat(Role.CLERK, USER, user="octocat")
    assert gateway.requests == []


async def test_unconfigured_role_is_a_config_error(gateway: Gateway) -> None:
    async with gateway.client() as client:
        with pytest.raises(ConfigError, match="THEMIS_MODEL_ARBITER"):
            await client.chat(Role.ARBITER, USER)


def test_missing_key_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        Gateway().client(make_settings(otari_api_key=None))


async def test_json_output_parsed_only_when_asked_and_bad_json_is_none(gateway: Gateway) -> None:
    gateway.script = [reply(body=chat_body('{"a": 1}')), reply(body=chat_body("not json"))]
    async with gateway.client() as client:
        first = await client.chat(Role.CLERK, USER, response_format={"type": "json_object"})
        second = await client.chat(Role.CLERK, USER, response_format={"type": "json_object"})
    assert first.parsed == {"a": 1} and second.parsed is None


async def test_tool_calls_come_from_the_gateway_report(gateway: Gateway) -> None:
    run = {"id": "otari_ci_1", "type": "code_execution", "status": "completed", "stdout": "7\n", "exit_code": 0}
    gateway.script = [reply(body=chat_body("the answer is 8", otari_tool_calls=[run]))]
    async with gateway.client() as client:
        result = await client.chat(Role.PROVER, USER, model="m")
    assert result.tool_calls[0].stdout == "7\n"


async def test_guardrail_verdicts_and_bad_header(gateway: Gateway) -> None:
    good = json.dumps([{"profile": "p", "status": "evaluated", "valid": True}])
    gateway.script = [
        reply(body=chat_body(), headers={"Otari-Guardrails": good}),
        reply(body=chat_body(), headers={"Otari-Guardrails": "{broken"}),
    ]
    async with gateway.client() as client:
        assert (await client.chat(Role.CLERK, USER)).guardrail_verdicts[0]["valid"] is True
        assert (await client.chat(Role.CLERK, USER)).guardrail_verdicts == []


async def test_429_honours_retry_after_and_keeps_the_idempotency_key(gateway: Gateway) -> None:
    gateway.script = [
        reply(429, {"code": "rate_limited"}, {"Retry-After": "2"}),
        reply(429, {"code": "rate_limited"}, {"Retry-After": "3"}),
        reply(body=chat_body()),
    ]
    async with gateway.client() as client:
        await client.chat(Role.CLERK, USER)
    assert gateway.sleeps == [2.0, 3.0]
    assert len({request.headers["idempotency-key"] for request in gateway.requests}) == 1


async def test_429_gives_up_after_two_retries(gateway: Gateway) -> None:
    gateway.script = [reply(429, {"code": "rate_limited"}, {"Retry-After": "1"}) for _ in range(3)]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariRateLimited):
            await client.chat(Role.CLERK, USER)
    assert len(gateway.requests) == 3


async def test_a_long_retry_after_is_not_slept_through(gateway: Gateway) -> None:
    gateway.script = [reply(429, {"code": "rate_limited"}, {"Retry-After": "3600"})]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariRateLimited):
            await client.chat(Role.CLERK, USER)
    assert gateway.sleeps == []


async def test_5xx_is_retried_once(gateway: Gateway) -> None:
    gateway.script = [reply(503), reply(503)]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError):
            await client.chat(Role.CLERK, USER)
    assert len(gateway.requests) == 2


async def test_5xx_recovers_on_the_retry(gateway: Gateway) -> None:
    gateway.script = [reply(500), reply(body=chat_body("back"))]
    async with gateway.client() as client:
        assert (await client.chat(Role.CLERK, USER)).content == "back"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 424])
async def test_4xx_is_never_retried(gateway: Gateway, status: int) -> None:
    gateway.script = [reply(status, {"detail": "no"})]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariError):
            await client.chat(Role.CLERK, USER)
    assert len(gateway.requests) == 1 and gateway.sleeps == []


async def test_budget_refusal_is_not_retried_and_keeps_request_id(gateway: Gateway) -> None:
    gateway.script = [reply(403, {"code": "budget_exceeded"}, {"Retry-After": "5", "Otari-Budget-Scope": "user"})]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariBudgetExceeded) as caught:
            await client.chat(Role.CLERK, USER, user="gh:1")
    assert caught.value.request_id == "req-1" and len(gateway.requests) == 1
    assert gateway.calls[0].outcome == "budget_exceeded"


async def test_guarded_502_fails_closed_without_a_retry(gateway: Gateway) -> None:
    gateway.script = [reply(502, {"detail": "guardrail down"})]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariGuardrailUnavailable):
            await client.chat(Role.CLERK, USER, guardrails=[{"profile": "p", "mode": "block"}])
    assert len(gateway.requests) == 1


async def test_timeout_retries_then_surfaces(gateway: Gateway) -> None:
    gateway.script = [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError) as caught:
            await client.chat(Role.CLERK, USER)
    assert caught.value.code == "timeout" and [c.outcome for c in gateway.calls] == ["timeout", "timeout"]


async def test_connection_failure_is_an_upstream_error(gateway: Gateway) -> None:
    gateway.script = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError):
            await client.chat(Role.CLERK, USER)


async def test_non_json_success_is_surfaced(gateway: Gateway) -> None:
    gateway.script = [reply(body="<html>")]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError):
            await client.chat(Role.CLERK, USER)


async def test_code_execute_is_never_retried_because_each_run_is_billed(gateway: Gateway) -> None:
    gateway.script = [reply(503)]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError):
            await client.code_execute("print(1)")
    assert len(gateway.requests) == 1


async def test_catalog_reads(gateway: Gateway) -> None:
    gateway.script = [reply(body={"data": [{"id": "m"}]}), reply(body={"data": [{"id": "t"}]})]
    async with gateway.client() as client:
        assert (await client.list_models())[0]["id"] == "m"
        assert (await client.list_tools())[0]["id"] == "t"


def _stream() -> str:
    return sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "content_block": {"type": "mcp_tool_use", "name": "read_wiki",
                                                          "server_name": "deepwiki", "input": {"repo": "a/b"}}},
        {"type": "content_block_start", "content_block": {"type": "mcp_tool_result", "is_error": False,
                                                          "content": [{"type": "text", "text": "wiki"}]}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
        {"type": "message_delta", "usage": {"output_tokens": 4, "cost_usd": "0.000300"}},
    )  # fmt: skip


async def test_stream_declares_beta_and_names_user_in_metadata(gateway: Gateway) -> None:
    gateway.script = [reply(body=_stream())]
    async with gateway.client() as client:
        result = await client.messages_stream(Role.CLERK, USER, mcp_servers=SERVER, user="gh:7")
    request = gateway.requests[0]
    body = gateway.body()
    assert request.headers["anthropic-beta"] == MCP_BETA
    assert body["metadata"] == {"user_id": "gh:7"} and "user" not in body
    assert body["mcp_servers"][0]["name"] == "deepwiki" and body["stream"] is True
    assert "idempotency-key" not in request.headers
    assert result.content == "done" and result.mcp_calls[0].output == "wiki"
    assert (result.call.usage.prompt, result.call.usage.completion, result.call.cost_usd) == (9, 4, 0.0003)


async def test_stream_refusal_uses_the_anthropic_envelope(gateway: Gateway) -> None:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "mcp_servers refused"}}
    gateway.script = [reply(400, body)]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariSchemaRejected, match="mcp_servers refused"):
            await client.messages_stream(Role.CLERK, USER, mcp_servers=SERVER)
    assert len(gateway.requests) == 1


async def test_stream_retries_a_429_before_it_opens(gateway: Gateway) -> None:
    gateway.script = [reply(429, {"code": "rate_limited"}, {"Retry-After": "1"}), reply(body=_stream())]
    async with gateway.client() as client:
        assert (await client.messages_stream(Role.CLERK, USER, mcp_servers=SERVER)).content == "done"


@pytest.mark.parametrize(
    "failure,code", [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ReadError("cut"), "connection_error")]
)
async def test_broken_stream_is_a_gateway_error(gateway: Gateway, failure: Exception, code: str) -> None:
    gateway.script = [failure]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError) as caught:
            await client.messages_stream(Role.CLERK, USER, mcp_servers=SERVER)
    assert caught.value.code == code and gateway.calls[0].outcome == code


async def test_failure_inside_a_stream_is_not_retried(gateway: Gateway) -> None:
    gateway.script = [reply(body=sse({"type": "error", "error": {"message": "cut off"}, "code": "provider_error"}))]
    async with gateway.client() as client:
        with pytest.raises(errors.OtariUpstreamError, match="cut off") as caught:
            await client.messages_stream(Role.CLERK, USER, mcp_servers=SERVER)
    assert caught.value.request_id == "req-1" and len(gateway.requests) == 1


async def test_key_budget_route_missing_is_not_an_error(gateway: Gateway) -> None:
    gateway.script = [reply(404, {"detail": "Not Found"})]
    async with gateway.client() as client:
        assert await client.key_budget() is None


async def test_contributor_budget_named_only_for_contributor_calls(gateway: Gateway) -> None:
    settings = make_settings(otari_contributor_budget_id="everyday")
    gateway.script = [reply(body=chat_body()) for _ in range(3)]
    async with gateway.client(settings) as client:
        await client.chat(Role.CLERK, USER, user="gh:42")
        await client.chat(Role.CLERK, USER)
        await client.chat(Role.CLERK, USER, user="gh:42", end_user_budget="tiny")
    budgets = [request.headers.get("otari-end-user-budget") for request in gateway.requests]
    assert budgets == ["everyday", None, "tiny"]
