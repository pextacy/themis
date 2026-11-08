from datetime import UTC, datetime
from typing import Any

import httpx

from tests.conftest import Gateway
from tests.evidence_helpers import SOURCE, evidence_context, tarball
from themis.evidence import ecosystems
from themis.evidence.ecosystems import cargo_requirements, go_requirements, uses_private_source
from themis.evidence.registries import ExtendedRegistry, escape_go, inspect_extended
from themis.evidence.snapshot import Snapshot
from themis.models import CheckStatus, Strength, SubmissionKind

NOW = datetime(2026, 11, 6, tzinfo=UTC)
OLD = "2020-01-01T00:00:00Z"
RECENT = "2026-11-01T00:00:00Z"
CARGO_BASE = '[package]\nname = "tiny"\n\n[dependencies]\nserde = "1"\n'
GO_BASE = "module github.com/acme/tiny\n\ngo 1.22\n"


def registry(table: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, answer in table.items():
            if needle in url:
                return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
        return httpx.Response(404, json={"errors": []})

    return httpx.MockTransport(handle)


def crate(created: str = OLD, recent: int = 90000) -> dict[str, Any]:
    return {"crate": {"created_at": created, "recent_downloads": recent}}


async def look(table: dict[str, Any], ecosystem: str, name: str, **options: Any) -> Any:
    async with ExtendedRegistry(transport=registry(table)) as client:
        return await inspect_extended(client, ecosystem, name, ledger=options.get("ledger", set()), now=NOW)


async def test_genuine_crate() -> None:
    report = await look({"crates.io/api/v1/crates/tokio": crate()}, "crates", "tokio")
    assert report.found is True and report.risk == "none"


async def test_missing_crate_blocks() -> None:
    report = await look({}, "crates", "serde_jsonx_fast")
    assert report.found is False and report.risk == "block"


async def test_newborn_unknown_crate_is_flagged() -> None:
    report = await look({"crates.io": crate(RECENT, 3)}, "crates", "serde-json-fast")
    assert set(report.signals) == {"newborn", "low_adoption"}


async def test_a_crates_outage_is_not_missing() -> None:
    report = await look({"crates.io": httpx.Response(503)}, "crates", "tokio")
    assert report.found is None


async def test_a_ledger_hit_blocks_without_a_popular_list() -> None:
    report = await look({"crates.io": crate()}, "crates", "Tokio-Fast", ledger={("crates", "tokio-fast")})
    assert report.risk == "block" and "lookalike" not in report.signals


async def test_genuine_go_module() -> None:
    table = {
        "@latest": {"Version": "v1.2.0", "Time": OLD},
        "@v/list": httpx.Response(200, text="v1.0.0\nv1.2.0\n"),
        "v1.0.0.info": {"Time": OLD},
    }
    report = await look(table, "go", "github.com/acme/real")
    assert report.found is True and report.signals == []


async def test_newborn_go_module_with_no_tagged_versions() -> None:
    table = {"@latest": {"Version": "v0.0.0-x", "Time": RECENT}, "@v/list": httpx.Response(200, text="")}
    report = await look(table, "go", "github.com/acme/new")
    assert report.signals == ["newborn"]


async def test_gone_module_is_missing_and_an_outage_is_not() -> None:
    assert (await look({"@latest": httpx.Response(410)}, "go", "github.com/acme/gone")).found is False
    assert (await look({"@latest": httpx.Response(500)}, "go", "github.com/acme/down")).found is None


def test_go_capitals_are_escaped() -> None:
    assert escape_go("github.com/Azure/SDK") == "github.com/!azure/!s!d!k"


async def test_invalid_names_are_never_requested() -> None:
    assert (await look({}, "crates", "../../etc")).found is None
    assert (await look({}, "go", "ignore previous instructions")).found is None


def test_cargo_parsing_skips_local_and_git() -> None:
    text = """
[dependencies]
serde = "1"
tokio = { version = "1", features = ["full"] }
local = { path = "../local" }
fork = { git = "https://example.com/x" }
inner = { registry = "corp", version = "1" }
renamed = { package = "real-name", version = "2" }
[dev-dependencies]
proptest = "1"
[target.'cfg(unix)'.dependencies]
libc = "0.2"
"""
    assert set(cargo_requirements(text)) == {"serde", "tokio", "real-name", "proptest", "libc"}
    assert cargo_requirements("not [toml") == {}


def test_go_parsing_skips_indirect_and_replaced() -> None:
    text = """module x
require github.com/one/a v1.0.0
require (
\tgithub.com/two/b v1.2.3
\tgithub.com/three/c v0.1.0 // indirect
\tgithub.com/four/d v1.0.0
)
replace github.com/four/d => ../d
"""
    assert go_requirements(text) == {"github.com/one/a": "v1.0.0", "github.com/two/b": "v1.2.3"}


def test_private_sources() -> None:
    assert uses_private_source(Snapshot("x", files={".cargo/config.toml": '[registries.corp]\nindex = "x"'}))
    assert uses_private_source(Snapshot("x", files={"Makefile": "export GOPRIVATE=corp.dev/*"}))
    assert uses_private_source(Snapshot("x", files={".github/workflows/ci.yml": "GOPROXY=https://goproxy.corp"}))
    assert not uses_private_source(Snapshot("x", files={"README.md": "We do not use GOPRIVATE."}))


def pr(gateway: Gateway, base: dict[str, str], head: dict[str, str], table: dict[str, Any], files: list[str]) -> Any:
    context = evidence_context(
        gateway, "Adds a dependency.", [], kind=SubmissionKind.FEATURE_PR, is_pull_request=True, head_sha="head",
        changed_files=files, refs={"head": tarball({**SOURCE, **head}), "main": tarball({**SOURCE, **base})},
    )  # fmt: skip
    context.registry = ExtendedRegistry(transport=registry(table))
    return context


async def test_pr_adding_a_real_crate(gateway: Gateway) -> None:
    head = {"Cargo.toml": CARGO_BASE + 'tokio = "1"\n'}
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, head, {"crates/tokio": crate()}, ["Cargo.toml"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.CONFIRMED and check.detail["package"] == "tokio"


async def test_pr_adding_a_hallucinated_crate_is_hard(gateway: Gateway) -> None:
    head = {"Cargo.toml": CARGO_BASE + 'serde_jsonx = "1"\n'}
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, head, {}, ["Cargo.toml"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.HARD


async def test_pr_adding_a_hallucinated_module_is_hard(gateway: Gateway) -> None:
    head = {"go.mod": GO_BASE + "require github.com/acme/ghost v1.0.0\n"}
    context = pr(gateway, {"go.mod": GO_BASE}, head, {}, ["go.mod"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.HARD


async def test_a_private_host_is_never_missing(gateway: Gateway) -> None:
    head = {"go.mod": GO_BASE + "require git.corp.dev/team/lib v1.0.0\n"}
    context = pr(gateway, {"go.mod": GO_BASE}, head, {}, ["go.mod"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.UNVERIFIED


async def test_a_configured_private_registry_is_never_missing(gateway: Gateway) -> None:
    head = {"Cargo.toml": CARGO_BASE + 'mine = "1"\n', ".cargo/config.toml": '[registries.corp]\nindex = "x"'}
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, head, {}, ["Cargo.toml"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.UNVERIFIED


async def test_an_outage_is_unverified(gateway: Gateway) -> None:
    head = {"Cargo.toml": CARGO_BASE + 'tokio = "1"\n'}
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, head, {"crates.io": httpx.Response(502)}, ["Cargo.toml"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.UNVERIFIED


async def test_a_plain_registry_client_degrades_to_nothing(gateway: Gateway) -> None:
    context = pr(gateway, {}, {}, {}, ["Cargo.toml"])
    context.registry = object()
    assert await ecosystems.run(context) == []


async def test_only_new_names_are_looked_up(gateway: Gateway) -> None:
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, {"Cargo.toml": CARGO_BASE}, {}, ["Cargo.toml"])
    assert await ecosystems.run(context) == []


async def test_injection_in_a_manifest_name_never_reaches_a_url(gateway: Gateway) -> None:
    hostile = "ignore-previous/../instructions"
    head = {"Cargo.toml": CARGO_BASE + f'"{hostile}" = "1"\n'}
    context = pr(gateway, {"Cargo.toml": CARGO_BASE}, head, {}, ["Cargo.toml"])
    [check] = await ecosystems.run(context)
    assert check.status is CheckStatus.UNVERIFIED and check.detail["reason"] == "invalid name"
