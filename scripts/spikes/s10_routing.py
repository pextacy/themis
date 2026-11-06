"""Routing policy whose primary fails on purpose, so on_failure must serve it."""

from typing import Any

from _common import Blocked, Context, Failed, open_weight_family

NAME = "routing-fallback"


def run(ctx: Context) -> dict[str, Any]:
    policy = ctx.require("THEMIS_SPIKE_FALLBACK_POLICY")
    reply = ctx.chat(policy, [{"role": "user", "content": "Say ok."}], max_tokens=8)
    if reply.status == 400 and reply.error_code == "invalid_model":
        raise Blocked(f"routing policy {policy!r} does not exist in this workspace")
    if reply.status != 200:
        raise Failed(f"answered {reply.status} ({reply.error_code})")
    fallback = reply.headers.get("otari-fallback")
    attempts = int(reply.headers.get("otari-attempt-count", "0"))
    if fallback != "true" or attempts < 2:
        raise Failed(f"expected a fallback, saw Otari-Fallback={fallback}, attempts={attempts}")
    served_by = reply.headers.get("otari-model", "")
    return {
        "policy": policy,
        "served_by": served_by,
        "family": open_weight_family(served_by),
        "attempts": attempts,
    }
