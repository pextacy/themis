"""Read-only JSON, metrics, badges, analytics and the optional dashboard token.

Nothing here writes to the store or calls the gateway. Contributor keys stay out of the JSON.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from themis.config import RepoPolicy, Settings
from themis.models import VerdictValue
from themis.store import Store
from themis.verdict.priority import rank

PUBLIC_PREFIXES = ("/badge/", "/webhook/")
PRIVATE_FIELDS = ("contributor_key",)
DETAIL_TABLES = ("claims", "checks", "shield_findings", "otari_calls", "overrides")
CHALLENGE = 'Basic realm="THEMIS", Bearer realm="THEMIS"'
GOOD = (VerdictValue.EVIDENCED.value, VerdictValue.PLAUSIBLE.value)
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BADGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label}: {message}">'
    "<title>{title}</title>"
    '<rect width="{left}" height="20" fill="#333"/><rect x="{left}" width="{right}" height="20" fill="#1f4e79"/>'
    '<g fill="#fff" font-family="Verdana,sans-serif" font-size="11" text-anchor="middle">'
    '<text x="{left_mid}" y="14">{label}</text><text x="{right_mid}" y="14">{message}</text></g></svg>'
)
CHAR_WIDTH = 7
BADGE_PADDING = 12

StoreFactory = Callable[[], Store]
Page = Callable[..., HTMLResponse]
Next = Callable[[Request], Awaitable[Response]]


def _presented(request: Request) -> str:
    """A bearer token for scripts, or a Basic password so a browser can log in."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    if scheme.lower() == "basic":
        try:
            return base64.b64decode(value.strip(), validate=True).decode().partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            return ""
    return ""


def install_auth(app: FastAPI, settings: Settings) -> None:
    """With no token set, nothing changes: the dashboard stays local and open."""
    if settings.dashboard_token is None:
        return
    expected = settings.dashboard_token.get_secret_value().encode()

    @app.middleware("http")
    async def require_token(request: Request, call_next: Next) -> Response:
        if request.url.path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        if hmac.compare_digest(_presented(request).encode(), expected):
            return await call_next(request)
        return PlainTextResponse("login required\n", status_code=401, headers={"WWW-Authenticate": CHALLENGE})


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in PRIVATE_FIELDS}


def _label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_text(stats: dict[str, Any]) -> str:
    lines = [
        "# HELP themis_submissions_total Analyzed submissions by verdict.",
        "# TYPE themis_submissions_total gauge",
    ]
    for verdict, count in sorted(stats["verdicts"].items(), key=lambda item: str(item[0])):
        lines.append(f'themis_submissions_total{{verdict="{_label(verdict)}"}} {count}')
    gauges = {
        "themis_gateway_calls_total": stats["calls"],
        "themis_fallbacks_total": stats["fallbacks"],
        "themis_budget_refusals_total": stats["budget_refusals"],
        "themis_guardrail_blocks_total": stats["guardrail_blocks"],
    }
    if stats["cost_usd"] is not None:  # An unpriced gateway must not read as zero spend.
        gauges["themis_cost_usd_total"] = stats["cost_usd"]
    for name, value in gauges.items():
        lines += [f"# TYPE {name} gauge", f"{name} {value}"]
    return "\n".join(lines) + "\n"


def badge_svg(repo: str, rows: list[dict[str, Any]]) -> str:
    label, message = "themis", f"{len(rows)} triaged" if rows else "no data"
    left = len(label) * CHAR_WIDTH + BADGE_PADDING
    right = len(message) * CHAR_WIDTH + BADGE_PADDING
    return BADGE.format(
        total=left + right, left=left, right=right, left_mid=left // 2, right_mid=left + right // 2,
        label=label, message=escape(message), title=escape(f"THEMIS: {repo}"),
    )  # fmt: skip


def _share(count: int, total: int) -> int:
    return round(100 * count / total) if total else 0


def _cost_rows(calls: list[dict[str, Any]], key: Callable[[dict[str, Any]], object]) -> list[dict[str, Any]]:
    """Cost stays None when no call in the group was priced, so unknown never reads as $0."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        groups[str(key(call) or "unknown")].append(call)
    rows = []
    for name, members in groups.items():
        priced = [item["cost_usd"] for item in members if item["cost_usd"] is not None]
        rows.append({"name": name, "calls": len(members), "cost": sum(priced) if priced else None})
    return sorted(rows, key=lambda row: (-(row["cost"] or 0), row["name"]))


def analytics(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    repos: dict[str, Counter[str]] = defaultdict(Counter)
    days: Counter[str] = Counter()
    for row in rows:
        repos[str(row["repo"])][str(row["verdict"])] += 1
        days[str(row["created_at"])[:10]] += 1
    mix = [
        {"repo": repo, "total": sum(counts.values()),
         "parts": [{"verdict": name, "count": count, "share": _share(count, sum(counts.values()))}
                   for name, count in sorted(counts.items())]}
        for repo, counts in sorted(repos.items())
    ]  # fmt: skip
    peak = max(days.values(), default=0)
    daily = [{"day": day, "count": days[day], "share": _share(days[day], peak)} for day in sorted(days)]
    return {
        "mix": mix,
        "daily": daily,
        "by_role": _cost_rows(calls, lambda call: call["role"]),
        "by_model": _cost_rows(calls, lambda call: call["effective_model"] or call["requested_model"]),
    }


def register(app: FastAPI, settings: Settings, store_factory: StoreFactory, page: Page) -> None:
    install_auth(app, settings)
    policy = RepoPolicy()

    @app.get("/api/v1/queue")
    def api_queue(verdict: str | None = None, kind: str | None = None, repo: str | None = None) -> JSONResponse:
        with store_factory() as database:
            rows = database.queue(verdict or None, kind or None)
        chosen = [public_row(row) for row in rows if not repo or row["repo"] == repo]
        return JSONResponse({"items": rank(chosen, policy)})

    @app.get("/api/v1/submissions/{submission_id}")
    def api_submission(submission_id: str) -> JSONResponse:
        with store_factory() as database:
            item = database.submission(submission_id)
            if item is None:
                raise HTTPException(status_code=404, detail="no such submission")
            tables = {name: database.rows(name, submission_id) for name in DETAIL_TABLES}
        return JSONResponse({"submission": public_row(item), **tables})

    @app.get("/api/v1/stats")
    def api_stats() -> JSONResponse:
        with store_factory() as database:
            return JSONResponse(database.stats())

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        with store_factory() as database:
            return PlainTextResponse(metrics_text(database.stats()), media_type=METRICS_TYPE)

    @app.get("/badge/{owner}/{repo}.svg")
    def badge(owner: str, repo: str) -> Response:
        """Public by design: it shows a count and nothing else."""
        name = f"{owner}/{repo}"
        with store_factory() as database:
            rows = [row for row in database.queue() if row["repo"] == name]
        headers = {"Cache-Control": "max-age=300", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}  # fmt: skip
        return Response(badge_svg(name, rows), media_type="image/svg+xml", headers=headers)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request) -> HTMLResponse:
        with store_factory() as database:
            data = analytics(database.queue(), database.otari_calls())
        return page(request, "analytics.html", **data)
