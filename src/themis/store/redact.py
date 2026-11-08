"""Redaction applied before anything is persisted, posted or written as a receipt."""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"
SECRET_FIELDS = frozenset({"authorization", "x-api-key", "otari-key", "api_key", "authorization_token"})
PATTERNS = (
    re.compile(r"\btk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\botk_[A-Za-z0-9_]{8,}"),
    re.compile(r"\btvly-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Return a copy of value with credentials and email addresses removed."""
    if isinstance(value, dict):
        return {
            key: MASK if str(key).lower() in SECRET_FIELDS and isinstance(item, str) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, MASK)
        for pattern in PATTERNS:
            value = pattern.sub(MASK, value)
    return value
