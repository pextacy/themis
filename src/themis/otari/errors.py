"""Gateway error taxonomy. Refusals map on the stable code, never on `detail` text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class OtariError(Exception):
    """A gateway failure. Always carries the request ID when the gateway issued one."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        page_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.page_status = page_status  # The fetched page's own status, on a failed fetch.

    def __str__(self) -> str:
        where = f"status={self.status} code={self.code} request_id={self.request_id}"
        return f"{self.message} ({where})"


class OtariConfigError(OtariError):
    """401: the key is wrong. Abort the run."""


class OtariProviderNotConfigured(OtariError):
    """424: the gateway has no working credential for the model's provider."""


class OtariFundingError(OtariError):
    """402: `insufficient_funds` or `pricing_required`."""


class OtariGuardrailBlocked(OtariError):
    """403 `guardrail_violation`: the input was flagged."""


class OtariGuardrailUnavailable(OtariError):
    """502 on a guarded call: a block guardrail could not run, so it failed closed."""


class OtariBudgetExceeded(OtariError):
    """403 `budget_exceeded`. Never the same thing as running out of funds."""

    def __init__(self, message: str, *, scope: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.scope = scope


class OtariForbidden(OtariError):
    """Any other 403, such as a `user` mismatch or a model the key may not use."""


class OtariSchemaRejected(OtariError):
    """400 or 422: our request is at fault. A bug on our side, logged with the request ID."""


class OtariRateLimited(OtariError):
    """429, after the retries ran out."""


class OtariUpstreamError(OtariError):
    """5xx, a timeout, or a failure inside an open stream."""


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict) and body["error"].get("message"):
            return str(body["error"]["message"])
        if body.get("detail"):
            return str(body["detail"])
    return str(body)[:300] if body else "no detail"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    try:
        return float(headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _page_status(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("otari-error-status", "")
    return int(raw) if raw.isdigit() else None


def error_code(headers: Mapping[str, str], body: Any) -> str | None:
    code = headers.get("otari-error-code")
    if code:
        return code
    if isinstance(body, dict) and body.get("code"):
        return str(body["code"])
    return None


def from_response(status: int, headers: Mapping[str, str], body: Any, *, guarded: bool = False) -> OtariError:
    """Map a refused response to its exception. Expects lower-case header names."""
    code = error_code(headers, body)
    info: dict[str, Any] = {
        "status": status,
        "code": code,
        "request_id": headers.get("otari-request-id"),
        "retry_after": _retry_after(headers),
        "page_status": _page_status(headers),
    }
    detail = _detail(body)
    if status == 401:
        return OtariConfigError(detail, **info)
    if status == 402:
        return OtariFundingError(detail, **info)
    if status == 424:
        return OtariProviderNotConfigured(detail, **info)
    if code == "guardrail_violation":
        return OtariGuardrailBlocked(detail, **info)
    if code == "budget_exceeded":
        return OtariBudgetExceeded(detail, scope=headers.get("otari-budget-scope"), **info)
    if status == 403:
        return OtariForbidden(detail, **info)
    if status == 429:
        return OtariRateLimited(detail, **info)
    if status == 502 and guarded:
        return OtariGuardrailUnavailable(detail, **info)
    if status >= 500:
        return OtariUpstreamError(detail, **info)
    return OtariSchemaRejected(detail, **info)
