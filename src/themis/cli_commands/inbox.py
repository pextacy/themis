"""`themis triage-file`: analyze a private report without posting anywhere."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from themis.config import ConfigError, load_settings
from themis.intake import inbox
from themis.intake.github import GitHubError
from themis.logs import configure_logging
from themis.models import SubmissionKind
from themis.otari.errors import OtariConfigError, OtariError
from themis.store import redact

EXIT_CONFIG = 2
EXIT_GATEWAY = 3


def triage_file(
    report: Annotated[Path, typer.Argument(help="The report: .md, .txt or .eml.")],
    repo: Annotated[str, typer.Option(help="The project it is about, owner/name.")],
    kind: Annotated[SubmissionKind, typer.Option(help="The kind of report.")] = SubmissionKind.SECURITY_REPORT,
) -> None:
    """Triage a private report from a file. Nothing is posted, and mail addresses are dropped."""
    try:
        if not report.is_file():
            raise ValueError(f"no such file: {report}")
        settings = load_settings()
        configure_logging(settings.log_level)
        item = inbox.build(report, repo, kind)
        outcome = asyncio.run(inbox.triage(settings, item))
    except (ConfigError, OtariConfigError, ValueError) as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_CONFIG) from error
    except (GitHubError, OtariError) as error:
        typer.echo(f"gateway error: {error}", err=True)
        raise typer.Exit(EXIT_GATEWAY) from error
    if item.attachments_ignored:
        typer.echo(f"{item.attachments_ignored} attachment(s) ignored. Read them yourself, with care.", err=True)
    typer.echo(redact(outcome.ledger, settings.secrets()))


def register(app: typer.Typer) -> None:
    app.command("triage-file")(triage_file)
