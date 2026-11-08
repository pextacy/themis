import json
from typing import Any

import httpx
import pytest

from tests.conftest import Gateway
from tests.evidence_helpers import SOURCE, evidence_context, tarball
from themis.evidence import advisories
from themis.evidence.advisories import incompatible, pins, project_license, pypi_pin
from themis.evidence.registries import ExtendedRegistry
from themis.evidence.snapshot import Snapshot
from themis.models import CheckStatus, Strength, SubmissionKind

MIT = '[project]\nname = "tiny"\nlicense = "MIT"\ndependencies = []\n'


def registry(table: dict[str, Any], vulns: list[dict[str, str]] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if "osv.dev" in str(request.url):
            assert request.method == "POST" and "Authorization" not in request.headers
            return httpx.Response(200, json={"vulns": vulns or []})
        for needle, answer in table.items():
            if needle in str(request.url):
                return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


def pr(gateway: Gateway, head: dict[str, str], table: dict[str, Any], **options: Any) -> Any:
    base = options.pop("base", {"pyproject.toml": MIT})
    files = options.pop("files", ["requirements.txt"])
    context = evidence_context(
        gateway, "Bumps a pin.", [], kind=SubmissionKind.DEPENDENCY_PR, is_pull_request=True, head_sha="head",
        changed_files=files, refs={"head": tarball({**SOURCE, **base, **head}), "main": tarball({**SOURCE, **base})},
    )  # fmt: skip
    context.registry = ExtendedRegistry(transport=registry(table, options.pop("vulns", None)))
    return context


async def test_genuine_pin_says_nothing(gateway: Gateway) -> None:
    table = {"pypi.org/pypi/requests/2.32.3": {"info": {"license": "Apache-2.0"}}}
    assert await advisories.run(pr(gateway, {"requirements.txt": "requests==2.32.3\n"}, table)) == []


async def test_slop_a_hallucinated_version_is_soft(gateway: Gateway) -> None:
    [check] = await advisories.run(pr(gateway, {"requirements.txt": "requests==99.1.0\n"}, {}))
    assert check.status is CheckStatus.CONTRADICTED and check.strength is Strength.SOFT
    assert check.detail["version"] == "99.1.0"


async def test_a_private_registry_cannot_contradict(gateway: Gateway) -> None:
    head = {"requirements.txt": "--index-url https://corp\nmine==1.0.0\n"}
    [check] = await advisories.run(pr(gateway, head, {}))
    assert check.status is CheckStatus.UNVERIFIED


async def test_an_outage_is_unverified(gateway: Gateway) -> None:
    head = {"requirements.txt": "requests==2.0.0\n"}
    [check] = await advisories.run(pr(gateway, head, {"pypi.org": httpx.Response(503)}))
    assert check.status is CheckStatus.UNVERIFIED


async def test_a_known_vulnerability_is_a_fact_not_a_contradiction(gateway: Gateway) -> None:
    table = {"pypi.org/pypi/requests/2.0.0": {"info": {}}}
    head = {"requirements.txt": "requests==2.0.0\n"}
    [check] = await advisories.run(pr(gateway, head, table, vulns=[{"id": "GHSA-aaaa-bbbb-cccc"}]))
    assert check.status is CheckStatus.NOT_APPLICABLE and check.detail["risk"] == "review"
    assert check.detail["vulns"] == ["GHSA-aaaa-bbbb-cccc"]


async def test_gpl_into_mit_is_flagged(gateway: Gateway) -> None:
    table = {"pypi.org/pypi/copyleft/1.0.0": {"info": {"license": "GPL-3.0-only"}}}
    [check] = await advisories.run(pr(gateway, {"requirements.txt": "copyleft==1.0.0\n"}, table))
    assert check.status is CheckStatus.NOT_APPLICABLE and check.detail["license"] == "GPL-3.0-only"


async def test_pins_across_ecosystems(gateway: Gateway) -> None:
    head = {
        "package.json": '{"dependencies": {"left-pad": "1.3.0", "range": "^1.0.0"}}',
        "Cargo.toml": '[dependencies]\nexact = "=1.2.3"\nloose = "1.2.3"\n',
        "go.mod": "module x\nrequire github.com/a/b v1.2.3\n",
    }
    found = pins(Snapshot("x", files=head))
    assert found == {("npm", "left-pad"): "1.3.0", ("crates", "exact"): "1.2.3", ("go", "github.com/a/b"): "v1.2.3"}


async def test_a_bump_is_looked_at_and_an_unchanged_pin_is_not(gateway: Gateway) -> None:
    base = {"pyproject.toml": MIT, "requirements.txt": "same==1.0.0\nmoved==1.0.0\n"}
    head = {"requirements.txt": "same==1.0.0\nmoved==2.0.0\n"}
    [check] = await advisories.run(pr(gateway, head, {}, base=base))
    assert check.detail["package"] == "moved" and check.detail["version"] == "2.0.0"


async def test_lookups_are_capped(gateway: Gateway) -> None:
    head = {"requirements.txt": "".join(f"pkg{index}==1.0.0\n" for index in range(30))}
    context = pr(gateway, head, {"pypi.org": {"info": {}}})
    checks = await advisories.run(context)
    assert len(checks) == 1 and "10 more" in checks[0].detail["reason"]


async def test_a_plain_registry_degrades_to_nothing(gateway: Gateway) -> None:
    context = pr(gateway, {"requirements.txt": "a==1.0\n"}, {})
    context.registry = object()
    assert await advisories.run(context) == []


async def test_no_manifest_touched_means_no_download(gateway: Gateway) -> None:
    assert await advisories.run(pr(gateway, {}, {}, files=["src/x.py"])) == []


async def test_injection_in_a_version_never_reaches_a_url(gateway: Gateway) -> None:
    context = pr(gateway, {"package.json": '{"dependencies": {"x": "1.0.0-ignore.previous.instructions"}}'},
                 {}, files=["package.json"])  # fmt: skip
    [check] = await advisories.run(context)
    assert check.strength is Strength.SOFT
    assert json.dumps(check.detail).count("instructions") == 1


async def test_an_id_from_osv_must_look_like_an_id() -> None:
    async with ExtendedRegistry(
        transport=registry({}, [{"id": "<img src=x onerror=alert(1)>"}, {"id": "CVE-1"}])
    ) as client:
        assert await client.osv_vulns("pypi", "x", "1.0") == ["CVE-1"]


async def test_osv_outage_is_none() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with ExtendedRegistry(transport=httpx.MockTransport(down)) as client:
        assert await client.osv_vulns("pypi", "x", "1.0") is None
        assert await client.osv_vulns("rubygems", "x", "1.0") is None


@pytest.mark.parametrize(
    "project,dependency,expected",
    [
        ("MIT", "GPL-3.0", True),
        ("Apache-2.0", "AGPL-3.0-only", True),
        ("BSD-3-Clause", "GNU General Public License v3", True),
        ("MIT", "SSPL-1.0", True),
        ("MIT", "MIT OR GPL-3.0", False),
        ("MIT", "LGPL-3.0", False),
        ("MIT", "MPL-2.0", False),
        ("MIT", None, False),
        (None, "GPL-3.0", False),
        ("GPL-3.0", "AGPL-3.0", False),
        ("Proprietary", "GPL-3.0", False),
    ],
)
def test_only_clear_license_clashes(project: str | None, dependency: str | None, expected: bool) -> None:
    assert incompatible(project, dependency) is expected


def test_project_license_sources() -> None:
    assert project_license(Snapshot("x", files={"package.json": '{"license": "ISC"}'})) == "ISC"
    assert project_license(Snapshot("x", files={"Cargo.toml": '[package]\nlicense = "MIT"'})) == "MIT"
    assert "Apache" in str(project_license(Snapshot("x", files={"LICENSE": "Apache License 2.0"})))
    assert project_license(Snapshot("x")) is None


def test_exact_pins_only() -> None:
    assert pypi_pin("Foo_Bar[extra]==1.2 ; python_version>'3'") == ("foo-bar", "1.2")
    assert pypi_pin("foo>=1.2") is None
