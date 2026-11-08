"""Read the per-file lines intake writes into `Submission.diff_summary`."""

from __future__ import annotations

import re
from dataclasses import dataclass

STAT_LINE = re.compile(r"^(.+) \((\w+) \+(\d+) -(\d+)\)$")


@dataclass(frozen=True)
class FileStat:
    path: str
    status: str
    additions: int
    deletions: int


def parse_summary(summary: str) -> dict[str, FileStat]:
    """Lines in any other shape are skipped, so a plain list of paths gives an empty result."""
    stats: dict[str, FileStat] = {}
    for line in summary.splitlines():
        match = STAT_LINE.match(line)
        if match:
            stats[match[1]] = FileStat(match[1], match[2], int(match[3]), int(match[4]))
    return stats
