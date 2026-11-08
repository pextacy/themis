"""An append-only hash chain over receipts.

Each entry names the receipt's SHA-256 and the hash of the entry before it, so changing an old
receipt or entry breaks every later link. It proves nothing against someone who rewrites the
whole file: for that, compare the head hash with a copy kept elsewhere, such as a release note.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from themis.store import redact
from themis.verdict.receipts import RECEIPTS_DIR

CHAIN_PATH = RECEIPTS_DIR / "chain.jsonl"
GENESIS = "0" * 64


class ChainEntry(BaseModel):
    index: int
    prev_hash: str
    receipt_sha256: str
    submission_id: str
    verdict: str
    at: str
    entry_sha256: str = ""


def _digest(entry: ChainEntry) -> str:
    body = entry.model_dump(exclude={"entry_sha256"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse(line: str) -> ChainEntry | None:
    try:
        return ChainEntry.model_validate(json.loads(line))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None


def append(
    receipt_path: Path,
    submission_id: str,
    verdict: str,
    *,
    chain: Path = CHAIN_PATH,
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ChainEntry:
    """Hashes the file as written, so what is chained is exactly what is on disk."""
    chain.parent.mkdir(parents=True, exist_ok=True)
    moment = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    with chain.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        lines = [line for line in handle.read().splitlines() if line.strip()]
        last = _parse(lines[-1]) if lines else None
        if lines and last is None:
            raise ValueError(f"the last entry of {chain} cannot be read, so nothing can be chained to it")
        entry = ChainEntry(
            index=last.index + 1 if last else 0,
            prev_hash=last.entry_sha256 if last else GENESIS,
            receipt_sha256=sha256_file(receipt_path),
            submission_id=str(redact(submission_id, secrets)),
            verdict=verdict,
            at=moment,
        )
        entry.entry_sha256 = _digest(entry)
        handle.write(entry.model_dump_json() + "\n")
        return entry


def entries(path: Path = CHAIN_PATH) -> list[ChainEntry | None]:
    """None marks a line that cannot be read."""
    if not path.exists():
        return []
    return [_parse(line) for line in path.read_text().splitlines() if line.strip()]


def verify(path: Path = CHAIN_PATH) -> int | None:
    """The first broken index, or None when the whole chain holds."""
    expected = GENESIS
    for index, entry in enumerate(entries(path)):
        if entry is None or entry.index != index or entry.prev_hash != expected or entry.entry_sha256 != _digest(entry):
            return index
        expected = entry.entry_sha256
    return None


def head(path: Path = CHAIN_PATH) -> str | None:
    rows = entries(path)
    last = rows[-1] if rows else None
    return last.entry_sha256 if last else None


def latest_entry(submission_id: str, path: Path = CHAIN_PATH) -> ChainEntry | None:
    found = [entry for entry in entries(path) if entry is not None and entry.submission_id == submission_id]
    return found[-1] if found else None


def verify_receipts(path: Path = CHAIN_PATH, directory: Path = RECEIPTS_DIR) -> list[dict[str, Any]]:
    """Compares each submission's newest entry with its file. A rerun overwrites older files."""
    latest = {entry.submission_id: entry for entry in entries(path) if entry is not None}
    problems: list[dict[str, Any]] = []
    for submission_id, entry in latest.items():
        receipt = directory / f"themis-{Path(submission_id).name}.json"
        if not receipt.exists():
            problems.append({"index": entry.index, "submission_id": submission_id, "problem": "receipt file missing"})
        elif sha256_file(receipt) != entry.receipt_sha256:
            problems.append({"index": entry.index, "submission_id": submission_id, "problem": "receipt file changed"})
    return problems
