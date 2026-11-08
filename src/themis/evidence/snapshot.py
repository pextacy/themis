"""A repository at one ref, read from its tarball into memory.

The archive is never extracted and nothing in it runs: PR code stays data here. Parsing
Python only builds a syntax tree. `complete` is False whenever part of the tree went unread,
and an incomplete snapshot can never prove that something is absent.
"""

from __future__ import annotations

import ast
import io
import re
import tarfile
from dataclasses import dataclass, field

MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_AST_BYTES = 256 * 1024
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class Snapshot:
    ref: str
    project: str = ""
    files: dict[str, str] = field(default_factory=dict)
    paths: set[str] = field(default_factory=set)
    complete: bool = True
    _words: set[str] | None = None

    @property
    def words(self) -> set[str]:
        if self._words is None:
            self._words = {word for text in self.files.values() for word in IDENTIFIER.findall(text)}
        return self._words

    def has_path(self, path: str) -> bool:
        for wanted in self._tails(path.lstrip("./").rstrip("/")):
            for item in self.paths:
                if item == wanted or item.startswith(f"{wanted}/") or item.endswith(f"/{wanted}"):
                    return True
                if f"/{wanted}/" in item:
                    return True
        return False

    def _tails(self, path: str) -> list[str]:
        """The path, and without a first directory: people often write `repo/src/x.py`."""
        head, _, rest = path.partition("/")
        return [path, rest] if rest and head == self.project else [path]

    def contains(self, literal: str) -> bool:
        return any(literal in text for text in self.files.values())

    def resolve(self, path: str) -> str | None:
        for wanted in self._tails(path.lstrip("./")):
            if wanted in self.files:
                return wanted
            found = next((item for item in sorted(self.files) if item.endswith(f"/{wanted}")), None)
            if found:
                return found
        return None

    def defining_file(self, name: str) -> str | None:
        """The first Python file that defines this function or class."""
        for path, text in sorted(self.files.items()):
            if path.endswith(".py") and name in text and name in python_definitions(text):
                return path
        return None


def from_tarball(ref: str, archive: bytes | None, project: str = "") -> Snapshot:
    """Build a snapshot. Corrupt or oversized input gives an incomplete one, never an error."""
    snapshot = Snapshot(ref=ref, project=project)
    if archive is None:
        snapshot.complete = False
        return snapshot
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                path = member.name.split("/", 1)[-1]
                snapshot.paths.add(path)
                if member.size > MAX_FILE_BYTES or total + member.size > MAX_TOTAL_BYTES:
                    snapshot.complete = False
                    continue
                stream = tar.extractfile(member)
                raw = stream.read() if stream else b""
                total += len(raw)
                if b"\0" not in raw[:8192]:
                    snapshot.files[path] = raw.decode("utf-8", errors="replace")
    except (tarfile.TarError, EOFError, OSError):
        snapshot.complete = False
    return snapshot


def python_definitions(text: str) -> set[str]:
    """Names defined in a Python file. Hostile input just yields nothing."""
    if len(text) > MAX_AST_BYTES:
        return set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return set()
    return {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def imported_roots(snapshot: Snapshot) -> set[str]:
    """Top-level names the project imports, so external symbols are not called missing."""
    pattern = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", re.MULTILINE)
    return {name for path, text in snapshot.files.items() if path.endswith(".py") for name in pattern.findall(text)}
