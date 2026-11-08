"""GitHub webhook receiver for self-hosted mode.

This process holds OTARI_API_KEY, so it reads pull requests through the API and never fetches
or runs their code (golden rule 6). A pull request seen here has no test artifact, so its test
checks stay UNVERIFIED.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from themis.action import dispatch
from themis.config import Settings
from themis.intake.github import Target
from themis.logs import get_logger
from themis.pipeline import KindDisabled, run_url
from themis.store.connect import open_db

log = get_logger(__name__)
PATH = "/webhook/github"
MAX_BODY_BYTES = 5 * 1024 * 1024
QUEUE_SIZE = 64
RETRY_AFTER_S = 30
DRAIN_S = 30
KEEP_DELIVERIES = timedelta(days=7)
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
DELIVERY_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
EVENT_ACTIONS = {
    "issues": frozenset({"opened", "edited"}),
    "issue_comment": frozenset({"created"}),
    "pull_request": frozenset({"opened", "edited", "synchronize", "reopened"}),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
"""
Handler = Callable[[str, dict[str, Any], str], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    delivery: str
    name: str
    event: dict[str, Any]
    repo: str


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


def valid_repo(name: object) -> str | None:
    if isinstance(name, str) and REPO_NAME.fullmatch(name) and ".." not in name:
        return name
    return None


def wanted(name: str, event: dict[str, Any]) -> bool:
    return event.get("action") in EVENT_ACTIONS.get(name, frozenset())


class Deliveries:
    """Remembers what was accepted, because GitHub can send one delivery more than once."""

    def __init__(self, url: str) -> None:
        self._url = url

    def claim(self, delivery: str, now: datetime) -> bool:
        db = open_db(self._url, SCHEMA)
        try:
            with db:
                db.execute(
                    "DELETE FROM webhook_deliveries WHERE received_at < ?", ((now - KEEP_DELIVERIES).isoformat(),)
                )
                done = db.execute(
                    "INSERT OR IGNORE INTO webhook_deliveries (id, received_at) VALUES (?, ?)",
                    (delivery, now.isoformat()),
                )
            return done.rowcount == 1
        finally:
            db.close()

    def release(self, delivery: str) -> None:
        """A delivery that was not queued must be able to come back."""
        db = open_db(self._url, SCHEMA)
        try:
            with db:
                db.execute("DELETE FROM webhook_deliveries WHERE id = ?", (delivery,))
        finally:
            db.close()


async def handle_pull_request(settings: Settings, event: dict[str, Any], repo: str) -> None:
    owner, name = repo.split("/", 1)
    try:
        await run_url(settings, Target(owner, name, int(event["number"])), automatic=True)
    except KindDisabled as error:
        log.info("webhook.kind_disabled", submission_id=str(error), stage="action")


def default_handler(settings: Settings) -> Handler:
    async def handle(name: str, event: dict[str, Any], repo: str) -> None:
        if name == "pull_request":
            await handle_pull_request(settings, event, repo)
        else:
            await dispatch(settings, name, event, repo, None)

    return handle


async def _worker(queue: asyncio.Queue[Job], handler: Handler) -> None:
    while True:
        job = await queue.get()
        try:
            await handler(job.name, job.event, job.repo)
        except Exception as error:  # A worker must outlive any one event. Bodies stay out of the log.
            log.warning("webhook.failed", stage="webhook", delivery=job.delivery, error=type(error).__name__)
        finally:
            queue.task_done()


def _install_workers(app: FastAPI, queue: asyncio.Queue[Job], handler: Handler, count: int) -> None:
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(inner: FastAPI) -> AsyncIterator[Any]:
        workers = [asyncio.create_task(_worker(queue, handler)) for _ in range(count)]
        async with previous(inner) as state:
            yield state
        try:
            await asyncio.wait_for(queue.join(), DRAIN_S)
        except TimeoutError:
            log.warning("webhook.drain_timeout", stage="webhook", waiting=queue.qsize())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    app.router.lifespan_context = lifespan


async def _read_capped(request: Request) -> bytes | None:
    """Reads no further than the cap, whatever Content-Length claimed."""
    declared = request.headers.get("content-length", "0")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _answer(status: int, text: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"status": text}, status_code=status, headers=headers)


def _parse(body: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _enqueue(queue: asyncio.Queue[Job], deliveries: Deliveries, job: Job) -> JSONResponse:
    if not deliveries.claim(job.delivery, datetime.now(UTC)):
        return _answer(200, "duplicate")
    try:
        queue.put_nowait(job)
    except asyncio.QueueFull:
        deliveries.release(job.delivery)
        return _answer(503, "busy", {"Retry-After": str(RETRY_AFTER_S)})
    return _answer(202, "queued")


def register(app: FastAPI, settings: Settings, handler: Handler | None = None) -> None:
    """Adds the route and its workers. Call before the app starts."""
    queue: asyncio.Queue[Job] = asyncio.Queue(QUEUE_SIZE)
    deliveries = Deliveries(settings.db_url)
    _install_workers(app, queue, handler or default_handler(settings), settings.max_concurrency)

    @app.post(PATH)
    async def receive(request: Request) -> JSONResponse:
        secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""
        if not secret:
            return _answer(503, "webhook secret not configured")
        body = await _read_capped(request)
        if body is None:
            return _answer(413, "body too large")
        if not signature_valid(secret, body, request.headers.get("x-hub-signature-256")):
            return _answer(401, "bad signature")
        name = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        event = _parse(body)
        if event is None or DELIVERY_ID.fullmatch(delivery) is None:
            return _answer(400, "bad request")
        if name == "ping":
            return _answer(200, "pong")
        if not wanted(name, event):
            return _answer(202, "ignored")
        repo = valid_repo((event.get("repository") or {}).get("full_name"))
        if repo is None:
            return _answer(400, "bad repository")
        return _enqueue(queue, deliveries, Job(delivery, name, event, repo))
