"""Contributor identity for per-user budgets, and the key budget read."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from themis.otari.errors import OtariBudgetExceeded

END_USER_BUDGET_HEADER = "Otari-End-User-Budget"
CONTRIBUTOR_KEY = re.compile(r"gh:\d+")


def contributor_key(github_id: int) -> str:
    return f"gh:{github_id}"


def check_contributor_key(key: str) -> str:
    """Only numeric ids reach Otari. A username or email is a bug, so refuse it."""
    if not CONTRIBUTOR_KEY.fullmatch(key):
        raise ValueError("contributor key must be gh:<numeric id>")
    return key


def end_user_headers(budget_id: str | None) -> dict[str, str]:
    """The header only takes effect on the request that creates the end user."""
    return {END_USER_BUDGET_HEADER: budget_id} if budget_id else {}


def is_contributor_refusal(error: OtariBudgetExceeded) -> bool:
    """True when the contributor's own budget refused, not the project ceiling."""
    return error.scope == "user"


class KeyBudget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    remaining_usd: float | None = None
