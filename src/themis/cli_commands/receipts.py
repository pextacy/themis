"""`themis receipts verify` and `themis replay`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis.config import ConfigError, load_policy
from themis.verdict import replay as replayer
from themis.verdict import transparency
from themis.verdict.receipts import RECEIPTS_DIR

EXIT_BROKEN = 1
EXIT_CONFIG = 2


def _verify(chain: Path, directory: Path, expect_head: str | None) -> None:
    broken = transparency.verify(chain)
    if broken is not None:
        typer.echo(f"chain broken at entry {broken}", err=True)
        raise typer.Exit(EXIT_BROKEN)
    problems = transparency.verify_receipts(chain, directory)
    for item in problems:
        typer.echo(f"entry {item['index']} {item['submission_id']}: {item['problem']}", err=True)
    head = transparency.head(chain)
    if expect_head is not None and head != expect_head:
        typer.echo(f"head {head} is not the expected {expect_head}", err=True)
        raise typer.Exit(EXIT_BROKEN)
    if problems:
        raise typer.Exit(EXIT_BROKEN)
    typer.echo(f"chain holds: {len(transparency.entries(chain))} entries, head {head or 'none'}")


def _replay(receipt: Path, policy: Path | None) -> None:
    try:
        rules = load_policy(policy) if policy else None
        result = replayer.replay(replayer.load(receipt), rules)
    except (ConfigError, replayer.ReplayError) as error:
        typer.echo(f"replay error: {error}", err=True)
        raise typer.Exit(EXIT_CONFIG) from error
    typer.echo(f"stored:   {result.stored_value.value} {result.stored_score}/100")
    typer.echo(f"replayed: {result.value.value} {result.score}/100")
    for note in result.assumptions:
        typer.echo(f"note: {note}")
    if not result.matches:
        typer.echo("MISMATCH: the receipt does not reproduce its verdict", err=True)
        raise typer.Exit(EXIT_BROKEN)
    typer.echo("match: the verdict follows from the receipt's checks")


def register(app: typer.Typer) -> None:
    receipts = typer.Typer(no_args_is_help=True)
    app.add_typer(receipts, name="receipts", help="Check the receipts on disk.")

    @receipts.command("verify")
    def verify(
        chain: Annotated[Path, typer.Option(help="The chain file.")] = transparency.CHAIN_PATH,
        directory: Annotated[Path, typer.Option(help="Where the receipts are.")] = RECEIPTS_DIR,
        expect_head: Annotated[str | None, typer.Option(help="A head hash kept elsewhere.")] = None,
    ) -> None:
        """Check the hash chain and each submission's newest receipt. Needs no key."""
        _verify(chain, directory, expect_head)

    @app.command("replay")
    def replay(
        receipt: Annotated[Path, typer.Argument(help="A receipt JSON file.")],
        policy: Annotated[Path | None, typer.Option(help="The policy file used for the run.")] = None,
    ) -> None:
        """Recompute a verdict from its receipt and compare. Needs no key."""
        _replay(receipt, policy)
