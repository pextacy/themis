"""Does a claimed release of this project exist? Deterministic."""

from __future__ import annotations

import re

from themis.evidence.base import EvidenceContext, make_check
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength

MODULE = "versions"
VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+){1,3}(?:[-.]?(?:rc|a|b|alpha|beta|dev|post)[.-]?\d*)?)(?![\w])", re.I)
# "Python 3.12" names another product. Only a bare or project-named version is ours.
OUR_WORDS = frozenset({"version", "versions", "release", "releases", "tag", "tags", "before", "after", "on",
                       "in", "at", "from", "to", "since", "until", "than", "of", "with", "and", "or", "v"})  # fmt: skip
MANIFESTS = ("pyproject.toml", "package.json", "setup.cfg", "setup.py", "Cargo.toml", "VERSION", "version.txt")


def versions_in(text: str) -> list[tuple[str, str]]:
    """Each version found, with the word before it."""
    found = []
    for match in VERSION.finditer(text):
        before = re.findall(r"[\w-]+", text[: match.start()])
        found.append((match[1], before[-1] if before else ""))
    return found


def is_ours(word: str, project: str) -> bool:
    lowered = word.lower()
    return not word or lowered in OUR_WORDS or lowered == project.lower()


def tagged(version: str, tags: list[str]) -> bool:
    """`1.2.0` matches `v1.2.0`, `release-1.2.0` and `name-1.2.0`, and `1.2` matches `v1.2.0`."""
    pattern = re.compile(rf"(?<![\d.]){re.escape(version)}(?:\.0)*(?![\d])")
    return any(pattern.search(tag) for tag in tags)


async def check_claim(ctx: EvidenceContext, claim: Claim) -> Check:
    start, end = claim.span or (0, 0)
    project = ctx.repo.split("/")[-1]
    quote = ctx.source_text[start:end] or claim.text
    ours = [version for version, word in versions_in(quote) if is_ours(word, project)]
    if not ours:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="no version of this project named")
    tags = await ctx.tags()
    if not tags:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="project has no tags")
    version = ours[0]
    if tagged(version, tags):
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, version=version)
    snapshot = await ctx.primary()
    if any(version in text for path, text in snapshot.files.items() if path.split("/")[-1] in MANIFESTS):
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, version=version, source="manifest")
    if not snapshot.complete or ctx.claims.kind.value in ("feature_request", "question"):
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, version=version, reason="absence not provable")
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.HARD, version=version, tags=len(tags))


async def run(ctx: EvidenceContext) -> list[Check]:
    return [await check_claim(ctx, claim) for claim in ctx.claims.claims if claim.type is ClaimType.VERSION_EXISTS]
