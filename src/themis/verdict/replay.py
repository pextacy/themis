"""Recompute a stored verdict from its receipt. No key, no model and no network.

The verdict is a pure function of check results, so a receipt can prove it. Inputs the receipt
does not hold are named in `assumptions`, so a match is never claimed on more than it shows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from themis.config import RepoPolicy, Scoring, Weights
from themis.models import Receipt, Role, ShieldStatus, VerdictValue
from themis.verdict import scoring

BUDGET_REFUSED = "budget_exceeded"


class ReplayError(Exception):
    """The file is not a receipt this version can read."""


@dataclass
class ReplayResult:
    stored_value: VerdictValue
    stored_score: int
    value: VerdictValue
    score: int
    assumptions: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return self.value is self.stored_value and self.score == self.stored_score


def load(path: Path) -> Receipt:
    try:
        return Receipt.model_validate(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        raise ReplayError(f"cannot read receipt {path}: {type(error).__name__}") from error


def _shield(receipt: Receipt, notes: list[str]) -> ShieldStatus:
    try:
        return ShieldStatus(str(receipt.shield["status"]))
    except (KeyError, ValueError):
        notes.append("The receipt holds no shield status. Assumed CLEAN.")
        return ShieldStatus.CLEAN


def _weights(receipt: Receipt, notes: list[str]) -> Weights:
    if not receipt.verdict.weights:
        notes.append("The receipt holds no weights. Used the defaults.")
        return Weights()
    try:
        return Weights.model_validate(receipt.verdict.weights)
    except ValidationError as error:
        raise ReplayError("the receipt's weights do not match this version") from error


def _budget(receipt: Receipt, notes: list[str]) -> bool:
    """The Arbiter runs after the verdict, so its refusal cannot have deferred anything."""
    refused = any(c.outcome == BUDGET_REFUSED and c.role is not Role.ARBITER for c in receipt.otari_calls)
    if refused:
        return True
    if receipt.verdict.value is VerdictValue.DEFERRED:
        notes.append(
            "No budget refusal is recorded, and a planner deferral makes no call. "
            "Took the deferral from the stored verdict, so DEFERRED is not independently checked here."
        )
        return True
    return False


def replay(receipt: Receipt, policy: RepoPolicy | None = None) -> ReplayResult:
    notes: list[str] = []
    threshold = (policy or RepoPolicy()).scoring.plausible_threshold
    source = "the given policy" if policy else "the default"
    notes.append(f"The receipt does not hold plausible_threshold. Used {threshold} from {source}.")
    rules = Scoring(weights=_weights(receipt, notes), plausible_threshold=threshold)
    shield = _shield(receipt, notes)
    deferred = _budget(receipt, notes)
    verdict = scoring.decide(shield, receipt.claims, receipt.checks, rules, budget_deferred=deferred)
    return ReplayResult(receipt.verdict.value, receipt.verdict.score, verdict.value, verdict.score, notes)
