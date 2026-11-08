"""A connection for feature tables that live beside the core schema in the same database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from themis.store.db import sqlite_path


def open_db(url: str, schema: str) -> sqlite3.Connection:
    """Foreign keys stay off: these tables must also work for items the core never stored."""
    path = sqlite_path(url)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(schema)
    return db
