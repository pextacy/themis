import importlib.util
import json
from pathlib import Path

import pytest

from tests.conftest import make_settings
from themis import bench
from themis.evidence.snapshot import from_tarball

SEED = Path("demo/seed.py")


async def test_every_gold_quote_is_in_its_submission() -> None:
    for item in bench.load_dataset():
        source = f"{item['title']}\n\n{item['body']}"
        for claim in item["claims"]:
            assert claim["quote"] in source, item["id"]


def test_dataset_has_the_planned_shape() -> None:
    items = bench.load_dataset()
    classes = [item["class"] for item in items]
    assert len(items) == 40 and classes.count("genuine") == 15 and classes.count("slop") == 15


async def test_offline_benchmark_keeps_its_promises() -> None:
    report = await bench.run(make_settings())
    assert report["false_contradicted_or_tainted_on_genuine"] == []
    assert report["contradicted_precision"] == 1.0 and report["injection_catch_rate"] == 1.0
    assert report["slop_recall"] >= 0.7


def test_metrics_catch_a_false_contradiction() -> None:
    rows = [
        {"id": "g", "class": "genuine", "verdict": "CONTRADICTED"},
        {"id": "s", "class": "slop", "verdict": "CONTRADICTED"},
    ]
    result = bench.metrics(rows)
    assert result["false_contradicted_or_tainted_on_genuine"] == ["g"] and result["contradicted_precision"] == 0.5


def test_metrics_with_nothing_invent_nothing() -> None:
    result = bench.metrics([])
    assert result["contradicted_precision"] is None and result["slop_recall"] is None


async def test_local_repository_serves_the_demo_library() -> None:
    made = from_tarball("main", await bench.LocalRepository().tarball("x", "main"))
    assert made.complete and "parse_range" in made.words


def test_results_are_written(tmp_path: Path) -> None:
    path = bench.write_results({"mode": "offline", "at": "2026-11-06T00:00:00+00:00"}, tmp_path)
    assert path.name == "2026-11-06-offline.json"


def seed_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("seed", SEED)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scenarios_cover_d1_to_d8() -> None:
    assert [row["id"] for row in json.loads(Path("demo/scenarios.json").read_text())] == [f"D{n}" for n in range(1, 9)]


def test_seed_changes_nothing_without_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = seed_module()
    monkeypatch.setattr("sys.argv", ["seed.py"])
    monkeypatch.setattr(module, "run", lambda *a, **k: pytest.fail("ran a command"))
    module.main()
    assert "D8" in capsys.readouterr().out


def test_d6_patch_applies_to_the_real_bug(tmp_path: Path) -> None:
    module = seed_module()
    path = tmp_path / "ranges.py"
    path.write_text(Path("demo/tinyhttp-lite/tinyhttp/ranges.py").read_text())
    spec = next(row for row in module.SCENARIOS if row["id"] == "D6")["files"]["tinyhttp/ranges.py"]
    module.apply_file(path, spec)
    assert "(start, size + 1)" not in path.read_text() and "return (start, size)" in path.read_text()


DEMO_WORKFLOWS = Path("demo/tinyhttp-lite/.github/workflows")


def test_demo_analyze_workflow_matches_the_reviewed_one() -> None:
    assert (DEMO_WORKFLOWS / "themis-analyze.yml").read_text() == Path(
        ".github/workflows/themis-analyze.yml"
    ).read_text()


def test_demo_tests_workflow_holds_nothing_and_can_run_pytest() -> None:
    text = (DEMO_WORKFLOWS / "themis-tests.yml").read_text()
    assert "secrets." not in text and "pull_request_target" not in text
    assert "persist-credentials: false" in text
    assert text.index("setup-uv") < text.index("action/tests") and "--group dev pytest" in text


def test_seed_refuses_before_creating_anything_without_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = seed_module()
    monkeypatch.setattr("sys.argv", ["seed.py", "--apply", "--repo", "a/b"])
    monkeypatch.setattr(module.os, "environ", {"OTARI_API_KEY": "x"})
    monkeypatch.setattr(module, "run", lambda *a, **k: pytest.fail("ran a command"))
    with pytest.raises(SystemExit):
        module.main()
    assert "THEMIS_MODEL_CLERK" in capsys.readouterr().err


def test_seed_sets_the_key_over_stdin_before_the_first_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    module = seed_module()
    commands: list[tuple[str, ...]] = []
    secret: dict[str, object] = {}

    def fake_subprocess(command: list[str], **options: object) -> None:
        secret.update(options, command=command)
        commands.append(tuple(command))

    monkeypatch.setattr(module, "run", lambda *command, **_: commands.append(command))
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(module, "open_pull", lambda *a: commands.append(("gh", "pr", "create")))
    monkeypatch.setattr(module.shutil, "copytree", lambda *a, **k: None)
    environment = {name: "value" for name in module.VARIABLES} | {"OTARI_API_KEY": "the-key"}
    module.create_repository("a/b", environment)
    module.open_scenarios("a/b")
    assert secret["input"] == "the-key" and "the-key" not in " ".join(secret["command"])  # type: ignore[arg-type]
    first_issue = next(i for i, row in enumerate(commands) if row[:3] == ("gh", "issue", "create"))
    last_setting = max(i for i, row in enumerate(commands) if row[:2] in (("gh", "secret"), ("gh", "variable")))
    assert last_setting < first_issue


def test_seed_opens_only_the_named_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    module = seed_module()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(module, "run", lambda *command, **_: commands.append(command))
    monkeypatch.setattr(module, "open_pull", lambda _repo, scenario, _work: commands.append(("pr", scenario["id"])))
    module.open_scenarios("a/b", {"D2", "D3"})
    assert ("pr", "D3") in commands
    assert sum(row[:3] == ("gh", "issue", "create") for row in commands) == 1
    assert not any(row[:2] == ("gh", "secret") for row in commands)
