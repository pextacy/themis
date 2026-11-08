"""Test evidence. Tests running is read from an artifact; tests being added from the diff.

PR code wrote that artifact on an untrusted runner, so it can lie. It only ever yields
CONFIRMED or a SOFT contradiction, and it must name the commit under review. The one HARD
outcome, "claims tests were added but none changed", comes from the file list alone.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from themis.evidence.base import EvidenceContext, make_check
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength

MODULE = "tests"
SCHEMA = "themis.tests.v1"
TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|spec|specs|__tests__|e2e)/|(?:^|/)test_[^/]*$|_test\.\w+$|[._-](?:test|spec)\.\w+$|Tests?\.\w+$"
)
NEGATED = re.compile(
    r"\b(?:no|not|without|never|missing|lacks?|didn't|haven't|hasn't|todo|will\s+add)\b", re.IGNORECASE
)


def is_test_path(path: str) -> bool:
    return TEST_PATH.search(path) is not None


def parse_junit(xml: bytes, head_sha: str | None = None) -> dict[str, Any]:
    """Summarize a JUnit report. Entity attacks and junk give an empty report."""
    empty: dict[str, Any] = {
        "schema": SCHEMA,
        "head_sha": head_sha,
        "total": 0,
        "failed": 0,
        "skipped": 0,
        "parsed": False,
    }
    try:
        root = ElementTree.fromstring(xml)
    except (ParseError, DefusedXmlException, ValueError):
        return empty
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    try:
        total = sum(int(suite.get("tests", 0)) for suite in suites)
        failed = sum(int(suite.get("failures", 0)) + int(suite.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
    except ValueError:
        return empty
    return {**empty, "total": total, "failed": failed, "skipped": skipped, "parsed": True}


def _valid_artifact(ctx: EvidenceContext) -> dict[str, Any] | None:
    data = ctx.test_results
    head = ctx.stage.submission.head_sha
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not data.get("parsed"):
        return None
    if head is None or data.get("head_sha") != head:
        return None
    counts = (data.get("total"), data.get("failed"))
    return data if all(isinstance(count, int) and count >= 0 for count in counts) else None


def check_artifact(ctx: EvidenceContext) -> Check:
    data = _valid_artifact(ctx)
    if data is None or data["total"] == 0:
        return make_check(MODULE, CheckStatus.UNVERIFIED, reason="no usable test results for this commit")
    counts = {"total": data["total"], "failed": data["failed"]}
    if data["failed"]:
        return make_check(MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, **counts)
    return make_check(MODULE, CheckStatus.CONFIRMED, **counts)


def check_added(ctx: EvidenceContext, claim: Claim) -> Check:
    submission = ctx.stage.submission
    if not submission.is_pull_request:
        return make_check(MODULE, CheckStatus.NOT_APPLICABLE, claim)
    changed = [path for path in submission.changed_files if is_test_path(path)]
    if changed:
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, test_files=changed[:10])
    start, end = claim.span or (0, 0)
    if NEGATED.search(ctx.source_text[start:end]) or not submission.changed_files:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="claim is not a plain assertion")
    return make_check(
        MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.HARD, changed=len(submission.changed_files)
    )


async def run(ctx: EvidenceContext) -> list[Check]:
    checks = [check_added(ctx, claim) for claim in ctx.claims.claims if claim.type is ClaimType.TEST_ADDED]
    if ctx.stage.submission.is_pull_request:
        checks.append(check_artifact(ctx))
    return checks
