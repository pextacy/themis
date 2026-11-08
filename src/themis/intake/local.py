"""The contributor's own working tree, for `themis precheck`. Nothing here runs project code.

Git runs with no shell and with the settings that could start a program turned off, so a
repository's own config cannot make this command execute anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - argv lists only, see _git
from pathlib import Path

from themis.evidence.snapshot import MAX_FILE_BYTES, MAX_TOTAL_BYTES, Snapshot
from themis.intake.diffs import split_unified_diff
from themis.intake.github import MAX_DIFF_CHARS

GIT_TIMEOUT_S = 30
SAFE_REF = re.compile(r"[A-Za-z0-9][\w./-]{0,200}")
SKIPPED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "target"})
HARDENING = (
    "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "diff.external=", "-c", "core.hooksPath=/dev/null",
)  # fmt: skip


def _git(root: Path, *args: str) -> str | None:
    """None when git is missing, this is not a repository, or the command failed."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, ref checked by SAFE_REF
            [git, *HARDENING, "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else None


def _tracked(root: Path) -> list[str] | None:
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return None if listed is None else [item for item in listed.split("\0") if item]


def _walk(root: Path) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*"))
        if not SKIPPED_DIRS & set(path.relative_to(root).parts)
    ]


def snapshot_from_disk(root: Path) -> Snapshot:
    """Same limits as a tarball snapshot. Links are not followed out of the tree."""
    snapshot = Snapshot(ref="working-tree", project=root.resolve().name)
    total = 0
    for relative in _tracked(root) or _walk(root):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        snapshot.paths.add(relative)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            snapshot.complete = False
            continue
        raw = path.read_bytes()
        total += len(raw)
        if b"\0" not in raw[:8192]:
            snapshot.files[relative] = raw.decode("utf-8", errors="replace")
    return snapshot


def local_tags(root: Path) -> list[str]:
    return (_git(root, "tag", "--list") or "").split()


def local_changes(root: Path, base: str) -> tuple[list[str], str] | None:
    """(changed files, excerpt in the shape intake writes), or None when git cannot say."""
    if not SAFE_REF.fullmatch(base):
        raise ValueError(f"not a usable git ref: {base!r}")
    text = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, "--")
    if text is None:
        return None
    files = split_unified_diff(text)
    excerpt = "\n".join(f"--- {item['filename']}\n{item['patch']}" for item in files if item.get("patch"))
    return [item["filename"] for item in files], excerpt[:MAX_DIFF_CHARS]
