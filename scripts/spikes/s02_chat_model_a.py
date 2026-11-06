"""Chat completion with open-weight model A, JSON output."""

import json
from typing import Any

from _common import Context, Failed, open_weight_family

NAME = "chat-model-a"
INDEX = 0


def run_chat(ctx: Context, index: int) -> dict[str, Any]:
    model = ctx.model_choice(index)
    reply = ctx.chat(
        model,
        [
            {"role": "system", "content": 'Answer with one JSON object: {"ok": true, "word": "<one word>"}.'},
            {"role": "user", "content": "Name a colour."},
        ],
        response_format={"type": "json_object"},
        max_tokens=256,
        headers={"Otari-Conversation-Id": "themis-spike-phase0"},
    )
    if reply.status != 200:
        raise Failed(f"{model} answered {reply.status} ({reply.error_code})")
    try:
        parsed = json.loads(reply.content)
    except json.JSONDecodeError as error:
        raise Failed(f"{model} did not return JSON: {error}") from error
    return {"model": model, "family": open_weight_family(model), "parsed": parsed}


def run(ctx: Context) -> dict[str, Any]:
    return run_chat(ctx, INDEX)
