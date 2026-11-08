"""Run the planned checks: deterministic first, model-backed only if they are worth it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from themis.evidence import (
    advisories,
    consistency,
    context,
    dependencies,
    duplicates,
    ecosystems,
    references,
    repro,
    symbols,
    test_impact,
    tests_results,
    tracebacks,
    versions,
    web,
)
from themis.evidence.base import EvidenceContext, make_check
from themis.intake.github import GitHubError
from themis.logs import get_logger
from themis.models import Check, CheckStatus, Strength
from themis.otari.errors import OtariBudgetExceeded, OtariConfigError, OtariError
from themis.verdict.plan import Plan

log = get_logger(__name__)
Module = Callable[[EvidenceContext], Awaitable[list[Check]]]
MODULES: dict[str, Module] = {
    "symbols": symbols.run,
    "versions": versions.run,
    "tests": tests_results.run,
    "dependencies": dependencies.run,
    "consistency": consistency.run,
    "duplicates": duplicates.run,
    "tracebacks": tracebacks.run,
    "references": references.run,
    "ecosystems": ecosystems.run,
    "advisories": advisories.run,
    "test_impact": test_impact.run,
    "web": web.run,
    "context": context.run,
    "reproduction": repro.run,
}


class EvidenceResult(BaseModel):
    checks: list[Check] = Field(default_factory=list)
    budget_refused: bool = False
    skipped: list[str] = Field(default_factory=list)


async def _run_one(ctx: EvidenceContext, name: str, limit: asyncio.Semaphore) -> list[Check]:
    """A module fails alone. Budget refusals and a rejected key go up to the caller."""
    try:
        async with limit:
            return await MODULES[name](ctx)
    except (OtariBudgetExceeded, OtariConfigError):
        raise
    except (OtariError, GitHubError, KeyError, ValueError, TypeError) as error:
        log.warning(
            "evidence.module_failed", stage=name, submission_id=ctx.stage.submission.id, error=type(error).__name__
        )
        return [make_check(name, CheckStatus.UNVERIFIED, reason=f"{type(error).__name__}: {error}")]


async def _gather(ctx: EvidenceContext, names: list[str]) -> tuple[list[Check], bool]:
    limit = asyncio.Semaphore(ctx.stage.settings.max_concurrency)
    results = await asyncio.gather(*(_run_one(ctx, name, limit) for name in names), return_exceptions=True)
    checks: list[Check] = []
    refused = False
    for result in results:
        if isinstance(result, OtariBudgetExceeded):
            refused = True
        elif isinstance(result, BaseException):
            raise result
        else:
            checks.extend(result)
    return checks, refused


async def run(ctx: EvidenceContext, plan: Plan) -> EvidenceResult:
    checks, refused = await _gather(ctx, plan.deterministic)
    backed = list(plan.model_backed)
    skipped: list[str] = []
    hard = any(item.status is CheckStatus.CONTRADICTED and item.strength is Strength.HARD for item in checks)
    if hard and "reproduction" in backed:
        # No point paying to reproduce a bug in a function that does not exist.
        backed.remove("reproduction")
        skipped.append("reproduction")
    if plan.deferred:
        return EvidenceResult(checks=checks, budget_refused=True, skipped=[*skipped, *backed])
    more, also_refused = await _gather(ctx, backed)
    return EvidenceResult(checks=[*checks, *more], budget_refused=refused or also_refused, skipped=skipped)
