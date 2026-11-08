"""Inline MCP server declarations and the streamed Messages activity parser."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

MCP_BETA = "mcp-client-2025-11-20"
DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"


class McpServer(BaseModel):
    name: str
    url: str
    purpose_hint: str | None = None
    allowed_tools: list[str] | None = None

    def declaration(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class McpCall(BaseModel):
    """One gateway-run tool call: an mcp_tool_use paired with its mcp_tool_result."""

    tool: str | None
    server: str | None
    input: dict[str, Any] | None
    is_error: bool
    output: str


def default_servers() -> list[McpServer]:
    return [McpServer(name="deepwiki", url=DEEPWIKI_URL)]


def parse_sse(lines: list[str]) -> list[dict[str, Any]]:
    """Turn raw server-sent event lines into JSON events, keeping stream order."""
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def pair_activity(events: list[dict[str, Any]]) -> list[McpCall]:
    """Pair each result with the use before it, by stream order."""
    calls: list[McpCall] = []
    pending: dict[str, Any] | None = None
    for event in events:
        if event.get("type") != "content_block_start":
            continue
        block = event.get("content_block") or {}
        if block.get("type") == "mcp_tool_use":
            pending = block
        elif block.get("type") == "mcp_tool_result" and pending is not None:
            calls.append(
                McpCall(
                    tool=pending.get("name"),
                    server=pending.get("server_name"),
                    input=pending.get("input"),
                    is_error=bool(block.get("is_error")),
                    output=_result_text(block),
                )
            )
            pending = None
    return calls


def stream_text(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        delta = event.get("delta") or {}
        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            parts.append(str(delta.get("text", "")))
    return "".join(parts)


def stream_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge usage from message_start and message_delta. The later event wins per field."""
    merged: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "message_start":
            merged.update((event.get("message") or {}).get("usage") or {})
        elif isinstance(event.get("usage"), dict):
            merged.update(event["usage"])
    return merged
