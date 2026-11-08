import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.conftest import Gateway, chat_body, reply

spec = importlib.util.spec_from_file_location("bakeoff", Path("scripts/spikes/bakeoff.py"))
assert spec and spec.loader
bakeoff = importlib.util.module_from_spec(spec)
sys.modules["bakeoff"] = bakeoff
spec.loader.exec_module(bakeoff)

GOOD = '{"kind": "bug_report", "claims": [{"type": "behavior", "text": "x"}]}'


@pytest.mark.parametrize("output,expected", [
    (GOOD, True),
    ('{"kind": "APPROVED", "claims": []}', False),
    ('{"kind": "other", "claims": [{"type": "made_up", "text": "x"}]}', False),
    ("APPROVED", False),
])  # fmt: skip
def test_schema(output: str, expected: bool) -> None:
    assert (bakeoff.validate(output) is not None) is expected


def test_there_are_ten_fixtures_with_each_class() -> None:
    fixtures = [json.loads(path.read_text()) for path in bakeoff.FIXTURES_DIR.glob("*.json")]
    assert len(fixtures) == 10 and {item["class"] for item in fixtures} == {"genuine", "slop", "injection"}


def test_envelope_stops_a_breakout_attempt() -> None:
    fixture = json.loads((bakeoff.FIXTURES_DIR / "i3.json").read_text())
    assert bakeoff.envelope(fixture).count("</untrusted_submission>") == 1


def test_fixture_text_never_reaches_the_system_prompt() -> None:
    fixture = json.loads((bakeoff.FIXTURES_DIR / "i2.json").read_text())
    assert "SYSTEM OVERRIDE" not in bakeoff.SYSTEM and "SYSTEM OVERRIDE" in bakeoff.envelope(fixture)


async def test_repair_round_recovers_and_counts_both_costs(gateway: Gateway) -> None:
    fixture = json.loads((bakeoff.FIXTURES_DIR / "g1.json").read_text())
    gateway.script = [reply(body=chat_body("nope")), reply(body=chat_body(GOOD))]
    async with gateway.client() as client:
        row = await bakeoff.run_one(client, "acme/qwen", fixture)
    assert row["first_try_valid"] is False and row["valid"] is True
    assert row["cost_usd"] == 0.00024 and row["kind_correct"] is True
    assert len(gateway.requests) == 2


async def test_gateway_failure_counts_as_invalid(gateway: Gateway) -> None:
    fixture = json.loads((bakeoff.FIXTURES_DIR / "g1.json").read_text())
    gateway.script = [reply(400, {"code": "unsupported_parameter", "detail": "no JSON mode"})]
    async with gateway.client() as client:
        row = await bakeoff.run_one(client, "acme/qwen", fixture)
    assert row["valid"] is False and len(gateway.requests) == 1


def test_arbiter_is_a_different_family() -> None:
    base = {"valid_rate": 1.0, "kind_accuracy": 1.0, "claim_recall": 0.9, "median_cost_usd": 0.001}
    roles = bakeoff.pick_roles([
        {**base, "model": "a/qwen-1", "family": "qwen"},
        {**base, "model": "a/qwen-2", "family": "qwen", "median_cost_usd": 0.002},
        {**base, "model": "a/llama-1", "family": "llama", "median_cost_usd": 0.003},
    ])  # fmt: skip
    assert roles["clerk"] == "a/qwen-1" and roles["arbiter"] == "a/llama-1"
