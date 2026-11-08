"""Do the tests in a PR touch the code it changes? Deterministic, SOFT at most.

`tests` answers whether a test file changed. This asks whether that change is about the code
under review: a test named after a changed file, or added test lines that import or call it.
Only the excerpt of the diff is read, so a cut-off diff can confirm but never contradict.
"""

from __future__ import annotations

import re

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.tests_results import is_test_path
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength
from themis.signals.base import AddedLine, make_context

MODULE = "test_impact"
MAX_LISTED = 20
MIN_NAME = 3
SOURCE_FILE = re.compile(r"\.(?:py|[cm]?[jt]s|[jt]sx|go|rs|java|kt|scala|c|cc|cpp|h|hpp|rb|php|cs|swift)$")
ENTRY_STEMS = frozenset({"__init__", "index", "mod", "lib", "main"})
DEFINITION = re.compile(
    r"\b(?:def|class|func|function|fn|interface|struct|enum|trait|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
)
HUNK_CONTEXT = re.compile(r"^@@[^@]*@@\s*(.*)$")
WORD = re.compile(r"[A-Za-z_]\w*")
COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*|--|$)")
TEST_AFFIX = re.compile(r"^tests?[_-]?|[_.-]?(?:tests?|spec)$", re.I)


def _flat(name: str) -> str:
    return re.sub(r"[_-]", "", name).lower()


def module_stem(path: str) -> str:
    """The name a test would use. `pkg/__init__.py` is known as `pkg`."""
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    return parts[-2] if stem in ENTRY_STEMS and len(parts) > 1 else stem


def test_stem(path: str) -> str:
    stem = path.split("/")[-1].rsplit(".", 1)[0]
    return _flat(TEST_AFFIX.sub("", stem))


def named_for(source: str, tests: list[str]) -> bool:
    wanted = _flat(module_stem(source))
    return len(wanted) >= MIN_NAME and any(wanted in test_stem(path) for path in tests)


def changed_symbols(excerpt: str, added: dict[str, list[AddedLine]], removed: dict[str, list[str]]) -> set[str]:
    """Names defined in the changed lines, or named by a hunk header, in non-test files."""
    found: set[str] = set()
    for path, lines in added.items():
        if not is_test_path(path):
            found |= {name for line in lines for name in DEFINITION.findall(line.text)}
    for path, texts in removed.items():
        if not is_test_path(path):
            found |= {name for text in texts for name in DEFINITION.findall(text)}
    current = ""
    rows = excerpt.splitlines()
    for index, row in enumerate(rows):
        if row.startswith("--- ") and index + 1 < len(rows) and rows[index + 1].startswith("@@"):
            current = row.removeprefix("--- ")
        elif (hunk := HUNK_CONTEXT.match(row)) and current and not is_test_path(current):
            found |= set(DEFINITION.findall(hunk[1]))
    return {name for name in found if len(name) >= MIN_NAME}


def code_lines(lines: list[AddedLine]) -> list[str]:
    return [line.text for line in lines if not COMMENT.match(line.text)]


def test_words(lines: list[str]) -> set[str]:
    return {word for line in lines for word in WORD.findall(line)}


def is_covered(source: str, tests: list[str], words: set[str]) -> bool:
    if named_for(source, tests):
        return True
    stem = module_stem(source)
    return len(stem) >= MIN_NAME and (stem in words or _flat(stem) in {_flat(word) for word in words})


def _sources(changed: list[str]) -> list[str]:
    return [path for path in changed if SOURCE_FILE.search(path) and not is_test_path(path)]


async def run(ctx: EvidenceContext) -> list[Check]:
    submission = ctx.stage.submission
    if not submission.is_pull_request or not submission.changed_files:
        return []
    view = make_context(submission, ctx.stage.policy, ctx.claims.kind)
    tests = [path for path in submission.changed_files if is_test_path(path)]
    sources = _sources(submission.changed_files)
    added = {patch.path: patch.added for patch in view.patches}
    removed = {patch.path: patch.removed for patch in view.patches}
    lines = [text for path in tests for text in code_lines(added.get(path, []))]
    words = test_words(lines)
    symbols = changed_symbols(submission.diff_excerpt, added, removed)
    touched = bool(symbols & words)
    covered = [path for path in sources if is_covered(path, tests, words)]
    checks = [
        _claim_check(claim, tests, sources, lines, bool(covered) or touched, view.truncated)
        for claim in ctx.claims.claims
        if claim.type is ClaimType.TEST_ADDED and tests
    ]
    bare = [path for path in sources if path not in covered]
    if bare and not touched:
        checks.append(
            make_check(MODULE, CheckStatus.NOT_APPLICABLE, untested=bare[:MAX_LISTED], partial=view.truncated)
        )
    return checks


def _claim_check(
    claim: Claim,
    tests: list[str],
    sources: list[str],
    lines: list[str],
    related: bool,
    truncated: bool,
) -> Check:
    shown = tests[:MAX_LISTED]
    if lines and (related or not sources):
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, test_files=shown)
    if truncated:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="the diff was cut off", test_files=shown)
    reason = "tests do not touch the changed code" if lines else "no test code was added"
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.SOFT, reason=reason, test_files=shown)
