"""Benchmark against the local demo library.

Offline mode feeds gold claims into the real sanitizer, evidence modules and verdict, with no
model and no network. It measures the deterministic core. `--live` swaps in the real Clerk.
The dataset was written alongside THEMIS, so these numbers show regression, not field accuracy.
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from themis.agents import clerk
from themis.config import RepoPolicy, Settings
from themis.context import StageContext
from themis.evidence import engine
from themis.evidence.base import EvidenceContext
from themis.evidence.dependencies import RegistryClient
from themis.intake.github import GitHubClient
from themis.models import ClaimSet, ShieldStatus, Submission, SubmissionKind, VerdictValue
from themis.otari.client import OtariClient
from themis.pipeline import analyze
from themis.shield import sanitize, stage
from themis.verdict import scoring
from themis.verdict.plan import Plan

DATASET_DIR = Path("bench/dataset")
RESULTS_DIR = Path("bench/results")
DEMO_DIR = Path("demo/tinyhttp-lite")
DEMO_REPO = "themis-demo/tinyhttp-lite"
DEMO_TAGS = ["v0.3.1", "v0.3.0", "v0.2.0"]
OFFLINE_MODULES = ["symbols", "versions", "tests", "consistency"]
FLAGGED = (VerdictValue.CONTRADICTED.value, VerdictValue.NEEDS_EVIDENCE.value)


class LocalRepository:
    """Stands in for GitHub with the demo library on disk. Reads files, writes nothing."""

    def __init__(self, root: Path = DEMO_DIR) -> None:
        self._root = root

    async def default_branch(self, repo: str) -> str:
        return "main"

    async def tags(self, repo: str) -> list[str]:
        return DEMO_TAGS

    async def tarball(self, repo: str, ref: str) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in sorted(self._root.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    tar.add(path, arcname=f"demo/{path.relative_to(self._root)}")
        return buffer.getvalue()

    async def issue_exists(self, repo: str, number: int) -> bool | None:
        return number == 12

    async def search_issues(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []


def load_dataset(subset: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(DATASET_DIR.glob("*.json")):
        if subset is None or path.stem == subset:
            items.extend(json.loads(path.read_text()))
    return items


def to_submission(item: dict[str, Any]) -> Submission:
    return Submission(
        id=item["id"], repo=DEMO_REPO, number=1, title=item["title"], body=item["body"], contributor_key="gh:1",
        is_pull_request=item.get("is_pull_request", False), changed_files=item.get("changed_files", []),
        diff_summary="\n".join(item.get("changed_files", [])),
    )  # fmt: skip


async def run_offline(settings: Settings, item: dict[str, Any], repository: LocalRepository) -> str:
    submission = to_submission(item)
    if any(field.tainted for field in stage.scan_fields(submission).values()):
        return VerdictValue.TAINTED.value
    gold = clerk.ClerkOutput.model_validate({"kind": item["kind"], "claims": item["claims"]})
    claims, _ = clerk.anchor(gold, sanitize.scan(f"{submission.title}\n\n{submission.body}", "submission"))
    claim_set = ClaimSet(kind=SubmissionKind(item["kind"]), claims=claims)
    context = StageContext(settings, RepoPolicy(), cast(OtariClient, None), submission)
    evidence = EvidenceContext(context, cast(GitHubClient, repository), claim_set)
    result = await engine.run(evidence, Plan(deterministic=OFFLINE_MODULES))
    return scoring.decide(ShieldStatus.CLEAN, claims, result.checks, RepoPolicy().scoring).value.value


async def run_live(settings: Settings, item: dict[str, Any], repository: LocalRepository) -> str:
    async with OtariClient(settings) as client, RegistryClient() as registry:
        outcome = await analyze(
            settings, RepoPolicy(), to_submission(item), client, cast(GitHubClient, repository), registry
        )
    return outcome.verdict.value.value


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def of(name: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["class"] == name]

    genuine, slop, injection = of("genuine"), of("slop"), of("injection")
    contradicted = [row for row in rows if row["verdict"] == VerdictValue.CONTRADICTED.value]
    wrong = [
        row["id"] for row in genuine if row["verdict"] in (VerdictValue.CONTRADICTED.value, VerdictValue.TAINTED.value)
    ]
    right = [row for row in contradicted if row["class"] != "genuine"]
    return {
        "items": len(rows),
        "false_contradicted_or_tainted_on_genuine": wrong,
        "contradicted_precision": len(right) / len(contradicted) if contradicted else None,
        "slop_recall": sum(row["verdict"] in FLAGGED for row in slop) / len(slop) if slop else None,
        "slop_contradicted": sum(row["verdict"] == VerdictValue.CONTRADICTED.value for row in slop),
        "injection_catch_rate": (
            sum(row["verdict"] == VerdictValue.TAINTED.value for row in injection) / len(injection)
            if injection
            else None
        ),
    }


async def run(settings: Settings, *, live: bool = False, subset: str | None = None) -> dict[str, Any]:
    repository = LocalRepository()
    runner = run_live if live else run_offline
    rows = []
    for item in load_dataset(subset):
        if not live and item.get("live_only"):
            continue
        rows.append({"id": item["id"], "class": item["class"], "verdict": await runner(settings, item, repository)})
    moment = datetime.now(UTC)
    return {
        "mode": "live" if live else "offline",
        "at": moment.isoformat(timespec="seconds"),
        **metrics(rows),
        "rows": rows,
    }


def write_results(report: dict[str, Any], directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['at'][:10]}-{report['mode']}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path
