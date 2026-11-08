"""Domain models shared by every pipeline stage and persisted record."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    CLERK = "clerk"
    PROVER = "prover"
    ARBITER = "arbiter"
    DOCTOR = "doctor"
    EVIDENCE = "evidence"


class SubmissionKind(StrEnum):
    BUG_REPORT = "bug_report"
    SECURITY_REPORT = "security_report"
    FEATURE_REQUEST = "feature_request"
    BUGFIX_PR = "bugfix_pr"
    FEATURE_PR = "feature_pr"
    DOCS_PR = "docs_pr"
    DEPENDENCY_PR = "dependency_pr"
    QUESTION = "question"
    OTHER = "other"


class ClaimType(StrEnum):
    SYMBOL_EXISTS = "symbol_exists"
    FILE_EXISTS = "file_exists"
    VERSION_EXISTS = "version_exists"
    BEHAVIOR = "behavior"
    FIXES_ISSUE = "fixes_issue"
    TEST_ADDED = "test_added"
    CVE_REFERENCE = "cve_reference"
    DEPENDENCY_ADDED = "dependency_added"
    PERFORMANCE = "performance"
    OTHER = "other"


class CheckStatus(StrEnum):
    REPRODUCED = "REPRODUCED"
    CONFIRMED = "CONFIRMED"
    CONTRADICTED = "CONTRADICTED"
    UNVERIFIED = "UNVERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Strength(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class VerdictValue(StrEnum):
    EVIDENCED = "EVIDENCED"
    PLAUSIBLE = "PLAUSIBLE"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    CONTRADICTED = "CONTRADICTED"
    TAINTED = "TAINTED"
    TAINTED_UNCHECKED = "TAINTED_UNCHECKED"
    DEFERRED = "DEFERRED"


class Submission(BaseModel):
    id: str
    repo: str
    number: int
    kind: SubmissionKind = SubmissionKind.OTHER
    title: str
    body: str = ""
    contributor_key: str
    first_time: bool = False
    is_pull_request: bool = False
    head_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    diff_excerpt: str = ""
    comments: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    id: str
    type: ClaimType
    text: str
    span: tuple[int, int] | None = None
    normalized_target: str | None = None


class Check(BaseModel):
    claim_id: str | None = None
    module: str
    status: CheckStatus
    strength: Strength | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    otari_request_id: str | None = None


class ShieldStatus(StrEnum):
    CLEAN = "CLEAN"
    TAINTED = "TAINTED"
    TAINTED_UNCHECKED = "TAINTED_UNCHECKED"
    DEFERRED = "DEFERRED"


class FindingSeverity(StrEnum):
    TAINT = "TAINT"
    NOTE = "NOTE"


class ShieldFinding(BaseModel):
    """Evidence of hidden or manipulative text. Span offsets index the original field."""

    source: Literal["sanitizer", "guardrail"]
    kind: str
    severity: FindingSeverity
    field: str
    span: tuple[int, int] | None = None
    excerpt_redacted: str = ""


class ShieldResult(BaseModel):
    status: ShieldStatus
    findings: list[ShieldFinding] = Field(default_factory=list)
    guardrail: dict[str, Any] = Field(default_factory=dict)
    clean_view: str = ""


class ClaimSet(BaseModel):
    kind: SubmissionKind
    claims: list[Claim] = Field(default_factory=list)
    degraded: bool = False
    reason: str | None = None
    dropped_unanchored: int = 0
    otari_request_ids: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    value: VerdictValue
    score: int = Field(ge=0, le=100)
    weights: dict[str, float] = Field(default_factory=dict)


class Usage(BaseModel):
    prompt: int | None = None
    completion: int | None = None


class OtariCall(BaseModel):
    """One gateway response. request_id is None only when the gateway issued none."""

    request_id: str | None
    role: Role
    stage: str | None = None
    endpoint: str
    status: int | None
    outcome: str
    requested_model: str | None = None
    effective_model: str | None = None
    provider: str | None = None
    fallback: bool | None = None
    attempt_count: int | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    latency_ms: int
    prompt_version: str | None = None


class SignalSeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Signal(BaseModel):
    """A fact for the maintainer about a change. Never an input to the verdict.

    `title` is fixed text from the scanner. Anything taken from a submission goes in `detail`
    and is made inert when rendered.
    """

    module: str
    code: str
    severity: SignalSeverity
    title: str
    file: str | None = None
    line: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SubmissionRef(BaseModel):
    repo: str
    number: int
    kind: SubmissionKind
    head_sha: str | None = None


class Receipt(BaseModel):
    schema_id: Literal["themis.receipt.v1"] = Field(default="themis.receipt.v1", alias="schema")
    submission: SubmissionRef
    verdict: Verdict
    claims: list[Claim] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    otari_calls: list[OtariCall] = Field(default_factory=list)
    shield: dict[str, Any] = Field(default_factory=dict)
    signals: list[Signal] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    themis_version: str

    model_config = {"populate_by_name": True}
