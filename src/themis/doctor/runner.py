"""Run probes, keep honest statuses, and build the readiness table."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from themis import __version__
from themis.config import ConfigError, Settings
from themis.doctor.probes import FEATURES, ClientFactory, Feature, ProbeBlocked, ProbeContext, ProbeFailed
from themis.models import OtariCall
from themis.otari.client import OtariClient
from themis.otari.errors import (
    OtariError,
    OtariForbidden,
    OtariFundingError,
    OtariProviderNotConfigured,
)

ADVANCED_NEEDED = 6
CORE_NEEDED = 2
# Rubric feature -> probes that must pass, and whether it is an advanced feature.
RUBRIC: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("Open-weight model", ("chat-a",), False),
    ("Second open-weight role", ("chat-b",), False),
    ("Routing & fallback", ("routing",), True),
    ("MCP", ("mcp",), True),
    ("Web search / fetch", ("web",), True),
    ("Code execution", ("code",), True),
    ("Budgets", ("budget",), True),
    ("Multiple model roles", ("chat-a", "chat-b"), True),
    ("Inference guardrails", ("guardrail",), True),
    ("Per-user budgets", ("per-user",), True),
)
SETUP_ERRORS = (OtariFundingError, OtariProviderNotConfigured, OtariForbidden)


def default_factory(settings: Settings, calls: list[OtariCall]) -> ClientFactory:
    def make(api_key: str | None) -> OtariClient:
        return OtariClient(settings, api_key=api_key, on_call=calls.append)

    return make


async def run_feature(ctx: ProbeContext, feature: Feature) -> dict[str, Any]:
    ctx.calls.clear()
    result: dict[str, Any] = {
        "feature": feature.name,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": ctx.settings.base_url,
        "themis_version": __version__,
    }
    try:
        result["observed"] = await feature.probe(ctx)
        result["status"] = "PASS"
    except ProbeBlocked as error:
        result.update(status="BLOCKED", reason=str(error))
    except SETUP_ERRORS as error:
        result.update(status="BLOCKED", reason=str(error))
    except (ProbeFailed, OtariError, httpx.HTTPError) as error:
        result.update(status="FAIL", reason=str(error))
    result["calls"] = [call.model_dump(mode="json") for call in ctx.calls]
    return result


async def run_features(
    settings: Settings,
    names: list[str] | None = None,
    make_client: ClientFactory | None = None,
    calls: list[OtariCall] | None = None,
) -> list[dict[str, Any]]:
    """Run probes in order. A key rejected by the first probe would fail every later call, so stop."""
    settings.api_key()
    recorded = [] if calls is None else calls
    make = make_client or default_factory(settings, recorded)
    results: list[dict[str, Any]] = []
    chosen = [feature for feature in FEATURES if not names or feature.name in names]
    async with make(None) as client:
        ctx = ProbeContext(settings=settings, make_client=make, client=client, calls=recorded)
        for feature in chosen:
            results.append(await run_feature(ctx, feature))
            if len(results) == 1 and any(call.status == 401 for call in recorded):
                raise ConfigError("the gateway rejected the API key (401)")
    return results


def gate(results: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Count features whose every probe passed: (advanced, core)."""
    advanced, core = 0, 0
    for _, probes, is_advanced in RUBRIC:
        if all(results.get(name, {}).get("status") == "PASS" for name in probes):
            advanced += is_advanced
            core += not is_advanced
    return advanced, core


def gate_met(results: dict[str, dict[str, Any]]) -> bool:
    advanced, core = gate(results)
    return advanced >= ADVANCED_NEEDED and core == CORE_NEEDED


def render_table(results: dict[str, dict[str, Any]]) -> str:
    icon = {"PASS": "✅", "FAIL": "⛔", "BLOCKED": "⛔"}
    rubric_rows = []
    for name, probes, is_advanced in RUBRIC:
        statuses = [results.get(probe, {}).get("status") for probe in probes]
        passed = all(status == "PASS" for status in statuses)
        mark = "✅" if passed else ("⛔" if any(status in icon for status in statuses) else "⏳")
        rubric_rows.append(f"| {name} | {'advanced' if is_advanced else 'core'} | {mark} |")
    feature_rows = []
    for feature in FEATURES:
        result = results.get(feature.name, {})
        status = result.get("status")
        ids = [call["request_id"] for call in result.get("calls", []) if call.get("request_id")]
        reason = str(result.get("reason", "")).replace("|", "\\|")
        state = f"{icon.get(status or '', '⏳')} {status or 'NOT RUN'}"
        feature_rows.append(f"| `{feature.name}` | {state} | {f'`{ids[-1]}`' if ids else '—'} | {reason} |")
    advanced, core = gate(results)
    verdict = "met" if gate_met(results) else "NOT met"
    live = any(call.get("request_id") for result in results.values() for call in result.get("calls", []))
    return "\n".join([
        "# Readiness",
        "",
        "Generated by `themis evidence capture all`. Do not edit by hand.",
        "",
        f"**Exit gate {verdict}:** {advanced} of 8 advanced features ✅ (need {ADVANCED_NEEDED}), "
        f"{core} of {CORE_NEEDED} core features ✅.",
        "",
        *([] if live else ["No live gateway call has been recorded yet.", ""]),
        "| Rubric feature | Kind | State |",
        "|---|---|---|",
        *rubric_rows,
        "",
        "| Probe | Result | Last request ID | Reason |",
        "|---|---|---|---|",
        *feature_rows,
        "",
    ])  # fmt: skip
