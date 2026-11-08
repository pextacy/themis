"""Tune scoring on a train split and report on a held-out split.

The verdict depends only on `plausible_threshold`. Weights move the score, which ranks the
queue. So the threshold is judged by verdict quality and the weights by how far the score
separates genuine from slop items. Results are a report: nothing here changes any default.

The dataset was written alongside THEMIS, so even a held-out number shows regression, not
field accuracy.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from themis import bench
from themis.agents import clerk
from themis.config import RepoPolicy, Scoring, Settings, Weights
from themis.context import StageContext
from themis.evidence import engine
from themis.evidence.base import EvidenceContext
from themis.intake.github import GitHubClient
from themis.models import Check, Claim, ClaimSet, ShieldStatus, SubmissionKind, VerdictValue
from themis.otari.client import OtariClient
from themis.shield import sanitize, stage
from themis.verdict import scoring
from themis.verdict.plan import Plan

HOLDOUT_PERCENT = 30
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
CONFIRMED = (0.5, 0.6, 0.7, 0.8, 0.9)
UNVERIFIED = (0.1, 0.2, 0.3, 0.4)
SOFT = (0.0, 0.1, 0.2)
PASSING = (VerdictValue.EVIDENCED.value, VerdictValue.PLAUSIBLE.value)
FORBIDDEN_ON_GENUINE = (VerdictValue.CONTRADICTED.value, VerdictValue.TAINTED.value)
COMPARED = ("contradicted_precision", "slop_recall", "injection_catch_rate")


@dataclass(frozen=True)
class Prepared:
    id: str
    label: str
    shield: ShieldStatus
    claims: list[Claim]
    checks: list[Check]


def is_holdout(item_id: str, percent: int = HOLDOUT_PERCENT) -> bool:
    """Fixed by the id alone, so adding items never reshuffles the ones already split."""
    return int(hashlib.sha256(item_id.encode()).hexdigest(), 16) % 100 < percent


async def prepare(settings: Settings, item: dict[str, Any], repository: bench.LocalRepository) -> Prepared:
    """The same path as `bench.run_offline`, stopping before the verdict."""
    submission = bench.to_submission(item)
    label = str(item["class"])
    if any(scan.tainted for scan in stage.scan_fields(submission).values()):
        return Prepared(item["id"], label, ShieldStatus.TAINTED, [], [])
    gold = clerk.ClerkOutput.model_validate({"kind": item["kind"], "claims": item["claims"]})
    claims, _ = clerk.anchor(gold, sanitize.scan(f"{submission.title}\n\n{submission.body}", "submission"))
    claim_set = ClaimSet(kind=SubmissionKind(item["kind"]), claims=claims)
    context = StageContext(settings, RepoPolicy(), cast(OtariClient, None), submission)
    evidence = EvidenceContext(context, cast(GitHubClient, repository), claim_set)
    result = await engine.run(evidence, Plan(deterministic=bench.OFFLINE_MODULES))
    return Prepared(item["id"], label, ShieldStatus.CLEAN, claims, result.checks)


def score_rows(items: list[Prepared], rules: Scoring) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        verdict = scoring.decide(item.shield, item.claims, item.checks, rules)
        rows.append({"id": item.id, "class": item.label, "verdict": verdict.value.value, "score": verdict.score})
    return rows


def _mean(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    genuine = [row for row in rows if row["class"] == "genuine"]
    slop = [row for row in rows if row["class"] == "slop"]
    return {
        **bench.metrics(rows),
        "genuine_pass_rate": sum(row["verdict"] in PASSING for row in genuine) / len(genuine) if genuine else None,
        "score_separation": _mean([row["score"] for row in genuine]) - _mean([row["score"] for row in slop]),
    }


def confusion(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for row in rows:
        verdicts = matrix.setdefault(row["class"], {})
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    return matrix


def feasible(rows: list[dict[str, Any]]) -> bool:
    return not any(row["class"] == "genuine" and row["verdict"] in FORBIDDEN_ON_GENUINE for row in rows)


def candidates() -> list[Scoring]:
    return [
        Scoring(
            weights=Weights(confirmed=confirmed, unverified=unverified, soft_contradicted=soft),
            plausible_threshold=threshold,
        )
        for threshold, confirmed, unverified, soft in itertools.product(THRESHOLDS, CONFIRMED, UNVERIFIED, SOFT)
    ]


def _objective(rows: list[dict[str, Any]]) -> tuple[float, float]:
    summary = summarize(rows)
    quality = (summary["slop_recall"] or 0.0) + (summary["genuine_pass_rate"] or 0.0)
    return round(quality, 6), round(summary["score_separation"], 6)


def _distance(rules: Scoring) -> float:
    """Ties go to the candidate nearest the defaults, so noise never moves a setting."""
    default, weights = Scoring(), rules.weights
    return (
        abs(rules.plausible_threshold - default.plausible_threshold)
        + abs(weights.confirmed - default.weights.confirmed)
        + abs(weights.unverified - default.weights.unverified)
        + abs(weights.soft_contradicted - default.weights.soft_contradicted)
    )


def choose(train: list[Prepared]) -> Scoring | None:
    """Sees only the train split. None when no candidate keeps genuine items safe."""
    best: tuple[tuple[float, float], float, Scoring] | None = None
    for rules in candidates():
        rows = score_rows(train, rules)
        if not feasible(rows):
            continue
        goal, distance = _objective(rows), _distance(rules)
        if best is None or goal > best[0] or (goal == best[0] and distance < best[1]):
            best = (goal, distance, rules)
    return best[2] if best else None


def tuning_report(items: list[Prepared], at: str) -> dict[str, Any]:
    train = [item for item in items if not is_holdout(item.id)]
    holdout = [item for item in items if is_holdout(item.id)]
    baseline = Scoring()
    chosen = choose(train)
    held_base = score_rows(holdout, baseline)
    report: dict[str, Any] = {
        "mode": "tuning",
        "at": at,
        "note": "A report only. Defaults change by hand, after reading the holdout numbers.",
        "split": {"train": len(train), "holdout": len(holdout), "holdout_percent": HOLDOUT_PERCENT},
        "candidates": len(candidates()),
        "baseline": {"rules": baseline.model_dump(), "train": summarize(score_rows(train, baseline)),
                     "holdout": summarize(held_base), "holdout_confusion": confusion(held_base)},
        "chosen": None,
        "holdout_constraint_met": None,
        "holdout_improves": None,
    }  # fmt: skip
    if chosen is not None:
        held = score_rows(holdout, chosen)
        report["chosen"] = {"rules": chosen.model_dump(), "train": summarize(score_rows(train, chosen)),
                            "holdout": summarize(held), "holdout_confusion": confusion(held)}  # fmt: skip
        report["holdout_constraint_met"] = feasible(held)
        report["holdout_improves"] = _objective(held) > _objective(held_base)
    return report


async def run(settings: Settings, at: str) -> dict[str, Any]:
    repository = bench.LocalRepository()
    items = [item for item in bench.load_dataset() if not item.get("live_only")]
    return tuning_report([await prepare(settings, item, repository) for item in items], at)


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Regressions: a metric fell, or a genuine item newly reads as false."""
    regressions: list[str] = []
    for name in COMPARED:
        before, after = old[name], new[name]
        if before is not None and (after is None or after < before):
            regressions.append(f"{name} fell from {before} to {after}")
    added = sorted(
        set(new["false_contradicted_or_tainted_on_genuine"]) - set(old["false_contradicted_or_tainted_on_genuine"])
    )
    regressions += [f"genuine item {item} is now contradicted or tainted" for item in added]
    before_rows = {row["id"]: row["verdict"] for row in old.get("rows", [])}
    after_rows = {row["id"]: row["verdict"] for row in new.get("rows", [])}
    changed = {
        item: (before_rows[item], after_rows[item])
        for item in sorted(before_rows.keys() & after_rows.keys())
        if before_rows[item] != after_rows[item]
    }
    only = sorted(before_rows.keys() ^ after_rows.keys())
    return {"regressions": regressions, "changed": changed, "not_in_both": only}


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a benchmark report")
    missing = [name for name in (*COMPARED, "false_contradicted_or_tainted_on_genuine") if name not in data]
    if missing:
        raise ValueError(f"{path} is not a benchmark report: missing {', '.join(missing)}")
    return data
