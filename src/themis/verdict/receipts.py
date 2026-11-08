"""Build and write `themis.receipt.v1`. Everything passes through redaction first."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from themis import __version__
from themis.models import (
    Check,
    Claim,
    OtariCall,
    Receipt,
    ShieldResult,
    Signal,
    Submission,
    SubmissionKind,
    SubmissionRef,
    Verdict,
)
from themis.store import redact

RECEIPTS_DIR = Path("receipts")


def build(
    submission: Submission,
    kind: SubmissionKind,
    verdict: Verdict,
    claims: list[Claim],
    checks: list[Check],
    calls: list[OtariCall],
    shield: ShieldResult,
    signals: list[Signal] | None = None,
) -> Receipt:
    guardrail = {key: shield.guardrail.get(key) for key in ("profile", "mode", "state", "request_id")}
    findings = [item.model_dump(mode="json") for item in shield.findings]
    return Receipt(
        submission=SubmissionRef(
            repo=submission.repo, number=submission.number, kind=kind, head_sha=submission.head_sha
        ),
        verdict=verdict,
        claims=claims,
        checks=checks,
        otari_calls=calls,
        shield={"guardrail": guardrail, "status": shield.status.value, "sanitizer": findings},
        signals=signals or [],
        themis_version=__version__,
    )


def to_json(receipt: Receipt, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    clean: dict[str, Any] = redact(receipt.model_dump(mode="json", by_alias=True), secrets)
    return clean


def write(receipt: Receipt, name: str, secrets: tuple[str, ...] = (), directory: Path = RECEIPTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"themis-{name}.json"
    path.write_text(json.dumps(to_json(receipt, secrets), indent=2, sort_keys=True) + "\n")
    return path
