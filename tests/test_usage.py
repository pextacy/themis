from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer import Typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import budget as budget_command
from themis.config import Budgets, RepoPolicy
from themis.models import OtariCall, Role, Submission
from themis.store import Store, usage
from themis.store.connect import open_db
from themis.store.db import SCHEMA

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def policy(**changes: Any) -> RepoPolicy:
    return RepoPolicy(budgets=Budgets(**{"local_enforcement": True, **changes}))


def item(number: int = 1, contributor: str = "gh:7", repo: str = "a/b") -> Submission:
    return Submission(id=f"s-{number}", repo=repo, number=number, title="t", contributor_key=contributor)


def stamp(table: str, moment: datetime, config: Any, row: str | None = None) -> None:
    db = open_db(config.db_url, SCHEMA)
    query, params = f"UPDATE {table} SET created_at = ?", [moment.isoformat(timespec="seconds")]  # noqa: S608
    if row:
        query, params = query + " WHERE id = ?", [*params, row]
    with db:
        db.execute(query, params)
    db.close()


def seed(config: Any, submission: Submission, costs: list[float | None]) -> None:
    with Store(config.db_url) as store:
        store.save_submission(submission)
        for cost in costs:
            call = OtariCall(
                request_id="r", role=Role.CLERK, endpoint="/c", status=200, outcome="ok", cost_usd=cost, latency_ms=1
            )
            store.save_otari_call(call, submission.id)


@pytest.fixture
def config(tmp_path: Path) -> Any:
    return make_settings(db_url=f"sqlite:///{tmp_path}/u.db")


def test_off_unless_the_policy_turns_it_on(config: Any) -> None:
    seed(config, item(), [99.0])
    stamp("otari_calls", NOW, config)
    assert usage.preflight(config, RepoPolicy(), item(), NOW) is None


def test_genuine_contributor_under_the_cap_passes(config: Any) -> None:
    seed(config, item(), [0.01])
    stamp("otari_calls", NOW, config)
    assert usage.preflight(config, policy(), item(), NOW) is None


def test_contributor_over_daily_cap_defers(config: Any) -> None:
    seed(config, item(), [0.05])
    stamp("otari_calls", NOW, config)
    deferral = usage.preflight(config, policy(), item(2), NOW)
    assert deferral is not None and deferral.code == "contributor_daily"


def test_yesterdays_spend_does_not_count(config: Any) -> None:
    seed(config, item(), [1.0])
    stamp("otari_calls", NOW - timedelta(days=1), config)
    assert usage.preflight(config, policy(project_monthly_usd=50), item(2), NOW) is None


def test_another_contributor_is_not_affected(config: Any) -> None:
    seed(config, item(), [1.0])
    stamp("otari_calls", NOW, config)
    assert usage.preflight(config, policy(project_monthly_usd=50), item(2, "gh:8"), NOW) is None


def test_project_cap_covers_only_that_repository(config: Any) -> None:
    seed(config, item(contributor="gh:1"), [10.0])
    stamp("otari_calls", NOW, config)
    deferral = usage.preflight(config, policy(), item(2, "gh:2"), NOW)
    assert deferral is not None and deferral.code == "project_monthly"
    assert usage.preflight(config, policy(), item(2, "gh:2", repo="c/d"), NOW) is None


def test_approved_recheck_skips_contributor_caps_but_not_the_project_cap(config: Any) -> None:
    seed(config, item(contributor=usage.PROJECT_KEY), [1.0])
    stamp("otari_calls", NOW, config)
    tight = policy(burst_per_contributor_hour=1, project_monthly_usd=50)
    assert usage.preflight(config, tight, item(2, usage.PROJECT_KEY), NOW) is None
    over = usage.preflight(config, policy(project_monthly_usd=1), item(2, usage.PROJECT_KEY), NOW)
    assert over is not None and over.code == "project_monthly"


def test_spam_burst_defers_and_an_edit_is_not_a_burst(config: Any) -> None:
    for number in (1, 2):
        seed(config, item(number), [])
    stamp("submissions", NOW - timedelta(minutes=5), config)
    tight = policy(burst_per_contributor_hour=2)
    deferral = usage.preflight(config, tight, item(3), NOW)
    assert deferral is not None and deferral.code == "burst"
    assert usage.preflight(config, tight, item(2), NOW) is None


def test_burst_window_slides(config: Any) -> None:
    seed(config, item(1), [])
    stamp("submissions", NOW - timedelta(hours=2), config)
    assert usage.preflight(config, policy(burst_per_contributor_hour=1), item(2), NOW) is None


def test_forecast_is_a_straight_line() -> None:
    middle = datetime(2026, 11, 16, tzinfo=UTC)
    assert usage.forecast_month_end(1.0, middle) == pytest.approx(2.0)


def test_report_is_honest_about_unpriced_calls(config: Any) -> None:
    seed(config, item(), [0.02, None])
    stamp("otari_calls", NOW, config)
    data = usage.report(config, policy(), NOW, repo="a/b")
    assert data["project"]["usd"] == 0.02 and data["project"]["unpriced"] == 1
    assert data["project"]["unpriced_share"] == 0.5
    assert data["contributors_today"][0]["contributor"] == "gh:7"
    assert data["contributors_today"][0]["percent_used"] == 40.0


def test_report_says_unknown_rather_than_zero(config: Any) -> None:
    seed(config, item(), [None])
    stamp("otari_calls", NOW, config)
    data = usage.report(config, policy(), NOW)
    assert data["project"]["usd"] is None and data["project"]["forecast_month_end_usd"] is None
    assert "not reported" in budget_command.render(data)


def test_unattributed_calls_count_only_in_the_global_view(config: Any) -> None:
    with Store(config.db_url) as store:
        store.save_otari_call(
            OtariCall(
                request_id="r", role=Role.DOCTOR, endpoint="/c", status=200, outcome="ok", cost_usd=1.0, latency_ms=1
            )
        )
    stamp("otari_calls", NOW, config)
    assert usage.report(config, policy(), NOW)["project"]["usd"] == 1.0
    assert usage.report(config, policy(), NOW, repo="a/b")["project"]["calls"] == 0


def test_cli_prints_json(config: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_command, "load_settings", lambda: config)
    app = Typer()
    budget_command.register(app)
    app.command("noop")(lambda: None)
    result = CliRunner().invoke(app, ["budget", "--json", "--policy", str(tmp_path / "none.yml")])
    assert result.exit_code == 0 and json.loads(result.stdout)["project"]["calls"] == 0
