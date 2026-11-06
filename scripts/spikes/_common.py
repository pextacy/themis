"""Shared plumbing for the Phase 0 spikes.

Spikes run before `src/themis/otari/client.py` exists, so this is the only spike
module that talks HTTP. Phase 1 replaces it with the real client.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPTS_DIR = Path(__file__).resolve().parent / "receipts"
DEFAULT_BASE_URL = "https://api.otari.ai/api/v1"
REAL_GATEWAY_HOSTS = ("api.otari.ai", "eu.api.otari.ai")

SECRET_PATTERNS = (
    re.compile(r"\btk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
)

CAPTURED_HEADERS = {
    "otari-request-id": "request_id",
    "otari-provider": "provider",
    "otari-model": "effective_model",
    "otari-fallback": "fallback",
    "otari-attempt-count": "attempt_count",
    "otari-error-code": "error_code",
    "otari-budget-scope": "budget_scope",
    "otari-end-user-budget": "end_user_budget",
    "otari-guardrails": "guardrails",
    "retry-after": "retry_after",
}

# Name patterns for open-weight families. These select candidates from the live
# catalog; they are not model ids, and the catalog stays the source of truth.
OPEN_WEIGHT_FAMILIES: dict[str, re.Pattern[str]] = {
    "qwen": re.compile(r"qwen(?!.*-max)"),
    "deepseek": re.compile(r"deepseek"),
    "llama": re.compile(r"llama"),
    "glm": re.compile(r"glm"),
    "kimi": re.compile(r"kimi"),
    "gemma": re.compile(r"gemma"),
    "nemotron": re.compile(r"nemotron"),
    "gpt-oss": re.compile(r"gpt-oss"),
}
PREFERRED_CODER_FAMILIES = ("deepseek", "qwen")


class Blocked(Exception):
    """A prerequisite is missing, so the feature was not exercised."""


class Failed(Exception):
    """The feature was exercised and did not behave as required."""


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read .env, letting real environment variables win."""
    values: dict[str, str] = {}
    env_file = path or REPO_ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("'\"")
    values.update(os.environ)
    return {name: value for name, value in values.items() if value}


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Return a copy of value with credentials removed from every string."""
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        for pattern in SECRET_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
    return value


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any
    record: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def error_code(self) -> str | None:
        code = self.headers.get("otari-error-code")
        if code:
            return code
        return self.body.get("code") if isinstance(self.body, dict) else None

    @property
    def content(self) -> str:
        try:
            return str(self.body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        calls = self.body.get("otari_tool_calls") if isinstance(self.body, dict) else None
        return calls or []


def parse_sse(lines: list[str]) -> list[dict[str, Any]]:
    """Turn raw server-sent event lines into JSON events, in stream order."""
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def open_weight_family(model_id: str) -> str | None:
    lowered = model_id.lower()
    for family, pattern in OPEN_WEIGHT_FAMILIES.items():
        if pattern.search(lowered):
            return family
    return None


def input_price(model: dict[str, Any]) -> float:
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("input_price_per_million"))
    except (TypeError, ValueError):
        return float("inf")


def open_weight_candidates(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cheapest priced JSON-capable chat model per open-weight family, cheapest first."""
    best: dict[str, dict[str, Any]] = {}
    for model in models:
        family = open_weight_family(str(model.get("id", "")))
        if family is None or model.get("pricing_source") in (None, "none", "dynamic"):
            continue
        if model.get("type") not in (None, "chat"):
            continue
        params = model.get("supported_parameters")
        if params and "response_format" not in params:
            continue
        outputs = (model.get("architecture") or {}).get("output_modalities")
        if outputs and "text" not in outputs:
            continue
        if family not in best or input_price(model) < input_price(best[family]):
            best[family] = model
    return sorted(best.values(), key=input_price)


class Context:
    """Holds configuration plus the calls made by the spike currently running."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = load_env() if env is None else env
        self.base_url = self.env.get("OTARI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.key = self.env.get("OTARI_API_KEY")
        self.timeout = float(self.env.get("THEMIS_OTARI_TIMEOUT_S", "60"))
        self.http = httpx.Client(timeout=self.timeout)
        self.calls: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.tools: Any = None
        self.receipts_dir = self._resolve_receipts_dir()

    def _resolve_receipts_dir(self) -> Path:
        host = urlparse(self.base_url).hostname or ""
        override = self.env.get("SPIKE_RECEIPT_DIR")
        if host in REAL_GATEWAY_HOSTS:
            return Path(override) if override else RECEIPTS_DIR
        if not override:
            raise Failed(f"{host} is not hosted Otari: set SPIKE_RECEIPT_DIR outside the repo")
        directory = Path(override).resolve()
        if directory.is_relative_to(REPO_ROOT):
            raise Failed("receipts from a test double must never be written inside the repo")
        return directory

    @property
    def secrets(self) -> tuple[str, ...]:
        names = ("OTARI_API_KEY", "OTARI_BUDGET_DEMO_KEY", "OTARI_SERVICE_KEY", "GITHUB_TOKEN")
        return tuple(self.env[name] for name in names if name in self.env)

    def require(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise Blocked(f"{name} is not set")
        return value

    def request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> Response:
        token = key or self.key
        if not token:
            raise Blocked("OTARI_API_KEY is not set")
        send_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        lines: list[str] = []
        if stream:
            with self.http.stream(method, url, json=body, headers=send_headers) as raw:
                status, reply_headers = raw.status_code, dict(raw.headers)
                lines = list(raw.iter_lines())
            text = "\n".join(lines)
        else:
            raw_reply = self.http.request(method, url, json=body, headers=send_headers)
            status, reply_headers = raw_reply.status_code, dict(raw_reply.headers)
            text = raw_reply.text
        latency_ms = round((time.monotonic() - started) * 1000)

        lowered = {name.lower(): value for name, value in reply_headers.items()}
        events = parse_sse(lines) if status < 400 else []
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        record = self._record(method, path, status, lowered, parsed, events, latency_ms)
        self.calls.append(record)
        return Response(status, lowered, parsed, record, events)

    def _record(
        self,
        method: str,
        path: str,
        status: int,
        headers: dict[str, str],
        body: Any,
        events: list[dict[str, Any]],
        latency_ms: int,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": latency_ms,
        }
        for header, name in CAPTURED_HEADERS.items():
            if header in headers:
                record[name] = headers[header]
        usage = self._usage(body, events)
        if usage is not None:
            record["usage"] = usage
            record["cost_usd"] = usage.get("cost_usd")
        if status >= 400:
            record["error_body"] = body
        return record

    @staticmethod
    def _usage(body: Any, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            return body["usage"]
        for event in reversed(events):
            if isinstance(event.get("usage"), dict):
                return event["usage"]
        return None

    def chat(self, model: str, messages: list[dict[str, str]], **extra: Any) -> Response:
        options = {"max_tokens": 64, "temperature": 0, **extra}
        key = options.pop("key", None)
        headers = options.pop("headers", None)
        body = {"model": model, "messages": messages, **options}
        return self.request("POST", "/chat/completions", key=key, body=body, headers=headers)

    def load_catalog(self) -> None:
        if self.models:
            return
        reply = self.request("GET", "/models")
        if reply.status == 401:
            raise Failed("gateway rejected OTARI_API_KEY (401)")
        if reply.status != 200 or not isinstance(reply.body, dict):
            raise Failed(f"GET /models answered {reply.status}")
        self.models = reply.body.get("data", [])

    def model_choice(self, index: int) -> str:
        """Pick model A (0) or model B (1): two different open-weight families."""
        name = "THEMIS_SPIKE_MODEL_A" if index == 0 else "THEMIS_SPIKE_MODEL_B"
        if name in self.env:
            return self.env[name]
        self.load_catalog()
        candidates = open_weight_candidates(self.models)
        if len(candidates) <= index:
            raise Blocked(f"catalog offers {len(candidates)} open-weight families, need 2; set {name}")
        return str(candidates[index]["id"])

    def tool_status(self, name: str) -> dict[str, Any]:
        """Return the /tools entry for name, raising Blocked when it is unavailable."""
        if self.tools is None:
            reply = self.request("GET", "/tools")
            if reply.status != 200:
                raise Failed(f"GET /tools answered {reply.status}")
            self.tools = reply.body
        for entry in self.tools.get("data", []):
            if name in (entry.get("name"), entry.get("type"), entry.get("id")):
                if entry.get("available") is False:
                    raise Blocked(f"{name} unavailable: {entry.get('reason', 'no reason given')}")
                return entry
        raise Blocked(f"{name} is not listed by GET /tools")

    def save_json(self, name: str, payload: Any) -> Path:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipts_dir / name
        clean = redact(payload, self.secrets)
        path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n")
        return path

    def now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
