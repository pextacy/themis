"""Streamed /messages with the DeepWiki MCP server.

The anthropic-beta header is what makes the gateway emit mcp_tool_use and
mcp_tool_result blocks. Tool activity is paired by stream order.
"""

from typing import Any

from _common import Blocked, Context, Failed

NAME = "mcp-deepwiki"
MCP_BETA = "mcp-client-2025-11-20"
DEFAULT_SERVER = {"name": "deepwiki", "url": "https://mcp.deepwiki.com/mcp"}


def paired_activity(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for event in events:
        if event.get("type") != "content_block_start":
            continue
        block = event.get("content_block") or {}
        if block.get("type") == "mcp_tool_use":
            pending = block
        elif block.get("type") == "mcp_tool_result" and pending is not None:
            pairs.append({
                "tool": pending.get("name"),
                "server": pending.get("server_name"),
                "input": pending.get("input"),
                "is_error": block.get("is_error"),
            })
            pending = None
    return pairs


def run(ctx: Context) -> dict[str, Any]:
    model = ctx.model_choice(0)
    reply = ctx.request(
        "POST",
        "/messages",
        headers={"anthropic-beta": MCP_BETA},
        stream=True,
        body={
            "model": model,
            "max_tokens": 300,
            "stream": True,
            "mcp_servers": [DEFAULT_SERVER],
            "max_tool_iterations": 3,
            "messages": [{
                "role": "user",
                "content": "Use the deepwiki tools to name the top-level topics of the "
                "repository mozilla-ai/otari, in one sentence.",
            }],
        },
    )
    if reply.status == 400 and "mcp" in str(reply.body).lower():
        raise Blocked(f"inline MCP refused, enable request_mcp_servers_allowed: {reply.body}")
    if reply.status != 200:
        raise Failed(f"answered {reply.status} ({reply.error_code})")
    pairs = paired_activity(reply.events)
    if not pairs:
        raise Failed(f"no mcp_tool_use/mcp_tool_result pair in {len(reply.events)} events")
    if all(pair["is_error"] for pair in pairs):
        raise Failed(f"every MCP call failed: {pairs}")
    return {"model": model, "activity": pairs}
