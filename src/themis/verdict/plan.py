"""Which checks apply, cheapest first, and whether the budget covers the model-backed ones."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from themis.config import RepoPolicy
from themis.models import ClaimSet, ClaimType, Role, Submission

TOKEN_ESTIMATES = {"web": (1500, 300), "context": (2000, 400), "reproduction": (4000, 800)}
DETERMINISTIC = (
    "symbols", "versions", "tests", "dependencies", "consistency", "duplicates",
    "tracebacks", "references", "ecosystems", "advisories", "test_impact",
)  # fmt: skip
MODEL_BACKED = ("web", "context", "reproduction")
CLAIMS_FOR: dict[str, frozenset[ClaimType]] = {
    "symbols": frozenset({ClaimType.SYMBOL_EXISTS, ClaimType.FILE_EXISTS}),
    "versions": frozenset({ClaimType.VERSION_EXISTS}),
    "web": frozenset({ClaimType.CVE_REFERENCE, ClaimType.OTHER}),
    "reproduction": frozenset({ClaimType.BEHAVIOR}),
}


class Plan(BaseModel):
    deterministic: list[str] = Field(default_factory=list)
    model_backed: list[str] = Field(default_factory=list)
    estimated_usd: float | None = None
    remaining_usd: float | None = None
    deferred: bool = False
    reason: str | None = None


def applies(name: str, claims: ClaimSet, submission: Submission) -> bool:
    if name in ("tests", "consistency", "dependencies", "ecosystems", "advisories", "test_impact"):
        return submission.is_pull_request
    if name in ("duplicates", "context", "tracebacks", "references"):
        return True
    return any(claim.type in CLAIMS_FOR[name] for claim in claims.claims)


def _price(model: dict[str, Any], tokens: tuple[int, int]) -> float | None:
    pricing = model.get("pricing") or {}
    try:
        rate_in = float(pricing["input_price_per_million"])
        rate_out = float(pricing["output_price_per_million"])
    except (KeyError, TypeError, ValueError):
        return None
    return (tokens[0] * rate_in + tokens[1] * rate_out) / 1_000_000


def estimate(names: list[str], catalog: list[dict[str, Any]], models: dict[Role, str | None]) -> float | None:
    """Model tokens only. None when a price is unknown, because a guess must not gate anyone."""
    total = 0.0
    for name in names:
        role = Role.PROVER if name == "reproduction" else Role.CLERK
        entry = next((model for model in catalog if model.get("id") == models.get(role)), None)
        cost = _price(entry, TOKEN_ESTIMATES[name]) if entry else None
        if cost is None:
            return None
        total += cost
    return total


def build_plan(
    policy: RepoPolicy,
    claims: ClaimSet,
    submission: Submission,
    *,
    catalog: list[dict[str, Any]] | None = None,
    models: dict[Role, str | None] | None = None,
    remaining_usd: float | None = None,
) -> Plan:
    enabled = {**policy.checks.model_dump(), "context": policy.checks.context_mcp}
    chosen = [name for name in DETERMINISTIC if enabled[name] and applies(name, claims, submission)]
    backed = [name for name in MODEL_BACKED if enabled[name] and applies(name, claims, submission)]
    cost = estimate(backed, catalog or [], models or {}) if backed else 0.0
    plan = Plan(deterministic=chosen, model_backed=backed, estimated_usd=cost, remaining_usd=remaining_usd)
    if cost is not None and remaining_usd is not None and cost > remaining_usd:
        plan.deferred = True
        plan.reason = f"estimated ${cost:.4f} exceeds the remaining ${remaining_usd:.4f}"
    return plan
