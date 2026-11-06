"""Chat completion with open-weight model B, from a different family than A."""

from typing import Any

from _common import Context, Failed, open_weight_family
from s02_chat_model_a import run_chat

NAME = "chat-model-b"
INDEX = 1


def run(ctx: Context) -> dict[str, Any]:
    if open_weight_family(ctx.model_choice(0)) == open_weight_family(ctx.model_choice(1)):
        raise Failed("model A and model B share a family")
    return run_chat(ctx, INDEX)
