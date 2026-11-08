"""Run every enabled scanner. One failing scanner never stops the others or the ledger."""

from __future__ import annotations

from themis.config import RepoPolicy
from themis.logs import get_logger
from themis.models import Signal, SignalSeverity, Submission, SubmissionKind
from themis.report.github_comment import inert
from themis.signals import change_quality, completeness, secrets, supply_chain
from themis.signals.base import Scanner, SignalContext, make_context

log = get_logger(__name__)
MAX_SHOWN = 15
ORDER = {SignalSeverity.CRITICAL: 0, SignalSeverity.WARN: 1, SignalSeverity.INFO: 2}
ICONS = {SignalSeverity.CRITICAL: "🛑", SignalSeverity.WARN: "⚠️", SignalSeverity.INFO: "ℹ️"}
SCANNERS: dict[str, Scanner] = {
    "secrets": secrets.scan,
    "supply_chain": supply_chain.scan,
    "change_quality": change_quality.scan,
    "completeness": completeness.scan,
}


def collect(
    submission: Submission,
    policy: RepoPolicy,
    kind: SubmissionKind | None = None,
    *,
    scanners: dict[str, Scanner] | None = None,
) -> list[Signal]:
    context: SignalContext = make_context(submission, policy, kind)
    enabled = policy.signals.model_dump()
    found: list[Signal] = []
    for name, scanner in (SCANNERS if scanners is None else scanners).items():
        if not enabled.get(name, True):
            continue
        try:
            found.extend(scanner(context))
        except (KeyError, ValueError, TypeError, IndexError) as error:
            log.warning("signals.scanner_failed", stage=name, submission_id=submission.id, error=type(error).__name__)
    return sorted(found, key=lambda item: (ORDER[item.severity], item.file or "", item.line or 0))


def labels_for(signals: list[Signal], prefix: str) -> list[str]:
    """A fixed table, so a submission cannot name its own label."""
    names = {"secret_leak": "secret-leak", "risky_change": "risky-change"}
    return [f"{prefix}{names[s.code]}" for s in signals if s.code in names and s.severity is SignalSeverity.CRITICAL]


def ledger_lines(signals: list[Signal]) -> list[str]:
    if not signals:
        return []
    lines = ["", "**Worth a look** (facts about the change, not part of the verdict):"]
    for item in signals[:MAX_SHOWN]:
        where = f" in {inert(item.file, 80)}" if item.file else ""
        line = f":{item.line}" if item.file and item.line else ""
        lines.append(f"- {ICONS[item.severity]} {inert(item.title, 120)}{where}{line}")
    if len(signals) > MAX_SHOWN:
        lines.append(f"- and {len(signals) - MAX_SHOWN} more in the receipt")
    return lines
