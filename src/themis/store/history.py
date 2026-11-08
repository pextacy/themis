"""Every run of a submission, and what changed between runs.

The core tables hold only the newest run. Claim ids restart at each run, so a claim is matched
by its type and target rather than by id.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from themis.config import Settings
from themis.models import Check, Claim, Verdict
from themis.store.connect import open_db
from themis.store.db import Store
from themis.store.redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL, head_sha TEXT, verdict TEXT NOT NULL,
    score INTEGER NOT NULL, claims_json TEXT NOT NULL, checks_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analysis_runs_submission ON analysis_runs (submission_id, id);
CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL, head_sha TEXT NOT NULL, contributor_key TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (submission_id, head_sha)
);
"""
NO_CLAIM = "-"


@contextmanager
def _database(url: str) -> Iterator[sqlite3.Connection]:
    """Commits on success and always closes, which a bare connection context does not."""
    db = open_db(url, SCHEMA)
    try:
        with db:
            yield db
    finally:
        db.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _claim_key(claim: Claim) -> str:
    return f"{claim.type.value}:{claim.normalized_target or claim.text}"


def _claims(claims: list[Claim]) -> list[str]:
    return sorted({_claim_key(claim) for claim in claims})


def _checks(claims: list[Claim], checks: list[Check]) -> dict[str, str]:
    """When a module has several results for one claim, the last wins in a stable order."""
    keys = {claim.id: _claim_key(claim) for claim in claims}
    found: dict[str, str] = {}
    for check in checks:
        name = f"{check.module}|{keys.get(check.claim_id or '', NO_CLAIM)}"
        found[name] = f"{check.status.value}/{check.strength.value}" if check.strength else check.status.value
    return found


def record_analysis(
    settings: Settings,
    submission_id: str,
    head_sha: str | None,
    verdict: Verdict,
    claims: list[Claim],
    checks: list[Check],
    *,
    now: str | None = None,
) -> int:
    secrets = settings.secrets()
    row = (
        submission_id, head_sha, verdict.value.value, verdict.score,
        json.dumps(redact(_claims(claims), secrets)),
        json.dumps(redact(_checks(claims, checks), secrets), sort_keys=True),
        now or _now(),
    )  # fmt: skip
    with _database(settings.db_url) as db:
        cursor = db.execute(
            """INSERT INTO analysis_runs (submission_id, head_sha, verdict, score, claims_json, checks_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        return int(cursor.lastrowid or 0)


def runs(url: str, submission_id: str) -> list[dict[str, Any]]:
    with _database(url) as db:
        rows = db.execute(
            "SELECT * FROM analysis_runs WHERE submission_id = ? ORDER BY id", (submission_id,)
        ).fetchall()
    return [
        {**dict(row), "claims": json.loads(row["claims_json"]), "checks": json.loads(row["checks_json"])}
        for row in rows
    ]  # fmt: skip


@dataclass
class RunDiff:
    at: str
    verdict: tuple[str, str]
    score: tuple[int, int]
    head_changed: bool
    claims_added: list[str] = field(default_factory=list)
    claims_removed: list[str] = field(default_factory=list)
    checks_flipped: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.verdict[0] != self.verdict[1]
            or self.score[0] != self.score[1]
            or self.head_changed
            or self.claims_added
            or self.claims_removed
            or self.checks_flipped
        )


def diff_runs(old: dict[str, Any], new: dict[str, Any]) -> RunDiff:
    before, after = old["checks"], new["checks"]
    flipped = {
        name: (before.get(name), after.get(name))
        for name in sorted({*before, *after})
        if before.get(name) != after.get(name)
    }
    return RunDiff(
        at=new["created_at"],
        verdict=(old["verdict"], new["verdict"]),
        score=(old["score"], new["score"]),
        head_changed=old["head_sha"] != new["head_sha"],
        claims_added=sorted(set(new["claims"]) - set(old["claims"])),
        claims_removed=sorted(set(old["claims"]) - set(new["claims"])),
        checks_flipped=flipped,
    )


def timeline(url: str, submission_id: str) -> list[RunDiff]:
    rows = runs(url, submission_id)
    return [diff_runs(old, new) for old, new in zip(rows, rows[1:], strict=False)]


def record_override(settings: Settings, submission_id: str, maintainer: str, verdict: str, reason: str) -> bool:
    """False when this database never saw the submission, as on a fresh Actions runner."""
    with Store(settings.db_url, secrets=settings.secrets()) as store:
        item = store.submission(submission_id)
        if item is None:
            return False
        store.save_override(submission_id, maintainer, str(item["verdict"]), verdict, reason)
    return True


def record_appeal(settings: Settings, submission_id: str, head_sha: str, contributor_key: str, reason: str) -> bool:
    """False when this commit already has an appeal in this database."""
    row = (submission_id, head_sha, contributor_key, redact(reason, settings.secrets()), _now())
    try:
        with _database(settings.db_url) as db:
            db.execute(
                """INSERT INTO appeals (submission_id, head_sha, contributor_key, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                row,
            )
    except sqlite3.IntegrityError:
        return False
    return True


def appeals(url: str, submission_id: str) -> list[dict[str, Any]]:
    with _database(url) as db:
        rows = db.execute("SELECT * FROM appeals WHERE submission_id = ? ORDER BY id", (submission_id,)).fetchall()
    return [dict(row) for row in rows]
