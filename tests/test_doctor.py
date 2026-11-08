import json
import re
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import Gateway, chat_body, make_settings, reply, sse
from themis import cli
from themis.config import ConfigError, Settings
from themis.doctor import capture as capture_module
from themis.doctor.capture import capture, pending_features, stored_results
from themis.doctor.probes import FEATURES
from themis.doctor.runner import gate, gate_met, render_table, run_features

MODELS = [
    {"id": "acme/qwen-small", "pricing": {"input_price_per_million": 0.1}, "pricing_source": "default"},
    {"id": "acme/llama-small", "pricing": {"input_price_per_million": 0.2}, "pricing_source": "default"},
]
TOOLS = ["otari_web_search", "otari_web_fetch", "otari_code_execution"]
BUDGET_KEY = "tk-budget-demo-999"


def healthy(request: httpx.Request) -> httpx.Response:
    """A gateway on which every feature behaves as documented."""
    path = request.url.path.removeprefix("/api/v1/")
    if request.method == "GET":
        bodies = {
            "models": {"data": MODELS},
            "tools": {"data": [{"id": tool, "available": True} for tool in TOOLS]},
            "tool-settings/guardrails/profiles": {"available": True, "profiles": [{"profile": "prompt-injection"}]},
            "key/budget": {"remaining_usd": 4.2},
        }
        return reply(body=bodies[path])
    body = json.loads(request.content)
    if path == "messages":
        use = {"type": "mcp_tool_use", "name": "read_wiki", "server_name": "deepwiki", "input": {}}
        result = {"type": "mcp_tool_result", "is_error": False, "content": "ok"}
        return reply(body=sse(*({"type": "content_block_start", "content_block": b} for b in (use, result))))
    if path == "fetch":
        if "99991" in body["url"]:
            return reply(
                502, {"detail": "fetch failed"}, {"Otari-Error-Status": "404", "Otari-Error-Tag": "http_status"}
            )
        return reply(body={"object": "fetch", "text": "CVE-2021-44228", "truncated": False})
    if path == "code/execute":
        number = int(re.search(r"print\((\d+) \* 3\)", body["code"]).group(1))
        return reply(body={"stdout": f"{number * 3}\n", "return_code": 0})
    if body.get("guardrails"):
        text = body["messages"][-1]["content"]
        if "Ignore all previous" in text:
            return reply(403, {"code": "guardrail_violation", "detail": "flagged"})
        verdict = [{"profile": "prompt-injection", "status": "evaluated", "valid": True}]
        return reply(body=chat_body("OK"), headers={"Otari-Guardrails": json.dumps(verdict)})
    if body.get("tools"):
        kinds = [tool["type"] for tool in body["tools"]]
        if "otari_code_execution" in kinds:
            number = int(re.search(r"print\((\d+) \* 7\)", body["messages"][0]["content"]).group(1))
            run = {"type": "code_execution", "status": "completed", "stdout": f"{number * 7}\n", "exit_code": 0}
            return reply(body=chat_body("done", otari_tool_calls=[run]))
        calls = [{"type": "web_search", "status": "completed"}, {"type": "web_fetch", "status": "completed"}]
        return reply(body=chat_body("10.0", otari_tool_calls=calls))
    if body["model"] == "themis-fallback-demo":
        headers = {"Otari-Fallback": "true", "Otari-Attempt-Count": "2", "Otari-Model": "glm-5"}
        return reply(body=chat_body(), headers=headers)
    if request.headers["authorization"].endswith(BUDGET_KEY):
        return reply(403, {"code": "budget_exceeded"}, {"Otari-Budget-Scope": "api_token"})
    if body.get("user"):
        return reply(403, {"code": "budget_exceeded"}, {"Otari-Budget-Scope": "user"})
    return reply(body=chat_body('{"ok": true, "word": "red"}'))


def configured(**overrides: object) -> Settings:
    values = {"otari_budget_demo_key": BUDGET_KEY, "otari_peruser_budget_id": "b1", "model_clerk": None, **overrides}
    return make_settings(**values)


def factory(gateway: Gateway, settings: Settings):  # type: ignore[no-untyped-def]
    return lambda key: gateway.client(settings, api_key=key)


async def run(gateway: Gateway, settings: Settings) -> dict[str, dict]:
    results = await run_features(settings, make_client=factory(gateway, settings), calls=gateway.calls)
    return {result["feature"]: result for result in results}


async def test_every_feature_passes_on_a_healthy_gateway(gateway: Gateway) -> None:
    gateway.respond = healthy
    settings = configured()
    results = await run(gateway, settings)
    assert {name: r["status"] for name, r in results.items()} == {f.name: "PASS" for f in FEATURES}
    assert gate(results) == (8, 2) and gate_met(results)
    assert results["per-user"]["observed"]["user"].startswith("gh:9")
    assert results["chat-a"]["calls"][-1]["request_id"] == "req-1"


async def test_missing_setup_blocks_instead_of_failing(gateway: Gateway) -> None:
    gateway.respond = healthy
    settings = configured(otari_budget_demo_key=None, otari_peruser_budget_id=None)
    results = await run(gateway, settings)
    assert results["budget"]["status"] == "BLOCKED" and "OTARI_BUDGET_DEMO_KEY" in results["budget"]["reason"]
    assert results["per-user"]["status"] == "BLOCKED"
    assert gate(results) == (6, 2)


async def _with(gateway: Gateway, override) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    def respond(request: httpx.Request) -> httpx.Response:
        return override(request) or healthy(request)

    gateway.respond = respond
    return await run(gateway, configured())


async def test_code_exec_rejects_output_the_sandbox_did_not_produce(gateway: Gateway) -> None:
    def fake(request: httpx.Request) -> httpx.Response | None:
        if request.content and b"otari_code_execution" in request.content:
            run_ = {"type": "code_execution", "status": "completed", "stdout": "42\n", "exit_code": 0}
            return reply(body=chat_body("42", otari_tool_calls=[run_]))
        return None

    assert (await _with(gateway, fake))["code"]["status"] == "FAIL"


async def test_tool_marked_unavailable_blocks_with_its_reason(gateway: Gateway) -> None:
    def disabled(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/tools"):
            rows = [{"id": tool, "available": tool != "otari_web_fetch", "reason": "workspace_disabled"}
                    for tool in TOOLS]  # fmt: skip
            return reply(body={"data": rows})
        return None

    result = (await _with(gateway, disabled))["web"]
    assert result["status"] == "BLOCKED" and "workspace_disabled" in result["reason"]


async def test_inline_mcp_refusal_names_the_fix(gateway: Gateway) -> None:
    def refused(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/messages"):
            return reply(400, {"type": "error", "error": {"message": "mcp_servers are not allowed here"}})
        return None

    result = (await _with(gateway, refused))["mcp"]
    assert result["status"] == "BLOCKED" and "request_mcp_servers_allowed" in result["reason"]


async def test_stream_without_activity_fails(gateway: Gateway) -> None:
    def silent(request: httpx.Request) -> httpx.Response | None:
        return reply(body=sse({"type": "message_stop"})) if request.url.path.endswith("/messages") else None

    assert (await _with(gateway, silent))["mcp"]["status"] == "FAIL"


async def test_unevaluated_guardrail_pass_is_not_accepted(gateway: Gateway) -> None:
    def unevaluated(request: httpx.Request) -> httpx.Response | None:
        if request.content and b'"guardrails"' in request.content:
            verdict = [{"profile": "p", "status": "unavailable", "valid": None, "reason": "not_configured"}]
            return reply(body=chat_body(), headers={"Otari-Guardrails": json.dumps(verdict)})
        return None

    assert (await _with(gateway, unevaluated))["guardrail"]["status"] == "FAIL"


async def test_served_injection_fails_the_guardrail(gateway: Gateway) -> None:
    good = json.dumps([{"status": "evaluated", "valid": True}])

    def served(request: httpx.Request) -> httpx.Response | None:
        if request.content and b'"guardrails"' in request.content:
            return reply(body=chat_body(), headers={"Otari-Guardrails": good})
        return None

    assert (await _with(gateway, served))["guardrail"]["status"] == "FAIL"


async def test_guardrail_outage_is_blocked(gateway: Gateway) -> None:
    def down(request: httpx.Request) -> httpx.Response | None:
        return reply(502, {"detail": "down"}) if request.content and b'"guardrails"' in request.content else None

    assert (await _with(gateway, down))["guardrail"]["status"] == "BLOCKED"


async def test_no_profiles_is_blocked(gateway: Gateway) -> None:
    def none(request: httpx.Request) -> httpx.Response | None:
        return reply(body={"available": False, "profiles": []}) if request.url.path.endswith("/profiles") else None

    assert (await _with(gateway, none))["guardrail"]["status"] == "BLOCKED"


async def test_per_user_refusal_from_another_scope_fails(gateway: Gateway) -> None:
    def project(request: httpx.Request) -> httpx.Response | None:
        if request.content and b'"user"' in request.content:
            return reply(403, {"code": "budget_exceeded"}, {"Otari-Budget-Scope": "organization"})
        return None

    assert (await _with(gateway, project))["per-user"]["status"] == "FAIL"


async def test_normal_key_user_mismatch_is_setup_not_failure(gateway: Gateway) -> None:
    def mismatch(request: httpx.Request) -> httpx.Response | None:
        if request.content and b'"user"' in request.content:
            return reply(403, {"detail": "'user' field does not match the authenticated API key's user"})
        return None

    assert (await _with(gateway, mismatch))["per-user"]["status"] == "BLOCKED"


async def test_budget_never_refused_fails(gateway: Gateway) -> None:
    def generous(request: httpx.Request) -> httpx.Response | None:
        return reply(body=chat_body()) if request.headers["authorization"].endswith(BUDGET_KEY) else None

    assert (await _with(gateway, generous))["budget"]["status"] == "FAIL"


async def test_routing_needs_a_real_fallback_and_an_existing_policy(gateway: Gateway) -> None:
    def no_fallback(request: httpx.Request) -> httpx.Response | None:
        if request.content and b"themis-fallback-demo" in request.content:
            return reply(body=chat_body(), headers={"Otari-Fallback": "false", "Otari-Attempt-Count": "1"})
        return None

    assert (await _with(gateway, no_fallback))["routing"]["status"] == "FAIL"

    def missing(request: httpx.Request) -> httpx.Response | None:
        if request.content and b"themis-fallback-demo" in request.content:
            return reply(400, {"code": "invalid_model", "detail": "nope"})
        return None

    assert (await _with(Gateway(), missing))["routing"]["status"] == "BLOCKED"


async def test_no_funds_is_blocked_with_the_request_id(gateway: Gateway) -> None:
    def broke(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST":
            return reply(402, {"code": "insufficient_funds", "detail": "top up"})
        return None

    result = (await _with(gateway, broke))["chat-a"]
    assert result["status"] == "BLOCKED" and "req-1" in result["reason"]


async def test_two_same_family_models_fail(gateway: Gateway) -> None:
    gateway.respond = healthy
    settings = configured(model_clerk="acme/qwen-a", model_arbiter="acme/qwen-b")
    assert (await run(gateway, settings))["chat-b"]["status"] == "FAIL"


async def test_rejected_key_stops_the_run(gateway: Gateway) -> None:
    gateway.respond = lambda request: httpx.Response(401, json={"detail": "Invalid API key"})
    settings = configured()
    with pytest.raises(ConfigError, match="401"):
        await run(gateway, settings)
    assert len(gateway.requests) == 1


async def test_a_bad_budget_key_fails_only_its_own_probe(gateway: Gateway) -> None:
    def rejected(request: httpx.Request) -> httpx.Response | None:
        if request.headers["authorization"].endswith(BUDGET_KEY):
            return httpx.Response(401, json={"detail": "Invalid API key"})
        return None

    results = await _with(gateway, rejected)
    assert results["budget"]["status"] == "FAIL" and results["routing"]["status"] == "PASS"


def test_gate_counts_only_passes() -> None:
    passed = {"status": "PASS", "calls": []}
    names = ("chat-a", "chat-b", "mcp", "web", "code", "budget")
    results = {name: passed for name in names}
    assert gate(results) == (5, 2) and not gate_met(results) and "NOT met" in render_table(results)
    results["guardrail"] = passed
    assert gate(results) == (6, 2) and gate_met(results)
    assert "Exit gate met" in render_table(results)


def test_empty_table_says_nothing_is_live() -> None:
    assert "No live gateway call" in render_table({})


def test_test_double_cannot_write_to_evidence(gateway: Gateway, tmp_path: Path) -> None:
    settings = configured()
    with pytest.raises(ConfigError, match="test double"):
        capture(settings, make_client=factory(gateway, settings), readiness=tmp_path / "r.md")
    assert gateway.requests == []


def test_evidence_only_comes_from_hosted_otari(tmp_path: Path) -> None:
    settings = make_settings(otari_base_url="http://127.0.0.1:9/api/v1")
    with pytest.raises(ConfigError, match="hosted"):
        capture(settings, directory=tmp_path / "e", readiness=tmp_path / "r.md")


def test_capture_writes_redacted_receipts_and_merges_readiness(gateway: Gateway, tmp_path: Path) -> None:
    gateway.respond = healthy
    settings = configured()
    out, table = tmp_path / "receipts", tmp_path / "READINESS.md"
    make = factory(gateway, settings)
    capture(settings, ["chat-a"], directory=out, readiness=table, make_client=make, calls=gateway.calls)
    capture(settings, ["web"], directory=out, readiness=table, make_client=make, calls=gateway.calls)
    assert {path.name for path in out.iterdir()} == {"chat-a-live.json", "web-live.json"}
    assert set(stored_results(out)) == {"chat-a", "web"}
    assert "`chat-a` | ✅ PASS" in table.read_text() and "`web` | ✅ PASS" in table.read_text()
    assert "tk-" not in "".join(path.read_text() for path in out.iterdir())


def test_call_without_a_request_id_writes_no_receipt(tmp_path: Path) -> None:
    settings = configured()
    results = [{"feature": "chat-a", "status": "FAIL", "reason": "x", "calls": [{"request_id": None}]}]
    capture_module.persist(settings, results, tmp_path / "e", tmp_path / "r.md")
    assert not (tmp_path / "e").exists()


runner = CliRunner()


def test_cli_exit_codes_without_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OTARI_API_KEY", raising=False)
    assert runner.invoke(cli.app, ["doctor"]).exit_code == 2
    assert runner.invoke(cli.app, ["evidence", "capture", "web"]).exit_code == 2
    assert runner.invoke(cli.app, ["evidence", "capture", "nonsense"]).exit_code == 2
    assert runner.invoke(cli.app, ["--version"]).exit_code == 0


def test_pending_skips_only_what_passed(tmp_path: Path) -> None:
    (tmp_path / "chat-a-live.json").write_text('{"status": "PASS"}')
    (tmp_path / "chat-b-live.json").write_text('{"status": "FAIL"}')
    names = pending_features(tmp_path)
    assert "chat-a" not in names and "chat-b" in names and "guardrail" in names
