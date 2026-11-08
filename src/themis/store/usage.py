"""Spend as THEMIS recorded it, and an optional local stop before the gateway's own budgets.

The gateway enforces real budgets. This covers what it cannot see: a project cap per repository
and a burst limit. A refusal here only ever defers. Unpriced calls add nothing, so a total can
be lower than the truth, and every report says how many there were.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from themis.config import RepoPolicy, Settings
from themis.models import Submission
from themis.store.connect import open_db
from themis.store.db import SCHEMA

PROJECT_KEY = "gh:0"  # Rechecks a maintainer approved bill the project, not the contributor.
BURST_WINDOW = timedelta(hours=1)
TOP_CONTRIBUTORS = 20


@dataclass(frozen=True)
class Spend:
    usd: float
    calls: int
    unpriced: int

    @property
    def unpriced_share(self) -> float | None:
        return self.unpriced / self.calls if self.calls else None


@dataclass(frozen=True)
class Deferral:
    """Why this submission waits. Fixed wording: nothing here comes from the submission."""

    code: str
    reason: str
    used: float
    limit: float


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _day_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(moment: datetime) -> datetime:
    return _day_start(moment).replace(day=1)


def spend(db: sqlite3.Connection, since: datetime, *, contributor: str | None = None, repo: str | None = None) -> Spend:
    """With neither filter, calls tied to no submission (doctor, captures) count too."""
    query = """SELECT COALESCE(SUM(c.cost_usd), 0) AS usd, COUNT(*) AS calls, COUNT(c.cost_usd) AS priced
               FROM otari_calls c LEFT JOIN submissions s ON s.id = c.submission_id WHERE c.created_at >= ?"""
    params: list[str] = [_stamp(since)]
    if contributor is not None:
        query, params = query + " AND s.contributor_key = ?", [*params, contributor]
    if repo is not None:
        query, params = query + " AND s.repo = ?", [*params, repo]
    row = db.execute(query, params).fetchone()
    return Spend(float(row["usd"]), int(row["calls"]), int(row["calls"]) - int(row["priced"]))


def recent_submissions(db: sqlite3.Connection, contributor: str, moment: datetime, exclude: str) -> int:
    """Distinct items, so editing one item many times is not a burst."""
    row = db.execute(
        "SELECT COUNT(*) FROM submissions WHERE contributor_key = ? AND created_at >= ? AND id != ?",
        (contributor, _stamp(moment - BURST_WINDOW), exclude),
    ).fetchone()
    return int(row[0])


def _project_cap(db: sqlite3.Connection, policy: RepoPolicy, repo: str, now: datetime) -> Deferral | None:
    used = spend(db, _month_start(now), repo=repo).usd
    limit = policy.budgets.project_monthly_usd
    return (
        Deferral("project_monthly", "the project's monthly budget is used up", used, limit) if used >= limit else None
    )


def _contributor_cap(db: sqlite3.Connection, policy: RepoPolicy, key: str, now: datetime) -> Deferral | None:
    used = spend(db, _day_start(now), contributor=key).usd
    limit = policy.budgets.per_contributor_daily_usd
    return (
        Deferral("contributor_daily", "this contributor's budget for today is used up", used, limit)
        if used >= limit
        else None
    )


def _burst(db: sqlite3.Connection, policy: RepoPolicy, item: Submission, now: datetime) -> Deferral | None:
    limit = policy.budgets.burst_per_contributor_hour
    if limit <= 0:
        return None
    count = recent_submissions(db, item.contributor_key, now, item.id)
    return Deferral("burst", "too many new items this hour", count, limit) if count >= limit else None


def preflight(settings: Settings, policy: RepoPolicy, submission: Submission, now: datetime) -> Deferral | None:
    """Off unless the policy turns it on. A Deferral maps to DEFERRED and nothing harsher."""
    if not policy.budgets.local_enforcement:
        return None
    db = open_db(settings.db_url, SCHEMA)
    try:
        project = _project_cap(db, policy, submission.repo, now)
        if project is not None or submission.contributor_key == PROJECT_KEY:
            return project
        return _contributor_cap(db, policy, submission.contributor_key, now) or _burst(db, policy, submission, now)
    finally:
        db.close()


def forecast_month_end(spent: float, now: datetime) -> float:
    """Straight line from the month so far. A projection, not a measurement."""
    moment = now.astimezone(UTC)
    elapsed = (moment - _month_start(moment)).total_seconds()
    total = calendar.monthrange(moment.year, moment.month)[1] * 86_400
    return spent * total / elapsed if elapsed > 0 else spent


def _percent(used: float, limit: float) -> float | None:
    return round(used / limit * 100, 1) if limit > 0 else None


def _contributors(db: sqlite3.Connection, now: datetime, repo: str | None) -> list[dict[str, Any]]:
    query = """SELECT s.contributor_key AS contributor, COALESCE(SUM(c.cost_usd), 0) AS usd, COUNT(*) AS calls,
               COUNT(*) - COUNT(c.cost_usd) AS unpriced
               FROM otari_calls c JOIN submissions s ON s.id = c.submission_id WHERE c.created_at >= ?"""
    params: list[str | int] = [_stamp(_day_start(now))]
    if repo is not None:
        query, params = query + " AND s.repo = ?", [*params, repo]
    query += " GROUP BY s.contributor_key ORDER BY usd DESC, contributor LIMIT ?"
    return [dict(row) for row in db.execute(query, [*params, TOP_CONTRIBUTORS])]


def report(settings: Settings, policy: RepoPolicy, now: datetime, *, repo: str | None = None) -> dict[str, Any]:
    db = open_db(settings.db_url, SCHEMA)
    try:
        month = spend(db, _month_start(now), repo=repo)
        rows = _contributors(db, now, repo)
    finally:
        db.close()
    cap, daily = policy.budgets.project_monthly_usd, policy.budgets.per_contributor_daily_usd
    for row in rows:
        row["usd"], row["daily_cap_usd"] = round(row["usd"], 6), daily
        row["percent_used"] = _percent(row["usd"], daily)
    priced = month.calls > month.unpriced
    return {
        "repo": repo,
        "local_enforcement": policy.budgets.local_enforcement,
        "project": {
            **asdict(month),
            "usd": round(month.usd, 6) if priced else None,
            "unpriced_share": month.unpriced_share,
            "monthly_cap_usd": cap,
            "percent_used": _percent(month.usd, cap) if priced else None,
            "forecast_month_end_usd": round(forecast_month_end(month.usd, now), 6) if priced else None,
        },
        "contributors_today": rows,
    }
