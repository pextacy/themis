"""Key budget refusal on themis-budget-demo, which has a tiny request_limit."""

from typing import Any

from _common import Context, Failed

NAME = "key-budget-refusal"


def run(ctx: Context) -> dict[str, Any]:
    key = ctx.require("OTARI_BUDGET_DEMO_KEY")
    model = ctx.model_choice(0)
    limit = int(ctx.env.get("SPIKE_BUDGET_MAX_CALLS", "8"))
    for attempt in range(1, limit + 1):
        reply = ctx.chat(model, [{"role": "user", "content": "Say ok."}], key=key, max_tokens=4)
        if reply.status == 403 and reply.error_code == "budget_exceeded":
            return {
                "admitted_before_refusal": attempt - 1,
                "budget_scope": reply.headers.get("otari-budget-scope"),
                "retry_after": reply.headers.get("retry-after"),
                "detail": reply.body.get("detail") if isinstance(reply.body, dict) else None,
            }
        if reply.status != 200:
            raise Failed(f"call {attempt} answered {reply.status} ({reply.error_code})")
    raise Failed(f"no refusal within {limit} calls; lower the key's request_limit")
