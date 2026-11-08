import pytest

from themis.otari import errors


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, None, errors.OtariConfigError),
        (402, "insufficient_funds", errors.OtariFundingError),
        (402, "pricing_required", errors.OtariFundingError),
        (403, "guardrail_violation", errors.OtariGuardrailBlocked),
        (403, "budget_exceeded", errors.OtariBudgetExceeded),
        (403, "model_not_allowed", errors.OtariForbidden),
        (403, None, errors.OtariForbidden),
        (400, "invalid_model", errors.OtariSchemaRejected),
        (422, "invalid_request", errors.OtariSchemaRejected),
        (424, "provider_not_configured", errors.OtariProviderNotConfigured),
        (429, "rate_limited", errors.OtariRateLimited),
        (500, None, errors.OtariUpstreamError),
        (502, "provider_error", errors.OtariUpstreamError),
        (504, None, errors.OtariUpstreamError),
    ],
)
def test_status_and_code_mapping(status: int, code: str | None, expected: type[errors.OtariError]) -> None:
    headers = {"otari-error-code": code} if code else {}
    assert type(errors.from_response(status, headers, {"detail": "x"})) is expected


def test_budget_and_guardrail_share_403_and_differ_by_code() -> None:
    budget = errors.from_response(403, {"otari-error-code": "budget_exceeded"}, {})
    guard = errors.from_response(403, {"otari-error-code": "guardrail_violation"}, {})
    assert type(budget) is not type(guard)


def test_code_is_read_from_body_when_the_header_is_absent() -> None:
    error = errors.from_response(403, {}, {"detail": "x", "code": "budget_exceeded"})
    assert isinstance(error, errors.OtariBudgetExceeded)


def test_budget_refusal_keeps_scope_and_retry_after() -> None:
    headers = {
        "otari-error-code": "budget_exceeded", "otari-budget-scope": "user",
        "retry-after": "3600", "otari-request-id": "r9",
    }  # fmt: skip
    error = errors.from_response(403, headers, {"detail": "has exceeded budget limit"})
    assert isinstance(error, errors.OtariBudgetExceeded)
    assert (error.scope, error.retry_after, error.request_id) == ("user", 3600.0, "r9")


def test_funds_are_never_reported_as_a_budget_refusal() -> None:
    error = errors.from_response(402, {"otari-error-code": "insufficient_funds"}, {})
    assert not isinstance(error, errors.OtariBudgetExceeded)


def test_502_means_guardrail_outage_only_on_a_guarded_call() -> None:
    assert isinstance(errors.from_response(502, {}, {}, guarded=True), errors.OtariGuardrailUnavailable)
    assert isinstance(errors.from_response(502, {}, {}, guarded=False), errors.OtariUpstreamError)


def test_messages_envelope_message_is_read() -> None:
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}, "code": "rate_limited"}
    error = errors.from_response(429, {}, body)
    assert isinstance(error, errors.OtariRateLimited) and error.message == "slow down"


def test_message_carries_the_request_id() -> None:
    error = errors.from_response(500, {"otari-request-id": "r7"}, {"detail": "boom"})
    assert "r7" in str(error)
