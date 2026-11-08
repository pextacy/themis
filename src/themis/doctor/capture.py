"""`themis evidence capture`: live probes written as redacted receipts.

Only hosted Otari may write to `evidence/`, and a test double never can.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from themis.config import ConfigError, Settings
from themis.doctor.probes import FEATURES, ClientFactory
from themis.doctor.runner import render_table, run_features
from themis.models import OtariCall
from themis.store import redact

EVIDENCE_DIR = Path("evidence")
READINESS_PATH = Path("docs/READINESS.md")


def stored_results(directory: Path) -> dict[str, dict[str, Any]]:
    stored: dict[str, dict[str, Any]] = {}
    for feature in FEATURES:
        path = directory / feature.receipt
        if path.exists():
            stored[feature.name] = json.loads(path.read_text())
    return stored


def pending_features(directory: Path) -> list[str]:
    """Rerunning a pass risks replacing it with a free-tier 429, so a retry skips what passed."""
    stored = stored_results(directory)
    return [feature.name for feature in FEATURES if stored.get(feature.name, {}).get("status") != "PASS"]


def write_receipt(directory: Path, name: str, result: dict[str, Any], settings: Settings) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(redact(result, settings.secrets()), indent=2, sort_keys=True) + "\n")
    return path


def check_destination(settings: Settings, directory: Path, *, test_double: bool) -> None:
    if test_double and directory.resolve().is_relative_to(EVIDENCE_DIR.resolve()):
        raise ConfigError("a test double must never write to evidence/")
    if not test_double and not settings.is_hosted:
        raise ConfigError(f"evidence must come from hosted Otari, not {settings.base_url}")


def persist(settings: Settings, results: list[dict[str, Any]], directory: Path, readiness: Path) -> None:
    by_name = {feature.name: feature for feature in FEATURES}
    for result in results:
        # Catalog reads carry no request ID, so a passing read is its own proof. A failed call
        # with no ID proves nothing and is not written.
        if result["status"] == "PASS" or any(call.get("request_id") for call in result["calls"]):
            write_receipt(directory, by_name[result["feature"]].receipt, result, settings)
    merged = {**stored_results(directory), **{result["feature"]: result for result in results}}
    readiness.parent.mkdir(parents=True, exist_ok=True)
    readiness.write_text(render_table(redact(merged, settings.secrets())))


def capture(
    settings: Settings,
    names: list[str] | None = None,
    *,
    directory: Path = EVIDENCE_DIR,
    readiness: Path = READINESS_PATH,
    make_client: ClientFactory | None = None,
    calls: list[OtariCall] | None = None,
) -> list[dict[str, Any]]:
    check_destination(settings, directory, test_double=make_client is not None)
    results = asyncio.run(run_features(settings, names, make_client, calls))
    persist(settings, results, directory, readiness)
    return results
