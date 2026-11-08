"""What to read first. A separate ranking: it never touches a verdict or a score.

Identity can only raise a value here. Nothing reads who a contributor is beyond the first-time
flag, and that flag adds to the value and never subtracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from themis.config import RepoPolicy
from themis.models import SubmissionKind, VerdictValue

MAX_VALUE = 100
SCORE_DIVISOR = 5
SECURITY_BONUS = 15
FIRST_TIME_BONUS = 10
MAX_WAIT_BONUS = 10
VERDICT_BASE = {
    VerdictValue.EVIDENCED.value: 60,
    VerdictValue.PLAUSIBLE.value: 45,
    VerdictValue.TAINTED_UNCHECKED.value: 35,
    VerdictValue.TAINTED.value: 30,
    VerdictValue.NEEDS_EVIDENCE.value: 25,
    VerdictValue.DEFERRED.value: 20,
    VerdictValue.CONTRADICTED.value: 5,
}
DEFAULT_BASE = 15


@dataclass(frozen=True)
class PriorityResult:
    value: int
    reasons: list[str] = field(default_factory=list)


def _days_waiting(created_at: object, now: datetime) -> int:
    try:
        created = datetime.fromisoformat(str(created_at))
    except ValueError:
        return 0
    created = created if created.tzinfo else created.replace(tzinfo=UTC)
    return max((now - created).days, 0)


def priority(row: dict[str, Any], policy: RepoPolicy, now: datetime | None = None) -> PriorityResult:
    verdict = str(row.get("verdict"))
    score = int(row.get("score") or 0)
    value = VERDICT_BASE.get(verdict, DEFAULT_BASE) + score // SCORE_DIVISOR
    reasons = [f"{verdict} at {score}/100"]
    if row.get("kind") == SubmissionKind.SECURITY_REPORT.value and verdict != VerdictValue.CONTRADICTED.value:
        value += SECURITY_BONUS
        reasons.append("security report")
    waited = min(_days_waiting(row.get("created_at"), now or datetime.now(UTC)), MAX_WAIT_BONUS)
    if waited:
        value += waited
        reasons.append(f"waiting {waited}+ days")
    if policy.policy.first_time_contributor_fast_track and row.get("first_time"):
        value += FIRST_TIME_BONUS
        reasons.append("first-time contributor")
    return PriorityResult(min(value, MAX_VALUE), reasons)


def rank(rows: list[dict[str, Any]], policy: RepoPolicy, now: datetime | None = None) -> list[dict[str, Any]]:
    """Highest first. Ties keep the store's order, which is evidence score then newest."""
    moment = now or datetime.now(UTC)
    scored = []
    for row in rows:
        result = priority(row, policy, moment)
        scored.append({**row, "priority": result.value, "priority_reasons": result.reasons})
    return sorted(scored, key=lambda item: -item["priority"])
