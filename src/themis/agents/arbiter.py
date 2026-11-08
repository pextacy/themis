"""Arbiter: explain a verdict from structured evidence.

It receives enumerated fields only: no claim text, no quotes, no check details. Those carry
submission text, and this role must never read it. Any failure falls back to a fixed template.
"""

from __future__ import annotations

import json
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from themis.context import StageContext
from themis.models import Check, Claim, Role, Verdict, VerdictValue
from themis.otari.errors import OtariConfigError, OtariError

MAX_TOKENS = 300
TEMPLATES = {
    VerdictValue.EVIDENCED: "The main claims were reproduced or confirmed, so this is ready for review.",
    VerdictValue.PLAUSIBLE: "Most claims checked out and nothing was contradicted.",
    VerdictValue.NEEDS_EVIDENCE: "There was not enough checkable evidence to confirm the claims yet.",
    VerdictValue.CONTRADICTED: "At least one claim does not match the repository. A maintainer decides what is next.",
    VerdictValue.TAINTED: "Text aimed at automated reviewers was found, so this waits for a person.",
    VerdictValue.TAINTED_UNCHECKED: "The injection check could not run, so this waits for a person.",
    VerdictValue.DEFERRED: "The checking budget ran out. This can be approved by a maintainer or run again later.",
}


class ArbiterOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    explanation: str = Field(min_length=1, max_length=900)


def load_prompt() -> tuple[str, str]:
    raw = resources.files("themis.agents.prompts").joinpath("arbiter.md").read_text()
    _, header, body = raw.split("---\n", 2)
    return header.split(":", 1)[1].strip(), body.strip()


def template(value: VerdictValue) -> str:
    return TEMPLATES[value]


def structured_evidence(verdict: Verdict, claims: list[Claim], checks: list[Check]) -> str:
    types = {claim.id: claim.type.value for claim in claims}
    rows = [
        {
            "module": check.module,
            "status": check.status.value,
            "strength": check.strength.value if check.strength else None,
            "claim_type": types.get(check.claim_id or ""),
        }
        for check in checks
    ]
    return json.dumps({"verdict": verdict.value.value, "score": verdict.score, "checks": rows})


def _parse(text: str) -> str | None:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return ArbiterOutput.model_validate(json.loads(cleaned)).explanation
    except (json.JSONDecodeError, ValidationError):
        return None


async def explain(ctx: StageContext, verdict: Verdict, claims: list[Claim], checks: list[Check]) -> str:
    """Never fails a run: a rejected key still raises, everything else uses the template."""
    version, system = load_prompt()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": structured_evidence(verdict, claims, checks)},
    ]
    try:
        reply = await ctx.client.chat(
            Role.ARBITER,
            messages,
            user=ctx.submission.contributor_key,
            conversation_id=ctx.conversation_id,
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
            stage="arbiter",
            prompt_version=version,
        )
    except OtariConfigError:
        raise
    except OtariError:
        return template(verdict.value)
    return _parse(reply.content) or template(verdict.value)
