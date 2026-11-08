"""One submission, end to end: Shield → Clerk → Plan → Evidence → Verdict → Arbiter → Report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from themis.agents import arbiter, clerk, fallback_clerk
from themis.config import RepoPolicy, Settings, parse_policy
from themis.context import StageContext
from themis.evidence import engine
from themis.evidence.base import EvidenceContext
from themis.evidence.dependencies import RegistryClient
from themis.evidence.registries import ExtendedRegistry
from themis.intake.forge import GITHUB, open_client
from themis.intake.github import GitHubClient, GitHubError, Target
from themis.logs import get_logger
from themis.models import (
    Check,
    ClaimSet,
    Receipt,
    Role,
    ShieldResult,
    ShieldStatus,
    Signal,
    Submission,
    SubmissionKind,
    Verdict,
)
from themis.models import OtariCall as Call
from themis.otari.client import OtariClient
from themis.otari.errors import OtariBudgetExceeded, OtariError
from themis.report import github_comment, publish
from themis.shield import sanitize, stage
from themis.signals import completeness
from themis.signals import runner as signal_runner
from themis.store import Store, cache, history, redact, usage
from themis.verdict import plan as planner
from themis.verdict import receipts, scoring, transparency

log = get_logger(__name__)
POLICY_PATH = ".github/themis.yml"


class KindDisabled(Exception):
    """The repository's policy turned this kind of submission off."""


@dataclass
class Outcome:
    submission: Submission
    shield: ShieldResult
    claims: ClaimSet
    verdict: Verdict
    explanation: str
    labels: list[str]
    ledger: str
    receipt: Receipt
    checks: list[Check] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    plan: planner.Plan | None = None
    signals: list[Signal] = field(default_factory=list)


async def remote_policy(github: GitHubClient, repo: str) -> RepoPolicy:
    """Read from the default branch, so a contributor's PR cannot relax the policy."""
    text = await github.file_text(repo, POLICY_PATH)
    return parse_policy(text, POLICY_PATH) if text else RepoPolicy()


async def _costs(client: OtariClient) -> tuple[list[dict[str, Any]], float | None]:
    """Budget facts are advisory. The gateway enforces budgets whatever this finds."""
    try:
        catalog = await client.list_models()
        budget = await client.key_budget()
    except OtariError:
        return [], None
    return catalog, budget.remaining_usd if budget else None


async def _evidence(
    ctx: StageContext, github: GitHubClient, registry: Any, claims: ClaimSet, test_results: dict[str, Any] | None
) -> tuple[list[Check], bool, planner.Plan]:
    catalog, remaining = await _costs(ctx.client)
    models = {Role.CLERK: ctx.settings.model_clerk, Role.PROVER: ctx.settings.model_prover}
    plan = planner.build_plan(
        ctx.policy, claims, ctx.submission, catalog=catalog, models=models, remaining_usd=remaining
    )
    evidence_ctx = EvidenceContext(ctx, github, claims, registry=registry, test_results=test_results)
    result = await engine.run(evidence_ctx, plan)
    return result.checks, result.budget_refused, plan


async def _claims(ctx: StageContext) -> ClaimSet:
    """The model first. Without it, plainly worded claims still get checked (rule 9)."""
    found = await clerk.run(ctx)
    if not found.degraded:
        return found
    text = f"{ctx.submission.title}\n\n{ctx.submission.body}"
    rescued = fallback_clerk.extract(ctx.submission, sanitize.scan(text, "submission"))
    reason = "; ".join(item for item in (found.reason, rescued.reason) if item)
    return rescued.model_copy(update={"reason": reason, "otari_request_ids": found.otari_request_ids})


def _labels(verdict: Verdict, checks: list[Check], signals: list[Signal], prefix: str) -> list[str]:
    return [*scoring.labels_for(verdict.value, checks, prefix), *signal_runner.labels_for(signals, prefix)]


async def analyze(
    settings: Settings,
    policy: RepoPolicy,
    submission: Submission,
    client: OtariClient,
    github: GitHubClient,
    registry: RegistryClient,
    *,
    test_results: dict[str, Any] | None = None,
    calls: list[Call] | None = None,
) -> Outcome:
    recorded = [] if calls is None else calls
    ctx = StageContext(settings, policy, client, submission)
    ctx.shield = await stage.run(ctx)
    claims = ClaimSet(kind=SubmissionKind.OTHER)
    checks: list[Check] = []
    deferred = False
    plan = None
    waiting = usage.preflight(settings, policy, submission, datetime.now(UTC))
    if waiting is not None:
        deferred = True
    elif ctx.shield.status is ShieldStatus.CLEAN:
        try:
            claims = await _claims(ctx)
            checks, deferred, plan = await _evidence(ctx, github, registry, claims, test_results)
        except OtariBudgetExceeded as error:
            log.info("pipeline.deferred", submission_id=submission.id, stage="clerk", otari_request_id=error.request_id)
            deferred = True
    verdict = scoring.decide(ctx.shield.status, claims.claims, checks, policy.scoring, budget_deferred=deferred)
    explanation = await arbiter.explain(ctx, verdict, claims.claims, checks)
    signals = signal_runner.collect(submission, policy, claims.kind)
    labels = _labels(verdict, checks, signals, policy.labels.prefix)
    receipt = receipts.build(submission, claims.kind, verdict, claims.claims, checks, recorded, ctx.shield, signals)
    errors = [claims.reason] if claims.degraded and claims.reason else []
    notes = signal_runner.ledger_lines(signals)
    if waiting is not None:
        notes = ["", f"**Deferred:** {github_comment.inert(waiting.reason, 200)}", *notes]
    ledger = github_comment.render(verdict, explanation, claims.claims, checks, ctx.shield, recorded,
                                   f"themis-{submission.id}.json", errors=errors, notes=notes,
                                   asks=completeness.checklist(signals))  # fmt: skip
    return Outcome(
        submission, ctx.shield, claims, verdict, explanation, labels, ledger, receipt, checks, recorded, plan, signals
    )


def persist(settings: Settings, outcome: Outcome) -> Path:
    with Store(settings.db_url, secrets=settings.secrets()) as store:
        store.save_submission(outcome.submission)
        store.save_shield(outcome.submission.id, outcome.shield)
        store.save_claims(outcome.submission.id, outcome.claims)
        store.save_checks(outcome.submission.id, outcome.checks)
        for call in outcome.calls:
            store.save_otari_call(call, outcome.submission.id)
        store.save_verdict(outcome.submission.id, outcome.verdict, outcome.claims.kind.value)
    path = receipts.write(outcome.receipt, outcome.submission.id, settings.secrets())
    path.with_suffix(".md").write_text(redact(outcome.ledger, settings.secrets()))
    transparency.append(path, outcome.submission.id, outcome.verdict.value.value, secrets=settings.secrets())
    history.record_analysis(settings, outcome.submission.id, outcome.submission.head_sha, outcome.verdict,
                            outcome.claims.claims, outcome.checks)  # fmt: skip
    return path


def _from_cache(hit: cache.CachedAnalysis, submission: Submission, prefix: str) -> Outcome | None:
    """A receipt that went missing or no longer parses is a cache miss, not an error."""
    try:
        receipt = hit.receipt()
    except (OSError, ValueError):
        return None
    shield = ShieldResult(status=ShieldStatus(receipt.shield.get("status", ShieldStatus.CLEAN)))
    claims = ClaimSet(kind=receipt.submission.kind, claims=receipt.claims)
    labels = _labels(receipt.verdict, receipt.checks, receipt.signals, prefix)
    return Outcome(submission, shield, claims, receipt.verdict, "", labels, hit.ledger, receipt, receipt.checks,
                   signals=receipt.signals)  # fmt: skip


async def run_url(
    settings: Settings,
    target: Target,
    *,
    post: bool = True,
    test_results: dict[str, Any] | None = None,
    submission: Submission | None = None,
    forge: str = GITHUB,
    force: bool = False,
    automatic: bool = False,
) -> Outcome:
    """Only GitHub can be posted to. `force` skips the cache, for an explicit recheck.

    `automatic` marks an event nobody asked for by hand. Only those honour `policy.kinds`, so a
    maintainer can still run one check on a kind they turned off.
    """
    calls: list[Call] = []
    async with (
        OtariClient(settings, on_call=calls.append) as client,
        cast(GitHubClient, open_client(settings, forge)) as github,
        ExtendedRegistry() as registry,
    ):
        item = submission or await github.fetch(target)
        policy = await remote_policy(github, item.repo)
        if automatic and not policy.kinds.allows(pull_request=item.is_pull_request, labels=item.labels):
            raise KindDisabled(item.id)
        key = cache.fingerprint(item, policy, settings, test_results)
        hit = cache.lookup(settings, key, force=force)
        outcome = _from_cache(hit, item, policy.labels.prefix) if hit else None
        if outcome is None:
            outcome = await analyze(
                settings, policy, item, client, github, registry, test_results=test_results, calls=calls
            )
            path = persist(settings, outcome)
            degraded = outcome.claims.degraded or any(call.status != 200 for call in calls)
            cache.remember(settings, key, item.id, outcome.verdict.value, outcome.verdict.score, outcome.ledger, path,
                           degraded=degraded)  # fmt: skip
        if post and forge == GITHUB:
            try:
                await publish.publish(github, item, outcome.verdict, outcome.ledger, outcome.labels, policy)
            except GitHubError as error:
                log.warning("report.failed", submission_id=item.id, stage="report", error=str(error))
        return outcome
