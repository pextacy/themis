"""GET /models, /tools and guardrail profiles, saved as redacted catalogs."""

from typing import Any

from _common import Context, Failed, open_weight_candidates

NAME = "catalog"
KEEP = ("id", "name", "owned_by", "type", "context_window", "max_output_tokens",
        "pricing", "pricing_source", "supported_parameters")


def run(ctx: Context) -> dict[str, Any]:
    ctx.load_catalog()
    tools = ctx.request("GET", "/tools")
    if tools.status != 200:
        raise Failed(f"GET /tools answered {tools.status}")
    ctx.tools = tools.body
    profiles = ctx.request("GET", "/tool-settings/guardrails/profiles")
    budget = ctx.request("GET", "/key/budget")

    trimmed = [{key: model[key] for key in KEEP if key in model} for model in ctx.models]
    ctx.save_json("catalog-models.json", trimmed)
    tool_rows = [{key: tool.get(key) for key in ("id", "available", "reason")} for tool in tools.body.get("data", [])]
    ctx.save_json("catalog-tools.json", tool_rows)
    ctx.save_json("catalog-guardrail-profiles.json", profiles.body)

    candidates = open_weight_candidates(ctx.models)
    return {
        "model_count": len(ctx.models),
        "open_weight_candidates": [model["id"] for model in candidates],
        "tools": tool_rows,
        "guardrail_profiles_status": profiles.status,
        "remaining_usd": budget.body.get("remaining_usd") if isinstance(budget.body, dict) else None,
    }
