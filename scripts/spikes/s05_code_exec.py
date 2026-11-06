"""Built-in code execution, in the tool loop and through the direct endpoint.

A random nonce proves stdout came from the sandbox and not from the model.
"""

import secrets
from typing import Any

from _common import Context, Failed

NAME = "code-execution"


def run(ctx: Context) -> dict[str, Any]:
    ctx.tool_status("otari_code_execution")
    model = ctx.model_choice(0)
    nonce = secrets.randbelow(10**9) + 10**9
    expected = str(nonce * 7)
    reply = ctx.chat(
        model,
        [{"role": "user", "content": f"Use the code tool to run exactly: print({nonce} * 7)"}],
        tools=[{"type": "otari_code_execution"}],
        max_tokens=200,
    )
    if reply.status != 200:
        raise Failed(f"tool loop answered {reply.status} ({reply.error_code})")
    runs = [call for call in reply.tool_calls if call.get("type") == "code_execution"]
    if not runs:
        raise Failed("model finished without calling the sandbox")
    if runs[-1].get("exit_code") != 0 or expected not in str(runs[-1].get("stdout")):
        raise Failed(f"unexpected sandbox result: {runs[-1]}")

    direct = ctx.request("POST", "/code/execute", body={"code": f"print({nonce} * 3)"})
    direct_ok = (
        direct.status == 200
        and isinstance(direct.body, dict)
        and direct.body.get("return_code") == 0
        and str(nonce * 3) in str(direct.body.get("stdout"))
    )
    return {
        "model": model,
        "tool_call": runs[-1],
        "direct_endpoint": {"status": direct.status, "ok": direct_ok, "code": direct.error_code},
    }
