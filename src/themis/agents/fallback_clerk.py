"""A regex stand-in for the Clerk, used when no model is available (golden rule 9).

It reads the sanitizer's clean text only, skips fenced code, and keeps a claim only when the
wording is plain. The kind stays what intake said, so without a model no symbol can reach
HARD: a missing Clerk gives less evidence, never a harsher verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from themis.agents.clerk import BACKTICKED, CVE, MAX_CLAIMS, coherent_kind
from themis.evidence.symbols import CALL, DOTTED, PATH_LIKE
from themis.evidence.tests_results import NEGATED
from themis.models import Claim, ClaimSet, ClaimType, Submission
from themis.shield.sanitize import Sanitized

REASON = "fallback: regex extraction, the Clerk was unavailable"
FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
MIN_SYMBOL = 3
LABEL_WINDOW = 12
LABELLED = re.compile(r"\b(?:function|method|class)\s*$", re.IGNORECASE)
PLAIN_PATH = re.compile(r"(?<![\w./@-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,8}(?![\w/])")
OWN_VERSION = re.compile(
    r"\b(?:on|in|at|from|to|since|until|after|before)\s+(?:the\s+)?(?:version|release|tag)\s+v?\d+(?:\.\d+){1,3}\b"
    r"(?!\s+of\b)",
    re.IGNORECASE,
)
FIXES = re.compile(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d{1,9})\b", re.IGNORECASE)
TESTS_ADDED = re.compile(
    r"\b(?:adds?|added|adding|includes?|including|wrote)\s+(?:(?:a|an|the|new|some|unit|regression)\s+){0,3}tests?\b",
    re.IGNORECASE,
)
DEPENDENCY = re.compile(
    r"\b(?:adds?|added|adding|introduces?)\s+(?:(?:a|an|the|new)\s+){0,2}(?:dependency|package|library)\s+"
    r"(?:on\s+)?`([\w.@/-]{2,100})`",
    re.IGNORECASE,
)
NEGATION_WINDOW = 24
FILE_SUFFIXES = frozenset(
    {
        "py",
        "js",
        "ts",
        "go",
        "rs",
        "md",
        "rst",
        "txt",
        "toml",
        "json",
        "yml",
        "yaml",
        "cfg",
        "c",
        "h",
        "java",
        "rb",
        "sh",
    }
)


@dataclass(frozen=True)
class Found:
    type: ClaimType
    quote: str
    target: str | None
    text: str


def _visible(text: str) -> str:
    """Blank out fenced code, which is often the author's own, keeping offsets."""
    return FENCE.sub(lambda match: " " * len(match[0]), text)


def _classify_backticked(value: str, before: str) -> ClaimType | None:
    """`a/b.py` and `b.py` are files, `a.b` or `f()` are symbols. A bare word needs a label."""
    if len(value) < MIN_SYMBOL or " " in value or ".." in value:
        return None
    if "/" in value:
        return ClaimType.FILE_EXISTS if PATH_LIKE.fullmatch(value) else None
    if CALL.match(value):
        return ClaimType.SYMBOL_EXISTS
    if not DOTTED.match(value):
        return None
    if value.rsplit(".", 1)[-1] in FILE_SUFFIXES and value.count(".") == 1:
        return ClaimType.FILE_EXISTS
    return ClaimType.SYMBOL_EXISTS if "." in value or "::" in value or LABELLED.search(before) else None


def _backticked(text: str) -> list[Found]:
    found: list[Found] = []
    for match in BACKTICKED.finditer(text):
        value = match[1].strip()
        kind = _classify_backticked(value, text[max(match.start() - LABEL_WINDOW, 0) : match.start()])
        if kind is not None:
            found.append(Found(kind, match[0], value, f"The submission refers to {value}."))
    return found


def _paths(text: str) -> list[Found]:
    without_code = BACKTICKED.sub(lambda match: " " * len(match[0]), text)
    return [
        Found(ClaimType.FILE_EXISTS, match[0], match[0], f"The submission refers to {match[0]}.")
        for match in PLAIN_PATH.finditer(without_code)
        if "://" not in without_code[max(match.start() - 3, 0) : match.start() + 1]
    ]


def _not_negated(text: str, match: re.Match[str]) -> bool:
    return NEGATED.search(text[max(match.start() - NEGATION_WINDOW, 0) : match.start()]) is None


def _pull_request_claims(text: str) -> list[Found]:
    found = [
        Found(ClaimType.FIXES_ISSUE, m[0], f"#{m[1]}", f"This change fixes #{m[1]}.") for m in FIXES.finditer(text)
    ]
    found += [
        Found(ClaimType.TEST_ADDED, m[0], None, "This change adds tests.")
        for m in TESTS_ADDED.finditer(text)
        if _not_negated(text, m)
    ]
    found += [
        Found(ClaimType.DEPENDENCY_ADDED, m[0], m[1], f"This change adds the dependency {m[1]}.")
        for m in DEPENDENCY.finditer(text)
        if _not_negated(text, m)
    ]
    return found


def _candidates(text: str, is_pull_request: bool) -> list[Found]:
    found = _backticked(text) + _paths(text)
    found += [Found(ClaimType.CVE_REFERENCE, m[0], m[0].upper(), f"The submission cites {m[0].upper()}.")
              for m in CVE.finditer(text)]  # fmt: skip
    found += [Found(ClaimType.VERSION_EXISTS, m[0], None, "The submission names a release of this project.")
              for m in OWN_VERSION.finditer(text)]  # fmt: skip
    return found + _pull_request_claims(text) if is_pull_request else found


def extract(submission: Submission, scanned: Sanitized) -> ClaimSet:
    """`scanned` must be the scan of `title\\n\\nbody`, so spans index what evidence reads."""
    kind = coherent_kind(submission.kind, submission.is_pull_request)
    if scanned.tainted:
        return ClaimSet(kind=kind, degraded=True, reason=f"{REASON}; tainted text was not read")
    anchored: list[tuple[tuple[int, int], Found]] = []
    seen: set[tuple[ClaimType, str]] = set()
    for item in _candidates(_visible(scanned.clean), submission.is_pull_request):
        key = (item.type, item.target or item.quote.lower())
        span = scanned.locate(item.quote)
        if key not in seen and span is not None:
            seen.add(key)
            anchored.append((span, item))
    anchored.sort(key=lambda pair: (pair[0], pair[1].type.value))
    claims = [
        Claim(id=f"c{index}", type=item.type, text=item.text, span=span, normalized_target=item.target)
        for index, (span, item) in enumerate(anchored[:MAX_CLAIMS], 1)
    ]
    return ClaimSet(kind=kind, claims=claims, degraded=True, reason=REASON)
