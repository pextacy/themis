"""Selecting open-weight candidates from the live model catalog."""

from __future__ import annotations

import re
from typing import Any

# Name patterns, not model ids: the catalog stays the source of truth.
OPEN_WEIGHT_FAMILIES: dict[str, re.Pattern[str]] = {
    "qwen": re.compile(r"qwen(?!.*-max)"),
    "deepseek": re.compile(r"deepseek"),
    "llama": re.compile(r"llama"),
    "glm": re.compile(r"glm"),
    "kimi": re.compile(r"kimi"),
    "gemma": re.compile(r"gemma"),
    "nemotron": re.compile(r"nemotron"),
    "gpt-oss": re.compile(r"gpt-oss"),
    "lfm": re.compile(r"\blfm"),
}
PREFERRED_CODER_FAMILIES = ("deepseek", "qwen")


def open_weight_family(model_id: str) -> str | None:
    lowered = model_id.lower()
    return next((name for name, pattern in OPEN_WEIGHT_FAMILIES.items() if pattern.search(lowered)), None)


def input_price(model: dict[str, Any]) -> float:
    try:
        return float((model.get("pricing") or {})["input_price_per_million"])
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _is_candidate(model: dict[str, Any]) -> bool:
    if model.get("pricing_source") in (None, "none", "dynamic") or model.get("type") not in (None, "chat"):
        return False
    params = model.get("supported_parameters")
    if params and "response_format" not in params:
        return False
    outputs = (model.get("architecture") or {}).get("output_modalities")
    return not outputs or "text" in outputs


def open_weight_candidates(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cheapest priced JSON-capable chat model per open-weight family, cheapest first."""
    best: dict[str, dict[str, Any]] = {}
    for model in models:
        family = open_weight_family(str(model.get("id", "")))
        if family is None or not _is_candidate(model):
            continue
        if family not in best or input_price(model) < input_price(best[family]):
            best[family] = model
    return sorted(best.values(), key=input_price)
