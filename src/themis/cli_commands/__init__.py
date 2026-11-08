"""Command groups added to the `themis` CLI. Each module exposes `register(app)`."""

from __future__ import annotations

import typer


def register_all(app: typer.Typer) -> None:
    """Imported here so a module's own imports never run before the CLI exists."""
    from themis.cli_commands import budget, digest, export, history, inbox, precheck, queue, receipts, scan, tune

    for module in (budget, digest, export, history, inbox, precheck, queue, receipts, scan, tune):
        module.register(app)
