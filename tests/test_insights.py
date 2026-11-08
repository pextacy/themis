import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import queue as queue_cli
from themis.config import RepoPolicy
from themis.models import OtariCall, Role, Submission, Usage, Verdict, VerdictValue
from themis.store import Store
from themis.verdict.priority import FIRST_TIME_BONUS, priority, rank
from themis.web import insights
from themis.web.app import create_app

NOW = datetime(2026, 11, 6, tzinfo=UTC)
FAST = RepoPolicy()
SLOW = RepoPolicy.model_validate({"policy": {"first_time_contributor_fast_track": False}})
TOKEN = "dash-token-1234567890"


def row(**fields: Any) -> dict[str, Any]:
    base = {"verdict": "PLAUSIBLE", "score": 50, "kind": "bug_report", "first_time": 0, "created_at": NOW.isoformat()}
    return {**base, **fields}


def test_evidence_decides_the_base_order() -> None:
    good = priority(row(verdict="EVIDENCED", score=90), FAST, NOW)
    bad = priority(row(verdict="CONTRADICTED", score=0), FAST, NOW)
    assert good.value > bad.value


def test_a_first_timer_gets_the_boost_and_a_reason() -> None:
    boosted = priority(row(first_time=1), FAST, NOW)
    assert boosted.value == priority(row(), FAST, NOW).value + FIRST_TIME_BONUS
    assert "first-time contributor" in boosted.reasons


def test_the_boost_follows_the_policy() -> None:
    assert priority(row(first_time=1), SLOW, NOW) == priority(row(), SLOW, NOW)


@given(
    st.sampled_from([value.value for value in VerdictValue]),
    st.integers(0, 100),
    st.sampled_from(["security_report", "bug_report"]),
    st.integers(0, 40),
)
def test_being_new_can_only_raise(verdict: str, score: int, kind: str, days: int) -> None:
    created = (NOW - timedelta(days=days)).isoformat()
    base = row(verdict=verdict, score=score, kind=kind, created_at=created)
    assert priority({**base, "first_time": 1}, FAST, NOW).value >= priority(base, FAST, NOW).value


def test_identity_is_never_read() -> None:
    assert priority(row(contributor_key="gh:1"), FAST, NOW) == priority(row(contributor_key="gh:999"), FAST, NOW)


def test_security_rises_unless_it_was_disproved() -> None:
    assert priority(row(kind="security_report"), FAST, NOW).value > priority(row(), FAST, NOW).value
    contradicted = row(kind="security_report", verdict="CONTRADICTED")
    assert priority(contradicted, FAST, NOW).value == priority(row(verdict="CONTRADICTED"), FAST, NOW).value


def test_waiting_helps_up_to_a_cap() -> None:
    def waited(days: int) -> int:
        return priority(row(created_at=(NOW - timedelta(days=days)).isoformat()), FAST, NOW).value

    assert waited(3) == waited(0) + 3 and waited(50) == waited(10)


def test_values_stay_in_range_and_bad_dates_do_not_crash() -> None:
    top = row(verdict="EVIDENCED", score=100, kind="security_report", first_time=1, created_at="not a date")
    assert priority(top, FAST, NOW).value <= 100


def test_rank_sorts_and_explains() -> None:
    ranked = rank([row(verdict="CONTRADICTED"), row(verdict="EVIDENCED")], FAST, NOW)
    assert ranked[0]["verdict"] == "EVIDENCED" and ranked[0]["priority_reasons"]


def seed(url: str) -> None:
    with Store(url) as store:
        for number, value, score in ((1, VerdictValue.EVIDENCED, 90), (2, VerdictValue.CONTRADICTED, 8)):
            sid = f"a-b-{number}"
            item = Submission(id=sid, repo="a/b", number=number, title="t", contributor_key="gh:77", first_time=True)
            store.save_submission(item)
            store.save_verdict(sid, Verdict(value=value, score=score), "bug_report")
        store.save_otari_call(OtariCall(
            request_id="r1", role=Role.CLERK, endpoint="/chat", status=200, outcome="ok", latency_ms=5,
            effective_model="acme/qwen", usage=Usage(prompt=1, completion=1), cost_usd=0.5), "a-b-1")  # fmt: skip
        store.save_otari_call(OtariCall(
            request_id="r2", role=Role.ARBITER, endpoint="/chat", status=200, outcome="ok", latency_ms=5,
            requested_model="acme/glm"), "a-b-2")  # fmt: skip


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return with_page(tmp_path)


def with_page(tmp_path: Path, **settings: Any) -> TestClient:
    """Mirrors what `create_app` passes once wired, using its own Jinja environment."""
    from fastapi.templating import Jinja2Templates
    from jinja2 import Environment, PackageLoader, select_autoescape

    config = make_settings(db_url=f"sqlite:///{tmp_path}/t.db", **settings)
    seed(config.db_url)
    app = create_app(config)
    templates = Jinja2Templates(env=Environment(
        loader=PackageLoader("themis.web", "templates"), autoescape=select_autoescape(("html",), default=True)))  # fmt: skip
    insights.register(
        app, config, lambda: Store(config.db_url),
        lambda request, name, **context: templates.TemplateResponse(request, name, context),
    )  # fmt: skip
    return TestClient(app)


def test_api_queue_ranks_and_hides_contributors(client: TestClient) -> None:
    items = client.get("/api/v1/queue").json()["items"]
    assert items[0]["id"] == "a-b-1" and items[0]["priority"] > items[1]["priority"]
    assert "gh:77" not in client.get("/api/v1/queue").text


def test_api_queue_filters(client: TestClient) -> None:
    assert len(client.get("/api/v1/queue", params={"verdict": "CONTRADICTED"}).json()["items"]) == 1
    assert client.get("/api/v1/queue", params={"repo": "x/y"}).json()["items"] == []


def test_api_submission(client: TestClient) -> None:
    data = client.get("/api/v1/submissions/a-b-1").json()
    assert data["submission"]["verdict"] == "EVIDENCED" and "contributor_key" not in data["submission"]
    assert data["otari_calls"][0]["request_id"] == "r1"
    assert client.get("/api/v1/submissions/nope").status_code == 404


def test_api_stats_and_metrics(client: TestClient) -> None:
    assert client.get("/api/v1/stats").json()["total"] == 2
    reply = client.get("/metrics")
    assert reply.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert (
        'themis_submissions_total{verdict="EVIDENCED"} 1' in reply.text and "themis_gateway_calls_total 2" in reply.text
    )


def test_unpriced_spend_is_not_reported_as_zero() -> None:
    stats = {"verdicts": {}, "calls": 1, "fallbacks": 0, "budget_refusals": 0, "guardrail_blocks": 0, "cost_usd": None}
    assert "themis_cost_usd_total" not in insights.metrics_text(stats)


def test_metric_labels_are_escaped() -> None:
    stats = {
        "verdicts": {'x"\n': 1},
        "calls": 0,
        "fallbacks": 0,
        "budget_refusals": 0,
        "guardrail_blocks": 0,
        "cost_usd": 0,
    }
    assert 'verdict="x\\"\\n"' in insights.metrics_text(stats)


def test_badge(client: TestClient) -> None:
    reply = client.get("/badge/a/b.svg")
    assert reply.headers["content-type"].startswith("image/svg+xml") and "2 triaged" in reply.text
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert "no data" in client.get("/badge/x/y.svg").text


def test_badge_escapes_its_input() -> None:
    assert "<script>" not in insights.badge_svg("<script>alert(1)</script>/x", [])


def test_analytics_page(client: TestClient) -> None:
    reply = client.get("/analytics")
    assert reply.status_code == 200
    assert "acme/qwen" in reply.text and "$0.5000" in reply.text and "not reported" in reply.text
    assert "Verdict mix" in reply.text and 'aria-hidden="true"' in reply.text


def test_analytics_groups_cost_honestly() -> None:
    rows = [{"repo": "a/b", "verdict": "EVIDENCED", "created_at": "2026-11-06T00:00:00"}]
    calls = [{"role": "clerk", "effective_model": None, "requested_model": "m", "cost_usd": None},
             {"role": "clerk", "effective_model": None, "requested_model": "m", "cost_usd": 0.0}]  # fmt: skip
    data = insights.analytics(rows, calls)
    assert data["by_model"] == [{"name": "m", "calls": 2, "cost": 0.0}]
    assert data["mix"][0]["parts"][0]["share"] == 100 and data["daily"][0]["share"] == 100


def test_analytics_empty() -> None:
    assert insights.analytics([], [])["daily"] == []


def test_no_token_keeps_local_behavior(client: TestClient) -> None:
    assert client.get("/stats").status_code == 200 and client.get("/api/v1/stats").status_code == 200


def test_a_token_guards_every_route(tmp_path: Path) -> None:
    client = with_page(tmp_path, dashboard_token=TOKEN)
    for path in ("/", "/stats", "/doctor", "/analytics", "/metrics", "/api/v1/queue", "/submissions/a-b-1"):
        reply = client.get(path)
        assert reply.status_code == 401, path
    assert "WWW-Authenticate" in client.get("/").headers


def test_a_wrong_token_is_refused(tmp_path: Path) -> None:
    client = with_page(tmp_path, dashboard_token=TOKEN)
    assert client.get("/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/stats", headers={"Authorization": f"Token {TOKEN}"}).status_code == 401
    assert client.get("/stats", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_the_right_token_opens_it(tmp_path: Path) -> None:
    client = with_page(tmp_path, dashboard_token=TOKEN)
    assert client.get("/stats", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_a_browser_can_log_in_with_basic(tmp_path: Path) -> None:
    client = with_page(tmp_path, dashboard_token=TOKEN)
    login = base64.b64encode(f"anyone:{TOKEN}".encode()).decode()
    assert client.get("/stats", headers={"Authorization": f"Basic {login}"}).status_code == 200


def test_badges_stay_public_under_a_token(tmp_path: Path) -> None:
    assert with_page(tmp_path, dashboard_token=TOKEN).get("/badge/a/b.svg").status_code == 200


def test_a_public_prefix_cannot_be_used_to_reach_a_private_page(tmp_path: Path) -> None:
    client = with_page(tmp_path, dashboard_token=TOKEN)
    reply = client.get("/badge/%2e%2e/stats")
    assert reply.status_code == 404 and "Gateway calls" not in reply.text


def test_cli_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_settings(db_url=f"sqlite:///{tmp_path}/t.db")
    seed(config.db_url)
    monkeypatch.setattr(queue_cli, "load_settings", lambda: config)
    app = typer.Typer()
    queue_cli.register(app)
    args = ["--policy", str(tmp_path / "none.yml")]
    done = CliRunner().invoke(app, [*args, "--limit", "1"])
    assert done.exit_code == 0 and "a/b#1" in done.output and "a/b#2" not in done.output
    assert "nothing" in CliRunner().invoke(app, [*args, "--repo", "x/y"]).output
