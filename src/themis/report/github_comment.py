"""Render the evidence ledger. All text from a submission or a model is made inert here."""

from __future__ import annotations

import html
from typing import Any

from themis.config import RepoPolicy
from themis.models import (
    Check,
    CheckStatus,
    Claim,
    ClaimType,
    FindingSeverity,
    OtariCall,
    ShieldResult,
    Verdict,
    VerdictValue,
)

MARKER = "<!-- themis:ledger -->"
MAX_CELL = 160
ICONS = {
    CheckStatus.REPRODUCED: "✅",
    CheckStatus.CONFIRMED: "✅",
    CheckStatus.CONTRADICTED: "❌",
    CheckStatus.UNVERIFIED: "❔",
    CheckStatus.NOT_APPLICABLE: "➖",
}
HINTS = {
    ClaimType.SYMBOL_EXISTS: "Link the exact file and line of the function or class.",
    ClaimType.FILE_EXISTS: "Give the full path of the file in this repository.",
    ClaimType.VERSION_EXISTS: "Name the released version you tested.",
    ClaimType.BEHAVIOR: "Add a short script that shows the behavior.",
    ClaimType.FIXES_ISSUE: "Link the issue this fixes.",
    ClaimType.TEST_ADDED: "Add a test that fails without this change.",
    ClaimType.CVE_REFERENCE: "Link the advisory.",
    ClaimType.DEPENDENCY_ADDED: "Link the package on its registry.",
    ClaimType.PERFORMANCE: "Share the benchmark and how to run it.",
}


def inert(text: object, limit: int = MAX_CELL) -> str:
    """No mentions, links, HTML, tables or code spans can survive this."""
    flat = " ".join(str(text).split())[:limit]
    escaped = html.escape(flat)
    for char, entity in (
        ("@", "&#64;"),
        ("|", "&#124;"),
        ("`", "&#96;"),
        ("[", "&#91;"),
        ("]", "&#93;"),
        ("#", "&#35;"),
    ):
        escaped = escaped.replace(char, entity)
    return escaped.replace("&amp;#", "&#")


def _result(check: Check) -> str:
    text = str(
        check.detail.get("reason") or check.detail.get("target") or check.detail.get("version") or check.status.value
    )
    strength = f" ({check.strength.value})" if check.strength else ""
    return f"{ICONS[check.status]} {inert(text, 90)}{strength}"


def claim_table(claims: list[Claim], checks: list[Check]) -> list[str]:
    rows = ["| # | Claim | Check | Result |", "|---|---|---|---|"]
    number = 0
    for claim in claims:
        linked = [check for check in checks if check.claim_id == claim.id]
        for check in linked:
            number += 1
            rows.append(f"| {number} | {inert(claim.text)} | {inert(check.module)} | {_result(check)} |")
        if not linked:
            number += 1
            rows.append(f"| {number} | {inert(claim.text)} | — | ❔ not checked |")
    for check in checks:
        if check.claim_id is None and check.status is not CheckStatus.NOT_APPLICABLE:
            number += 1
            rows.append(f"| {number} | — | {inert(check.module)} | {_result(check)} |")
    return rows


def missing_evidence(claims: list[Claim], checks: list[Check]) -> list[str]:
    """Deterministic, so a contributor always gets the same fair checklist."""
    settled = {c.claim_id for c in checks if c.status in (CheckStatus.CONFIRMED, CheckStatus.REPRODUCED)}
    hints = [HINTS[claim.type] for claim in claims if claim.id not in settled and claim.type in HINTS]
    return list(dict.fromkeys(hints))


def _findings(shield: ShieldResult) -> list[str]:
    taints = [item for item in shield.findings if item.severity is FindingSeverity.TAINT]
    lines = [
        f"- `{inert(item.kind, 40)}` in {inert(item.field, 30)}: {inert(item.excerpt_redacted, 100)}" for item in taints
    ]
    return ["", "**Injection findings:**", *lines] if lines else []


def _footer(calls: list[OtariCall], receipt_name: str) -> str:
    priced = [call.cost_usd for call in calls if call.cost_usd is not None]
    cost = f"${sum(priced):.4f}" if priced else "not reported"
    return (
        f"Receipt: `{inert(receipt_name, 80)}` · cost {cost} · Otari requests: {len(calls)} · "
        "override: `/themis override <verdict> <reason>`"
    )


def render(
    verdict: Verdict,
    explanation: str,
    claims: list[Claim],
    checks: list[Check],
    shield: ShieldResult,
    calls: list[OtariCall],
    receipt_name: str,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    asks: list[str] | None = None,
) -> str:
    """`notes` are lines the caller already made inert. `asks` are fixed text from a scanner."""
    lines = [
        MARKER,
        f"### THEMIS · {verdict.value.value} · evidence score {verdict.score}/100",
        "",
        inert(explanation, 900),
        "",
    ]
    lines += claim_table(claims, checks) if claims or checks else ["No checkable claims were found."]
    needs = verdict.value is VerdictValue.NEEDS_EVIDENCE
    wanted = [*missing_evidence(claims, checks), *(asks or [])] if needs else []
    if wanted:
        lines += ["", "**What would help:**", *[f"- [ ] {item}" for item in wanted]]
    lines += _findings(shield)
    lines += notes or []
    if errors:
        lines += ["", "**Gateway errors:**", *[f"- {inert(item, 200)}" for item in errors]]
    return "\n".join([*lines, "", _footer(calls, receipt_name)]) + "\n"


def render_explain(checks: list[Check]) -> str:
    rows = ["### THEMIS checks", "", "| Module | Status | Strength | Detail |", "|---|---|---|---|"]
    for check in checks:
        strength = check.strength.value if check.strength else "—"
        detail = inert(check.detail, 140)
        rows.append(f"| {inert(check.module)} | {check.status.value} | {strength} | {detail} |")
    return "\n".join(rows) + "\n"


def check_run(verdict: Verdict, policy: RepoPolicy, head_sha: str, summary: str) -> dict[str, Any]:
    """Only `gate` mode can fail a check, and only on these two verdicts. Nothing closes."""
    blocking = (VerdictValue.CONTRADICTED, VerdictValue.TAINTED, VerdictValue.TAINTED_UNCHECKED)
    failed = policy.mode == "gate" and verdict.value in blocking
    return {
        "name": "THEMIS",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "failure" if failed else "neutral",
        "output": {"title": f"{verdict.value.value} ({verdict.score}/100)", "summary": summary[:60000]},
    }
