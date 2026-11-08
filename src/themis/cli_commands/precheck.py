"""`themis precheck`: a free self-check before opening a pull request.

No key, no network, no model, and nothing from the project runs. It tells a contributor what
a maintainer's THEMIS would likely question, using the deterministic parts only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from themis.agents import fallback_clerk
from themis.config import ConfigError, RepoPolicy, load_policy
from themis.evidence.dependencies import MANIFEST_NAMES
from themis.evidence.snapshot import Snapshot
from themis.evidence.symbols import missing_parts, parse_target
from themis.evidence.tests_results import is_test_path
from themis.evidence.versions import tagged, versions_in
from themis.intake.local import local_changes, local_tags, snapshot_from_disk
from themis.models import Claim, ClaimType, FindingSeverity, ShieldFinding, SignalSeverity, Submission
from themis.pipeline import POLICY_PATH
from themis.shield import confusables, encoded, sanitize
from themis.signals import runner

OK, FIX, UNKNOWN = "ok", "fix", "unknown"
MARKS = {OK: "[x]", FIX: "[ ]", UNKNOWN: "[?]"}
EXIT_PROBLEMS = 1
EXIT_USAGE = 2


@dataclass
class Item:
    status: str
    text: str


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    hidden_text: list[str] = field(default_factory=list)
    change_notes: list[str] = field(default_factory=list)
    blocking: bool = False


def shield_findings(text: str) -> list[ShieldFinding]:
    return [*sanitize.scan(text, "body").findings, *confusables.scan(text, "body"), *encoded.scan(text, "body")]


def _check_reference(claim: Claim, snapshot: Snapshot) -> Item:
    target = claim.normalized_target or ""
    reference = parse_target(target, claim.type)
    if reference is None:
        return Item(UNKNOWN, f"Could not tell what `{target}` points to.")
    if not missing_parts(reference, snapshot):
        return Item(OK, f"`{target}` exists in this tree.")
    return Item(FIX, f"`{target}` is not in this tree. Check the name, or say that it is your own new code.")


def _check_version(claim: Claim, text: str, tags: list[str]) -> Item:
    start, end = claim.span or (0, 0)
    found = versions_in(text[start:end])
    if not found or not tags:
        return Item(UNKNOWN, "No local tags to compare this version with.")
    version = found[0][0]
    if tagged(version, tags):
        return Item(OK, f"Version {version} is tagged here.")
    return Item(FIX, f"Version {version} has no tag here. Name a released version.")


def _check_change(claim: Claim, changed: list[str] | None) -> Item:
    if changed is None:
        return Item(UNKNOWN, "No diff to compare with: pass --base.")
    if claim.type is ClaimType.TEST_ADDED:
        if any(is_test_path(path) for path in changed):
            return Item(OK, "You said tests were added, and a test file changed.")
        return Item(FIX, "You said tests were added, but no test file changed.")
    if any(path.split("/")[-1] in MANIFEST_NAMES or "requirements" in path for path in changed):
        return Item(OK, "You said a dependency was added, and a manifest changed.")
    return Item(FIX, "You said a dependency was added, but no manifest changed.")


def check_claim(claim: Claim, text: str, snapshot: Snapshot, tags: list[str], changed: list[str] | None) -> Item:
    if claim.type in (ClaimType.SYMBOL_EXISTS, ClaimType.FILE_EXISTS):
        return _check_reference(claim, snapshot)
    if claim.type is ClaimType.VERSION_EXISTS:
        return _check_version(claim, text, tags)
    if claim.type in (ClaimType.TEST_ADDED, ClaimType.DEPENDENCY_ADDED):
        return _check_change(claim, changed)
    if claim.type is ClaimType.CVE_REFERENCE:
        return Item(
            UNKNOWN, f"{claim.normalized_target}: the maintainer's THEMIS will look this up. Link the advisory."
        )
    return Item(UNKNOWN, f"{claim.normalized_target}: this needs the live repository, so it was not checked.")


def _policy(root: Path) -> RepoPolicy:
    """A broken policy file is the maintainer's concern, not a reason to fail this check."""
    try:
        return load_policy(root / POLICY_PATH)
    except ConfigError:
        return RepoPolicy()


def build_report(title: str, body: str, root: Path, base: str | None) -> Report:
    text = f"{title}\n\n{body}"
    report = Report()
    findings = shield_findings(text)
    taints = [item for item in findings if item.severity is FindingSeverity.TAINT]
    report.hidden_text = sorted({item.kind for item in taints})
    report.blocking = bool(taints)
    changes = local_changes(root, base) if base else None
    changed, excerpt = changes if changes is not None else (None, "")
    item = Submission(id="precheck", repo="local/precheck", number=0, title=title, body=body, contributor_key="gh:0",
                      is_pull_request=base is not None, changed_files=changed or [], diff_excerpt=excerpt)  # fmt: skip
    claims = fallback_clerk.extract(item, sanitize.scan(text, "submission")).claims
    snapshot, tags = snapshot_from_disk(root), local_tags(root)
    report.items = [check_claim(claim, text, snapshot, tags, changed) for claim in claims]
    report.blocking = report.blocking or any(entry.status == FIX for entry in report.items)
    signals = runner.collect(item, _policy(root))
    report.change_notes = [signal.title for signal in signals]
    report.blocking = report.blocking or any(signal.severity is SignalSeverity.CRITICAL for signal in signals)
    return report


def render(report: Report) -> str:
    lines = ["THEMIS precheck. Nothing left this machine.", ""]
    if report.hidden_text:
        lines += ["Text a reviewer would quarantine. Remove it before you submit:"]
        lines += [f"  [ ] {kind}" for kind in report.hidden_text] + [""]
    lines += [f"  {MARKS[item.status]} {item.text}" for item in report.items] or ["  No checkable claims found."]
    if report.change_notes:
        lines += ["", "Worth a look in your change:", *[f"  - {note}" for note in report.change_notes]]
    return "\n".join([*lines, "", "This is a local estimate. The real check also runs tests and looks things up."])


def as_json(report: Report) -> dict[str, Any]:
    return asdict(report)


def precheck(
    body: Annotated[Path, typer.Option(help="The text you plan to submit, as a file.")],
    title: Annotated[str, typer.Option(help="The title you plan to use.")] = "",
    repo_dir: Annotated[Path, typer.Option(help="Your clone of the project.")] = Path("."),
    base: Annotated[str | None, typer.Option(help="Compare your change with this git ref.")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 when something needs fixing.")] = False,
) -> None:
    """Check a pull request or report locally before submitting it. Free, offline, runs no code."""
    if not body.is_file() or not repo_dir.is_dir():
        typer.echo("--body must be a file and --repo-dir a directory", err=True)
        raise typer.Exit(EXIT_USAGE)
    try:
        report = build_report(title, body.read_text(errors="replace"), repo_dir, base)
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_USAGE) from error
    typer.echo(json.dumps(as_json(report), indent=2) if output_json else render(report))
    if strict and report.blocking:
        raise typer.Exit(EXIT_PROBLEMS)


def register(app: typer.Typer) -> None:
    app.command("precheck")(precheck)
