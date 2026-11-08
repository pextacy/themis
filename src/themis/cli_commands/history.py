"""`themis history`."""

from __future__ import annotations

from typing import Annotated

import typer

from themis.config import ConfigError, load_settings
from themis.store import Store
from themis.store.history import RunDiff, appeals, runs, timeline

EXIT_CONFIG = 2


def describe(diff: RunDiff) -> list[str]:
    lines = [f"{diff.at}: {diff.verdict[0]} {diff.score[0]} -> {diff.verdict[1]} {diff.score[1]}"]
    if diff.head_changed:
        lines.append("  new commit")
    lines += [f"  + claim {item}" for item in diff.claims_added]
    lines += [f"  - claim {item}" for item in diff.claims_removed]
    lines += [f"  check {name}: {old or 'none'} -> {new or 'none'}" for name, (old, new) in diff.checks_flipped.items()]
    if not diff.changed:
        lines.append("  no change")
    return lines


def register(app: typer.Typer) -> None:
    @app.command("history")
    def history(submission_id: Annotated[str, typer.Argument(help="Submission ID, like owner-repo-12.")]) -> None:
        """Show every run of a submission and what changed between them."""
        try:
            settings = load_settings()
        except ConfigError as error:
            typer.echo(f"configuration error: {error}", err=True)
            raise typer.Exit(EXIT_CONFIG) from error
        rows = runs(settings.db_url, submission_id)
        if not rows:
            typer.echo(f"no runs recorded for {submission_id}", err=True)
            raise typer.Exit(EXIT_CONFIG)
        typer.echo(f"{rows[0]['created_at']}: first run, {rows[0]['verdict']} {rows[0]['score']}")
        for diff in timeline(settings.db_url, submission_id):
            for line in describe(diff):
                typer.echo(line)
        with Store(settings.db_url, secrets=settings.secrets()) as store:
            overrides = store.rows("overrides", submission_id) if store.submission(submission_id) else []
        for row in overrides:
            typer.echo(f"{row['created_at']}: override {row['from_verdict']} -> {row['to_verdict']}: {row['reason']}")
        for row in appeals(settings.db_url, submission_id):
            typer.echo(f"{row['created_at']}: appeal on {row['head_sha']}: {row['reason']}")
