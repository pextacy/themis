"""Reading gateway response fields."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from themis.models import Usage


def parse_cost(usage: Mapping[str, Any]) -> float | None:
    """Never estimated: None when the gateway reported no price."""
    try:
        return float(usage["cost_usd"])
    except (KeyError, TypeError, ValueError):
        return None


def to_usage(usage: Mapping[str, Any]) -> Usage:
    return Usage(
        prompt=usage.get("prompt_tokens", usage.get("input_tokens")),
        completion=usage.get("completion_tokens", usage.get("output_tokens")),
    )


def guardrail_verdicts_from(headers: Mapping[str, str]) -> list[dict[str, Any]]:
    try:
        verdicts = json.loads(headers.get("otari-guardrails", "[]"))
    except json.JSONDecodeError:
        return []
    return verdicts if isinstance(verdicts, list) else []


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
