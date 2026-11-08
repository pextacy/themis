"""Everything here is a TEST DOUBLE. Nothing it produces is evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import structlog

from themis.config import Settings
from themis.models import OtariCall
from themis.otari.client import OtariClient

API_KEY = "tk-testkey-1234567890"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"otari_api_key": API_KEY, "model_clerk": "acme/qwen-small", **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def reply(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    base = {"Otari-Request-ID": "req-1"}
    if isinstance(body, str):
        return httpx.Response(status, text=body, headers={**base, **(headers or {})})
    return httpx.Response(status, json=body if body is not None else {}, headers={**base, **(headers or {})})


def chat_body(content: str = "ok", **extra: Any) -> dict[str, Any]:
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": "0.000120"}
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": usage, **extra}


def sse(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


class Gateway:
    """Answers from a script, or from `respond`, and remembers every request."""

    def __init__(self) -> None:
        self.script: list[httpx.Response | Exception] = []
        self.respond: Callable[[httpx.Request], httpx.Response] | None = None
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.calls: list[OtariCall] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self, settings: Settings | None = None, api_key: str | None = None) -> OtariClient:
        return OtariClient(
            settings or make_settings(),
            api_key=api_key,
            transport=httpx.MockTransport(self),
            sleep=self.sleep,
            on_call=self.calls.append,
        )

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


@pytest.fixture(autouse=True)
def isolated_logging() -> Iterator[None]:
    """A CLI test binds structlog to a captured stream that closes when the test ends."""
    structlog.reset_defaults()
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
    yield
    structlog.reset_defaults()


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()
