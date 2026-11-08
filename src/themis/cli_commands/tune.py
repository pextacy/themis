"""`themis bench-tune` and `themis bench-compare`."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from themis import bench, bench_tune
from themis.config import ConfigError, load_settings

EXIT_REGRESSED = 1
EXIT_CONFIG = 2


def register(app: typer.Typer) -> None:
    @app.command("bench-tune")
    def tune() -> None:
        """Search scoring settings on a train split and report on a held-out split. Offline."""
        try:
            settings = load_settings()
        except ConfigError as error:
            typer.echo(f"configuration error: {error}", err=True)
            raise typer.Exit(EXIT_CONFIG) from error
        at = datetime.now(UTC).isoformat(timespec="seconds")
        report = asyncio.run(bench_tune.run(settings, at))
        path = bench.write_results(report)
        typer.echo(json.dumps({key: report[key] for key in ("split", "chosen", "holdout_constraint_met",
                                                             "holdout_improves")}, indent=2))  # fmt: skip
        typer.echo(f"wrote {path}")

    @app.command("bench-compare")
    def compare(
        old: Annotated[Path, typer.Argument(help="The earlier bench/results file.")],
        new: Annotated[Path, typer.Argument(help="The later bench/results file.")],
    ) -> None:
        """Compare two benchmark runs. Exits 1 on a regression."""
        try:
            result = bench_tune.compare(bench_tune.load_report(old), bench_tune.load_report(new))
        except (OSError, ValueError) as error:
            typer.echo(f"compare error: {error}", err=True)
            raise typer.Exit(EXIT_CONFIG) from error
        typer.echo(json.dumps(result, indent=2))
        if result["regressions"]:
            raise typer.Exit(EXIT_REGRESSED)
