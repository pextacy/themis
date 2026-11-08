import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests.conftest import make_settings
from themis.cli_commands import export as export_cli
from themis.models import Submission, Verdict, VerdictValue
from themis.report import exports
from themis.store import Store
from themis.verdict import transparency

SCRIPT = "<script>@attacker [x](http://evil)</script>"


def receipt_doc(**extra: object) -> dict:
    return {
        "submission": {"repo": "a/b", "number": 7},
        "verdict": {"value": "CONTRADICTED", "score": 0},
        "signals": [
            {"module": "secrets", "code": "secret_leak", "severity": "CRITICAL", "title": "A key was added",
             "file": "src/app.py", "line": 12},
            {"module": "quality", "code": "whitespace_only", "severity": "INFO", "title": "Whitespace only"},
        ],
        "checks": [
            {"module": "symbols", "status": "CONTRADICTED", "strength": "HARD", "detail": {"target": SCRIPT}},
            {"module": "versions", "status": "CONTRADICTED", "strength": "SOFT", "detail": {"file": "x.py"}},
            {"module": "symbols", "status": "CONFIRMED", "detail": {"file": "y.py"}},
        ],
        **extra,
    }  # fmt: skip


def run_of(document: dict) -> dict:
    return document["runs"][0]


def test_sarif_is_shaped_for_code_scanning() -> None:
    document = exports.sarif([receipt_doc()])
    assert document["version"] == "2.1.0" and document["$schema"].endswith("sarif-2.1.0.json")
    run = run_of(document)
    assert [item["ruleId"] for item in run["results"]] == ["themis/signal/secret_leak"]
    result = run["results"][0]
    assert result["level"] == "error"
    physical = result["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "src/app.py" and physical["region"]["startLine"] == 12
    assert result["partialFingerprints"]["themisFingerprint/v1"]
    assert run["tool"]["driver"]["rules"][0]["id"] == "themis/signal/secret_leak"


def test_results_without_a_file_are_counted_not_placed_on_a_made_up_file() -> None:
    run = run_of(exports.sarif([receipt_doc()]))
    assert run["properties"]["omittedWithoutLocation"] == 2


def test_results_without_a_file_can_be_kept_for_other_readers() -> None:
    run = run_of(exports.sarif([receipt_doc()], include_locationless=True))
    rules = [item["ruleId"] for item in run["results"]]
    assert "themis/check/symbols-hard" in rules and "themis/signal/whitespace_only" in rules
    logical = [item for item in run["results"] if "logicalLocations" in item["locations"][0]]
    assert logical[0]["locations"][0]["logicalLocations"][0]["fullyQualifiedName"].startswith("a/b")


def test_sarif_only_takes_hard_contradictions() -> None:
    run = run_of(exports.sarif([receipt_doc()], include_locationless=True))
    assert not any("versions" in item["ruleId"] for item in run["results"])


def test_injected_text_is_inert_in_sarif() -> None:
    run = run_of(exports.sarif([receipt_doc()], include_locationless=True))
    text = json.dumps(run)
    assert "<script>" not in text and "@attacker" not in text and "](http" not in text


def test_a_hostile_code_cannot_shape_a_rule_id() -> None:
    doc = receipt_doc(
        signals=[{"code": "../../Evil Rule\n", "severity": "WARN", "title": "t", "file": "a.py"}], checks=[]
    )
    assert run_of(exports.sarif([doc]))["results"][0]["ruleId"] == "themis/signal/_evil_rule_"


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../b", "http://evil/x", "c:\\x", "a\nb", "", None, 3])
def test_paths_from_a_diff_cannot_escape(path: object) -> None:
    assert exports.safe_uri(path) is None


def test_odd_but_honest_paths_are_encoded() -> None:
    assert exports.safe_uri("docs/my file.md") == "docs/my%20file.md"


def test_a_bad_line_falls_back_to_one() -> None:
    doc = receipt_doc(signals=[{"code": "x", "severity": "WARN", "title": "t", "file": "a.py", "line": -4}], checks=[])
    assert run_of(exports.sarif([doc]))["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_fingerprints_are_stable() -> None:
    assert exports.sarif([receipt_doc()]) == exports.sarif([receipt_doc()])


def test_load_receipts_skips_junk_and_filters_by_repo(tmp_path: Path) -> None:
    (tmp_path / "themis-a.json").write_text(json.dumps(receipt_doc()))
    (tmp_path / "themis-b.json").write_text("{not json")
    (tmp_path / "themis-c.json").write_text(json.dumps({"submission": {"repo": "c/d"}}))
    assert len(exports.load_receipts(tmp_path)) == 2
    assert len(exports.load_receipts(tmp_path, "a/b")) == 1


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "  =1", "\r=1"])
def test_formula_cells_are_neutralized(value: str) -> None:
    assert str(exports.neutralize(value)).startswith("'")


def test_plain_cells_and_numbers_stay() -> None:
    assert exports.neutralize("a/b") == "a/b" and exports.neutralize(-5) == -5


def test_csv_quotes_and_neutralizes() -> None:
    text = exports.to_csv([{"id": "=cmd|'/c calc'!A1", "repo": "a,b", "number": 1, "score": 5}])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1][0].startswith("'=") and rows[1][1] == "a,b"
    assert "contributor_key" not in rows[0]


def test_jsonl_redacts_and_holds_no_contributor() -> None:
    line = exports.to_jsonl([{"id": "a@example.com", "contributor_key": "gh:1"}])
    assert "example.com" not in line and "gh:1" not in line and json.loads(line)


def seeded(tmp_path: Path) -> tuple[Path, Path]:
    file = tmp_path / "themis-a-b-7.json"
    file.write_text(json.dumps(receipt_doc()))
    file.with_suffix(".md").write_text("ledger")
    chain = tmp_path / "chain.jsonl"
    transparency.append(file, "a-b-7", "CONTRADICTED", chain=chain)
    return file, chain


def test_bundle_holds_receipt_ledger_and_digests(tmp_path: Path) -> None:
    _, chain = seeded(tmp_path)
    path = exports.bundle("a-b-7", directory=tmp_path, chain=chain)
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {"receipt.json", "ledger.md", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["chain_matches"] is True and manifest["chain_entry"]["submission_id"] == "a-b-7"
    assert exports.verify_bundle(path) == []


def test_bundle_says_when_the_receipt_left_the_chain(tmp_path: Path) -> None:
    file, chain = seeded(tmp_path)
    file.write_text("{}")
    path = exports.bundle("a-b-7", directory=tmp_path, chain=chain)
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("manifest.json"))["chain_matches"] is False


def test_bundle_finds_a_changed_file(tmp_path: Path) -> None:
    _, chain = seeded(tmp_path)
    path = exports.bundle("a-b-7", directory=tmp_path, chain=chain)
    with zipfile.ZipFile(path) as old, zipfile.ZipFile(tmp_path / "x.zip", "w") as new:
        for name in old.namelist():
            new.writestr(name, b"forged" if name == "ledger.md" else old.read(name))
    assert exports.verify_bundle(tmp_path / "x.zip") == ["ledger.md"]


def test_bundle_needs_a_receipt_and_stays_in_its_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        exports.bundle("../../etc/passwd", directory=tmp_path, chain=tmp_path / "c")


def cli() -> typer.Typer:
    app = typer.Typer()
    export_cli.register(app)
    return app


def test_cli_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setattr(export_cli, "load_settings", lambda: make_settings(db_url=url))
    with Store(url) as store:
        store.save_submission(Submission(id="a-b-1", repo="a/b", number=1, title="t", contributor_key="gh:1"))
        store.save_verdict("a-b-1", Verdict(value=VerdictValue.PLAUSIBLE, score=50), "other")
    runner = CliRunner()
    assert "a-b-1" in runner.invoke(cli(), ["export", "csv", "--repo", "a/b"]).output
    assert "a-b-1" not in runner.invoke(cli(), ["export", "jsonl", "--repo", "c/d"]).output
    output = tmp_path / "out.sarif"
    done = runner.invoke(cli(), ["export", "sarif", "--directory", str(tmp_path), "--output", str(output)])
    assert done.exit_code == 0 and json.loads(output.read_text())["version"] == "2.1.0"
    assert runner.invoke(cli(), ["export", "xml"]).exit_code == 2


def test_cli_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    seeded(tmp_path)
    output = tmp_path / "b.zip"
    args = ["bundle", "a-b-7", "--directory", str(tmp_path), "--output", str(output)]
    assert runner.invoke(cli(), args).exit_code == 0 and output.exists()
    assert runner.invoke(cli(), ["bundle", "nope", "--directory", str(tmp_path)]).exit_code == 2
