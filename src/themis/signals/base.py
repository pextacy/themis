"""What a signal scanner reads. Pure data: no network, no model, no clock."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from themis.config import RepoPolicy
from themis.intake.github import MAX_DIFF_CHARS
from themis.models import Signal, SignalSeverity, Submission, SubmissionKind

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FILE_HEADER = "--- "


@dataclass(frozen=True)
class AddedLine:
    number: int
    text: str


@dataclass
class FilePatch:
    """One file from `Submission.diff_excerpt`. Line numbers index the new version."""

    path: str
    added: list[AddedLine] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def parse_patches(excerpt: str) -> list[FilePatch]:
    """Reads the `--- path` + GitHub patch format that intake writes.

    A `--- x` line inside a hunk is a removed line that starts with `-- x`, so a file header
    only counts when no hunk is open or when the next line starts a hunk.
    """
    lines = excerpt.splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    number = 0
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.startswith(FILE_HEADER) and (current is None or HUNK.match(following)):
            current = FilePatch(line.removeprefix(FILE_HEADER))
            patches.append(current)
            number = 0
            continue
        if current is None:
            continue
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk[1])
        elif line.startswith("+"):
            current.added.append(AddedLine(number, line[1:]))
            number += 1
        elif line.startswith("-"):
            current.removed.append(line[1:])
        elif not line.startswith("\\"):
            number += 1
    return patches


@dataclass
class SignalContext:
    submission: Submission
    policy: RepoPolicy
    kind: SubmissionKind = SubmissionKind.OTHER
    patches: list[FilePatch] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        """Intake caps the excerpt, so a scan of it may have seen only part of the change."""
        listed = {patch.path for patch in self.patches}
        cut = len(self.submission.diff_excerpt) >= MAX_DIFF_CHARS * 0.9
        return cut or any(path not in listed for path in self.submission.changed_files)


def make_context(submission: Submission, policy: RepoPolicy, kind: SubmissionKind | None = None) -> SignalContext:
    return SignalContext(submission, policy, kind or submission.kind, parse_patches(submission.diff_excerpt))


def make_signal(
    module: str,
    code: str,
    severity: SignalSeverity,
    title: str,
    *,
    file: str | None = None,
    line: int | None = None,
    **detail: Any,
) -> Signal:
    return Signal(module=module, code=code, severity=severity, title=title, file=file, line=line, detail=detail)


Scanner = Callable[[SignalContext], list[Signal]]
