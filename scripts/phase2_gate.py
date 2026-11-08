"""Phase 2 exit gate on the 26 submission fixtures.

    uv run python scripts/phase2_gate.py          # offline: the sanitizer only
    uv run python scripts/phase2_gate.py --live   # Shield and Clerk on hosted Otari

Live mode writes bench/results/phase2-gate.json. Gates: Clerk JSON validity >= 95% and every
injection fixture flagged. The fixtures were written alongside the sanitizer, so this is a
regression gate, not a measure of real-world catch rate.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from themis.agents import clerk
from themis.config import ConfigError, RepoPolicy, Settings, load_settings
from themis.context import StageContext
from themis.doctor.probes import SYNTHETIC_ID_BASE
from themis.logs import configure_logging
from themis.models import ShieldStatus, Submission
from themis.otari.budgets import contributor_key
from themis.otari.client import OtariClient
from themis.otari.errors import OtariBudgetExceeded, OtariError
from themis.shield import stage
from themis.store import redact

FIXTURES = Path("tests/fixtures/submissions")
RESULTS = Path("bench/results/phase2-gate.json")
MIN_VALIDITY = 0.95
# A fresh synthetic contributor per run, so an earlier run's end-user budget never refuses this one.
CONTRIBUTOR = contributor_key(SYNTHETIC_ID_BASE + secrets.randbelow(10**9))


def load_fixtures() -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))]


def to_submission(fixture: dict[str, Any]) -> Submission:
    return Submission(
        id=fixture["id"],
        repo="themis-demo/tinyhttp-lite",
        number=1,
        title=fixture["title"],
        body=fixture["body"],
        contributor_key=CONTRIBUTOR,
        is_pull_request=fixture["is_pull_request"],
        changed_files=fixture["changed_files"],
        diff_summary="\n".join(fixture["changed_files"]),
    )


def offline_report(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    tainted = {item["id"]: any(s.tainted for s in stage.scan_fields(to_submission(item)).values()) for item in fixtures}
    honest = [item["id"] for item in fixtures if item["class"] != "injection" and tainted[item["id"]]]
    missed = [item["id"] for item in fixtures if item["class"] == "injection" and not tainted[item["id"]]]
    return {"fixtures": len(fixtures), "honest_tainted": honest, "injection_missed": missed}


async def live_one(settings: Settings, client: OtariClient, fixture: dict[str, Any]) -> dict[str, Any]:
    ctx = StageContext(settings, RepoPolicy(), client, to_submission(fixture))
    ctx.shield = await stage.run(ctx)
    row: dict[str, Any] = {"id": fixture["id"], "class": fixture["class"], "shield": ctx.shield.status.value}
    row["guardrail_state"] = ctx.shield.guardrail.get("state")
    if ctx.shield.status is not ShieldStatus.CLEAN:
        return row
    result = await clerk.run(ctx)
    found = {claim.type.value for claim in result.claims}
    expected = set(fixture["expected_claim_types"])
    row.update(
        valid=not result.degraded,
        reason=result.reason,
        kind_correct=result.kind.value == fixture["expected_kind"],
        claim_recall=round(len(found & expected) / len(expected), 3) if expected else 1.0,
        dropped_unanchored=result.dropped_unanchored,
        request_ids=result.otari_request_ids,
    )
    return row


async def live_report(settings: Settings, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    async with OtariClient(settings) as client:
        rows = [await live_one(settings, client, fixture) for fixture in fixtures]
    # A provider quota refusal is no answer at all, so it says nothing about the model's validity.
    checked = [row for row in rows if "valid" in row and "upstream_rate_limited" not in (row["reason"] or "")]
    validity = sum(row["valid"] for row in checked) / len(checked) if checked else 0.0
    missed = [r["id"] for r in rows if r["class"] == "injection" and r["shield"] != ShieldStatus.TAINTED.value]
    return {"clerk_validity": validity, "answered": len(checked), "injection_missed": missed, "rows": rows}


def main() -> int:
    fixtures = load_fixtures()
    report = offline_report(fixtures)
    print(json.dumps(report, indent=2))
    if report["honest_tainted"] or report["injection_missed"]:
        return 1
    if "--live" not in sys.argv:
        print("offline checks passed; Clerk validity needs --live")
        return 0
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        live = asyncio.run(live_report(settings, fixtures))
    except (ConfigError, OtariBudgetExceeded, OtariError) as error:
        print(f"cannot run live: {error}")
        return 1
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(redact(live, settings.secrets()), indent=2) + "\n")
    print(
        f"clerk validity {live['clerk_validity']:.0%} of {live['answered']} answered,",
        f"injection missed {live['injection_missed']}",
    )
    return 0 if live["clerk_validity"] >= MIN_VALIDITY and not live["injection_missed"] else 1


if __name__ == "__main__":
    sys.exit(main())
