"""SARIF, CSV, JSONL and evidence bundles. All read stored results; none call a model.

Receipts hold text that came from submissions, so messages go through `inert` and file paths
are checked before they become SARIF locations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from themis import __version__
from themis.models import CheckStatus, SignalSeverity, Strength
from themis.report.github_comment import inert
from themis.store import redact
from themis.verdict import transparency
from themis.verdict.receipts import RECEIPTS_DIR

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/pextacy/themis"
LEVELS = {
    SignalSeverity.CRITICAL.value: "error",
    SignalSeverity.WARN.value: "warning",
    SignalSeverity.INFO.value: "note",
}
RULE_UNSAFE = re.compile(r"[^a-z0-9_-]+")
CSV_COLUMNS = ("id", "repo", "number", "kind", "verdict", "score", "status", "first_time", "created_at", "head_sha")
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MAX_MESSAGE = 200


def load_receipts(directory: Path = RECEIPTS_DIR, repo: str | None = None) -> list[dict[str, Any]]:
    """A file that is not valid JSON is skipped, because one bad file must not stop an export."""
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("themis-*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and (repo is None or (data.get("submission") or {}).get("repo") == repo):
            found.append(data)
    return found


def _rule_id(prefix: str, name: object) -> str:
    return f"themis/{prefix}/{RULE_UNSAFE.sub('_', str(name).lower())[:60]}"


def safe_uri(path: object) -> str | None:
    """A relative repository path, or None. Paths come from the diff, so they are untrusted."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or len(path) > 400:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or any(ord(char) < 32 for char in path):
        return None
    return quote(path, safe="/")


def _line(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _location(uri: str | None, line: object, logical: str) -> dict[str, Any]:
    if uri is None:
        return {"logicalLocations": [{"fullyQualifiedName": logical}]}
    region = {"startLine": _line(line)}
    return {"physicalLocation": {"artifactLocation": {"uri": uri, "uriBaseId": "%SRCROOT%"}, "region": region}}


def _result(
    rule: str, level: str, text: str, uri: str | None, line: object, repo: str, number: object
) -> dict[str, Any]:
    logical = f"{repo}#{number}"
    key = f"{rule}|{uri}|{_line(line)}|{logical}"
    return {
        "ruleId": rule,
        "level": level,
        "message": {"text": text},
        "locations": [_location(uri, line, inert(logical, 120))],
        "partialFingerprints": {"themisFingerprint/v1": hashlib.sha256(key.encode()).hexdigest()},
    }


def _signal_result(signal: dict[str, Any], repo: str, number: object) -> tuple[dict[str, Any], str]:
    title = inert(signal.get("title", "Signal"), MAX_MESSAGE)
    rule = _rule_id("signal", signal.get("code"))
    level = LEVELS.get(str(signal.get("severity")), "note")
    return _result(rule, level, title, safe_uri(signal.get("file")), signal.get("line"), repo, number), title


def _check_result(check: dict[str, Any], repo: str, number: object) -> tuple[dict[str, Any], str]:
    detail = check.get("detail") or {}
    module = inert(check.get("module", "check"), 40)
    subject = detail.get("target") or detail.get("package") or detail.get("reason") or "a claim"
    text = f"The {module} check contradicted this claim: {inert(subject, MAX_MESSAGE)}"
    uri = safe_uri(detail.get("file") or detail.get("path"))
    rule = _rule_id("check", f"{check.get('module')}-hard")
    return _result(
        rule, "error", text, uri, detail.get("line"), repo, number
    ), f"The {module} check found a HARD contradiction"


def _results(receipt: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    reference = receipt.get("submission") or {}
    repo, number = str(reference.get("repo", "")), reference.get("number", 0)
    found = [_signal_result(signal, repo, number) for signal in receipt.get("signals") or []]
    hard = [
        check for check in receipt.get("checks") or []
        if check.get("status") == CheckStatus.CONTRADICTED.value and check.get("strength") == Strength.HARD.value
    ]  # fmt: skip
    return found + [_check_result(check, repo, number) for check in hard]


def sarif(receipts: list[dict[str, Any]], *, include_locationless: bool = False) -> dict[str, Any]:
    """GitHub code scanning refuses a result with no file, and many checks prove something absent.

    Those results are left out by default and counted in the run's properties, because pointing
    them at an unrelated file would invent a location. Other SARIF readers can ask for them.
    """
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    omitted = 0
    for receipt in receipts:
        for result, description in _results(receipt):
            if "physicalLocation" not in result["locations"][0] and not include_locationless:
                omitted += 1
                continue
            results.append(result)
            rule = result["ruleId"]
            rules.setdefault(rule, {
                "id": rule,
                "shortDescription": {"text": description},
                "defaultConfiguration": {"level": result["level"]},
            })  # fmt: skip
    driver = {"name": "THEMIS", "version": __version__, "informationUri": INFORMATION_URI,
              "rules": [rules[name] for name in sorted(rules)]}  # fmt: skip
    run = {"tool": {"driver": driver}, "results": results, "properties": {"omittedWithoutLocation": omitted}}
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}


def neutralize(value: object) -> object:
    """A spreadsheet runs a cell starting with these, and repo names reach cells."""
    if isinstance(value, str) and value.lstrip().startswith(FORMULA_STARTS):
        return f"'{value}"
    return value


def to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([neutralize(redact(row.get(name))) for name in CSV_COLUMNS])
    return buffer.getvalue()


def to_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps({name: redact(row.get(name)) for name in CSV_COLUMNS}, sort_keys=True) + "\n" for row in rows
    )


def _add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    archive.writestr(zipfile.ZipInfo(name, date_time=ZIP_TIME), data, zipfile.ZIP_DEFLATED)


def bundle(
    submission_id: str,
    output: Path | None = None,
    *,
    directory: Path = RECEIPTS_DIR,
    chain: Path = transparency.CHAIN_PATH,
) -> Path:
    """One file a judge can check by hand. Raises FileNotFoundError without a receipt."""
    name = Path(submission_id).name
    receipt = directory / f"themis-{name}.json"
    if not receipt.exists():
        raise FileNotFoundError(f"no receipt for {submission_id}")
    files = {"receipt.json": receipt.read_bytes()}
    ledger = receipt.with_suffix(".md")
    if ledger.exists():
        files["ledger.md"] = ledger.read_bytes()
    entry = transparency.latest_entry(submission_id, chain)
    digests = {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}
    manifest = {
        "submission_id": submission_id,
        "themis_version": __version__,
        "sha256": digests,
        "chain_entry": entry.model_dump() if entry else None,
        "chain_matches": entry.receipt_sha256 == digests["receipt.json"] if entry else None,
    }
    target = output or directory / f"themis-bundle-{name}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        for key, value in files.items():
            _add(archive, key, value)
        _add(archive, "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return target


def verify_bundle(path: Path) -> list[str]:
    """Names of the files whose digest no longer matches the manifest."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        return [
            name for name, digest in manifest["sha256"].items()
            if hashlib.sha256(archive.read(name)).hexdigest() != digest
        ]  # fmt: skip
