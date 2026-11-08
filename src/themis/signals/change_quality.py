"""The shape of a change, in neutral words. Reads the diff only, never who sent it."""

from __future__ import annotations

import re
from collections import Counter

from themis.evidence.dependencies import edit_distance
from themis.evidence.tests_results import is_test_path
from themis.models import Signal, SignalSeverity
from themis.signals.base import FilePatch, SignalContext, make_signal
from themis.signals.summary import FileStat, parse_summary

MODULE = "change_quality"
SPRAWL_TOP_DIRS = 5
OVERSIZED_LINES = 1000
OVERSIZED_FILES = 50
TYPO_MANY_FILES = 5
TYPO_DISTANCE = 2
TYPO_MIN_WORD = 3
TYPO_MAX_LINE = 500
MASS_RENAME_FILES = 10
MAX_SAMPLE = 5

TOKEN = re.compile(r"\w+|[^\w\s]")
SUPPORT_DIRS = frozenset({"docs", "doc", "test", "tests", ".github"})
GENERATED = re.compile(
    r"(?:^|/)(?:vendor|vendored|third_party|node_modules|dist|build)/|\.min\.(?:js|css)$|_pb2(?:_grpc)?\.py$|"
    r"\.pb\.go$|\.generated\.|\.g\.dart$|(?:^|/)(?:package-lock\.json|yarn\.lock|uv\.lock|poetry\.lock|Cargo\.lock)$"
)
COMMENT_PREFIXES = {
    ("#",): (".py", ".sh", ".rb", ".yml", ".yaml", ".toml"),
    ("//", "/*", "*"): (".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".kt", ".swift"),
}
TEST_NAME = re.compile(
    r"\bdef\s+(test_\w+)|\bfunc\s+(Test\w+)|\bfn\s+(test_\w+)|\b(?:it|test)\s*\(\s*[\"'`]([^\"'`]+)|"
    r"\bvoid\s+(test\w+)\s*\("
)


def _squeeze(lines: list[str]) -> str:
    return "".join("".join(lines).split())


def _changed(patches: list[FilePatch]) -> list[FilePatch]:
    return [patch for patch in patches if patch.added or patch.removed]


def _added_text(patch: FilePatch) -> list[str]:
    return [item.text for item in patch.added]


def is_no_op(patches: list[FilePatch]) -> bool:
    return all(Counter(_added_text(patch)) == Counter(patch.removed) for patch in patches)


def is_whitespace_only(patches: list[FilePatch]) -> bool:
    return all(_squeeze(_added_text(patch)) == _squeeze(patch.removed) for patch in patches)


def _comment_prefixes(path: str) -> tuple[str, ...] | None:
    return next((prefixes for prefixes, ends in COMMENT_PREFIXES.items() if path.endswith(ends)), None)


def is_comment_only(patches: list[FilePatch]) -> bool:
    for patch in patches:
        prefixes = _comment_prefixes(patch.path)
        if prefixes is None:
            return False
        lines = [line.strip() for line in [*_added_text(patch), *patch.removed]]
        if not all(line.startswith(prefixes) for line in lines if line):
            return False
    return True


def _spelling_only(old: str, new: str) -> bool:
    return (
        min(len(old), len(new)) >= TYPO_MIN_WORD
        and old.isalpha()
        and new.isalpha()
        and edit_distance(old.lower(), new.lower(), TYPO_DISTANCE) <= TYPO_DISTANCE
    )


def _small_edit(old: str, new: str) -> bool:
    """Whole words differ by a few letters, so `return x` to `return x + 1` is never a typo fix."""
    left, right = TOKEN.findall(old), TOKEN.findall(new)
    if left == right or len(left) != len(right) or max(len(old), len(new)) > TYPO_MAX_LINE:
        return False
    return all(a == b or _spelling_only(a, b) for a, b in zip(left, right, strict=True))


def is_small_edits(patches: list[FilePatch]) -> bool:
    for patch in patches:
        added = _added_text(patch)
        if len(added) != len(patch.removed) or not all(map(_small_edit, patch.removed, added)):
            return False
    return True


def _whole_diff(ctx: SignalContext) -> list[Signal]:
    """Only said when every changed file was seen, since each one is a claim about all of it."""
    patches = _changed(ctx.patches)
    if ctx.truncated or not patches:
        return []
    files = len(patches)
    if is_no_op(patches):
        return [make_signal(MODULE, "no_op", SignalSeverity.WARN, "Added lines equal the removed lines", files=files)]
    if is_whitespace_only(patches):
        return [make_signal(MODULE, "whitespace_only", SignalSeverity.INFO, "Only whitespace changes", files=files)]
    if is_small_edits(patches):
        if files >= TYPO_MANY_FILES:
            title = "Small spelling-sized edits across many files"
            return [make_signal(MODULE, "typo_churn", SignalSeverity.WARN, title, files=files)]
        return [make_signal(MODULE, "small_text_fix", SignalSeverity.INFO, "Small text fix", files=files)]
    if is_comment_only(patches):
        return [make_signal(MODULE, "comment_only", SignalSeverity.INFO, "Only comments change", files=files)]
    return []


def _size(ctx: SignalContext, stats: dict[str, FileStat]) -> int:
    """Generated files do not count: a lockfile refresh is not a large change to review."""
    if stats:
        return sum(s.additions + s.deletions for s in stats.values() if not GENERATED.search(s.path))
    return sum(len(p.added) + len(p.removed) for p in ctx.patches if not GENERATED.search(p.path))


def _oversized(ctx: SignalContext, stats: dict[str, FileStat]) -> list[Signal]:
    lines, files = _size(ctx, stats), len(ctx.submission.changed_files)
    if lines < OVERSIZED_LINES and files < OVERSIZED_FILES:
        return []
    return [make_signal(MODULE, "oversized", SignalSeverity.WARN, "Large change", lines=lines, files=files)]


def _sprawl(changed: list[str]) -> list[Signal]:
    dirs = {path.split("/")[0] for path in changed if "/" in path} - SUPPORT_DIRS
    if len(dirs) <= SPRAWL_TOP_DIRS:
        return []
    return [make_signal(MODULE, "file_sprawl", SignalSeverity.WARN, "Touches many top-level directories",
                        directories=len(dirs))]  # fmt: skip


def _renames(stats: dict[str, FileStat]) -> list[Signal]:
    renamed = sum(stat.status == "renamed" for stat in stats.values())
    if renamed < MASS_RENAME_FILES:
        return []
    return [make_signal(MODULE, "mass_rename", SignalSeverity.WARN, "Many files renamed", files=renamed)]


def _generated(changed: list[str]) -> list[Signal]:
    found = [path for path in changed if GENERATED.search(path)]
    if not found:
        return []
    return [make_signal(MODULE, "generated_files", SignalSeverity.INFO, "Generated or vendored files in the change",
                        count=len(found), sample=found[:MAX_SAMPLE])]  # fmt: skip


def _test_names(lines: list[str]) -> set[str]:
    return {name for line in lines if (match := TEST_NAME.search(line)) for name in match.groups() if name}


def _removed_tests(ctx: SignalContext, stats: dict[str, FileStat]) -> list[Signal]:
    """Names, so a test moved to another file is not counted, and a net, so a rename is not."""
    tests = [patch for patch in ctx.patches if is_test_path(patch.path)]
    gone = _test_names([line for patch in tests for line in patch.removed])
    new = _test_names([text for patch in tests for text in _added_text(patch)])
    lost = len(gone - new) - len(new - gone)
    seen = {patch.path for patch in ctx.patches}
    files = sum(s.status == "removed" and is_test_path(s.path) and s.path not in seen for s in stats.values())
    if lost <= 0 and not files:
        return []
    return [make_signal(MODULE, "removed_tests", SignalSeverity.WARN, "Tests removed without a replacement",
                        tests=max(lost, 0), files=files)]  # fmt: skip


def scan(ctx: SignalContext) -> list[Signal]:
    if not ctx.submission.is_pull_request:
        return []
    stats = parse_summary(ctx.submission.diff_summary)
    changed = ctx.submission.changed_files
    return [
        *_whole_diff(ctx),
        *_oversized(ctx, stats),
        *_sprawl(changed),
        *_renames(stats),
        *_generated(changed),
        *_removed_tests(ctx, stats),
    ]
