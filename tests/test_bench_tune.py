import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis import bench_tune
from themis.bench_tune import Prepared
from themis.cli_commands import tune as tune_cli
from themis.config import Scoring
from themis.models import Check, CheckStatus, Claim, ClaimType, ShieldStatus, Strength

AT = "2026-11-06T00:00:00+00:00"


def claims(count: int) -> list[Claim]:
    return [Claim(id=f"c{index}", type=ClaimType.OTHER, text="x") for index in range(count)]


def item(name: str, label: str, confirmed: int, total: int, *, hard: bool = False) -> Prepared:
    checks = [Check(claim_id=f"c{index}", module="symbols", status=CheckStatus.CONFIRMED) for index in range(confirmed)]
    if hard:
        checks.append(Check(claim_id="c0", module="symbols", status=CheckStatus.CONTRADICTED, strength=Strength.HARD))
    return Prepared(name, label, ShieldStatus.CLEAN, claims(total), checks)


def test_the_split_is_stable_and_about_the_right_size() -> None:
    names = [f"item-{index}" for index in range(1000)]
    held = [name for name in names if bench_tune.is_holdout(name)]
    assert held == [name for name in names if bench_tune.is_holdout(name)]
    assert 230 < len(held) < 370


def test_a_candidate_harming_genuine_items_is_never_picked() -> None:
    train = [item("g", "genuine", 1, 1, hard=True), item("s", "slop", 0, 1)]
    assert bench_tune.choose(train) is None


def test_ties_keep_the_defaults() -> None:
    train = [item("g", "genuine", 3, 3), item("s", "slop", 0, 3)]
    chosen = bench_tune.choose(train)
    assert chosen is not None and chosen.plausible_threshold == Scoring().plausible_threshold


def test_the_threshold_is_picked_for_better_verdicts() -> None:
    train = [item("g", "genuine", 2, 3), item("s", "slop", 2, 4)]  # 0.67 vs 0.5
    chosen = bench_tune.choose(train)
    assert chosen is not None and 0.5 < chosen.plausible_threshold <= 0.6


def test_choose_sees_only_train(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [item(f"i{index}", "genuine" if index % 2 else "slop", 1, 1) for index in range(40)]
    seen: list[str] = []
    monkeypatch.setattr(bench_tune, "choose", lambda train: seen.extend(x.id for x in train) or Scoring())
    bench_tune.tuning_report(items, AT)
    assert seen and not any(bench_tune.is_holdout(name) for name in seen)


def test_the_report_keeps_holdout_numbers_and_says_it_changes_nothing() -> None:
    items = [item(f"i{index}", "genuine" if index % 2 else "slop", 1, 1) for index in range(40)]
    report = bench_tune.tuning_report(items, AT)
    assert report["mode"] == "tuning" and "report only" in report["note"].lower()
    assert report["split"]["train"] + report["split"]["holdout"] == 40
    assert "holdout_confusion" in report["chosen"] and report["candidates"] == 5 * 5 * 4 * 3


def test_a_genuine_item_failing_on_holdout_is_reported() -> None:
    names = [f"i{index}" for index in range(60)]
    held = next(name for name in names if bench_tune.is_holdout(name))
    items = [item(name, "genuine", 1, 1, hard=name == held) for name in names]
    assert bench_tune.tuning_report(items, AT)["holdout_constraint_met"] is False


def test_the_confusion_matrix_counts_by_class() -> None:
    rows = [{"class": "slop", "verdict": "CONTRADICTED"}, {"class": "slop", "verdict": "CONTRADICTED"}]
    assert bench_tune.confusion(rows) == {"slop": {"CONTRADICTED": 2}}


async def test_the_real_dataset_runs_offline() -> None:
    report = await bench_tune.run(make_settings(), AT)
    assert report["baseline"]["train"]["items"] == report["split"]["train"] > 0
    assert report["baseline"]["holdout"]["false_contradicted_or_tainted_on_genuine"] == []
    assert report["chosen"] is not None and report["holdout_constraint_met"] is True


def report(**changes: object) -> dict:
    base = {
        "contradicted_precision": 1.0,
        "slop_recall": 0.9,
        "injection_catch_rate": 1.0,
        "false_contradicted_or_tainted_on_genuine": [],
        "rows": [{"id": "a", "verdict": "PLAUSIBLE"}],
    }
    return {**base, **changes}


def test_compare_passes_when_nothing_fell() -> None:
    assert bench_tune.compare(report(), report(slop_recall=0.95))["regressions"] == []


def test_compare_flags_a_lower_metric() -> None:
    assert "slop_recall" in bench_tune.compare(report(), report(slop_recall=0.5))["regressions"][0]


def test_compare_flags_a_metric_that_vanished() -> None:
    assert bench_tune.compare(report(), report(injection_catch_rate=None))["regressions"]


def test_compare_flags_a_genuine_item_turned_false() -> None:
    new = report(false_contradicted_or_tainted_on_genuine=["g1"])
    assert "g1" in bench_tune.compare(report(), new)["regressions"][0]


def test_compare_lists_changed_rows() -> None:
    new = report(rows=[{"id": "a", "verdict": "EVIDENCED"}, {"id": "b", "verdict": "PLAUSIBLE"}])
    result = bench_tune.compare(report(), new)
    assert result["changed"] == {"a": ("PLAUSIBLE", "EVIDENCED")} and result["not_in_both"] == ["b"]


def cli() -> typer.Typer:
    app = typer.Typer()
    tune_cli.register(app)
    return app


def test_cli_compare_exit_codes(tmp_path: Path) -> None:
    runner = CliRunner()
    good, worse, junk = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    good.write_text(json.dumps(report()))
    worse.write_text(json.dumps(report(slop_recall=0.1)))
    junk.write_text("[]")
    assert runner.invoke(cli(), ["bench-compare", str(good), str(good)]).exit_code == 0
    assert runner.invoke(cli(), ["bench-compare", str(good), str(worse)]).exit_code == 1
    assert runner.invoke(cli(), ["bench-compare", str(good), str(junk)]).exit_code == 2


def test_cli_tune_writes_a_dated_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(settings: object, at: str) -> dict:
        return {"mode": "tuning", "at": at, "split": {}, "chosen": None, "holdout_constraint_met": None,
                "holdout_improves": None}  # fmt: skip

    monkeypatch.setattr(tune_cli, "load_settings", make_settings)
    monkeypatch.setattr(tune_cli.bench_tune, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    done = CliRunner().invoke(cli(), ["bench-tune"])
    assert done.exit_code == 0 and list((tmp_path / "bench/results").glob("*-tuning.json"))
