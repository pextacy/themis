"""A periodic summary for maintainers, from the store only. Stored values are made inert.

It reports what was counted. Time saved is not measured, so it is not claimed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from themis.config import Settings
from themis.intake.github import GitHubClient
from themis.models import VerdictValue
from themis.report.github_comment import inert
from themis.store import redact
from themis.store.connect import open_db
from themis.store.db import SCHEMA

TOP_ITEMS = 5
FLAGGED = (VerdictValue.CONTRADICTED.value, VerdictValue.TAINTED.value)
QUIET = (VerdictValue.TAINTED.value, VerdictValue.TAINTED_UNCHECKED.value, VerdictValue.DEFERRED.value)
ITEM_COLUMNS = "repo, number, kind, verdict, score"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _scope(start: datetime, end: datetime, repo: str | None) -> tuple[str, list[str]]:
    query = "status = 'done' AND created_at >= ? AND created_at < ?"
    params = [_stamp(start), _stamp(end)]
    return (query + " AND repo = ?", [*params, repo]) if repo else (query, params)


def _verdicts(db: sqlite3.Connection, start: datetime, end: datetime, repo: str | None) -> dict[str, int]:
    where, params = _scope(start, end, repo)
    rows = db.execute(f"SELECT verdict, COUNT(*) FROM submissions WHERE {where} GROUP BY verdict", params)  # noqa: S608
    return {str(row[0]): int(row[1]) for row in rows}


def _items(db: sqlite3.Connection, where: str, params: list[str], tail: str) -> list[dict[str, Any]]:
    query = f"SELECT {ITEM_COLUMNS} FROM submissions WHERE {where} {tail} LIMIT {TOP_ITEMS}"  # noqa: S608
    return [dict(row) for row in db.execute(query, params)]


def _to_review(db: sqlite3.Connection, start: datetime, end: datetime, repo: str | None) -> list[dict[str, Any]]:
    where, params = _scope(start, end, repo)
    marks = ", ".join("?" for _ in QUIET)
    return _items(db, f"{where} AND verdict NOT IN ({marks})", [*params, *QUIET], "ORDER BY score DESC, id")


def _flagged(db: sqlite3.Connection, start: datetime, end: datetime, repo: str | None) -> list[dict[str, Any]]:
    where, params = _scope(start, end, repo)
    marks = ", ".join("?" for _ in FLAGGED)
    return _items(db, f"{where} AND verdict IN ({marks})", [*params, *FLAGGED], "ORDER BY created_at DESC, id")


def _costs(db: sqlite3.Connection, start: datetime, end: datetime, repo: str | None) -> dict[str, Any]:
    query = """SELECT COALESCE(SUM(c.cost_usd), 0) AS usd, COUNT(*) AS calls, COUNT(c.cost_usd) AS priced,
               COALESCE(SUM(c.fallback), 0) AS fallbacks
               FROM otari_calls c JOIN submissions s ON s.id = c.submission_id
               WHERE c.created_at >= ? AND c.created_at < ?"""
    params = [_stamp(start), _stamp(end)]
    if repo:
        query, params = query + " AND s.repo = ?", [*params, repo]
    row = db.execute(query, params).fetchone()
    return {
        "usd": round(row["usd"], 6) if row["priced"] else None,
        "calls": row["calls"],
        "unpriced_calls": row["calls"] - row["priced"],
        "fallbacks": row["fallbacks"],
    }


def _flagged_count(verdicts: dict[str, int]) -> int:
    return sum(verdicts.get(name, 0) for name in FLAGGED)


def build(settings: Settings, *, days: int = 7, repo: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    end = now or datetime.now(UTC)
    start, earlier = end - timedelta(days=days), end - timedelta(days=days * 2)
    db = open_db(settings.db_url, SCHEMA)
    try:
        verdicts = _verdicts(db, start, end, repo)
        previous = _verdicts(db, earlier, start, repo)
        data = {
            "repo": repo,
            "days": days,
            "start": _stamp(start),
            "end": _stamp(end),
            "total": sum(verdicts.values()),
            "previous_total": sum(previous.values()),
            "verdicts": verdicts,
            "flagged": _flagged_count(verdicts),
            "previous_flagged": _flagged_count(previous),
            "to_review": _to_review(db, start, end, repo),
            "new_flagged": _flagged(db, start, end, repo),
            "costs": _costs(db, start, end, repo),
        }
    finally:
        db.close()
    cleaned: dict[str, Any] = redact(data, settings.secrets())
    return cleaned


def _change(current: int, previous: int) -> str:
    delta = current - previous
    return f"{current} ({delta:+d} on the previous period)"


def _item_line(item: dict[str, Any]) -> str:
    where = f"{inert(item['repo'], 100)} number {inert(item['number'], 12)}"
    return f"- {where}: {inert(item['verdict'], 24)}, {inert(item['kind'], 24)}, score {inert(item['score'], 4)}"


def _section(title: str, items: list[dict[str, Any]]) -> list[str]:
    return ["", f"**{title}**", *[_item_line(item) for item in items]] if items else []


def _cost_lines(costs: dict[str, Any]) -> list[str]:
    usd = "not reported" if costs["usd"] is None else f"${costs['usd']:.4f}"
    lines = [f"- Cost: {usd} over {costs['calls']} gateway calls", f"- Fallbacks: {costs['fallbacks']}"]
    if costs["unpriced_calls"]:
        lines.append(f"- {costs['unpriced_calls']} calls reported no price, so the real cost may be higher")
    return lines


def render(data: dict[str, Any]) -> str:
    scope = inert(data["repo"], 100) if data["repo"] else "all repositories"
    lines = [
        f"### THEMIS digest · {scope} · last {data['days']} days",
        "",
        f"- Submissions: {_change(data['total'], data['previous_total'])}",
        f"- Contradicted or tainted: {_change(data['flagged'], data['previous_flagged'])}",
        *_cost_lines(data["costs"]),
    ]
    if data["verdicts"]:
        lines += ["", "| Verdict | Count |", "|---|---|"]
        lines += [f"| {inert(name, 24)} | {count} |" for name, count in sorted(data["verdicts"].items())]
    lines += _section("Review first", data["to_review"])
    lines += _section("New contradicted or tainted", data["new_flagged"])
    lines += ["", "Time saved is not reported, because THEMIS does not measure it."]
    return "\n".join(lines) + "\n"


async def post_issue(github: GitHubClient, repo: str, data: dict[str, Any], body: str) -> int:
    """One new issue on the named repository. It never closes or edits an older digest."""
    title = f"THEMIS digest to {str(data['end'])[:10]}"
    created = await github.post(f"/repos/{repo}/issues", {"title": title, "body": body})
    return int(created.get("number", 0))
