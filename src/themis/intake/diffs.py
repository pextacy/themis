"""Split a unified diff into per-file patches, in the shape GitHub's file list gives."""

from __future__ import annotations

import re

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK_START = "@@ "


def split_unified_diff(text: str) -> list[dict[str, str]]:
    """One `{filename, status, patch}` per file. Binary files carry no patch."""
    files: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    hunks: list[str] = []
    in_hunk = False

    def close() -> None:
        if current is not None and hunks:
            current["patch"] = "\n".join(hunks)

    for line in text.splitlines():
        header = DIFF_HEADER.match(line)
        if header:
            close()
            current, hunks, in_hunk = {"filename": header[2], "status": "modified"}, [], False
            files.append(current)
        elif current is not None:
            if line.startswith(HUNK_START):
                in_hunk = True
            if in_hunk:
                hunks.append(line)
            elif line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "removed"
    close()
    return files
