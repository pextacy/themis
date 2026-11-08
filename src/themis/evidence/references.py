"""Do the links, line numbers and commits a submission cites exist? Deterministic, SOFT at most.

`symbols` already checks targets the Clerk extracted, including flags and `path:line` targets.
This reads what the Clerk may have skipped: permalinks, `path:line` mentions in prose, commit
hashes, and flags quoted inside other claims. Code blocks are ignored for mentions, because log
output there usually comes from the reporter's own project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from themis.evidence.base import FENCED, NOT_PROJECT_CODE, EvidenceContext, make_check
from themis.evidence.snapshot import Snapshot
from themis.intake.github import GitHubError
from themis.models import Check, CheckStatus, Strength

MODULE = "references"
MAX_REFERENCES = 10
MAX_COMMITS = 5
MAX_FLAGS = 5
CONTEXT_CHARS = 60
SOURCE_EXT = (
    r"py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|scala|c|h|cc|cpp|hpp|rb|php|cs|swift|sh|md|rst|toml|ya?ml|json|cfg"
)
BLOB_URL = re.compile(
    r"https://github\.com/([\w.-]+)/([\w.-]+)/blob/([\w.-]+)/([\w./@+-]+?)(?:#L(\d{1,7})(?:-L(\d{1,7}))?)?(?=\.?(?:[\s)\]>\"'`,;]|$))"
)
COMMIT_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/commit/([0-9a-f]{7,40})\b")
ANY_URL = re.compile(r"https?://\S+")
MENTION = re.compile(rf"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.(?:{SOURCE_EXT})):(\d{{1,7}})(?:-(\d{{1,7}}))?(?![\w:])")
SHA = re.compile(r"(?<![\w/#-])[0-9a-f]{7,40}(?![\w-])")
SHA_CONTEXT = re.compile(r"\b(?:commits?|sha|rev(?:ision)?|regress\w*|introduced|bisect\w*|cherry-pick\w*)\b", re.I)
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{1,40}\b")
FULL_SHA = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True)
class Cited:
    path: str
    start: int | None = None
    end: int | None = None
    ref: str | None = None

    @property
    def label(self) -> str:
        lines = f":{self.start}" if self.start else ""
        return f"{self.path}{lines}"[:200]


def _is_sha(ref: str | None) -> bool:
    return bool(ref and FULL_SHA.fullmatch(ref) and any(char.isdigit() for char in ref))


def links_in(text: str, repo: str) -> list[Cited]:
    found = []
    for match in BLOB_URL.finditer(text):
        same = f"{match[1]}/{match[2]}".lower() == repo.lower()
        if same and ".." not in match[4]:
            start = int(match[5]) if match[5] else None
            found.append(Cited(match[4], start, int(match[6]) if match[6] else start, match[3]))
    return found


def mentions_in(prose: str) -> list[Cited]:
    """`path/file.py:12` in prose. Text around it that shows the author's own file drops it."""
    found = []
    for match in MENTION.finditer(prose):
        nearby = prose[max(match.start() - CONTEXT_CHARS, 0) : match.start()]
        if ".." not in match[1] and NOT_PROJECT_CODE.search(nearby) is None:
            start = int(match[2])
            found.append(Cited(match[1], start, int(match[3]) if match[3] else start))
    return found


def commits_in(body: str, repo: str) -> list[str]:
    """Hex words only count near a word like "commit": a checksum is not a citation."""
    linked = [m[3] for m in COMMIT_URL.finditer(body) if f"{m[1]}/{m[2]}".lower() == repo.lower()]
    prose = ANY_URL.sub(" ", FENCED.sub(" ", body))
    named = [
        match[0]
        for match in SHA.finditer(prose)
        if _is_sha(match[0])
        and any(char.isalpha() for char in match[0])
        and SHA_CONTEXT.search(prose[max(match.start() - CONTEXT_CHARS, 0) : match.start()])
    ]
    return list(dict.fromkeys([*linked, *named]))[:MAX_COMMITS]


def _fits(cited: Cited, snapshot: Snapshot) -> tuple[bool, str | None]:
    """(found, problem): whether the file is here, and what is wrong with the lines if so."""
    path = snapshot.resolve(cited.path) if "/" in cited.path else (cited.path if cited.path in snapshot.files else None)
    if path is None:
        return False, None
    length = snapshot.files[path].count("\n") + 1
    start, end = cited.start or 1, cited.end or cited.start or 1
    return True, None if 1 <= start <= end <= length else f"lines past the end of the file ({length})"


async def _named_snapshot(ctx: EvidenceContext, ref: str) -> Snapshot | None:
    """None when GitHub has no such ref. A branch may be gone, so only a hash means anything."""
    try:
        return await ctx.snapshot(ref)
    except GitHubError as error:
        if error.status in (404, 422):
            return None
        raise


async def _snapshots_for(ctx: EvidenceContext, cited: Cited) -> tuple[list[Snapshot], bool]:
    if cited.ref:
        named = await _named_snapshot(ctx, cited.ref)
        if named is not None and _is_sha(cited.ref):
            return [named], False
        if named is None and _is_sha(cited.ref):
            return [], True
        searched = [] if named is None else [named]
    else:
        searched = []
    return [*searched, *[await ctx.snapshot(ref) for ref in await ctx.search_refs()]], False


def _contradiction(cited: Cited, reason: str, **detail: Any) -> Check:
    return make_check(
        MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, reason=reason, target=cited.label, **detail
    )


async def check_cited(ctx: EvidenceContext, cited: Cited) -> Check:
    snapshots, gone = await _snapshots_for(ctx, cited)
    if gone:
        return _contradiction(cited, "the linked commit does not exist", commit=cited.ref)
    seen = [_fits(cited, snapshot) for snapshot in snapshots]
    if any(found and problem is None for found, problem in seen):
        return make_check(MODULE, CheckStatus.CONFIRMED, target=cited.label)
    if not all(snapshot.complete for snapshot in snapshots):
        return make_check(
            MODULE, CheckStatus.UNVERIFIED, reason="part of the repository went unread", target=cited.label
        )
    problem = next((item for found, item in seen if found and item), None)
    if problem:
        return _contradiction(cited, problem)
    if "/" in cited.path:
        return _contradiction(cited, "no such file at any ref read")
    return make_check(MODULE, CheckStatus.UNVERIFIED, reason="a bare file name may be the author's", target=cited.label)


async def check_commit(ctx: EvidenceContext, sha: str) -> Check:
    if await _named_snapshot(ctx, sha) is None:
        return make_check(MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, reason="no such commit", commit=sha)
    return make_check(MODULE, CheckStatus.CONFIRMED, commit=sha, target=f"commit {sha}")


def _flags(ctx: EvidenceContext) -> list[str]:
    """Flags inside a claim's text. A claim whose target is the flag belongs to `symbols`."""
    taken = {claim.normalized_target for claim in ctx.claims.claims}
    found = [flag for claim in ctx.claims.claims for flag in FLAG.findall(claim.text) if flag not in taken]
    return list(dict.fromkeys(found))[:MAX_FLAGS]


async def check_flag(ctx: EvidenceContext, flag: str) -> Check:
    """Absent never contradicts: `symbols.provable` explains why. Unlinked, since a flag being
    real says nothing about the behavior the claim describes."""
    for ref in await ctx.search_refs():
        if (await ctx.snapshot(ref)).contains(flag):
            return make_check(MODULE, CheckStatus.CONFIRMED, target=flag, ref=ref)
    return make_check(MODULE, CheckStatus.UNVERIFIED, reason="flag not found in the code", target=flag)


def _citations(ctx: EvidenceContext) -> list[Cited]:
    """Prose mentions the Clerk already turned into a target are left to `symbols`."""
    body = ctx.stage.submission.body
    taken = {claim.normalized_target for claim in ctx.claims.claims}
    prose = ANY_URL.sub(" ", FENCED.sub(" ", body))
    mentions = [item for item in mentions_in(prose) if item.label not in taken]
    return list(dict.fromkeys([*links_in(body, ctx.repo), *mentions]))[:MAX_REFERENCES]


async def run(ctx: EvidenceContext) -> list[Check]:
    checks = [await check_cited(ctx, cited) for cited in _citations(ctx)]
    checks += [await check_commit(ctx, sha) for sha in commits_in(ctx.stage.submission.body, ctx.repo)]
    checks += [await check_flag(ctx, flag) for flag in _flags(ctx)]
    return checks
