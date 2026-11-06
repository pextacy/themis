"""Offline tests. The stub gateway below is a TEST DOUBLE: nothing it produces is
evidence, and Context refuses to write its output inside the repository."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bakeoff
import pytest
import run_all
import s05_code_exec
import s06_mcp
import s07_guardrail
import s08_key_budget
import s09_per_user_budget
import s10_routing
from _common import Blocked, Context, Failed, open_weight_candidates, parse_sse, redact

MODELS = [
    {"id": "acme/qwen-small", "pricing": {"input_price_per_million": 0.1}, "pricing_source": "default",
     "supported_parameters": ["response_format", "tools"]},
    {"id": "acme/llama-small", "pricing": {"input_price_per_million": 0.2}, "pricing_source": "default"},
    {"id": "acme/glm-small", "pricing": {"input_price_per_million": 0.3}, "pricing_source": "default"},
    {"id": "acme/closed-model", "pricing": {"input_price_per_million": 0.01}, "pricing_source": "default"},
]


class Stub(BaseHTTPRequestHandler):
    scripted: list[tuple[int, dict[str, str], Any]] = []
    seen: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, headers: dict[str, str], body: Any) -> None:
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send(200, {}, {"data": MODELS})
        elif self.path.endswith("/tools"):
            self._send(200, {}, {"object": "list", "data": [
                {"id": "otari_code_execution", "object": "tool", "available": True},
                {"id": "otari_web_search", "object": "tool", "available": False, "reason": "workspace_disabled"},
            ]})
        elif self.path.endswith("/profiles"):
            self._send(200, {}, {"available": True, "profiles": [{"profile": "prompt-injection"}]})
        else:
            self._send(200, {}, {"remaining_usd": "1.00"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        Stub.seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        status, headers, reply = Stub.scripted.pop(0)
        self._send(status, {"Otari-Request-ID": "stub-req", **headers}, reply)


@pytest.fixture
def ctx(tmp_path: Path) -> Any:
    Stub.scripted, Stub.seen = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Context({
        "OTARI_API_KEY": "tk-stubkey12345",
        "OTARI_BUDGET_DEMO_KEY": "tk-stubbudget123",
        "OTARI_PERUSER_BUDGET_ID": "budget-1",
        "THEMIS_SPIKE_FALLBACK_POLICY": "themis-fallback-demo",
        "OTARI_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}/api/v1",
        "SPIKE_RECEIPT_DIR": str(tmp_path),
    })
    server.shutdown()


def chat_reply(content: str = "ok", **extra: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost_usd": "0.000010"}, **extra}


def test_candidates_are_open_weight_one_per_family_cheapest_first() -> None:
    ids = [m["id"] for m in open_weight_candidates(MODELS)]
    assert ids == ["acme/qwen-small", "acme/llama-small", "acme/glm-small"]


def test_redact_removes_tokens_nested_and_exact() -> None:
    clean = redact({"a": ["Bearer abcdef123456", "tk-secret123"], "b": "my-raw-value"}, ("my-raw-value",))
    assert "abcdef" not in str(clean) and "secret123" not in str(clean) and "my-raw" not in str(clean)


def test_receipts_from_a_test_double_cannot_land_in_the_repo() -> None:
    with pytest.raises(Failed):
        Context({"OTARI_BASE_URL": "http://127.0.0.1:1/api/v1"})
    repo_dir = str(Path(__file__).resolve().parents[1] / "receipts")
    with pytest.raises(Failed):
        Context({"OTARI_BASE_URL": "http://127.0.0.1:1/api/v1", "SPIKE_RECEIPT_DIR": repo_dir})


def test_missing_key_is_blocked_not_failed(tmp_path: Path) -> None:
    empty = Context({"OTARI_BASE_URL": "http://127.0.0.1:1/api/v1", "SPIKE_RECEIPT_DIR": str(tmp_path)})
    with pytest.raises(Blocked):
        empty.load_catalog()


def test_disabled_tool_blocks_the_spike(ctx: Context) -> None:
    with pytest.raises(Blocked, match="workspace_disabled"):
        ctx.tool_status("otari_web_search")


def test_code_exec_rejects_output_the_sandbox_did_not_produce(ctx: Context) -> None:
    fake = {"type": "code_execution", "status": "completed", "exit_code": 0, "stdout": "42\n"}
    Stub.scripted = [(200, {}, chat_reply(otari_tool_calls=[fake]))]
    with pytest.raises(Failed, match="unexpected sandbox result"):
        s05_code_exec.run(ctx)


def test_guardrail_pass_and_block(ctx: Context) -> None:
    verdict = json.dumps([{"profile": "prompt-injection", "status": "evaluated", "valid": True}])
    Stub.scripted = [
        (200, {"Otari-Guardrails": verdict}, chat_reply()),
        (403, {"Otari-Error-Code": "guardrail_violation"}, {"detail": "flagged", "code": "guardrail_violation"}),
    ]
    assert s07_guardrail.run(ctx)["block_code"] == "guardrail_violation"
    assert Stub.seen[0]["body"]["guardrails"][0]["mode"] == "block"


def test_guardrail_unevaluated_pass_is_not_accepted(ctx: Context) -> None:
    verdict = json.dumps([{"profile": "prompt-injection", "status": "unavailable", "valid": None}])
    Stub.scripted = [(200, {"Otari-Guardrails": verdict}, chat_reply())]
    with pytest.raises(Failed):
        s07_guardrail.run(ctx)


def test_guardrail_service_down_is_blocked(ctx: Context) -> None:
    Stub.scripted = [(502, {}, {"detail": "unavailable"})]
    with pytest.raises(Blocked):
        s07_guardrail.run(ctx)


def test_key_budget_refusal_reads_scope(ctx: Context) -> None:
    Stub.scripted = [
        (200, {}, chat_reply()),
        (403, {"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "api_token", "Retry-After": "60"},
         {"detail": "has exceeded budget limit", "code": "budget_exceeded"}),
    ]
    observed = s08_key_budget.run(ctx)
    assert observed["admitted_before_refusal"] == 1 and observed["budget_scope"] == "api_token"


def test_per_user_sends_user_and_requires_user_scope(ctx: Context) -> None:
    Stub.scripted = [
        (200, {"Otari-End-User-Budget": "budget-1"}, chat_reply()),
        (403, {"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "user"}, {"code": "budget_exceeded"}),
    ]
    observed = s09_per_user_budget.run(ctx)
    assert observed["assigned_budget_seen"] is True
    assert Stub.seen[0]["body"]["user"].startswith("gh:spike-")
    assert Stub.seen[0]["headers"]["Otari-End-User-Budget"] == "budget-1"


def test_per_user_refusal_from_another_scope_fails(ctx: Context) -> None:
    Stub.scripted = [(403, {"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "organization"},
                      {"code": "budget_exceeded"})]
    with pytest.raises(Failed, match="organization"):
        s09_per_user_budget.run(ctx)


def test_routing_requires_a_real_fallback(ctx: Context) -> None:
    Stub.scripted = [(200, {"Otari-Fallback": "false", "Otari-Attempt-Count": "1"}, chat_reply())]
    with pytest.raises(Failed):
        s10_routing.run(ctx)
    served = {"Otari-Fallback": "true", "Otari-Attempt-Count": "2", "Otari-Model": "glm-x"}
    Stub.scripted = [(200, served, chat_reply())]
    assert s10_routing.run(ctx)["family"] == "glm"


def test_mcp_pairs_blocks_by_stream_order(ctx: Context) -> None:
    def event(block: dict[str, Any]) -> str:
        return "data: " + json.dumps({"type": "content_block_start", "content_block": block}) + "\n\n"

    stream = (
        event({"type": "mcp_tool_use", "name": "read_wiki", "server_name": "deepwiki", "input": {}})
        + event({"type": "mcp_tool_result", "is_error": False})
        + 'data: {"type":"message_delta","usage":{"cost_usd":"0.000020"}}\n\n'
    )
    Stub.scripted = [(200, {"Content-Type": "text/event-stream"}, stream)]
    observed = s06_mcp.run(ctx)
    assert observed["activity"][0]["tool"] == "read_wiki"
    assert Stub.seen[0]["headers"]["anthropic-beta"] == s06_mcp.MCP_BETA
    assert ctx.calls[-1]["cost_usd"] == "0.000020"


def test_mcp_refused_inline_is_blocked_with_the_fix(ctx: Context) -> None:
    Stub.scripted = [(400, {}, {"type": "error", "error": {"message": "mcp_servers not allowed"}})]
    with pytest.raises(Blocked, match="request_mcp_servers_allowed"):
        s06_mcp.run(ctx)


def test_parse_sse_skips_noise() -> None:
    assert parse_sse(["event: x", "data: [DONE]", "data: nope", 'data: {"a": 1}']) == [{"a": 1}]


def test_runner_redacts_the_receipt(ctx: Context) -> None:
    Stub.scripted = [(500, {}, {"detail": "boom tk-leakedsecret99"})]
    result = run_all.execute(ctx, s10_routing)
    assert result["status"] == "FAIL" and "leakedsecret" not in json.dumps(result)


def test_readiness_gate_counts_only_passes() -> None:
    results = {name: {"status": "PASS", "calls": [{"request_id": "r"}]} for name in (
        "chat-model-a", "chat-model-b", "mcp-deepwiki", "web-search-fetch", "code-execution", "key-budget-refusal")}
    text, advanced, core = run_all.readiness(results)
    assert (advanced, core) == (5, 2) and "NOT met" in text
    results["guardrail"] = {"status": "PASS", "calls": []}
    text, advanced, _ = run_all.readiness(results)
    assert advanced == 6 and "**Exit gate met" in text


@pytest.mark.parametrize("output,expected", [
    ('{"kind":"bug_report","claims":[{"type":"behavior","text":"x"}]}', True),
    ('{"kind":"APPROVED","claims":[]}', False),
    ('{"kind":"other","claims":[{"type":"made_up","text":"x"}]}', False),
    ("APPROVED", False),
])
def test_bakeoff_schema(output: str, expected: bool) -> None:
    assert (bakeoff.validate(output) is not None) is expected


def test_bakeoff_envelope_escapes_a_breakout_attempt() -> None:
    fixture = json.loads((Path(bakeoff.FIXTURES_DIR) / "i3.json").read_text())
    text = bakeoff.envelope(fixture)
    assert text.count("</untrusted_submission>") == 1


def test_bakeoff_arbiter_is_a_different_family() -> None:
    base = {"valid_rate": 1.0, "kind_accuracy": 1.0, "claim_recall": 0.9, "median_cost_usd": 0.001}
    roles = bakeoff.pick_roles([
        {**base, "model": "a/qwen-1", "family": "qwen"},
        {**base, "model": "a/qwen-2", "family": "qwen", "median_cost_usd": 0.002},
        {**base, "model": "a/llama-1", "family": "llama", "median_cost_usd": 0.003},
    ])
    assert roles["clerk"] == "a/qwen-1" and roles["arbiter"] == "a/llama-1"
