"""Shield stage: sanitize every field, build the data envelope, ask the guardrail."""

from __future__ import annotations

import html

from themis.context import StageContext
from themis.models import FindingSeverity, ShieldFinding, ShieldResult, ShieldStatus, Submission
from themis.shield import confusables, encoded, guardrails, sanitize
from themis.shield.guardrails import GuardrailOutcome, GuardrailState

MAX_VIEW_CHARS = 20_000


def scan_fields(submission: Submission) -> dict[str, sanitize.Sanitized]:
    fields = {"title": submission.title, "body": submission.body, "diff": submission.diff_excerpt}
    fields |= {f"comment[{index}]": text for index, text in enumerate(submission.comments)}
    scanned = {name: sanitize.scan(text, name) for name, text in fields.items()}
    for name, text in fields.items():
        scanned[name].findings.extend([*confusables.scan(text, name), *encoded.scan(text, name)])
    return scanned


def envelope(submission: Submission, content: str) -> str:
    """The only way submission text reaches a model: escaped, inside a user message."""
    kind = "pull_request" if submission.is_pull_request else "issue"
    return f'<untrusted_submission id="{submission.id}" kind="{kind}">\n{content}\n</untrusted_submission>'


def clean_view(submission: Submission, scanned: dict[str, sanitize.Sanitized]) -> str:
    """Author text only: other people's comments are screened, never read as claims."""
    parts = [f"TITLE: {scanned['title'].clean}", f"BODY:\n{scanned['body'].clean}"]
    if submission.is_pull_request:
        parts.append(f"CHANGED FILES:\n{submission.diff_summary}")
    joined = "\n\n".join(parts)
    return html.escape(joined[:MAX_VIEW_CHARS], quote=False)


def _guardrail_payload(view: str, scanned: dict[str, sanitize.Sanitized]) -> str:
    """Hidden text never reaches the Clerk, but the guardrail should still judge it."""
    hidden = [text for item in scanned.values() for text in item.hidden]
    comments = [item.clean for name, item in scanned.items() if name.startswith("comment[")]
    extra = html.escape("\n".join([*hidden, *comments, scanned["diff"].clean]), quote=False)
    return f"{view}\n\n{extra}"[: MAX_VIEW_CHARS * 2]


def _status(findings: list[ShieldFinding], outcome: GuardrailOutcome) -> ShieldStatus:
    """Taint outranks everything: an injection stays visible even when budget ran out."""
    if outcome.state is GuardrailState.BLOCKED or any(f.severity is FindingSeverity.TAINT for f in findings):
        return ShieldStatus.TAINTED
    if outcome.state is GuardrailState.DEFERRED:
        return ShieldStatus.DEFERRED
    if outcome.state is GuardrailState.UNCHECKED:
        return ShieldStatus.TAINTED_UNCHECKED
    return ShieldStatus.CLEAN


async def run(ctx: StageContext) -> ShieldResult:
    scanned = scan_fields(ctx.submission)
    findings = [finding for item in scanned.values() for finding in item.findings]
    view = envelope(ctx.submission, clean_view(ctx.submission, scanned))
    outcome = await guardrails.check(
        ctx, envelope(ctx.submission, _guardrail_payload(clean_view(ctx.submission, scanned), scanned))
    )
    if outcome.state in (GuardrailState.BLOCKED, GuardrailState.FLAGGED):
        severity = FindingSeverity.TAINT if outcome.state is GuardrailState.BLOCKED else FindingSeverity.NOTE
        findings.append(ShieldFinding(source="guardrail", kind=outcome.profile, severity=severity, field="submission"))
    status = _status(findings, outcome)
    return ShieldResult(
        status=status,
        findings=findings,
        guardrail=outcome.model_dump(mode="json"),
        clean_view=view if status is ShieldStatus.CLEAN else "",
    )
