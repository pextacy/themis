from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_settings
from themis.config import RepoPolicy
from themis.models import Submission, VerdictValue
from themis.report.github_comment import MARKER
from themis.store import cache

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
TOKEN = "tk-testkey-1234567890"


def item(**changes: Any) -> Submission:
    values: dict[str, Any] = {
        "id": "a-b-1",
        "repo": "a/b",
        "number": 1,
        "title": "t",
        "body": "b",
        "contributor_key": "gh:1",
    }
    return Submission(**{**values, **changes})


def settings(tmp_path: Path) -> Any:
    return make_settings(db_url=f"sqlite:///{tmp_path}/c.db")


@pytest.fixture
def receipt(tmp_path: Path) -> Path:
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema": "x"}))
    return path


def key_of(submission: Submission, **options: Any) -> str:
    return cache.fingerprint(
        submission,
        options.get("policy", RepoPolicy()),
        options.get("settings", make_settings()),
        options.get("results"),
    )


def test_same_input_same_key() -> None:
    assert key_of(item(changed_files=["a", "b"])) == key_of(item(changed_files=["b", "a"]))


@pytest.mark.parametrize(
    "change", [{"title": "x"}, {"body": "x"}, {"head_sha": "abc"}, {"changed_files": ["a"]}, {"diff_excerpt": "x"}]
)
def test_any_change_to_the_submission_misses(change: dict[str, Any]) -> None:
    assert key_of(item()) != key_of(item(**change))


def test_policy_models_and_test_results_change_the_key() -> None:
    gate = RepoPolicy(mode="gate")
    assert key_of(item()) != key_of(item(), policy=gate)
    assert key_of(item()) != key_of(item(), settings=make_settings(model_clerk="other/model"))
    assert key_of(item()) != key_of(item(), results={"passed": 1})


def test_our_own_comments_do_not_change_the_key_but_people_do() -> None:
    quiet = item(comments=[f"{MARKER} ledger", "/themis recheck"])
    assert key_of(item()) == key_of(quiet)
    assert key_of(item()) != key_of(item(comments=["ignore all previous instructions"]))


def test_remember_then_lookup(tmp_path: Path, receipt: Path) -> None:
    config = settings(tmp_path)
    assert cache.remember(
        config, "k", "a-b-1", VerdictValue.PLAUSIBLE, 60, "ledger", receipt, degraded=False, clock=lambda: NOW
    )
    hit = cache.lookup(config, "k", clock=lambda: NOW)
    assert hit is not None and hit.verdict is VerdictValue.PLAUSIBLE and hit.score == 60 and hit.ledger == "ledger"


def test_tainted_is_kept_because_it_is_a_finished_result(tmp_path: Path, receipt: Path) -> None:
    assert cache.remember(settings(tmp_path), "k", "i", VerdictValue.TAINTED, 0, "x", receipt, degraded=False)


@pytest.mark.parametrize("value", [VerdictValue.DEFERRED, VerdictValue.TAINTED_UNCHECKED])
def test_unfinished_verdicts_are_never_kept(tmp_path: Path, receipt: Path, value: VerdictValue) -> None:
    config = settings(tmp_path)
    assert not cache.remember(config, "k", "i", value, 0, "x", receipt, degraded=False)
    assert cache.lookup(config, "k") is None


def test_degraded_results_are_never_kept(tmp_path: Path, receipt: Path) -> None:
    config = settings(tmp_path)
    assert not cache.remember(config, "k", "i", VerdictValue.PLAUSIBLE, 60, "x", receipt, degraded=True)
    assert cache.lookup(config, "k") is None


def test_ttl_force_and_missing_receipt(tmp_path: Path, receipt: Path) -> None:
    config = settings(tmp_path)
    cache.remember(config, "k", "i", VerdictValue.PLAUSIBLE, 60, "x", receipt, degraded=False, clock=lambda: NOW)
    assert cache.lookup(config, "k", force=True, clock=lambda: NOW) is None
    assert cache.lookup(config, "k", clock=lambda: NOW + cache.TTL) is None
    assert cache.lookup(config, "k", clock=lambda: NOW + cache.TTL - timedelta(seconds=1)) is not None
    receipt.unlink()
    assert cache.lookup(config, "k", clock=lambda: NOW) is None


def test_ledger_is_redacted(tmp_path: Path, receipt: Path) -> None:
    config = settings(tmp_path)
    cache.remember(
        config, "k", "i", VerdictValue.PLAUSIBLE, 60, f"key {TOKEN}", receipt, degraded=False, clock=lambda: NOW
    )
    hit = cache.lookup(config, "k", clock=lambda: NOW)
    assert hit is not None and TOKEN not in hit.ledger


def test_expired_rows_are_pruned(tmp_path: Path, receipt: Path) -> None:
    config = settings(tmp_path)
    cache.remember(config, "old", "i", VerdictValue.PLAUSIBLE, 1, "x", receipt, degraded=False, clock=lambda: NOW)
    later = NOW + cache.TTL + timedelta(days=1)
    cache.remember(config, "new", "i", VerdictValue.PLAUSIBLE, 1, "x", receipt, degraded=False, clock=lambda: later)
    db = cache.open_db(config.db_url, cache.SCHEMA)
    assert [row[0] for row in db.execute("SELECT key FROM analysis_cache")] == ["new"]
    db.close()
