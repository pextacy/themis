"""Likely duplicates, for the maintainer's information. Never affects a verdict."""

from __future__ import annotations

import re

from themis.evidence.base import EvidenceContext, make_check
from themis.intake.github import GitHubError
from themis.models import Check, CheckStatus

MODULE = "duplicates"
WORD = re.compile(r"[a-z0-9]{3,}")
STOP = frozenset({"the", "and", "for", "with", "this", "that", "from", "when", "not", "are", "bug", "fix", "add",
                  "issue", "error", "problem", "does", "should", "into", "use", "using"})  # fmt: skip
MIN_SIMILARITY = 0.6
MAX_TERMS = 6


def tokens(text: str) -> set[str]:
    return set(WORD.findall(text.lower())) - STOP


def similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


async def run(ctx: EvidenceContext) -> list[Check]:
    submission = ctx.stage.submission
    words = tokens(submission.title)
    if len(words) < 2:
        return []
    terms = " ".join(sorted(words)[:MAX_TERMS])
    try:
        items = await ctx.github.search_issues(f"repo:{ctx.repo} {terms}")
    except GitHubError as error:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason=str(error))]
    near = [
        {"number": item["number"], "similarity": round(score, 2)}
        for item in items
        if item.get("number") != submission.number
        and (score := similarity(words, tokens(str(item.get("title"))))) >= MIN_SIMILARITY
    ]
    return [make_check(MODULE, CheckStatus.NOT_APPLICABLE, matches=near)] if near else []
