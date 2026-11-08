from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from themis.doctor.runner import render_table
from themis.models import (
    Check,
    CheckStatus,
    Claim,
    ClaimSet,
    ClaimType,
    FindingSeverity,
    OtariCall,
    Role,
    ShieldFinding,
    ShieldResult,
    ShieldStatus,
    Submission,
    SubmissionKind,
    Verdict,
    VerdictValue,
)
from themis.store import Store
from themis.web.app import create_app, readiness_rows

SCRIPT = "<script>alert(1)</script>"


def seed(url: str) -> None:
    with Store(url) as store:
        for number, value, score, kind in ((1, VerdictValue.EVIDENCED, 90, SubmissionKind.BUGFIX_PR),
                                           (2, VerdictValue.CONTRADICTED, 8, SubmissionKind.BUG_REPORT)):  # fmt: skip
            sid = f"a-b-{number}"
            store.save_submission(Submission(id=sid, repo="a/b", number=number, title="t", contributor_key="gh:1"))
            store.save_verdict(sid, Verdict(value=value, score=score), kind.value)
        store.save_claims(
            "a-b-2",
            ClaimSet(kind=SubmissionKind.BUG_REPORT, claims=[Claim(id="c1", type=ClaimType.OTHER, text=SCRIPT)]),
        )
        store.save_checks(
            "a-b-2",
            [Check(claim_id="c1", module="symbols", status=CheckStatus.CONTRADICTED, detail={"target": SCRIPT})],
        )
        finding = ShieldFinding(
            source="guardrail",
            kind="prompt-injection",
            severity=FindingSeverity.TAINT,
            field="body",
            excerpt_redacted=SCRIPT,
        )
        store.save_shield("a-b-2", ShieldResult(status=ShieldStatus.TAINTED, findings=[finding]))
        store.save_otari_call(
            OtariCall(
                request_id="req-1",
                role=Role.CLERK,
                endpoint="chat/completions",
                status=200,
                outcome="ok",
                latency_ms=100,
                cost_usd=0.002,
                fallback=True,
            ),
            "a-b-2",
        )
        store.save_otari_call(
            OtariCall(
                request_id="req-2",
                role=Role.CLERK,
                endpoint="chat/completions",
                status=403,
                outcome="budget_exceeded",
                latency_ms=20,
            ),
            "a-b-2",
        )


@pytest.fixture
def client(tmp_path: Path) -> Any:
    url = f"sqlite:///{tmp_path}/t.db"
    seed(url)
    return TestClient(create_app(make_settings(db_url=url)))


def test_queue_is_sorted_by_evidence_score(client: TestClient) -> None:
    text = client.get("/").text
    assert text.index("a/b#1") < text.index("a/b#2")


def test_filters_narrow_the_queue(client: TestClient) -> None:
    text = client.get("/", params={"verdict": "CONTRADICTED"}).text
    assert "a/b#2" in text and "a/b#1" not in text
    assert "a/b#2" not in client.get("/", params={"kind": "bugfix_pr"}).text


def test_htmx_gets_rows_only(client: TestClient) -> None:
    text = client.get("/", headers={"HX-Request": "true"}).text
    assert "a/b#1" in text and "<html" not in text


def test_detail_shows_receipts_and_escapes_everything(client: TestClient) -> None:
    text = client.get("/submissions/a-b-2").text
    assert "req-1" in text and "$0.002000" in text and "not reported" in text
    assert SCRIPT not in text and "&lt;script&gt;" in text


def test_unknown_submission_is_404(client: TestClient) -> None:
    assert client.get("/submissions/nope").status_code == 404


def test_stats_report_real_numbers(client: TestClient) -> None:
    text = client.get("/stats").text
    assert "50%" in text and "$0.0020" in text and "$0.0010" in text


def test_stats_on_an_empty_store_do_not_invent_numbers(tmp_path: Path) -> None:
    empty = TestClient(create_app(make_settings(db_url=f"sqlite:///{tmp_path}/e.db")))
    assert "not reported" in empty.get("/stats").text


def test_store_stats_values(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/t.db"
    seed(url)
    with Store(url) as store:
        stats = store.stats()
    assert (stats["fallbacks"], stats["budget_refusals"], stats["guardrail_blocks"]) == (1, 1, 1)


def test_doctor_reads_the_table_and_writes_nothing(client: TestClient) -> None:
    assert client.get("/doctor").status_code == 200


def test_readiness_parser() -> None:
    summary, rows = readiness_rows(render_table({"chat-a": {"status": "PASS", "calls": []}}))
    assert summary.startswith("Exit gate") and "**" not in summary and any(row[0] == "chat-a" for row in rows)
    assert readiness_rows("")[0] == "No capture yet."


def test_dashboard_has_no_write_routes(client: TestClient) -> None:
    assert client.post("/").status_code == 405
    pages = [route for route in client.app.routes if getattr(route, "path", "") != "/webhook/github"]
    methods = {method for route in pages for method in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}


def test_serve_binds_locally_by_default() -> None:
    import inspect

    from themis import cli

    assert inspect.signature(cli.serve).parameters["host"].default == "127.0.0.1"
