"""Built-in tool declarations and the gateway's report of the calls it ran."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

WEB_SEARCH = "otari_web_search"
WEB_FETCH = "otari_web_fetch"
CODE_EXECUTION = "otari_code_execution"


def declare(*types: str) -> list[dict[str, str]]:
    """Type-only declarations. Extra keys have been rejected by the hosted validator."""
    return [{"type": name} for name in types]


class GatewayToolCall(BaseModel):
    """One entry of `otari_tool_calls`. Trust this over the model's account of a tool."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    type: Literal["web_search", "web_fetch", "code_execution", "mcp"] | str
    status: str
    query: str | None = None
    url: str | None = None
    sources: list[dict[str, Any]] | None = None
    code: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    server: str | None = None
    name: str | None = None
    output: str | None = None
    truncated: bool = False
    error: dict[str, Any] | None = None


class FetchedPage(BaseModel):
    """A page read through POST /fetch. `found` is False only when the page itself said 404."""

    found: bool
    text: str = ""
    truncated: bool = False
    request_id: str | None = None


class CodeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
