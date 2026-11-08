"""Does the description match the change? Every mismatch here is SOFT."""

from __future__ import annotations

import re

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.dependencies import MANIFEST_NAMES
from themis.evidence.tests_results import is_test_path
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength, SubmissionKind

MODULE = "consistency"
ISSUE_REF = re.compile(r"(?<![\w/#])#(\d{1,9})\b")
CHANGE_VERB = re.compile(r"\b(?:updates?|changes?|modif(?:y|ies)|edits?|rewrites?|refactors?|fixes|touches)\b", re.I)
DOC_PATH = re.compile(r"(?:^|/)docs?/|\.(?:md|rst|txt|adoc)$|(?:^|/)(?:README|CHANGELOG|LICENSE)", re.I)


def _quote(ctx: EvidenceContext, claim: Claim) -> str:
    start, end = claim.span or (0, 0)
    return ctx.source_text[start:end]


async def check_issue_ref(ctx: EvidenceContext, claim: Claim) -> Check | None:
    match = ISSUE_REF.search(_quote(ctx, claim))
    if match is None:
        return None
    exists = await ctx.github.issue_exists(ctx.repo, int(match[1]))
    if exists is None:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="GitHub did not answer")
    if exists:
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, issue=int(match[1]))
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.SOFT, issue=int(match[1]))


def check_changed_file(ctx: EvidenceContext, claim: Claim) -> Check | None:
    """ "Updates docs/x.md" on a PR that never touches it."""
    target = claim.normalized_target
    changed = ctx.stage.submission.changed_files
    if not target or not changed or CHANGE_VERB.search(_quote(ctx, claim)) is None:
        return None
    touched = any(path == target or path.endswith(f"/{target}") for path in changed)
    if touched:
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, file=target)
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.SOFT, file=target)


def check_kind(ctx: EvidenceContext) -> Check | None:
    changed = ctx.stage.submission.changed_files
    if not changed:
        return None
    kind = ctx.claims.kind
    if kind is SubmissionKind.DOCS_PR:
        code = [path for path in changed if not DOC_PATH.search(path)]
        if code:
            return make_check(
                MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, kind=kind.value, code_files=code[:5]
            )
    if kind is SubmissionKind.BUGFIX_PR and all(DOC_PATH.search(path) or is_test_path(path) for path in changed):
        return make_check(
            MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, kind=kind.value, reason="no code changed"
        )
    return None


def check_dependency_claim(ctx: EvidenceContext, claim: Claim) -> Check | None:
    submission = ctx.stage.submission
    if not submission.is_pull_request or not submission.changed_files:
        return None
    if any(path.split("/")[-1] in MANIFEST_NAMES or "requirements" in path for path in submission.changed_files):
        return None
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.SOFT, reason="no manifest changed")


async def run(ctx: EvidenceContext) -> list[Check]:
    if not ctx.stage.submission.is_pull_request:
        return []
    found: list[Check | None] = [check_kind(ctx)]
    for claim in ctx.claims.claims:
        if claim.type is ClaimType.FIXES_ISSUE:
            found.append(await check_issue_ref(ctx, claim))
        elif claim.type is ClaimType.FILE_EXISTS:
            found.append(check_changed_file(ctx, claim))
        elif claim.type is ClaimType.DEPENDENCY_ADDED:
            found.append(check_dependency_claim(ctx, claim))
    return [check for check in found if check is not None]
