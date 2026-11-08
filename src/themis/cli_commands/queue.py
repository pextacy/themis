"""`themis queue`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis.config import ConfigError, load_policy, load_settings
from themis.store import Store
from themis.verdict.priority import rank

EXIT_CONFIG = 2


def register(app: typer.Typer) -> None:
    @app.command("queue")
    def queue(
        repo: Annotated[str | None, typer.Option(help="Only this owner/name.")] = None,
        limit: Annotated[int, typer.Option(min=1, help="How many rows.")] = 20,
        policy: Annotated[Path, typer.Option(help="Policy file for the fast-track setting.")] = Path(
            ".github/themis.yml"
        ),
    ) -> None:
        """List stored submissions, most worth reading first."""
        try:
            settings = load_settings()
            rules = load_policy(policy)
        except ConfigError as error:
            typer.echo(f"configuration error: {error}", err=True)
            raise typer.Exit(EXIT_CONFIG) from error
        with Store(settings.db_url, secrets=settings.secrets()) as store:
            rows = store.queue()
        chosen = rank([row for row in rows if repo is None or row["repo"] == repo], rules)[:limit]
        if not chosen:
            typer.echo("nothing in the queue")
        for row in chosen:
            reasons = "; ".join(row["priority_reasons"])
            typer.echo(f"{row['priority']:3}  {row['repo']}#{row['number']}  {row['kind']}  {reasons}")
