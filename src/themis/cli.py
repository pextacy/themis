"""The `themis` command line."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from themis import __version__, bench, cli_commands
from themis.action import dispatch
from themis.config import ConfigError, load_settings
from themis.doctor.capture import EVIDENCE_DIR, capture, pending_features
from themis.doctor.probes import FEATURES
from themis.doctor.runner import gate_met, render_table, run_features
from themis.evidence.dependencies import PackageReport, inspect
from themis.evidence.registries import ExtendedRegistry, inspect_extended
from themis.evidence.tests_results import parse_junit
from themis.intake.forge import GITHUB, open_client, parse_any_url
from themis.intake.github import GitHubError
from themis.logs import configure_logging
from themis.models import OtariCall, VerdictValue
from themis.otari.errors import OtariConfigError, OtariError
from themis.pipeline import Outcome, run_url
from themis.shield import stage
from themis.store import Store, redact
from themis.verdict.receipts import RECEIPTS_DIR, to_json
from themis.web.app import create_app

EXIT_CONFIG = 2
EXIT_GATEWAY = 3
EXIT_DEFERRED = 4
EXIT_TAINTED = 5

app = typer.Typer(no_args_is_help=True, add_completion=False)
evidence = typer.Typer(no_args_is_help=True)
app.add_typer(evidence, name="evidence", help="Capture redacted receipts from live calls.")
cli_commands.register_all(app)


def _print_version(requested: bool) -> None:
    if requested:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", help="Print the version.", is_eager=True, callback=_print_version)
    ] = False,
) -> None:
    """Evidence, not opinions, for every contribution."""


def _fail(error: ConfigError) -> typer.Exit:
    typer.echo(f"configuration error: {error}", err=True)
    return typer.Exit(EXIT_CONFIG)


@app.command()
def doctor() -> None:
    """Check key, balance, models, tools, MCP, guardrails and budgets against the live gateway.

    Spends a little: about 15 small calls, including a deliberate budget refusal.
    """
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        results = asyncio.run(run_features(settings))
    except ConfigError as error:
        raise _fail(error) from error
    by_name = {result["feature"]: result for result in results}
    typer.echo(redact(render_table(by_name), settings.secrets()))
    if not gate_met(by_name):
        raise typer.Exit(EXIT_GATEWAY)


@app.command()
def deps(
    ecosystem: Annotated[str, typer.Argument(help="`pypi`, `npm`, `crates` or `go`.")],
    name: Annotated[str, typer.Argument(help="Package name.")],
) -> None:
    """Run the dependency verifier on one package. Exits 1 when the risk is `block`."""
    if ecosystem not in ("pypi", "npm", "crates", "go"):
        typer.echo("ecosystem must be pypi, npm, crates or go", err=True)
        raise typer.Exit(EXIT_CONFIG)

    async def inspect_one() -> PackageReport:
        registry = ExtendedRegistry()
        async with registry:
            if ecosystem in ("crates", "go"):
                return await inspect_extended(registry, ecosystem, name)
            return await inspect(registry, ecosystem, name)

    report = asyncio.run(inspect_one())
    state = {True: "found", False: "missing", None: "unknown"}[report.found]
    typer.echo(
        f"{ecosystem} {name}: {state}, risk {report.risk}, signals {report.signals or 'none'} {report.detail or ''}"
    )
    if report.risk == "block":
        raise typer.Exit(1)


@app.command("tests-report")
def tests_report(
    junit: Annotated[Path, typer.Argument(help="JUnit XML file. A missing file is an empty report.")],
    output: Annotated[Path, typer.Option(help="Where to write the JSON report.")] = Path("themis-test-results.json"),
    head_sha: Annotated[str | None, typer.Option(help="The commit under test.")] = None,
) -> None:
    """Summarize test results for the analysis workflow."""
    report = parse_junit(junit.read_bytes() if junit.exists() else b"", head_sha)
    output.write_text(json.dumps(report, indent=2) + "\n")


def _exit_code(outcome: Outcome, gate: bool) -> int:
    value = outcome.verdict.value
    if value is VerdictValue.DEFERRED:
        return EXIT_DEFERRED
    return EXIT_TAINTED if gate and value in (VerdictValue.TAINTED, VerdictValue.TAINTED_UNCHECKED) else 0


async def _dry_run(url: str) -> dict[str, Any]:
    """Intake and the sanitizer only. No model call and nothing posted."""
    settings = load_settings()
    forge, target = parse_any_url(url, settings)
    async with open_client(settings, forge) as github:
        item = await github.fetch(target)
    scanned = stage.scan_fields(item)
    return {
        "submission": item.id,
        "pull_request": item.is_pull_request,
        "sanitizer_tainted": any(field.tainted for field in scanned.values()),
        "findings": sum(len(field.findings) for field in scanned.values()),
        "stages": ["shield", "clerk", "plan", "evidence", "verdict", "arbiter", "report"],
    }


@app.command()
def check(
    url: Annotated[str, typer.Argument(help="GitHub issue or pull request URL.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="No Otari calls; show the planned pipeline.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the receipt as JSON.")] = False,
    no_post: Annotated[bool, typer.Option("--no-post", help="Do not write to GitHub.")] = False,
    gate: Annotated[bool, typer.Option("--gate", help="Exit 5 on a tainted submission.")] = False,
) -> None:
    """Analyze one issue or pull request, end to end."""
    try:
        settings = load_settings()
        forge, target = parse_any_url(url, settings)
        if dry_run:
            typer.echo(json.dumps(asyncio.run(_dry_run(url)), indent=2))
            return
        configure_logging(settings.log_level)
        outcome = asyncio.run(run_url(settings, target, post=not no_post and forge == GITHUB, forge=forge))
    except (ConfigError, ValueError) as error:
        raise _fail(ConfigError(str(error))) from error
    except OtariConfigError as error:
        raise _fail(ConfigError(str(error))) from error
    except (GitHubError, OtariError) as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error
    typer.echo(json.dumps(to_json(outcome.receipt, settings.secrets()), indent=2) if as_json else outcome.ledger)
    code = _exit_code(outcome, gate)
    if code:
        raise typer.Exit(code)


@app.command()
def ledger(submission_id: Annotated[str, typer.Argument(help="Submission ID, like owner-repo-12.")]) -> None:
    """Print a stored ledger."""
    path = RECEIPTS_DIR / f"themis-{Path(submission_id).name}.md"
    if not path.exists():
        typer.echo(f"no ledger for {submission_id}", err=True)
        raise typer.Exit(EXIT_CONFIG)
    typer.echo(path.read_text())


@app.command()
def action(
    event: Annotated[Path, typer.Option(help="Path to the GitHub event JSON.")],
    event_name: Annotated[str, typer.Option(help="GITHUB_EVENT_NAME.")],
    repo: Annotated[str, typer.Option(help="GITHUB_REPOSITORY.")],
    test_results: Annotated[Path | None, typer.Option(help="Downloaded test results.")] = None,
) -> None:
    """Handle one GitHub Actions event."""
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        asyncio.run(dispatch(settings, event_name, json.loads(event.read_text()), repo, test_results))
    except ConfigError as error:
        raise _fail(error) from error
    except (GitHubError, OtariError) as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address. Keep it local: the dashboard has no login.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8080,
) -> None:
    """Run the read-only dashboard."""
    try:
        settings = load_settings()
    except ConfigError as error:
        raise _fail(error) from error
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


@app.command("bench")
def bench_command(
    live: Annotated[bool, typer.Option("--live", help="Use the real Clerk through Otari. Costs money.")] = False,
    subset: Annotated[str | None, typer.Option(help="genuine, slop or adversarial.")] = None,
) -> None:
    """Run the labeled benchmark against the demo library and write bench/results/."""
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        report = asyncio.run(bench.run(settings, live=live, subset=subset))
    except ConfigError as error:
        raise _fail(error) from error
    except (GitHubError, OtariError) as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error
    path = bench.write_results(report)
    summary = {key: value for key, value in report.items() if key != "rows"}
    typer.echo(json.dumps(summary, indent=2))
    typer.echo(f"wrote {path}")


@evidence.command("capture")
def evidence_capture(
    feature: Annotated[
        str, typer.Argument(help="A feature name, `all`, or `pending` for those without a passing receipt.")
    ],
    store: Annotated[bool, typer.Option(help="Also record the calls in the SQLite store.")] = True,
) -> None:
    """Run live probes and write redacted receipts to evidence/."""
    known = [item.name for item in FEATURES]
    if feature not in (*known, "all", "pending"):
        typer.echo(f"unknown feature {feature!r}. Known: {', '.join(known)}, all, pending", err=True)
        raise typer.Exit(EXIT_CONFIG)
    calls: list[OtariCall] = []
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        names = {"all": None, "pending": pending_features(EVIDENCE_DIR)}.get(feature, [feature])
        results = capture(settings, names, calls=calls)
        if store:
            with Store(settings.db_url, secrets=settings.secrets()) as database:
                for call in calls:
                    database.save_otari_call(call)
    except ConfigError as error:
        raise _fail(error) from error
    for result in results:
        reason = result.get("reason", "")
        typer.echo(redact(f"{result['status']:8} {result['feature']:10} {reason}", settings.secrets()))
    if any(result["status"] != "PASS" for result in results):
        raise typer.Exit(EXIT_GATEWAY)
