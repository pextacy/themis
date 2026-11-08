"""`themis budget`: what this project has spent, as THEMIS recorded it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from themis.config import ConfigError, load_policy, load_settings
from themis.store import redact, usage

EXIT_CONFIG = 2


def _usd(value: float | None) -> str:
    return "not reported" if value is None else f"${value:.4f}"


def render(data: dict[str, Any]) -> str:
    project = data["project"]
    percent = project["percent_used"]
    lines = [
        f"Scope: {data['repo'] or 'all repositories, plus calls tied to no submission'}",
        f"Month so far: {_usd(project['usd'])} of ${project['monthly_cap_usd']:.2f}"
        + (f" ({percent}%)" if percent is not None else ""),
        f"Month-end at this rate: {_usd(project['forecast_month_end_usd'])} (a projection)",
        f"Calls: {project['calls']}, without a reported price: {project['unpriced']}",
        f"Local enforcement: {'on' if data['local_enforcement'] else 'off'}",
    ]
    if project["unpriced"]:
        lines.append(f"Real spend may be higher: {project['unpriced_share']:.0%} of calls reported no price.")
    if data["contributors_today"]:
        lines += ["", "Today, by contributor:"]
    for row in data["contributors_today"]:
        share = f" ({row['percent_used']}%)" if row["percent_used"] is not None else ""
        lines.append(f"  {row['contributor']:16} ${row['usd']:.4f} of ${row['daily_cap_usd']:.2f}{share}")
    return "\n".join(lines)


def budget(
    repo: Annotated[str | None, typer.Option(help="Only this owner/name.")] = None,
    policy_file: Annotated[Path, typer.Option("--policy", help="Policy file with the caps.")] = Path(
        ".github/themis.yml"
    ),
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Show spend against the policy's caps, with a month-end forecast."""
    try:
        settings = load_settings()
        policy = load_policy(policy_file)
    except ConfigError as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_CONFIG) from error
    data = redact(usage.report(settings, policy, datetime.now(UTC), repo=repo), settings.secrets())
    typer.echo(json.dumps(data, indent=2) if as_json else render(data))


def register(app: typer.Typer) -> None:
    app.command("budget")(budget)
