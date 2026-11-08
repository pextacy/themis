"""Triage a repository's existing items. Resumable, capped, and one failure stays one failure."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from themis.config import ConfigError, Settings
from themis.intake.github import GitHubRateLimited, Target
from themis.intake.listing import ListedItem, Listing, RepoLister, target_for
from themis.logs import get_logger
from themis.models import OtariCall, VerdictValue
from themis.otari.errors import OtariConfigError, OtariFundingError
from themis.pipeline import Outcome, run_url
from themis.store.connect import open_db
from themis.store.redact import redact

log = get_logger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_state (
    repo TEXT NOT NULL, number INTEGER NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
    verdict TEXT, error TEXT, finished_at TEXT NOT NULL, PRIMARY KEY (repo, number)
);
"""
DONE, FAILED, DEFERRED = "done", "failed", "deferred"
MAX_ERROR = 200
Runner = Callable[..., Awaitable[Outcome]]


@dataclass(frozen=True)
class ScanOptions:
    repo: str
    state: str = "open"
    kind: str = "all"
    since: datetime | None = None
    limit: int = 50
    post: bool = False
    max_usd: float | None = None
    concurrency: int = 2


@dataclass
class ScanReport:
    repo: str
    listed: int = 0
    listing_truncated: bool = False
    analyzed: int = 0
    resumed: int = 0
    failed: int = 0
    not_started: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    spent_usd: float = 0.0
    priced_calls: int = 0
    unpriced_calls: int = 0
    stopped: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class ScanState:
    """An item is finished for this version of it. An update makes it due again."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.db_url
        self._secrets = settings.secrets()

    def finished(self, repo: str) -> dict[int, str]:
        db = open_db(self._url, SCHEMA)
        try:
            rows = db.execute("SELECT number, updated_at FROM scan_state WHERE repo = ? AND status = ?", (repo, DONE))
            return {int(row["number"]): str(row["updated_at"]) for row in rows}
        finally:
            db.close()

    def record(self, repo: str, item: ListedItem, status: str, verdict: str | None, error: str | None) -> None:
        clean = redact(error, self._secrets)[:MAX_ERROR] if error else None
        moment = datetime.now(UTC).isoformat(timespec="seconds")
        db = open_db(self._url, SCHEMA)
        try:
            with db:
                db.execute(
                    """INSERT OR REPLACE INTO scan_state
                       (repo, number, updated_at, status, verdict, error, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (repo, item.number, item.updated_at, status, verdict, clean, moment),
                )
        finally:
            db.close()


class _Scan:
    def __init__(self, settings: Settings, options: ScanOptions, runner: Runner, report: ScanReport) -> None:
        self.settings, self.options, self.runner, self.report = settings, options, runner, report
        self.state = ScanState(settings)
        self.verdicts: Counter[str] = Counter()

    def _charge(self, calls: list[OtariCall]) -> None:
        for call in calls:
            if call.cost_usd is None:
                self.report.unpriced_calls += 1
            else:
                self.report.priced_calls += 1
                self.report.spent_usd += call.cost_usd

    def _due_stop(self) -> None:
        cap = self.options.max_usd
        if self.report.stopped is None and cap is not None and self.report.spent_usd >= cap:
            self.report.stopped = f"spent ${self.report.spent_usd:.4f}, cap is ${cap:.4f}"

    async def _analyze(self, target: Target) -> Outcome:
        return await self.runner(self.settings, target, post=self.options.post)

    async def one(self, item: ListedItem, limit: asyncio.Semaphore) -> None:
        async with limit:
            if self.report.stopped is not None:
                self.report.not_started += 1
                return
            repo = self.options.repo
            try:
                outcome = await self._analyze(target_for(repo, item.number))
            except (GitHubRateLimited, OtariConfigError, OtariFundingError, ConfigError) as error:
                # The next item would fail the same way, so stopping saves nothing but noise.
                self.report.failed += 1
                self.report.stopped = f"{type(error).__name__}: {redact(str(error), self.settings.secrets())}"
                self.state.record(repo, item, FAILED, None, str(error))
            except Exception as error:  # One item must never stop the run, whatever it broke.
                log.warning("scan.item_failed", stage="scan", number=item.number, error=type(error).__name__)
                self.report.failed += 1
                self.state.record(repo, item, FAILED, None, f"{type(error).__name__}: {error}")
            else:
                self._finish(item, outcome)

    def _finish(self, item: ListedItem, outcome: Outcome) -> None:
        value = outcome.verdict.value
        self._charge(outcome.calls)
        self.report.analyzed += 1
        self.verdicts[value.value] += 1
        status = DEFERRED if value is VerdictValue.DEFERRED else DONE
        self.state.record(self.options.repo, item, status, value.value, None)
        self._due_stop()


def _pending(listing: Listing, finished: dict[int, str]) -> tuple[list[ListedItem], int]:
    pending = [item for item in listing.items if finished.get(item.number) != item.updated_at]
    return pending, len(listing.items) - len(pending)


async def _list(settings: Settings, options: ScanOptions) -> Listing:
    lister = RepoLister(settings)
    async with lister:
        return await lister.list_items(
            options.repo, state=options.state, kind=options.kind, since=options.since, limit=options.limit
        )


async def run_scan(settings: Settings, options: ScanOptions, *, runner: Runner = run_url) -> ScanReport:
    """Items already running when the cap is reached finish, so spend can pass it by that much."""
    listing = await _list(settings, options)
    report = ScanReport(options.repo, listed=len(listing.items), listing_truncated=listing.truncated)
    scan = _Scan(settings, options, runner, report)
    pending, report.resumed = _pending(listing, scan.state.finished(options.repo))
    limit = asyncio.Semaphore(max(options.concurrency, 1))
    await asyncio.gather(*(scan.one(item, limit) for item in pending))
    report.verdicts = dict(scan.verdicts)
    return report
