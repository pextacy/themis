"""SQLite persistence. Everything written here passes through redact first."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from themis.config import ConfigError
from themis.models import Check, ClaimSet, OtariCall, ShieldResult, Submission, Verdict
from themis.store.redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL,
    contributor_key TEXT NOT NULL, first_time INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    head_sha TEXT, verdict TEXT, score INTEGER, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT NOT NULL, submission_id TEXT NOT NULL REFERENCES submissions(id), type TEXT NOT NULL,
    text TEXT NOT NULL, span_start INTEGER, span_end INTEGER, normalized_target TEXT,
    PRIMARY KEY (submission_id, id)
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id), claim_id TEXT,
    module TEXT NOT NULL, status TEXT NOT NULL, strength TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
    otari_request_id TEXT
);
CREATE TABLE IF NOT EXISTS otari_calls (
    id INTEGER PRIMARY KEY, request_id TEXT, submission_id TEXT REFERENCES submissions(id), role TEXT NOT NULL,
    stage TEXT, endpoint TEXT NOT NULL, status INTEGER, requested_model TEXT, effective_model TEXT, provider TEXT,
    fallback INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, latency_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL, prompt_version TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shield_findings (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id), source TEXT NOT NULL,
    kind TEXT NOT NULL, span_start INTEGER, span_end INTEGER, excerpt_redacted TEXT
);
CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id), ecosystem TEXT NOT NULL,
    name TEXT NOT NULL, version TEXT, signals_json TEXT NOT NULL DEFAULT '{}', risk TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id), maintainer TEXT NOT NULL,
    from_verdict TEXT NOT NULL, to_verdict TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
TABLES = ("submissions", "claims", "checks", "otari_calls", "shield_findings", "dependencies", "overrides")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sqlite_path(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ConfigError(f"unsupported THEMIS_DB_URL scheme: {url.split(':', 1)[0]}")
    return url.removeprefix(prefix)


class Store:
    def __init__(self, url: str, *, secrets: tuple[str, ...] = ()) -> None:
        path = sqlite_path(url)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._secrets = secrets

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _clean(self, value: Any) -> Any:
        return redact(value, self._secrets)

    def save_submission(self, item: Submission) -> None:
        """Keeps metadata only: raw submission text is not stored in this table."""
        with self._db:
            self._db.execute(
                """INSERT INTO submissions (id, repo, number, kind, contributor_key, first_time, created_at, head_sha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, head_sha = excluded.head_sha""",
                (item.id, item.repo, item.number, item.kind.value, item.contributor_key, int(item.first_time),
                 _now(), item.head_sha),
            )  # fmt: skip

    def save_otari_call(self, call: OtariCall, submission_id: str | None = None) -> None:
        clean = OtariCall.model_validate(self._clean(call.model_dump(mode="json")))
        with self._db:
            self._db.execute(
                """INSERT INTO otari_calls (request_id, submission_id, role, stage, endpoint, status, requested_model,
                   effective_model, provider, fallback, prompt_tokens, completion_tokens, cost_usd, latency_ms,
                   outcome, prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (clean.request_id, submission_id, clean.role.value, clean.stage, clean.endpoint, clean.status,
                 clean.requested_model, clean.effective_model, clean.provider,
                 None if clean.fallback is None else int(clean.fallback), clean.usage.prompt, clean.usage.completion,
                 clean.cost_usd, clean.latency_ms, clean.outcome, clean.prompt_version, _now()),
            )  # fmt: skip

    def save_shield(self, submission_id: str, result: ShieldResult) -> None:
        rows = [
            (submission_id, item.source, item.kind, *(item.span or (None, None)), self._clean(item.excerpt_redacted))
            for item in result.findings
        ]
        with self._db:
            self._db.execute("DELETE FROM shield_findings WHERE submission_id = ?", (submission_id,))
            self._db.executemany(
                """INSERT INTO shield_findings (submission_id, source, kind, span_start, span_end, excerpt_redacted)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def save_claims(self, submission_id: str, claim_set: ClaimSet) -> None:
        rows = [
            (claim.id, submission_id, claim.type.value, self._clean(claim.text),
             *(claim.span or (None, None)), claim.normalized_target)
            for claim in claim_set.claims
        ]  # fmt: skip
        with self._db:
            self._db.execute("DELETE FROM claims WHERE submission_id = ?", (submission_id,))
            self._db.executemany(
                """INSERT INTO claims (id, submission_id, type, text, span_start, span_end, normalized_target)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def save_checks(self, submission_id: str, checks: list[Check]) -> None:
        rows = [
            (submission_id, item.claim_id, item.module, item.status.value,
             item.strength.value if item.strength else None,
             json.dumps(self._clean(item.detail), sort_keys=True), item.otari_request_id)
            for item in checks
        ]  # fmt: skip
        with self._db:
            self._db.execute("DELETE FROM checks WHERE submission_id = ?", (submission_id,))
            self._db.executemany(
                """INSERT INTO checks (submission_id, claim_id, module, status, strength, detail_json, otari_request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def save_verdict(self, submission_id: str, verdict: Verdict, kind: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE submissions SET verdict = ?, score = ?, kind = ?, status = 'done' WHERE id = ?",
                (verdict.value.value, verdict.score, kind, submission_id),
            )

    def save_override(self, submission_id: str, maintainer: str, old: str, new: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO overrides (submission_id, maintainer, from_verdict, to_verdict, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (submission_id, maintainer, old, new, self._clean(reason), _now()),
            )
            self._db.execute("UPDATE submissions SET verdict = ? WHERE id = ?", (new, submission_id))

    def submission(self, submission_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        return dict(row) if row else None

    def queue(self, verdict: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        """Most credible first, so maintainers see the strongest evidence at the top."""
        query = "SELECT * FROM submissions WHERE status = 'done'"
        params: list[str] = []
        if verdict:
            query, params = query + " AND verdict = ?", [*params, verdict]
        if kind:
            query, params = query + " AND kind = ?", [*params, kind]
        return [dict(row) for row in self._db.execute(query + " ORDER BY score DESC, created_at DESC", params)]

    def stats(self) -> dict[str, Any]:
        verdicts = dict(
            self._db.execute(
                "SELECT verdict, COUNT(*) FROM submissions WHERE status = 'done' GROUP BY verdict"
            ).fetchall()
        )
        calls = self._db.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(fallback), 0) AS fallbacks,
               COUNT(cost_usd) AS priced, SUM(outcome = 'budget_exceeded') AS budget_refusals FROM otari_calls"""
        ).fetchone()
        latency = [row[0] for row in self._db.execute("SELECT latency_ms FROM otari_calls ORDER BY latency_ms")]
        blocks = self._db.execute(
            "SELECT COUNT(DISTINCT submission_id) FROM shield_findings WHERE source = 'guardrail'"
        ).fetchone()[0]
        total = sum(verdicts.values())
        flagged = verdicts.get("CONTRADICTED", 0) + verdicts.get("TAINTED", 0)
        return {
            "total": total,
            "verdicts": verdicts,
            "flagged_rate": flagged / total if total else None,
            "calls": calls["calls"],
            "cost_usd": calls["cost"] if calls["priced"] else None,
            "cost_per_submission": calls["cost"] / total if total and calls["priced"] else None,
            "fallbacks": calls["fallbacks"],
            "budget_refusals": calls["budget_refusals"] or 0,
            "guardrail_blocks": blocks,
            "median_latency_ms": latency[len(latency) // 2] if latency else None,
        }

    def rows(self, table: str, submission_id: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        query = f"SELECT * FROM {table} WHERE submission_id = ?"  # noqa: S608 - table checked above
        return [dict(row) for row in self._db.execute(query, (submission_id,))]

    def otari_calls(self, submission_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM otari_calls"
        params: tuple[str, ...] = ()
        if submission_id is not None:
            query, params = query + " WHERE submission_id = ?", (submission_id,)
        return [dict(row) for row in self._db.execute(query + " ORDER BY id", params)]

    def tables(self) -> list[str]:
        rows = self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return sorted(row["name"] for row in rows)
