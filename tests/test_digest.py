from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer import Typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import digest as digest_command
from themis.intake.github import GitHubClient
from themis.models import OtariCall, Role, Submission, Verdict, VerdictValue
from themis.report import digest
from themis.store import Store
from themis.store.connect import open_db
from themis.store.db import SCHEMA

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def add(
    config: Any,
    number: int,
    value: VerdictValue,
    age_days: float,
    repo: str = "a/b",
    cost: float | None = None,
    fallback: bool = False,
) -> None:
    sid = f"{repo.replace('/', '-')}-{number}"
    with Store(config.db_url) as store:
        store.save_submission(Submission(id=sid, repo=repo, number=number, title="t", contributor_key="gh:1"))
        store.save_verdict(sid, Verdict(value=value, score=number), "bug_report")
        call = OtariCall(
            request_id="r",
            role=Role.CLERK,
            endpoint="/c",
            status=200,
            outcome="ok",
            cost_usd=cost,
            fallback=fallback,
            latency_ms=1,
        )
        store.save_otari_call(call, sid)
    moment = (NOW - timedelta(days=age_days)).isoformat(timespec="seconds")
    db = open_db(config.db_url, SCHEMA)
    with db:
        db.execute("UPDATE submissions SET created_at = ? WHERE id = ?", (moment, sid))
        db.execute("UPDATE otari_calls SET created_at = ? WHERE submission_id = ?", (moment, sid))
    db.close()


@pytest.fixture
def config(tmp_path: Path) -> Any:
    settings = make_settings(db_url=f"sqlite:///{tmp_path}/d.db")
    add(settings, 90, VerdictValue.EVIDENCED, 1, cost=0.01)
    add(settings, 5, VerdictValue.CONTRADICTED, 2, cost=0.02, fallback=True)
    add(settings, 0, VerdictValue.TAINTED, 3)
    add(settings, 40, VerdictValue.PLAUSIBLE, 10)
    add(settings, 70, VerdictValue.EVIDENCED, 1, repo="c/d", cost=5.0)
    return settings


def test_window_counts_and_week_over_week(config: Any) -> None:
    data = digest.build(config, repo="a/b", now=NOW)
    assert data["verdicts"] == {"EVIDENCED": 1, "CONTRADICTED": 1, "TAINTED": 1}
    assert data["total"] == 3 and data["previous_total"] == 1
    assert data["flagged"] == 2 and data["previous_flagged"] == 0


def test_cost_is_scoped_and_unpriced_is_stated(config: Any) -> None:
    costs = digest.build(config, repo="a/b", now=NOW)["costs"]
    assert costs["usd"] == 0.03 and costs["unpriced_calls"] == 1 and costs["fallbacks"] == 1
    assert "no price" in digest.render(digest.build(config, repo="a/b", now=NOW))


def test_review_first_is_by_score_and_skips_tainted(config: Any) -> None:
    data = digest.build(config, repo="a/b", now=NOW)
    assert [item["number"] for item in data["to_review"]] == [90, 5]
    assert {item["verdict"] for item in data["new_flagged"]} == {"CONTRADICTED", "TAINTED"}


def test_no_time_saved_claim(config: Any) -> None:
    assert "does not measure" in digest.render(digest.build(config, now=NOW))


def test_empty_store_says_unknown_cost(tmp_path: Path) -> None:
    data = digest.build(make_settings(db_url=f"sqlite:///{tmp_path}/e.db"), now=NOW)
    text = digest.render(data)
    assert data["total"] == 0 and "not reported" in text and "Review first" not in text


def test_stored_text_is_inert(tmp_path: Path) -> None:
    settings = make_settings(db_url=f"sqlite:///{tmp_path}/i.db")
    add(settings, 1, VerdictValue.PLAUSIBLE, 1)
    db = open_db(settings.db_url, SCHEMA)
    with db:
        db.execute("UPDATE submissions SET repo = ?", ("@evil [x](http://y) <script>",))
    db.close()
    text = digest.render(digest.build(settings, now=NOW))
    assert "@evil" not in text and "<script>" not in text and "[x]" not in text


async def test_post_opens_one_issue_on_the_named_repo(config: Any) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"number": 9})

    data = digest.build(config, repo="a/b", now=NOW)
    async with GitHubClient(config, transport=httpx.MockTransport(handle)) as github:
        assert await digest.post_issue(github, "a/b", data, "body") == 9
    assert len(requests) == 1 and requests[0].url.path == "/repos/a/b/issues"
    assert json.loads(requests[0].content)["title"] == "THEMIS digest to 2026-11-15"


def cli() -> Typer:
    app = Typer()
    digest_command.register(app)
    app.command("noop")(lambda: None)
    return app


def test_cli_prints_without_posting(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_command, "load_settings", lambda: config)
    posted: list[Any] = []
    monkeypatch.setattr(digest_command, "_post", lambda *args: posted.append(args))
    result = CliRunner().invoke(cli(), ["digest", "--repo", "a/b"])
    assert result.exit_code == 0 and "THEMIS digest" in result.stdout and not posted


def test_cli_refuses_a_digest_of_another_repo(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_command, "load_settings", lambda: config)
    result = CliRunner().invoke(cli(), ["digest", "--repo", "a/b", "--post", "c/d"])
    assert result.exit_code == 2


def test_post_alone_scopes_the_digest_to_that_repo() -> None:
    assert digest_command._scope(None, "a/b") == "a/b"
