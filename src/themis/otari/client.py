"""The only module that talks HTTP to Otari."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, Field

from themis.config import Settings
from themis.logs import get_logger
from themis.models import OtariCall, Role
from themis.otari import errors, mcp
from themis.otari.budgets import KeyBudget, check_contributor_key, end_user_headers
from themis.otari.mcp import McpCall, McpServer
from themis.otari.parsing import guardrail_verdicts_from, parse_cost, parse_json, to_usage
from themis.otari.tools import CodeResult, FetchedPage, GatewayToolCall

log = get_logger(__name__)

MAX_RATE_LIMIT_RETRIES = 2
MAX_UPSTREAM_RETRIES = 1
MAX_RETRY_WAIT_S = 30.0
DEFAULT_RETRY_WAIT_S = 1.0
KEPT_HEADERS = frozenset({
    "otari-request-id", "otari-provider", "otari-model", "otari-attempt-count", "otari-fallback",
    "otari-guardrails", "otari-end-user-budget", "otari-budget-scope", "otari-error-code",
    "otari-idempotent-replayed", "retry-after",
})  # fmt: skip


class OtariResult(BaseModel):
    content: str
    finish_reason: str | None = None
    parsed: Any = None
    tool_calls: list[GatewayToolCall] = Field(default_factory=list)
    guardrail_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    call: OtariCall
    raw_headers_redacted: dict[str, str] = Field(default_factory=dict)


class OtariStreamResult(BaseModel):
    content: str
    mcp_calls: list[McpCall] = Field(default_factory=list)
    call: OtariCall
    raw_headers_redacted: dict[str, str] = Field(default_factory=dict)


class OtariClient:
    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_call: Callable[[OtariCall], None] | None = None,
    ) -> None:
        self._settings = settings
        self._key = api_key or settings.api_key()
        self._sleep = sleep
        self._on_call = on_call
        self._limit = asyncio.Semaphore(settings.max_concurrency)
        self._http = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            timeout=settings.otari_timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        self.is_test_double = transport is not None

    async def __aenter__(self) -> OtariClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        role: Role,
        messages: list[dict[str, str]],
        *,
        user: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        tools: list[dict[str, str]] | None = None,
        response_format: dict[str, Any] | None = None,
        guardrails: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        end_user_budget: str | None = None,
        stage: str | None = None,
        prompt_version: str | None = None,
    ) -> OtariResult:
        target = model or self._settings.model_for(role)
        body: dict[str, Any] = {"model": target, "messages": messages, "max_tokens": max_tokens, "temperature": 0}
        if user is not None:
            body["user"] = check_contributor_key(user)
        optional = {"tools": tools, "response_format": response_format, "guardrails": guardrails}
        body.update({name: value for name, value in optional.items() if value})
        headers = self._headers(conversation_id, self._budget_for(user, end_user_budget))
        # One key across retries, so a lost response is replayed instead of billed twice.
        headers["Idempotency-Key"] = str(uuid.uuid4())

        def record(reply_headers: Mapping[str, str], status: int | None, outcome: str, ms: int, data: Any) -> OtariCall:
            usage = data.get("usage") if isinstance(data, dict) else None
            return self._record(role, stage, "chat/completions", target, prompt_version,
                                reply_headers, status, outcome, ms, usage or {})  # fmt: skip

        reply, data, call = await self._request("POST", "chat/completions", body, headers, record, bool(guardrails))
        content = str(data["choices"][0]["message"].get("content") or "") if data.get("choices") else ""
        kept = {name: value for name, value in reply.items() if name in KEPT_HEADERS}
        finish_reason = data["choices"][0].get("finish_reason") if data.get("choices") else None
        return OtariResult(
            content=content,
            finish_reason=finish_reason,
            parsed=parse_json(content) if response_format else None,
            tool_calls=[GatewayToolCall.model_validate(item) for item in data.get("otari_tool_calls") or []],
            guardrail_verdicts=guardrail_verdicts_from(reply),
            call=call,
            raw_headers_redacted=kept,
        )

    async def messages_stream(
        self,
        role: Role,
        messages: list[dict[str, str]],
        *,
        mcp_servers: list[McpServer],
        user: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        max_tool_iterations: int = 3,
        end_user_budget: str | None = None,
        stage: str | None = None,
        prompt_version: str | None = None,
    ) -> OtariStreamResult:
        target = model or self._settings.model_for(role)
        body: dict[str, Any] = {
            "model": target,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "max_tool_iterations": max_tool_iterations,
            "mcp_servers": [server.declaration() for server in mcp_servers],
        }
        if user is not None:
            # /messages names the end user here, not in a `user` field.
            body["metadata"] = {"user_id": check_contributor_key(user)}
        headers = self._headers(conversation_id, self._budget_for(user, end_user_budget))
        headers["anthropic-beta"] = mcp.MCP_BETA

        started = time.monotonic()
        lines, reply = await self._open_stream(body, headers, target, role, stage, prompt_version)
        events = mcp.parse_sse(lines)
        elapsed = round((time.monotonic() - started) * 1000)
        usage = mcp.stream_usage(events)
        failure = next((event for event in events if event.get("type") == "error"), None)
        outcome = "stream_error" if failure else "ok"
        call = self._record(role, stage, "messages", target, prompt_version, reply, 200, outcome, elapsed, usage)
        if failure:
            raise errors.OtariUpstreamError(
                errors.from_response(502, reply, failure).message,
                status=200,
                code=errors.error_code(reply, failure),
                request_id=call.request_id,
            )
        return OtariStreamResult(
            content=mcp.stream_text(events),
            mcp_calls=mcp.pair_activity(events),
            call=call,
            raw_headers_redacted={name: value for name, value in reply.items() if name in KEPT_HEADERS},
        )

    async def code_execute(self, code: str, *, user: str | None = None) -> tuple[CodeResult, OtariCall]:
        body: dict[str, Any] = {"code": code}
        if user is not None:
            body["user"] = check_contributor_key(user)

        def record(reply_headers: Mapping[str, str], status: int | None, outcome: str, ms: int, data: Any) -> OtariCall:
            return self._record(Role.PROVER, "code_execute", "code/execute", None, None,
                                reply_headers, status, outcome, ms, {})  # fmt: skip

        # Each run is billed, even a failed one, and the endpoint has no idempotency: never retry it.
        _, data, call = await self._request("POST", "code/execute", body, {}, record, False, retry=False)
        return CodeResult.model_validate(data), call

    async def fetch_url(self, url: str, *, max_chars: int = 4000, user: str | None = None) -> FetchedPage:
        """Read a page with no model involved. Raises for anything but a page or its own 404."""
        body: dict[str, Any] = {"url": url, "max_chars": max_chars}
        if user is not None:
            body["user"] = check_contributor_key(user)

        def record(reply_headers: Mapping[str, str], status: int | None, outcome: str, ms: int, data: Any) -> OtariCall:
            return self._record(Role.EVIDENCE, "fetch", "fetch", None, None, reply_headers, status, outcome, ms, {})

        try:
            reply, data, _ = await self._request("POST", "fetch", body, {}, record, False, retry=False)
        except errors.OtariUpstreamError as error:
            if error.page_status == 404:
                return FetchedPage(found=False, request_id=error.request_id)
            raise
        return FetchedPage(
            found=True,
            text=str(data.get("text") or ""),
            truncated=bool(data.get("truncated")),
            request_id=reply.get("otari-request-id"),
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return list((await self._get("models")).get("data", []))

    async def list_tools(self) -> list[dict[str, Any]]:
        return list((await self._get("tools")).get("data", []))

    async def guardrail_profiles(self) -> dict[str, Any]:
        return await self._get("tool-settings/guardrails/profiles")

    async def key_budget(self) -> KeyBudget | None:
        """None where the route is not deployed: documented, but 404 on hosted Otari in Nov 2026."""
        try:
            return KeyBudget.model_validate(await self._get("key/budget"))
        except errors.OtariSchemaRejected as error:
            if error.status == 404:
                return None
            raise

    def _budget_for(self, user: str | None, explicit: str | None) -> str | None:
        """A contributor's first request names the everyday budget; without it Otari picks the key's own default."""
        if explicit is not None or user is None:
            return explicit
        return self._settings.otari_contributor_budget_id

    def _headers(self, conversation_id: str | None, end_user_budget: str | None) -> dict[str, str]:
        headers = end_user_headers(end_user_budget)
        if conversation_id:
            headers["Otari-Conversation-Id"] = conversation_id
        return headers

    def _record(
        self,
        role: Role,
        stage: str | None,
        endpoint: str,
        requested: str | None,
        prompt_version: str | None,
        headers: Mapping[str, str],
        status: int | None,
        outcome: str,
        latency_ms: int,
        usage: Mapping[str, Any],
    ) -> OtariCall:
        attempts = headers.get("otari-attempt-count")
        fallback = headers.get("otari-fallback")
        call = OtariCall(
            request_id=headers.get("otari-request-id"),
            role=role,
            stage=stage,
            endpoint=endpoint,
            status=status,
            outcome=outcome,
            requested_model=requested,
            effective_model=headers.get("otari-model"),
            provider=headers.get("otari-provider"),
            fallback=None if fallback is None else fallback == "true",
            attempt_count=int(attempts) if attempts and attempts.isdigit() else None,
            usage=to_usage(usage),
            cost_usd=parse_cost(usage),
            latency_ms=latency_ms,
            prompt_version=prompt_version,
        )
        log.info("otari.call", stage=stage, otari_request_id=call.request_id, status=status, outcome=outcome,
                 model=requested, effective_model=call.effective_model, latency_ms=latency_ms)  # fmt: skip
        if self._on_call:
            self._on_call(call)
        return call

    async def _wait(self, error: errors.OtariError) -> None:
        wait = error.retry_after if error.retry_after is not None else DEFAULT_RETRY_WAIT_S
        if wait > MAX_RETRY_WAIT_S:
            raise error
        await self._sleep(wait)

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        record: Callable[[Mapping[str, str], int | None, str, int, Any], OtariCall],
        guarded: bool,
        *,
        retry: bool = True,
    ) -> tuple[dict[str, str], dict[str, Any], OtariCall]:
        rate_limited, upstream = 0, 0
        while True:
            started = time.monotonic()
            try:
                async with self._limit:
                    reply = await self._http.request(method, path, json=body, headers=headers)
            except httpx.TimeoutException as cause:
                ms = round((time.monotonic() - started) * 1000)
                record({}, None, "timeout", ms, None)
                failure: errors.OtariError = errors.OtariUpstreamError(f"timed out calling {path}", code="timeout")
                failure.__cause__ = cause
            except httpx.TransportError as cause:
                ms = round((time.monotonic() - started) * 1000)
                record({}, None, "connection_error", ms, None)
                failure = errors.OtariUpstreamError(f"could not reach the gateway: {cause}", code="connection_error")
                failure.__cause__ = cause
            else:
                ms = round((time.monotonic() - started) * 1000)
                reply_headers = {name.lower(): value for name, value in reply.headers.items()}
                data = parse_json(reply.text)
                if reply.is_success and isinstance(data, dict):
                    return reply_headers, data, record(reply_headers, reply.status_code, "ok", ms, data)
                if reply.is_success:
                    record(reply_headers, reply.status_code, "bad_body", ms, None)
                    raise errors.OtariUpstreamError(
                        f"{path} answered a body that is not a JSON object",
                        status=reply.status_code,
                        request_id=reply_headers.get("otari-request-id"),
                    )
                failure = errors.from_response(reply.status_code, reply_headers, data or reply.text, guarded=guarded)
                record(reply_headers, reply.status_code, failure.code or type(failure).__name__, ms, data)

            if retry and isinstance(failure, errors.OtariRateLimited) and rate_limited < MAX_RATE_LIMIT_RETRIES:
                rate_limited += 1
            elif retry and isinstance(failure, errors.OtariUpstreamError) and upstream < MAX_UPSTREAM_RETRIES:
                upstream += 1
            else:
                raise failure
            await self._wait(failure)

    async def _get(self, path: str) -> dict[str, Any]:
        def record(reply_headers: Mapping[str, str], status: int | None, outcome: str, ms: int, data: Any) -> OtariCall:
            return self._record(Role.DOCTOR, "catalog", path, None, None, reply_headers, status, outcome, ms, {})

        _, data, _ = await self._request("GET", path, None, {}, record, False)
        return data

    async def _open_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        target: str,
        role: Role,
        stage: str | None,
        prompt_version: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        """Read a whole stream. A refusal is retried like any call; a broken stream is not."""
        rate_limited, upstream = 0, 0
        while True:
            started = time.monotonic()
            failure: errors.OtariError
            try:
                async with self._limit, self._http.stream("POST", "messages", json=body, headers=headers) as reply:
                    reply_headers = {name.lower(): value for name, value in reply.headers.items()}
                    if reply.is_success:
                        return [line async for line in reply.aiter_lines()], reply_headers
                    raw = (await reply.aread()).decode(errors="replace")
                failure = errors.from_response(reply.status_code, reply_headers, parse_json(raw) or raw)
                ms = round((time.monotonic() - started) * 1000)
                self._record(role, stage, "messages", target, prompt_version, reply_headers, reply.status_code,
                             failure.code or type(failure).__name__, ms, {})  # fmt: skip
            except httpx.TransportError as cause:
                code = "timeout" if isinstance(cause, httpx.TimeoutException) else "connection_error"
                ms = round((time.monotonic() - started) * 1000)
                self._record(role, stage, "messages", target, prompt_version, {}, None, code, ms, {})
                raise errors.OtariUpstreamError(f"stream failed: {cause}", code=code) from cause
            if isinstance(failure, errors.OtariRateLimited) and rate_limited < MAX_RATE_LIMIT_RETRIES:
                rate_limited += 1
            elif isinstance(failure, errors.OtariUpstreamError) and upstream < MAX_UPSTREAM_RETRIES:
                upstream += 1
            else:
                raise failure
            await self._wait(failure)
