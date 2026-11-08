"""Facts about the exact versions a PR pins: do they exist, are they known bad, may we use them?

Only exact pins are read. `^1.2` or `>=1.2` names a range, and a range has no one version to
check. A pin that does not exist is a SOFT contradiction. A known vulnerability or a license
clash is a fact for the maintainer, not a claim anyone made, so it is NOT_APPLICABLE and adds
`risk: review`, which never moves the verdict. Unknown licenses say nothing.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.dependencies import LOCAL_SPEC, NPM_GROUPS, normalize_pypi, uses_private_registry
from themis.evidence.ecosystems import cargo_requirements, go_requirements, uses_private_source
from themis.evidence.snapshot import Snapshot
from themis.models import Check, CheckStatus, Strength

MODULE = "advisories"
MAX_LOOKUPS = 20
Pin = tuple[str, str]  # (ecosystem, name)
MANIFEST_NAMES = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][\w.+-]+)?")
EXACT_PYTHON = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9][\w.!+-]*)\s*(?:[;#].*)?$"
)
COPYLEFT = re.compile(r"(?<![L\w-])(?:A?GPL|SSPL)|GNU (?:AFFERO )?GENERAL PUBLIC", re.I)
WEAK = re.compile(r"LGPL|LESSER", re.I)
PERMISSIVE = re.compile(r"\b(?:MIT|APACHE|BSD|ISC|ZLIB|UNLICENSE|0BSD)\b", re.I)
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")


def pypi_pin(line: str) -> tuple[str, str] | None:
    match = EXACT_PYTHON.match(line)
    return (normalize_pypi(match[1]), match[2]) if match else None


def _npm_pins(text: str) -> dict[Pin, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    pins: dict[Pin, str] = {}
    for group in NPM_GROUPS:
        section = data.get(group) if isinstance(data, dict) else None
        if isinstance(section, dict):
            for name, spec in section.items():
                if isinstance(spec, str) and SEMVER.fullmatch(spec) and not LOCAL_SPEC.match(spec):
                    pins[("npm", str(name))] = spec
    return pins


def _python_pins(lines: list[str]) -> dict[Pin, str]:
    found = (pypi_pin(line) for line in lines)
    return {("pypi", pin[0]): pin[1] for pin in found if pin}


def _pyproject_pins(text: str) -> dict[Pin, str]:
    try:
        project = tomllib.loads(text).get("project") or {}
    except tomllib.TOMLDecodeError:
        return {}
    specs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group)
    return _python_pins([str(spec) for spec in specs])


def _cargo_pins(text: str) -> dict[Pin, str]:
    """Cargo reads a bare `1.2.3` as `^1.2.3`. Only `=1.2.3` is exact."""
    found = cargo_requirements(text).items()
    return {("crates", name): spec[1:].strip() for name, spec in found if spec and spec.startswith("=")}


def pins(snapshot: Snapshot) -> dict[Pin, str]:
    found: dict[Pin, str] = {}
    for path, text in snapshot.files.items():
        name = path.split("/")[-1]
        if "node_modules/" in path or "vendor/" in path:
            continue
        if name == "package.json":
            found |= _npm_pins(text)
        elif name == "pyproject.toml":
            found |= _pyproject_pins(text)
        elif name.startswith("requirements") and name.endswith(".txt"):
            found |= _python_pins(text.splitlines())
        elif name == "Cargo.toml":
            found |= _cargo_pins(text)
        elif name == "go.mod":
            found |= {("go", module): version for module, version in go_requirements(text).items()}
    return found


def _declared(snapshot: Snapshot) -> str | None:
    """The project's license, as a manifest says it."""
    if text := snapshot.files.get("package.json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        value = data.get("license") if isinstance(data, dict) else None
        if isinstance(value, str):
            return value
    for name in ("pyproject.toml", "Cargo.toml"):
        if text := snapshot.files.get(name):
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            table: Any = data.get("project") or data.get("package") or {}
            value = table.get("license") if isinstance(table, dict) else None
            if isinstance(value, dict):
                value = value.get("text")
            if isinstance(value, str):
                return value
    return None


def project_license(snapshot: Snapshot) -> str | None:
    if declared := _declared(snapshot):
        return declared
    for name in LICENSE_FILES:
        if text := snapshot.files.get(name):
            return text[:300]
    return None


def is_permissive(license_text: str) -> bool:
    return PERMISSIVE.search(license_text) is not None and COPYLEFT.search(license_text) is None


def incompatible(project: str | None, dependency: str | None) -> bool:
    """Only the clear case: a strong copyleft package with no other option in a permissive project.

    "MIT OR GPL-3.0" lets the project take MIT. LGPL and MPL depend on how a package is linked,
    and a copyleft project can take either side, so those stay silent.
    """
    if not project or not dependency or not is_permissive(project):
        return False
    options = re.split(r"\s+OR\s+|/", dependency, flags=re.I)
    return all(COPYLEFT.search(item) and not WEAK.search(item) for item in options)


def changed_pins(head: Snapshot, base: Snapshot) -> dict[Pin, str]:
    old = pins(base)
    return {key: version for key, version in sorted(pins(head).items()) if old.get(key) != version}


async def check_pin(
    ctx: EvidenceContext, pin: Pin, version: str, license_text: str | None, private: bool
) -> list[Check]:
    ecosystem, name = pin
    detail: dict[str, Any] = {"ecosystem": ecosystem, "package": name, "version": version}
    known = await ctx.registry.version_exists(ecosystem, name, version)
    if known is None:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="registry gave no answer", **detail)]
    if not known:
        if private or name.startswith("@"):
            return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="may live on a private registry", **detail)]
        reason = "pinned version does not exist"
        return [make_check(MODULE, CheckStatus.CONTRADICTED, strength=Strength.SOFT, reason=reason, **detail)]
    checks: list[Check] = []
    vulns = await ctx.registry.osv_vulns(ecosystem, name, version)
    if vulns:
        reason = f"{len(vulns)} known vulnerabilities at this version"
        checks.append(
            make_check(MODULE, CheckStatus.NOT_APPLICABLE, reason=reason, risk="review", vulns=vulns, **detail)
        )
    dependency = await ctx.registry.license_of(ecosystem, name, version)
    if incompatible(license_text, dependency):
        reason = "license does not fit the project's"
        checks.append(
            make_check(MODULE, CheckStatus.NOT_APPLICABLE, reason=reason, risk="review", license=dependency, **detail)
        )
    return checks


async def run(ctx: EvidenceContext) -> list[Check]:
    submission = ctx.stage.submission
    if not submission.is_pull_request or not hasattr(ctx.registry, "version_exists"):
        return []
    touched = (path.split("/")[-1] for path in submission.changed_files)
    if not any(name in MANIFEST_NAMES or name.startswith("requirements") for name in touched):
        return []
    head, base = await ctx.primary(), await ctx.snapshot(await ctx.default_ref())
    if not head.complete or not base.complete:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="could not read the manifests")]
    changed = changed_pins(head, base)
    private = uses_private_registry(head) or uses_private_source(head)
    license_text = project_license(head)
    checks: list[Check] = []
    for pin, version in list(changed.items())[:MAX_LOOKUPS]:
        checks.extend(await check_pin(ctx, pin, version, license_text, private))
    if len(changed) > MAX_LOOKUPS:
        unread = len(changed) - MAX_LOOKUPS
        checks.append(make_check(MODULE, CheckStatus.NOT_APPLICABLE, reason=f"{unread} more pins were not looked up"))
    return checks
