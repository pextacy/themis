"""`themis digest`: a summary for maintainers. Prints unless told to post."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer

from themis.config import ConfigError, Settings, load_settings
from themis.intake.github import GitHubClient, GitHubError
from themis.intake.listing import parse_repo
from themis.report import digest as report
from themis.store import redact

EXIT_CONFIG, EXIT_GATEWAY = 2, 3


def _scope(repo: str | None, post: str | None) -> str | None:
    """A posted digest covers only its own repository, so no other project leaks into it."""
    if repo and post and repo != post:
        raise ValueError("--repo and --post must name the same repository")
    chosen = repo or post
    return parse_repo(chosen) if chosen else None


async def _post(settings: Settings, repo: str, data: dict[str, object], body: str) -> int:
    async with GitHubClient(settings) as github:
        return await report.post_issue(github, repo, data, redact(body, settings.secrets()))


def digest(
    days: Annotated[int, typer.Option(min=1, help="Days to cover.")] = 7,
    repo: Annotated[str | None, typer.Option(help="Only this owner/name.")] = None,
    post: Annotated[str | None, typer.Option(help="Open one issue with the digest on this owner/name.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Summarize recent triage: verdicts, what to review first, cost, change on the prior period."""
    try:
        settings = load_settings()
        scope = _scope(repo, post)
        data = report.build(settings, days=days, repo=scope)
        body = report.render(data)
        typer.echo(json.dumps(data, indent=2) if as_json else body)
        if post and scope:
            typer.echo(f"opened issue number {asyncio.run(_post(settings, scope, data, body))} on {scope}")
    except (ConfigError, ValueError) as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_CONFIG) from error
    except GitHubError as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error


def register(app: typer.Typer) -> None:
    app.command("digest")(digest)
