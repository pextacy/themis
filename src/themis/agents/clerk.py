"""Clerk: classify a submission and extract atomic claims.

The model proposes; code decides. Spans are computed by finding the model's quote in the
submission, so a claim the submission does not contain is dropped rather than checked.
"""

from __future__ import annotations

import html
import json
import re
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from themis.context import StageContext
from themis.models import Claim, ClaimSet, ClaimType, Role, ShieldStatus, SubmissionKind
from themis.otari.client import OtariResult
from themis.otari.errors import OtariBudgetExceeded, OtariConfigError, OtariError
from themis.shield.sanitize import Sanitized, scan

MAX_CLAIMS = 30
MAX_TOKENS = 4096  # Reasoning models spend part of it thinking; 1200 cut replies short
PR_KINDS = frozenset({
    SubmissionKind.BUGFIX_PR, SubmissionKind.FEATURE_PR, SubmissionKind.DOCS_PR, SubmissionKind.DEPENDENCY_PR,
})  # fmt: skip
REPAIR = "That did not match the required JSON format. Reply with only the JSON object."
FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
BACKTICKED = re.compile(r"`([^`\n]{1,120})`")
CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)


class ClerkClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ClaimType
    text: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=600)


class ClerkOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: SubmissionKind
    claims: list[ClerkClaim] = Field(max_length=MAX_CLAIMS)


class ClerkSkipped(Exception):
    """The Clerk was called on a submission the Shield did not pass."""


def load_prompt() -> tuple[str, str]:
    """Return (prompt_version, system prompt) from the versioned prompt file."""
    raw = resources.files("themis.agents.prompts").joinpath("clerk.md").read_text()
    _, header, body = raw.split("---\n", 2)
    version = header.split(":", 1)[1].strip()
    return version, body.strip()


def parse(text: str) -> ClerkOutput | None:
    fenced = FENCE.match(text)
    try:
        return ClerkOutput.model_validate(json.loads(fenced[1] if fenced else text))
    except (json.JSONDecodeError, ValidationError):
        return None


def problem(reply: OtariResult) -> str:
    """Why a reply failed, for the receipt. Names fields and error types, never submission text."""
    fenced = FENCE.match(reply.content)
    try:
        ClerkOutput.model_validate(json.loads(fenced[1] if fenced else reply.content))
    except json.JSONDecodeError:
        cause = "empty reply" if not reply.content.strip() else "not JSON"
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False, include_context=False)[0]
        cause = f"{'.'.join(str(part) for part in first['loc'])}: {first['type']}"
    else:
        cause = "valid"
    return f"{cause}, finish_reason={reply.finish_reason}"


def coherent_kind(kind: SubmissionKind, is_pull_request: bool) -> SubmissionKind:
    """The envelope is a fact: the model cannot call an issue a PR, or the reverse."""
    if is_pull_request and kind not in PR_KINDS and kind is not SubmissionKind.OTHER:
        return SubmissionKind.OTHER
    if not is_pull_request and kind in PR_KINDS:
        return SubmissionKind.OTHER
    return kind


def normalized_target(claim_type: ClaimType, quote: str) -> str | None:
    if claim_type is ClaimType.CVE_REFERENCE:
        match = CVE.search(quote)
        return match[0].upper() if match else None
    match = BACKTICKED.search(quote)
    return match[1] if match else None


def anchor(output: ClerkOutput, text: Sanitized) -> tuple[list[Claim], int]:
    """Keep claims whose quote is really in the submission, with spans computed here."""
    claims: list[Claim] = []
    dropped = 0
    for item in output.claims:
        quote = html.unescape(item.quote)
        span = text.locate(quote)
        if span is None:
            dropped += 1
            continue
        claims.append(
            Claim(
                id=f"c{len(claims) + 1}",
                type=item.type,
                text=item.text,
                span=span,
                normalized_target=normalized_target(item.type, quote),
            )
        )
    return claims, dropped


async def _ask(ctx: StageContext, messages: list[dict[str, str]], version: str) -> OtariResult:
    return await ctx.client.chat(
        Role.CLERK,
        messages,
        user=ctx.submission.contributor_key,
        conversation_id=ctx.conversation_id,
        response_format={"type": "json_object"},
        max_tokens=MAX_TOKENS,
        stage="clerk",
        prompt_version=version,
    )


def _degraded(kind: SubmissionKind, reason: str, ids: list[str]) -> ClaimSet:
    return ClaimSet(kind=kind, degraded=True, reason=reason, otari_request_ids=[item for item in ids if item])


async def run(ctx: StageContext) -> ClaimSet:
    """Budget refusals and a rejected key propagate. Other failures degrade to no claims."""
    if ctx.shield is None or ctx.shield.status is not ShieldStatus.CLEAN:
        raise ClerkSkipped(f"shield status is {ctx.shield.status if ctx.shield else None}")
    version, system = load_prompt()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": ctx.shield.clean_view},
    ]
    ids: list[str] = []
    fallback_kind = SubmissionKind.OTHER
    last: OtariResult | None = None
    try:
        first = last = await _ask(ctx, messages, version)
        ids.append(first.call.request_id or "")
        output = parse(first.content)
        if output is None:
            repair = [*messages, {"role": "assistant", "content": first.content[:2000]},
                      {"role": "user", "content": REPAIR}]  # fmt: skip
            second = last = await _ask(ctx, repair, version)
            ids.append(second.call.request_id or "")
            output = parse(second.content)
    except (OtariBudgetExceeded, OtariConfigError):
        raise
    except OtariError as error:
        return _degraded(fallback_kind, str(error), [*ids, error.request_id or ""])
    if output is None:
        detail = f" ({problem(last)})" if last else ""
        return _degraded(fallback_kind, f"invalid output after one repair{detail}", ids)

    body = scan(f"{ctx.submission.title}\n\n{ctx.submission.body}", "submission")
    claims, dropped = anchor(output, body)
    return ClaimSet(
        kind=coherent_kind(output.kind, ctx.submission.is_pull_request),
        claims=claims,
        dropped_unanchored=dropped,
        otari_request_ids=[item for item in ids if item],
    )
