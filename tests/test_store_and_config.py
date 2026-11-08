from pathlib import Path

import pytest

from tests.conftest import make_settings
from themis.config import ConfigError, RepoPolicy, Settings, load_policy
from themis.models import OtariCall, Receipt, Role, Submission, SubmissionKind, SubmissionRef, Verdict, VerdictValue
from themis.store import Store, redact
from themis.store.db import TABLES


def call(**overrides: object) -> OtariCall:
    base: dict = {"request_id": "r1", "role": Role.CLERK, "endpoint": "chat/completions", "status": 200,
                  "outcome": "ok", "latency_ms": 5}  # fmt: skip
    return OtariCall(**{**base, **overrides})


def test_redact_nested_exact_and_by_field_name() -> None:
    clean = redact(
        {"a": ["Bearer abcdef123456", "tk-secret123"], "b": ("raw-value",), "Authorization": "plain",
         "api_key": "x", "note": "mail me@example.com", "prompt_tokens": 4},
        ("raw-value",),
    )  # fmt: skip
    text = str(clean)
    for leaked in ("abcdef", "secret123", "raw-value", "plain", "me@example.com"):
        assert leaked not in text
    assert clean["prompt_tokens"] == 4


def test_store_creates_every_planned_table() -> None:
    with Store("sqlite:///:memory:") as store:
        assert set(TABLES) <= set(store.tables())


def test_store_keeps_metadata_not_the_raw_body(tmp_path: Path) -> None:
    item = Submission(id="s1", repo="a/b", number=1, kind=SubmissionKind.BUG_REPORT, title="t",
                      body="UNIQUE-RAW-BODY", contributor_key="gh:1")  # fmt: skip
    path = tmp_path / "nested" / "t.db"
    with Store(f"sqlite:///{path}") as store:
        store.save_submission(item)
        store.save_submission(item)
    assert b"UNIQUE-RAW-BODY" not in path.read_bytes()


def test_store_redacts_calls_and_allows_a_missing_request_id() -> None:
    with Store("sqlite:///:memory:", secrets=("hunter2hunter2",)) as store:
        store.save_otari_call(call(outcome="failed with hunter2hunter2"))
        store.save_otari_call(call(request_id=None, status=401, fallback=True, cost_usd=None))
        rows = store.otari_calls()
    assert "hunter2" not in rows[0]["outcome"]
    assert rows[1]["request_id"] is None and rows[1]["cost_usd"] is None and rows[1]["fallback"] == 1


def test_store_links_calls_to_a_submission() -> None:
    item = Submission(id="s1", repo="a/b", number=1, title="t", contributor_key="gh:1")
    with Store("sqlite:///:memory:") as store:
        store.save_submission(item)
        store.save_otari_call(call(), "s1")
        store.save_otari_call(call())
        assert len(store.otari_calls("s1")) == 1


def test_store_rejects_another_database() -> None:
    with pytest.raises(ConfigError):
        Store("postgres://x")


def test_settings_read_documented_names_and_ignore_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTARI_API_KEY", "tk-from-env-123")
    monkeypatch.setenv("THEMIS_MODEL_CLERK", "")
    monkeypatch.setenv("OTARI_BASE_URL", "https://eu.api.otari.ai/api/v1/")
    monkeypatch.setenv("THEMIS_MCP_SERVERS", '[{"name": "x", "url": "https://x"}]')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_key() == "tk-from-env-123" and settings.model_clerk is None
    assert settings.base_url == "https://eu.api.otari.ai/api/v1" and settings.is_hosted
    assert settings.mcp_servers[0].name == "x"


def test_key_never_appears_in_settings_repr() -> None:
    assert "tk-testkey" not in repr(make_settings())


def test_only_otari_hosts_count_as_hosted() -> None:
    assert not make_settings(otari_base_url="http://127.0.0.1:8000/api/v1").is_hosted
    assert not make_settings(otari_base_url="https://api.otari.ai.evil.example/api/v1").is_hosted


def test_policy_defaults_are_safe_when_the_file_is_missing(tmp_path: Path) -> None:
    policy = load_policy(tmp_path / "none.yml")
    assert policy == RepoPolicy() and policy.policy.auto_close is False and policy.mode == "advisory"


def test_documented_policy_example_loads(tmp_path: Path) -> None:
    text = Path("docs/DOCS.md").read_text()
    example = text.split("```yaml\nversion: 1")[1].split("```")[0]
    path = tmp_path / "themis.yml"
    path.write_text("version: 1" + example)
    assert load_policy(path).scoring.weights.confirmed == 0.7


@pytest.mark.parametrize(
    "text", ["mode: yolo", "unknown_key: 1", "version: 2", "a: [", "budgets: {project_monthly_usd: -1}"]
)
def test_bad_policy_is_an_error_not_defaults(tmp_path: Path, text: str) -> None:
    path = tmp_path / "themis.yml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_policy(path)


def test_receipt_serializes_as_documented() -> None:
    receipt = Receipt(
        submission=SubmissionRef(repo="a/b", number=1, kind=SubmissionKind.BUGFIX_PR),
        verdict=Verdict(value=VerdictValue.CONTRADICTED, score=8),
        themis_version="0.1.0",
    )
    assert receipt.model_dump(by_alias=True, mode="json")["schema"] == "themis.receipt.v1"


def test_regional_key_format_is_redacted() -> None:
    assert "abcd1234" not in redact("key otk_v1_eu_abcd1234efgh5678 here")


def test_search_key_format_is_redacted() -> None:
    assert "abcd1234" not in redact("key tvly-dev-abcd1234efgh here")
