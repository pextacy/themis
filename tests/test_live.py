"""Live smoke against hosted Otari. Opt in with `uv run pytest -q -m live`. Costs money."""

import pytest

from themis.config import load_settings
from themis.doctor.runner import run_features

pytestmark = pytest.mark.live


async def test_catalog_on_the_real_gateway() -> None:
    settings = load_settings()
    if settings.otari_api_key is None:
        pytest.skip("OTARI_API_KEY is not set")
    results = await run_features(settings, ["catalog"])
    assert results[0]["status"] == "PASS", results[0]
