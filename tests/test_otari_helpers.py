import pytest

from themis.otari import tools
from themis.otari.budgets import check_contributor_key, contributor_key, end_user_headers, is_contributor_refusal
from themis.otari.catalog import open_weight_candidates, open_weight_family
from themis.otari.errors import OtariBudgetExceeded
from themis.otari.mcp import McpServer, default_servers, pair_activity, parse_sse

PRICED = {"pricing_source": "default"}
MODELS = [
    {"id": "acme/qwen-small", "pricing": {"input_price_per_million": 0.1}, **PRICED,
     "supported_parameters": ["response_format", "tools"]},
    {"id": "acme/qwen-large", "pricing": {"input_price_per_million": 0.9}, **PRICED},
    {"id": "acme/qwen3-max", "pricing": {"input_price_per_million": 0.01}, **PRICED},
    {"id": "acme/llama-small", "pricing": {"input_price_per_million": 0.2}, **PRICED},
    {"id": "acme/glm-nojson", "pricing": {"input_price_per_million": 0.05}, **PRICED,
     "supported_parameters": ["tools"]},
    {"id": "acme/deepseek-embed", "pricing": {"input_price_per_million": 0.01}, **PRICED, "type": "embedding"},
    {"id": "acme/kimi-unpriced", "pricing_source": "none"},
    {"id": "acme/closed-model", "pricing": {"input_price_per_million": 0.01}, **PRICED},
]  # fmt: skip


def test_candidates_cheapest_per_family_open_weight_only() -> None:
    assert [m["id"] for m in open_weight_candidates(MODELS)] == ["acme/qwen-small", "acme/llama-small"]


def test_closed_models_have_no_family() -> None:
    assert open_weight_family("openai/gpt-5") is None and open_weight_family("qwen/qwen3-max") is None
    assert open_weight_family("openai/gpt-oss-120b") == "gpt-oss"
    assert open_weight_family("lfm2.5-2.6b-free") == "lfm"
    assert open_weight_family("liquid/lfm-2.5-2.6b:free") == "lfm"


def test_tools_are_declared_type_only() -> None:
    assert tools.declare(tools.WEB_SEARCH, tools.CODE_EXECUTION) == [
        {"type": "otari_web_search"}, {"type": "otari_code_execution"},
    ]  # fmt: skip


def test_contributor_keys_are_numeric() -> None:
    assert check_contributor_key(contributor_key(583231)) == "gh:583231"
    for bad in ("gh:octocat", "octocat", "me@example.com", "gh:12 "):
        with pytest.raises(ValueError):
            check_contributor_key(bad)


def test_end_user_header_only_when_set() -> None:
    assert end_user_headers(None) == {} and end_user_headers("b1") == {"Otari-End-User-Budget": "b1"}


def test_contributor_and_project_refusals_differ() -> None:
    assert is_contributor_refusal(OtariBudgetExceeded("x", scope="user"))
    assert not is_contributor_refusal(OtariBudgetExceeded("x", scope="api_token"))


def test_mcp_declaration_omits_unset_fields() -> None:
    assert McpServer(name="a", url="https://x").declaration() == {"name": "a", "url": "https://x"}
    assert McpServer(name="a", url="https://x", allowed_tools=[]).declaration()["allowed_tools"] == []
    assert default_servers()[0].name == "deepwiki"


def test_parse_sse_skips_noise() -> None:
    assert parse_sse(["event: x", "data: [DONE]", "data: nope", "data: [1]", 'data: {"a": 1}']) == [{"a": 1}]


def test_activity_pairs_by_order_and_ignores_an_orphan_result() -> None:
    def start(block: dict) -> dict:
        return {"type": "content_block_start", "content_block": block}

    calls = pair_activity([
        start({"type": "mcp_tool_result", "content": "orphan"}),
        start({"type": "mcp_tool_use", "name": "one"}),
        start({"type": "mcp_tool_result", "is_error": True, "content": "bad"}),
        start({"type": "mcp_tool_use", "name": "two"}),
    ])  # fmt: skip
    assert [(call.tool, call.is_error, call.output) for call in calls] == [("one", True, "bad")]
