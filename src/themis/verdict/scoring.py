"""The deterministic verdict. No model output and no identity is an input here.

This function takes check results only. It cannot see who wrote a submission, so history
cannot lower a verdict, and a model's opinion can never produce CONTRADICTED alone: SOFT
checks block PLAUSIBLE but cannot reach that verdict.
"""

from __future__ import annotations

from themis.config import Scoring
from themis.models import Check, CheckStatus, Claim, ShieldStatus, Strength, Verdict, VerdictValue

SHIELD_VERDICTS = {
    ShieldStatus.TAINTED: VerdictValue.TAINTED,
    ShieldStatus.TAINTED_UNCHECKED: VerdictValue.TAINTED_UNCHECKED,
    ShieldStatus.DEFERRED: VerdictValue.DEFERRED,
}
TESTS_MODULE = "tests"
DEPENDENCY_MODULES = ("dependencies", "ecosystems", "advisories")


def is_hard(check: Check) -> bool:
    return check.status is CheckStatus.CONTRADICTED and check.strength is Strength.HARD


def is_soft(check: Check) -> bool:
    return check.status is CheckStatus.CONTRADICTED and not is_hard(check)


def outcome(checks: list[Check]) -> str:
    """One result per claim. A contradiction outranks any confirmation."""
    if any(is_hard(check) for check in checks):
        return "hard_contradicted"
    if any(is_soft(check) for check in checks):
        return "soft_contradicted"
    if any(check.status is CheckStatus.REPRODUCED for check in checks):
        return "reproduced"
    if any(check.status is CheckStatus.CONFIRMED for check in checks):
        return "confirmed"
    return "unverified"


def _claim_outcomes(claims: list[Claim], checks: list[Check]) -> list[str]:
    outcomes = [outcome([check for check in checks if check.claim_id == claim.id]) for claim in claims]
    unlinked = [c for c in checks if c.claim_id is None and c.status is not CheckStatus.NOT_APPLICABLE]
    return outcomes + [outcome([check]) for check in unlinked]


def compute_score(outcomes: list[str], scoring: Scoring) -> int:
    if not outcomes:
        return 0
    weights: dict[str, float] = scoring.weights.model_dump()
    mean = sum(weights[item] for item in outcomes) / len(outcomes)
    coverage = sum(item != "unverified" for item in outcomes) / len(outcomes)
    return max(0, min(100, round(mean * coverage * 100)))


def decide(
    shield: ShieldStatus, claims: list[Claim], checks: list[Check], scoring: Scoring, *, budget_deferred: bool = False
) -> Verdict:
    """Pure: the same inputs always give the same verdict."""
    outcomes = _claim_outcomes(claims, checks)
    value = _value(shield, claims, checks, outcomes, scoring, budget_deferred)
    score = 0 if value in SHIELD_VERDICTS.values() else compute_score(outcomes, scoring)
    return Verdict(value=value, score=score, weights=scoring.weights.model_dump())


def _value(
    shield: ShieldStatus,
    claims: list[Claim],
    checks: list[Check],
    outcomes: list[str],
    scoring: Scoring,
    budget_deferred: bool,
) -> VerdictValue:
    if shield in SHIELD_VERDICTS:
        return SHIELD_VERDICTS[shield]
    if budget_deferred:
        return VerdictValue.DEFERRED
    if any(is_hard(check) for check in checks):
        return VerdictValue.CONTRADICTED
    if not claims:
        return VerdictValue.NEEDS_EVIDENCE
    good = sum(item in ("reproduced", "confirmed") for item in outcomes[: len(claims)])
    ratio = good / len(claims)
    soft = any(is_soft(check) for check in checks)
    if any(check.status is CheckStatus.REPRODUCED for check in checks):
        return VerdictValue.EVIDENCED
    tests_pass = any(
        c.module == TESTS_MODULE and c.claim_id is None and c.status is CheckStatus.CONFIRMED for c in checks
    )
    if tests_pass and not soft and ratio >= scoring.plausible_threshold:
        return VerdictValue.EVIDENCED
    if ratio >= scoring.plausible_threshold and not soft:
        return VerdictValue.PLAUSIBLE
    return VerdictValue.NEEDS_EVIDENCE


def labels_for(value: VerdictValue, checks: list[Check], prefix: str) -> list[str]:
    """Labels come from this table alone. A model never chooses one."""
    name = {
        VerdictValue.EVIDENCED: "evidenced",
        VerdictValue.PLAUSIBLE: "plausible",
        VerdictValue.NEEDS_EVIDENCE: "needs-evidence",
        VerdictValue.CONTRADICTED: "contradicted",
        VerdictValue.TAINTED: "tainted",
        VerdictValue.TAINTED_UNCHECKED: "tainted",
        VerdictValue.DEFERRED: "deferred",
    }[value]
    labels = [f"{prefix}{name}"]
    if any(c.module in DEPENDENCY_MODULES and c.detail.get("risk") in ("review", "block") for c in checks):
        labels.append(f"{prefix}dependency-risk")
    return labels
