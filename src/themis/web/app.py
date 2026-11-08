"""FastAPI dashboard. Its pages read the store and make no gateway calls.

The one exception is the signed webhook route, which queues analysis (see `webhook.py`).

Everything shown may hold submission text, so Jinja autoescaping stays on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape

from themis.config import Settings
from themis.models import SubmissionKind, VerdictValue
from themis.store import Store
from themis.web import insights, webhook

READINESS_PATH = Path("docs/READINESS.md")
DETAIL_TABLES = ("claims", "checks", "shield_findings", "otari_calls", "overrides")


def readiness_rows(text: str) -> tuple[str, list[list[str]]]:
    """Split the generated table without rendering its Markdown as HTML."""
    summary = next(
        (line.replace("**", "").strip() for line in text.splitlines() if line.startswith("**")), "No capture yet."
    )
    rows = []
    for line in text.splitlines():
        cells = [cell.strip(" `") for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and not set(cells[0]) <= {"-"} and cells[0] != "Probe":
            rows.append(cells)
    return summary, rows


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="THEMIS", docs_url=None, redoc_url=None, openapi_url=None)
    environment = Environment(
        loader=PackageLoader("themis.web", "templates"), autoescape=select_autoescape(("html",), default=True)
    )
    templates = Jinja2Templates(env=environment)

    def store() -> Store:
        return Store(settings.db_url, secrets=settings.secrets())

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, verdict: str | None = None, kind: str | None = None) -> HTMLResponse:
        with store() as database:
            items = database.queue(verdict or None, kind or None)
        name = "_rows.html" if request.headers.get("hx-request") else "queue.html"
        return page(
            request, name, items=items, verdict=verdict, kind=kind,
            verdicts=[value.value for value in VerdictValue], kinds=[value.value for value in SubmissionKind],
        )  # fmt: skip

    @app.get("/submissions/{submission_id}", response_class=HTMLResponse)
    def detail(request: Request, submission_id: str) -> HTMLResponse:
        with store() as database:
            item = database.submission(submission_id)
            if item is None:
                raise HTTPException(status_code=404, detail="no such submission")
            tables = {name: database.rows(name, submission_id) for name in DETAIL_TABLES}
        return page(request, "detail.html", item=item, **tables)

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request) -> HTMLResponse:
        with store() as database:
            return page(request, "stats.html", stats=database.stats())

    @app.get("/doctor", response_class=HTMLResponse)
    def doctor(request: Request) -> HTMLResponse:
        text = READINESS_PATH.read_text() if READINESS_PATH.exists() else ""
        summary, rows = readiness_rows(text)
        return page(request, "doctor.html", summary=summary, rows=rows)

    insights.register(app, settings, store, page)
    webhook.register(app, settings)
    return app
