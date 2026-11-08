"""`themis export` and `themis bundle`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from themis.config import ConfigError, load_settings
from themis.report import exports
from themis.store import Store
from themis.verdict.receipts import RECEIPTS_DIR

EXIT_CONFIG = 2
FORMATS = ("sarif", "csv", "jsonl")


def _rows(repo: str | None) -> list[dict[str, object]]:
    settings = load_settings()
    with Store(settings.db_url, secrets=settings.secrets()) as store:
        rows = store.queue()
    return [row for row in rows if repo is None or row["repo"] == repo]


def render(kind: str, repo: str | None, directory: Path, include_locationless: bool) -> str:
    if kind == "sarif":
        document = exports.sarif(exports.load_receipts(directory, repo), include_locationless=include_locationless)
        return json.dumps(document, indent=2) + "\n"
    return exports.to_csv(_rows(repo)) if kind == "csv" else exports.to_jsonl(_rows(repo))


def register(app: typer.Typer) -> None:
    @app.command("export")
    def export(
        kind: Annotated[str, typer.Argument(help="sarif, csv or jsonl.")],
        repo: Annotated[str | None, typer.Option(help="Only this owner/name.")] = None,
        output: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
        directory: Annotated[Path, typer.Option(help="Where the receipts are.")] = RECEIPTS_DIR,
        include_locationless: Annotated[
            bool, typer.Option(help="SARIF: keep results with no file. GitHub code scanning refuses them.")
        ] = False,
    ) -> None:
        """Export stored results for code scanning or a spreadsheet."""
        if kind not in FORMATS:
            typer.echo(f"format must be one of {', '.join(FORMATS)}", err=True)
            raise typer.Exit(EXIT_CONFIG)
        try:
            text = render(kind, repo, directory, include_locationless)
        except ConfigError as error:
            typer.echo(f"configuration error: {error}", err=True)
            raise typer.Exit(EXIT_CONFIG) from error
        if output is None:
            typer.echo(text, nl=False)
        else:
            output.write_text(text)

    @app.command("bundle")
    def bundle(
        submission_id: Annotated[str, typer.Argument(help="Submission ID, like owner-repo-12.")],
        output: Annotated[Path | None, typer.Option(help="The zip to write.")] = None,
        directory: Annotated[Path, typer.Option(help="Where the receipts are.")] = RECEIPTS_DIR,
    ) -> None:
        """Pack one receipt, its ledger and a SHA-256 manifest into a zip."""
        try:
            typer.echo(exports.bundle(submission_id, output, directory=directory))
        except FileNotFoundError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(EXIT_CONFIG) from error
