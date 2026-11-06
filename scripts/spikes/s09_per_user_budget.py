"""Per-user budget: a service key caps one synthetic end user independently."""

import secrets
from typing import Any

from _common import Context, Failed

NAME = "per-user-budget"


def run(ctx: Context) -> dict[str, Any]:
    key = ctx.env.get("OTARI_SERVICE_KEY") or ctx.require("OTARI_API_KEY")
    budget_id = ctx.require("OTARI_PERUSER_BUDGET_ID")
    model = ctx.model_choice(0)
    # A fresh synthetic id each run: deleting an end user does not reset its spend.
    user = f"gh:spike-{secrets.token_hex(4)}"
    headers = {"Otari-End-User-Budget": budget_id}
    limit = int(ctx.env.get("SPIKE_BUDGET_MAX_CALLS", "8"))
    assigned = None
    for attempt in range(1, limit + 1):
        reply = ctx.chat(
            model, [{"role": "user", "content": "Say ok."}], key=key, user=user, headers=headers, max_tokens=4
        )
        if reply.status == 200:
            assigned = reply.headers.get("otari-end-user-budget", assigned)
            continue
        if reply.status == 403 and reply.error_code == "budget_exceeded":
            scope = reply.headers.get("otari-budget-scope")
            if scope != "user":
                raise Failed(f"refused by scope {scope!r}, not the end user's budget")
            return {
                "user": user,
                "admitted_before_refusal": attempt - 1,
                "assigned_budget_seen": assigned is not None,
                "retry_after": reply.headers.get("retry-after"),
            }
        raise Failed(f"call {attempt} answered {reply.status} ({reply.error_code})")
    raise Failed(f"no refusal within {limit} calls; lower the budget's request_limit")
