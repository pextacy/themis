"""Backfill tests. Everything here is a TEST DOUBLE."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer import Typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import scan as scan_command
from themis.intake.backfill import ScanOptions, ScanState, run_scan
from themis.intake.github import GitHubRateLimited, Target
from themis.intake.listing import ListedItem, RepoLister, parse_repo
from themis.models import (
    ClaimSet,
    OtariCall,
    Role,
    ShieldResult,
    ShieldStatus,
    Submission,
    SubmissionKind,
    Verdict,
    VerdictValue,
)
from themis.otari.errors import OtariConfigError
from themis.pipeline import Outcome
from themis.verdict import receipts


def rows(*numbers: int, pulls: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    made = []
    for number in numbers:
        row: dict[str, Any] = {"number": number, "updated_at": "2026-11-01T00:00:00Z"}
        if number in pulls:
            row["pull_request"] = {}
        made.append(row)
    return made


def lister(handler: Any) -> RepoLister:
    item = RepoLister(make_settings())
    item._http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return item


async def test_listing_sends_filters_and_splits_kinds() -> None:
    seen: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=rows(1, 2, 3, pulls=(2,)))

    listing = await lister(handle).list_items("a/b", state="closed", kind="pulls")
    assert [item.number for item in listing.items] == [2] and listing.items[0].is_pull_request
    assert seen[0].params["state"] == "closed" and seen[0].params["page"] == "1"


async def test_listing_says_when_it_hit_the_page_cap() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows(1), headers={"link": '<https://x.test?page=9>; rel="next"'})

    listing = await lister(handle).list_items("a/b")
    assert listing.truncated and len(listing.items) == 3


@pytest.mark.parametrize("bad", ["a", "a/b/c", "../x", "a/..", "a/b?x=1", "a b/c"])
def test_repo_names_are_strict(bad: str) -> None:
    with pytest.raises(ValueError, match="repository"):
        parse_repo(bad)


def outcome(number: int, value: VerdictValue, costs: list[float | None]) -> Outcome:
    item = Submission(id=f"a-b-{number}", repo="a/b", number=number, title="t", contributor_key="gh:1")
    verdict = Verdict(value=value, score=50)
    shield = ShieldResult(status=ShieldStatus.CLEAN)
    calls = [
        OtariCall(
            request_id="r", role=Role.CLERK, endpoint="/chat", status=200, outcome="ok", cost_usd=cost, latency_ms=1
        )
        for cost in costs
    ]
    receipt = receipts.build(item, SubmissionKind.OTHER, verdict, [], [], [], shield)
    return Outcome(item, shield, ClaimSet(kind=SubmissionKind.OTHER), verdict, "", [], "", receipt, calls=calls)


class FakeRunner:
    """Stands in for pipeline.run_url."""

    def __init__(self, script: dict[int, Any]) -> None:
        self.script = script
        self.ran: list[int] = []
        self.posts: list[bool] = []

    async def __call__(self, settings: Any, target: Target, *, post: bool) -> Outcome:
        self.ran.append(target.number)
        self.posts.append(post)
        step = self.script.get(target.number, (VerdictValue.PLAUSIBLE, [0.01]))
        if isinstance(step, Exception):
            raise step
        return outcome(target.number, *step)


def scan_settings(tmp_path: Any) -> Any:
    return make_settings(db_url=f"sqlite:///{tmp_path}/s.db")


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": rows(1, 2, 3)}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=state["rows"])

    original = RepoLister.__init__

    def patched(self: RepoLister, settings: Any, **_: Any) -> None:
        original(self, settings, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(RepoLister, "__init__", patched)
    return state


async def test_scan_is_no_post_and_counts_verdicts(tmp_path: Any, listed: dict[str, Any]) -> None:
    runner = FakeRunner({2: (VerdictValue.CONTRADICTED, [0.02, None])})
    report = await run_scan(scan_settings(tmp_path), ScanOptions("a/b"), runner=runner)
    assert report.verdicts == {"PLAUSIBLE": 2, "CONTRADICTED": 1}
    assert report.unpriced_calls == 1 and report.priced_calls == 3
    assert runner.posts == [False, False, False]
    assert round(report.spent_usd, 2) == 0.04


async def test_one_failure_does_not_stop_the_run_and_is_retried(tmp_path: Any, listed: dict[str, Any]) -> None:
    settings = scan_settings(tmp_path)
    first = FakeRunner({2: KeyError("broke")})
    report = await run_scan(settings, ScanOptions("a/b"), runner=first)
    assert report.failed == 1 and report.analyzed == 2 and report.stopped is None

    second = FakeRunner({})
    again = await run_scan(settings, ScanOptions("a/b"), runner=second)
    assert second.ran == [2] and again.resumed == 2


async def test_deferred_items_stay_due(tmp_path: Any, listed: dict[str, Any]) -> None:
    settings = scan_settings(tmp_path)
    await run_scan(settings, ScanOptions("a/b"), runner=FakeRunner({1: (VerdictValue.DEFERRED, [])}))
    second = FakeRunner({})
    await run_scan(settings, ScanOptions("a/b"), runner=second)
    assert second.ran == [1]


async def test_an_updated_item_is_due_again(tmp_path: Any, listed: dict[str, Any]) -> None:
    settings = scan_settings(tmp_path)
    await run_scan(settings, ScanOptions("a/b"), runner=FakeRunner({}))
    listed["rows"][0]["updated_at"] = "2026-11-05T00:00:00Z"
    second = FakeRunner({})
    await run_scan(settings, ScanOptions("a/b"), runner=second)
    assert second.ran == [1]


async def test_cap_stops_new_items_cleanly(tmp_path: Any, listed: dict[str, Any]) -> None:
    runner = FakeRunner({})
    options = ScanOptions("a/b", max_usd=0.01, concurrency=1)
    report = await run_scan(scan_settings(tmp_path), options, runner=runner)
    assert runner.ran == [1] and report.not_started == 2 and "cap" in (report.stopped or "")


@pytest.mark.parametrize("error", [GitHubRateLimited("limit"), OtariConfigError("bad key")])
async def test_fatal_errors_stop_the_run(tmp_path: Any, listed: dict[str, Any], error: Exception) -> None:
    runner = FakeRunner({1: error})
    report = await run_scan(scan_settings(tmp_path), ScanOptions("a/b", concurrency=1), runner=runner)
    assert runner.ran == [1] and report.stopped and report.not_started == 2


def test_state_redacts_errors(tmp_path: Any) -> None:
    settings = scan_settings(tmp_path)
    state = ScanState(settings)
    state.record("a/b", ListedItem(1, False, "t"), "failed", None, "boom tk-testkey-1234567890 x")
    from themis.intake.backfill import SCHEMA
    from themis.store.connect import open_db

    db = open_db(settings.db_url, SCHEMA)
    assert "tk-testkey" not in db.execute("SELECT error FROM scan_state").fetchone()[0]
    db.close()


def cli() -> Typer:
    app = Typer()
    scan_command.register(app)
    app.command("noop")(lambda: None)
    return app


def test_cli_refuses_a_bad_repo() -> None:
    result = CliRunner().invoke(cli(), ["scan", "not-a-repo"])
    assert result.exit_code == 2


def test_render_states_unknown_cost() -> None:
    from themis.intake.backfill import ScanReport

    text = scan_command.render(ScanReport("a/b", unpriced_calls=3))
    assert "not reported" in text and "3 calls reported no price" in text
