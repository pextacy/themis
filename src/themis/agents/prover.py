"""Prover: write a probe for one behavior claim. It never judges the outcome."""

from __future__ import annotations

import html
import json
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from themis.context import StageContext
from themis.models import Claim, Role
from themis.otari.client import OtariResult

MAX_TOKENS = 3000  # Room for a reasoning model's thinking before the code
REPAIR = 'That did not match the required format. Reply with only {"probe": "<python code>"}.'


class ProverOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    probe: str = Field(min_length=1, max_length=6000)


def load_prompt() -> tuple[str, str]:
    raw = resources.files("themis.agents.prompts").joinpath("prover.md").read_text()
    _, header, body = raw.split("---\n", 2)
    return header.split(":", 1)[1].strip(), body.strip()


def parse(text: str) -> ProverOutput | None:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return ProverOutput.model_validate(json.loads(cleaned))
    except (json.JSONDecodeError, ValidationError):
        return None


def build_message(ctx: StageContext, claim: Claim, source: str) -> str:
    submission_id = ctx.submission.id
    return (
        f'<untrusted_submission id="{submission_id}">\n{html.escape(claim.text, quote=False)}\n'
        f"</untrusted_submission>\n<untrusted_context>\n{html.escape(source, quote=False)}\n</untrusted_context>"
    )


async def _ask(ctx: StageContext, messages: list[dict[str, str]], version: str) -> OtariResult:
    return await ctx.client.chat(
        Role.PROVER,
        messages,
        user=ctx.submission.contributor_key,
        conversation_id=ctx.conversation_id,
        response_format={"type": "json_object"},
        max_tokens=MAX_TOKENS,
        stage="prover",
        prompt_version=version,
    )


async def write_probe(ctx: StageContext, claim: Claim, source: str) -> tuple[str | None, str | None]:
    """Return (probe, request id). One repair round, then no probe."""
    version, system = load_prompt()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": build_message(ctx, claim, source)}]
    first = await _ask(ctx, messages, version)
    output = parse(first.content)
    if output is not None:
        return output.probe, first.call.request_id
    repair = [*messages, {"role": "assistant", "content": first.content[:2000]}, {"role": "user", "content": REPAIR}]
    second = await _ask(ctx, repair, version)
    output = parse(second.content)
    return (output.probe if output else None), second.call.request_id
