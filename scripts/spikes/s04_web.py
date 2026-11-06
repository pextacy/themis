"""Built-in web search and fetch, declared type-only."""

from typing import Any

from _common import Context, Failed

NAME = "web-search-fetch"


def run(ctx: Context) -> dict[str, Any]:
    ctx.tool_status("otari_web_search")
    ctx.tool_status("otari_web_fetch")
    model = ctx.model_choice(0)
    reply = ctx.chat(
        model,
        [{
            "role": "user",
            "content": "Search the web for CVE-2021-44228, then fetch "
            "https://nvd.nist.gov/vuln/detail/CVE-2021-44228 and state its CVSS base score.",
        }],
        tools=[{"type": "otari_web_search"}, {"type": "otari_web_fetch"}],
        max_tokens=300,
    )
    if reply.status != 200:
        raise Failed(f"answered {reply.status} ({reply.error_code})")
    kinds = {call.get("type"): call.get("status") for call in reply.tool_calls}
    missing = [k for k in ("web_search", "web_fetch") if kinds.get(k) != "completed"]
    if missing:
        raise Failed(f"no completed call for {missing}; saw {kinds}")
    return {"model": model, "tool_calls": reply.tool_calls}
