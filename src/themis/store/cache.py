"""Skip re-analysis of a submission that has not changed, so repeat events are not billed twice.

Only finished, trustworthy results are kept. A deferred, unchecked or degraded result says the
analysis could not complete, and the next attempt must run again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from themis import __version__
from themis.config import RepoPolicy, Settings
from themis.models import Receipt, Submission, VerdictValue
from themis.report.github_comment import MARKER
from themis.store.connect import open_db
from themis.store.redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    key TEXT PRIMARY KEY, submission_id TEXT NOT NULL, verdict TEXT NOT NULL, score INTEGER NOT NULL,
    ledger TEXT NOT NULL, receipt_path TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
TTL = timedelta(days=7)
UNCACHEABLE = frozenset({VerdictValue.DEFERRED, VerdictValue.TAINTED_UNCHECKED})
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CachedAnalysis:
    verdict: VerdictValue
    score: int
    ledger: str
    receipt_path: Path

    def receipt(self) -> Receipt:
        return Receipt.model_validate_json(self.receipt_path.read_text())


def _people_comments(comments: list[str]) -> list[str]:
    """Our ledger and `/themis` commands must not count, or every recheck would miss.

    Anyone can start a comment that way to keep it out of the key. A hit makes no model call,
    so such a comment reaches nothing. Its only effect is to wait for the next real change.
    """
    return [text for text in comments if not text.lstrip().startswith((MARKER, "/themis"))]


def fingerprint(
    submission: Submission, policy: RepoPolicy, settings: Settings, test_results: dict[str, Any] | None = None
) -> str:
    """Everything the result depends on. Test results count: they arrive after the first run."""
    material = {
        "head_sha": submission.head_sha,
        "title": submission.title,
        "body": submission.body,
        "changed_files": sorted(submission.changed_files),
        "diff_excerpt": submission.diff_excerpt,
        "comments": _people_comments(submission.comments),
        "policy": policy.model_dump(mode="json"),
        "version": __version__,
        "models": [settings.model_clerk, settings.model_prover, settings.model_arbiter],
        "test_results": test_results,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def cacheable(value: VerdictValue, *, degraded: bool) -> bool:
    """`degraded` means any step fell back: a claim set that degraded, or a gateway failure."""
    return not degraded and value not in UNCACHEABLE


def lookup(settings: Settings, key: str, *, force: bool = False, clock: Clock = utc_now) -> CachedAnalysis | None:
    """None on a miss. A hit whose receipt file has gone is a miss, since a caller needs it."""
    if force:
        return None
    db = open_db(settings.db_url, SCHEMA)
    try:
        row = db.execute("SELECT * FROM analysis_cache WHERE key = ?", (key,)).fetchone()
    finally:
        db.close()
    if row is None or datetime.fromisoformat(row["created_at"]) + TTL <= clock():
        return None
    path = Path(row["receipt_path"])
    if not path.exists():
        return None
    return CachedAnalysis(VerdictValue(row["verdict"]), row["score"], row["ledger"], path)


def remember(
    settings: Settings,
    key: str,
    submission_id: str,
    value: VerdictValue,
    score: int,
    ledger: str,
    receipt_path: Path,
    *,
    degraded: bool,
    clock: Clock = utc_now,
) -> bool:
    """True when stored. Refuses by itself, so a caller cannot store a result that must rerun."""
    if not cacheable(value, degraded=degraded):
        return False
    moment = clock()
    clean = redact(ledger, settings.secrets())
    db = open_db(settings.db_url, SCHEMA)
    try:
        with db:
            db.execute("DELETE FROM analysis_cache WHERE created_at <= ?", ((moment - TTL).isoformat(),))
            db.execute(
                """INSERT OR REPLACE INTO analysis_cache
                   (key, submission_id, verdict, score, ledger, receipt_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key, submission_id, value.value, score, clean, str(receipt_path), moment.isoformat()),
            )
    finally:
        db.close()
    return True
