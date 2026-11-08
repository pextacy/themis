"""Shared context and the rules for when a check may be HARD."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from themis.context import StageContext
from themis.evidence.snapshot import Snapshot, from_tarball
from themis.intake.github import GitHubClient
from themis.models import Check, CheckStatus, Claim, ClaimSet, Strength, SubmissionKind

MAX_TAG_FALLBACKS = 3
HARD_KINDS = frozenset({
    SubmissionKind.BUG_REPORT, SubmissionKind.SECURITY_REPORT, SubmissionKind.BUGFIX_PR,
    SubmissionKind.FEATURE_PR, SubmissionKind.DOCS_PR, SubmissionKind.DEPENDENCY_PR,
})  # fmt: skip
# Words beside a claim showing it is about the author's own or proposed code.
NOT_PROJECT_CODE = re.compile(
    r"\b(?:my|our|mine|i\s+wrote|i\s+have\s+a|we\s+have\s+a|custom|proposed?|propose|new|adds?|adding|introduc\w+|"
    r"would|should|could|might|such\s+as|e\.g\.|like\s+a|hypothetical|example|creat\w+|save\w*|put|write|"
    r"files?\s+named|reproduc\w+|following|project\s+with)\b",
    re.IGNORECASE,
)
FENCED = re.compile(r"```.*?```", re.DOTALL)
CONTEXT_CHARS = 60


@dataclass
class EvidenceContext:
    stage: StageContext
    github: GitHubClient
    claims: ClaimSet
    registry: Any = None
    test_results: dict[str, Any] | None = None
    _snapshots: dict[str, Snapshot] = field(default_factory=dict)
    _tags: list[str] | None = None
    _default: str | None = None

    @property
    def repo(self) -> str:
        return self.stage.submission.repo

    @property
    def source_text(self) -> str:
        """The text that claim spans index."""
        return f"{self.stage.submission.title}\n\n{self.stage.submission.body}"

    async def default_ref(self) -> str:
        if self._default is None:
            self._default = await self.github.default_branch(self.repo)
        return self._default

    async def tags(self) -> list[str]:
        if self._tags is None:
            self._tags = await self.github.tags(self.repo)
        return self._tags

    async def snapshot(self, ref: str) -> Snapshot:
        if ref not in self._snapshots:
            project = self.repo.split("/")[-1]
            self._snapshots[ref] = from_tarball(ref, await self.github.tarball(self.repo, ref), project)
        return self._snapshots[ref]

    async def primary(self) -> Snapshot:
        return await self.snapshot(self.stage.submission.head_sha or await self.default_ref())

    async def search_refs(self) -> list[str]:
        """Where something must be absent before it counts as absent.

        The head, then the default branch so a PR may remove things, then recent tags so a
        report against an older release is not contradicted by a later rename.
        """
        refs = [self.stage.submission.head_sha, await self.default_ref(), *(await self.tags())[:MAX_TAG_FALLBACKS]]
        return list(dict.fromkeys(ref for ref in refs if ref))

    def claim_context(self, claim: Claim) -> str:
        if claim.span is None:
            return ""
        start, end = claim.span
        return self.source_text[max(start - CONTEXT_CHARS, 0) : end + CONTEXT_CHARS]

    def defined_by_author(self, name: str) -> bool:
        """True when the submission's own code block defines this name."""
        pattern = rf"\b(?:def|class|function|fn|func|const|let|var)\s+{re.escape(name)}\b|\b{re.escape(name)}\s*="
        return any(re.search(pattern, block) for block in FENCED.findall(self.stage.submission.body))

    def may_be_hard(self, claim: Claim) -> bool:
        """Precision first: only a plain assertion about this project can be contradicted."""
        if self.claims.kind not in HARD_KINDS or claim.normalized_target is None:
            return False
        return NOT_PROJECT_CODE.search(self.claim_context(claim)) is None


def make_check(
    module: str, status: CheckStatus, claim: Claim | None = None, *, strength: Strength | None = None, **detail: Any
) -> Check:
    return Check(claim_id=claim.id if claim else None, module=module, status=status, strength=strength, detail=detail)
