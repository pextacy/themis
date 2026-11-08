"""The dependency verifier for Rust crates and Go modules.

Same rules as `dependencies`: names come from parsing manifests at head and base, an outage
never means missing, and a project with a private source can never hold a package "missing".
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.dependencies import PackageReport
from themis.evidence.registries import inspect_extended
from themis.evidence.snapshot import Snapshot
from themis.models import Check, CheckStatus, Strength

MODULE = "ecosystems"
MANIFEST_NAMES = ("Cargo.toml", "go.mod")
CARGO_GROUPS = ("dependencies", "dev-dependencies", "build-dependencies")
PUBLIC_GO_HOSTS = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "golang.org", "google.golang.org", "gopkg.in", "go.uber.org",
    "k8s.io", "sigs.k8s.io", "go.opentelemetry.io", "honnef.co",
})  # fmt: skip
GO_REQUIRE_LINE = re.compile(r"^\s*(?:require\s+)?([^\s()]+)\s+(v\S+)(\s*//\s*indirect)?\s*$")
GO_REPLACE_LINE = re.compile(r"^\s*(?:replace\s+)?([^\s()]+)(?:\s+v\S+)?\s+=>")
GO_BLOCK = re.compile(r"^\s*(require|replace)\s*\(\s*$")
GO_PRIVATE = re.compile(r"\bGO(?:PRIVATE|NOPROXY|NOSUMDB)\b|\bGOPROXY=(?!https://proxy\.golang\.org)")


def _cargo_section(section: Any) -> dict[str, str | None]:
    """Crates from the public registry, with their version text. `package` is the real name."""
    found: dict[str, str | None] = {}
    if not isinstance(section, dict):
        return found
    for key, spec in section.items():
        if isinstance(spec, str):
            found[str(key)] = spec
        elif isinstance(spec, dict) and not {"path", "git", "workspace", "registry"} & set(spec):
            version = spec.get("version")
            found[str(spec.get("package", key))] = version if isinstance(version, str) else None
    return found


def cargo_requirements(text: str) -> dict[str, str | None]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    found: dict[str, str | None] = {}
    tables = [data, *(data.get("target") or {}).values()]
    for table in tables:
        for group in CARGO_GROUPS:
            found |= _cargo_section(table.get(group) if isinstance(table, dict) else None)
    workspace = data.get("workspace")
    return found | _cargo_section(workspace.get("dependencies") if isinstance(workspace, dict) else None)


def go_requirements(text: str) -> dict[str, str]:
    """Direct requirements with their versions. `replace` sends a module elsewhere, so it is dropped."""
    required: dict[str, str] = {}
    replaced: set[str] = set()
    block: str | None = None
    for raw in text.splitlines():
        line = raw.split("//")[0] if "indirect" not in raw else raw
        opened = GO_BLOCK.match(line)
        if opened:
            block = opened[1]
        elif line.strip() == ")":
            block = None
        elif block == "replace" or line.lstrip().startswith("replace "):
            if match := GO_REPLACE_LINE.match(line):
                replaced.add(match[1])
        elif block == "require" or line.lstrip().startswith("require "):
            if (match := GO_REQUIRE_LINE.match(line)) and not match[3]:
                required[match[1]] = match[2]
    return {name: version for name, version in required.items() if name not in replaced}


def manifest_requirements(snapshot: Snapshot) -> dict[str, dict[str, str | None]]:
    found: dict[str, dict[str, str | None]] = {"crates": {}, "go": {}}
    for path, text in snapshot.files.items():
        name = path.split("/")[-1]
        if "vendor/" in path or "target/" in path:
            continue
        if name == "Cargo.toml":
            found["crates"] |= cargo_requirements(text)
        elif name == "go.mod":
            found["go"] |= go_requirements(text)
    return found


def uses_private_source(snapshot: Snapshot) -> bool:
    """A module missing from the public proxy proves nothing if the project has its own."""
    for path, text in snapshot.files.items():
        name = path.split("/")[-1]
        if path.endswith((".cargo/config.toml", ".cargo/config")) and "[registries" in text:
            return True
        if name == "Cargo.toml" and "[registries" in text:
            return True
        if GO_PRIVATE.search(text) and name in ("go.env", ".envrc", "Makefile", "Dockerfile", ".env.example"):
            return True
        if ".github/workflows/" in path and GO_PRIVATE.search(text):
            return True
    return False


def _unprovable(report: PackageReport, private: bool) -> bool:
    if private:
        return True
    return report.ecosystem == "go" and report.name.split("/")[0] not in PUBLIC_GO_HOSTS


def to_check(report: PackageReport, private: bool) -> Check:
    detail = {"ecosystem": report.ecosystem, "package": report.name, "signals": report.signals,
              "risk": report.risk, **report.detail}  # fmt: skip
    if report.found is None:
        reason = detail.pop("reason", "registry gave no answer")
        return make_check(MODULE, CheckStatus.UNVERIFIED, reason=reason, **detail)
    if report.found:
        return make_check(MODULE, CheckStatus.CONFIRMED, **detail)
    if _unprovable(report, private):
        return make_check(MODULE, CheckStatus.UNVERIFIED, reason="may live on a private source", **detail)
    return make_check(MODULE, CheckStatus.CONTRADICTED, strength=Strength.HARD, **detail)


async def run(ctx: EvidenceContext) -> list[Check]:
    submission = ctx.stage.submission
    registry = ctx.registry
    if not submission.is_pull_request or not hasattr(registry, "crates"):
        return []
    if not any(path.split("/")[-1] in MANIFEST_NAMES for path in submission.changed_files):
        return []
    head, base = await ctx.primary(), await ctx.snapshot(await ctx.default_ref())
    if not head.complete or not base.complete:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="could not read the manifests")]
    now_deps, old_deps = manifest_requirements(head), manifest_requirements(base)
    private = uses_private_source(head)
    checks: list[Check] = []
    for ecosystem in ("crates", "go"):
        for name in sorted(set(now_deps[ecosystem]) - set(old_deps[ecosystem])):
            checks.append(to_check(await inspect_extended(registry, ecosystem, name), private))
    return checks
