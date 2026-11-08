"""`themis scan`: backfill triage for one repository."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated

import typer

from themis.config import ConfigError, load_settings
from themis.intake.backfill import ScanOptions, ScanReport, run_scan
from themis.intake.github import GitHubError
from themis.intake.listing import KINDS, STATES, parse_repo
from themis.logs import configure_logging

EXIT_CONFIG, EXIT_GATEWAY = 2, 3


def _since(text: str | None) -> datetime | None:
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def render(report: ScanReport) -> str:
    lines = [f"{report.repo}: {report.listed} listed, {report.analyzed} analyzed, {report.resumed} already finished"]
    if report.listing_truncated:
        lines.append("the listing hit its page cap: older items were not listed")
    lines += [f"  {name:20} {count}" for name, count in sorted(report.verdicts.items())]
    if report.failed:
        lines.append(f"  {'failed':20} {report.failed}")
    if report.not_started:
        lines.append(f"  {'not started':20} {report.not_started}")
    cost = f"${report.spent_usd:.4f}" if report.priced_calls else "not reported"
    lines.append(f"cost {cost} over {report.priced_calls} priced calls")
    if report.unpriced_calls:
        lines.append(f"{report.unpriced_calls} calls reported no price, so real spend may be higher than shown")
    if report.stopped:
        lines.append(f"stopped early: {report.stopped}")
    return "\n".join(lines)


def scan(
    repo: Annotated[str, typer.Argument(help="owner/name.")],
    state: Annotated[str, typer.Option(help="open, closed or all.")] = "open",
    limit: Annotated[int, typer.Option(min=1, help="Most items to look at.")] = 50,
    kind: Annotated[str, typer.Option(help="issues, pulls or all.")] = "all",
    since: Annotated[str | None, typer.Option(help="Only items updated after this ISO date.")] = None,
    post: Annotated[bool, typer.Option("--post/--no-post", help="Comment on the items. Off by default.")] = False,
    max_usd: Annotated[float | None, typer.Option(min=0, help="Stop starting items at this reported spend.")] = None,
    concurrency: Annotated[int, typer.Option(min=1, help="Items analyzed at once.")] = 2,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Analyze a repository's existing items. Finished items are skipped when you run it again."""
    try:
        if state not in STATES or kind not in KINDS:
            raise ValueError("state must be open, closed or all; kind must be issues, pulls or all")
        options = ScanOptions(parse_repo(repo), state, kind, _since(since), limit, post, max_usd, concurrency)
        settings = load_settings()
        configure_logging(settings.log_level)
        report = asyncio.run(run_scan(settings, options))
    except (ConfigError, ValueError) as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_CONFIG) from error
    except GitHubError as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error
    typer.echo(json.dumps(report.to_json(), indent=2) if as_json else render(report))
    if report.stopped and report.failed:
        raise typer.Exit(EXIT_GATEWAY)


def register(app: typer.Typer) -> None:
    app.command("scan")(scan)
