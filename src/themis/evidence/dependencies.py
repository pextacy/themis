"""Anti-slopsquatting: do the packages a PR adds exist, and do they look safe?

New names come from parsing the manifests at head and at base, never from diff lines, so a
script entry is not mistaken for a package. A registry outage or a private registry can
never make a package "missing".
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.snapshot import Snapshot
from themis.models import Check, CheckStatus, Strength

MODULE = "dependencies"
MANIFEST_NAMES = ("package.json", "pyproject.toml", "requirements.txt")
LEDGER_PATH = Path("data/hallucination-ledger.jsonl")
NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}")
PYPI_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,200}[A-Za-z0-9])?")
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
LOCAL_SPEC = re.compile(r"^(?:file:|link:|workspace:|portal:|git[+:]|https?:|npm:|github:|[\w-]+/[\w.-]+$)")
NPM_GROUPS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
INSTALL_SCRIPTS = ("preinstall", "install", "postinstall")
NEWBORN_DAYS = 30
LOW_WEEKLY_DOWNLOADS = 100
LOOKALIKE_DISTANCE = 2
MIN_LOOKALIKE_LENGTH = 5


@dataclass
class PackageReport:
    ecosystem: str
    name: str
    found: bool | None
    signals: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def risk(self) -> str:
        if self.found is False or "ledger_hit" in self.signals:
            return "block"
        return "review" if self.signals else "none"


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _popular(ecosystem: str) -> frozenset[str]:
    text = resources.files("themis.evidence.data").joinpath(f"popular-{ecosystem}.txt").read_text()
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def edit_distance(left: str, right: str, limit: int = LOOKALIKE_DISTANCE) -> int:
    """Levenshtein, giving up early once the distance passes limit."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, char in enumerate(left, 1):
        current = [row]
        for column, other in enumerate(right, 1):
            current.append(min(previous[column] + 1, current[-1] + 1, previous[column - 1] + (char != other)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def lookalike_of(name: str, ecosystem: str) -> str | None:
    popular = _popular(ecosystem)
    lowered = name.lower()
    if lowered in popular:
        return None
    if len(lowered) >= MIN_LOOKALIKE_LENGTH:
        for candidate in sorted(popular):
            if edit_distance(lowered, candidate) <= LOOKALIKE_DISTANCE:
                return candidate
    parts = [part for part in re.split(r"[-_.]", lowered) if len(part) >= 3]
    return "+".join(parts[:2]) if len(parts) >= 2 and sum(part in popular for part in parts) >= 2 else None


def load_ledger(path: Path = LEDGER_PATH) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {(row["ecosystem"], row["name"].lower()) for row in rows}


def _age_signal(first_seen: str | None, now: datetime) -> bool:
    if not first_seen:
        return False
    published = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
    return now - published < timedelta(days=NEWBORN_DAYS)


class RegistryClient:
    """Plain HTTP to public package registries. Carries no credentials and no model traffic."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(timeout=20, transport=transport, follow_redirects=True)

    async def __aenter__(self) -> RegistryClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def _json(self, url: str) -> tuple[bool | None, dict[str, Any]]:
        """(True, data) when found, (False, {}) on a real 404, (None, {}) when unsure."""
        try:
            reply = await self._http.get(url)
        except httpx.TransportError:
            return None, {}
        if reply.status_code == 404:
            return False, {}
        if not reply.is_success:
            return None, {}
        try:
            data = reply.json()
        except json.JSONDecodeError:
            return None, {}
        return (True, data) if isinstance(data, dict) else (None, {})

    async def pypi(self, name: str, now: datetime) -> PackageReport:
        if not PYPI_NAME.fullmatch(name):
            return PackageReport("pypi", name, None, detail={"reason": "invalid name"})
        found, data = await self._json(f"https://pypi.org/pypi/{quote(normalize_pypi(name), safe='')}/json")
        report = PackageReport("pypi", name, found)
        if found:
            uploads = [item["upload_time_iso_8601"] for files in data.get("releases", {}).values() for item in files]
            if _age_signal(min(uploads, default=None), now):
                report.signals.append("newborn")
        return report

    async def npm(self, name: str, now: datetime) -> PackageReport:
        if not NPM_NAME.fullmatch(name):
            return PackageReport("npm", name, None, detail={"reason": "invalid name"})
        found, data = await self._json(f"https://registry.npmjs.org/{quote(name, safe='@')}")
        report = PackageReport("npm", name, found)
        if not found:
            return report
        if _age_signal((data.get("time") or {}).get("created"), now):
            report.signals.append("newborn")
        latest = (data.get("versions") or {}).get((data.get("dist-tags") or {}).get("latest"), {})
        if set(latest.get("scripts") or {}) & set(INSTALL_SCRIPTS):
            report.signals.append("install_scripts")
        report.detail["maintainers"] = len(data.get("maintainers") or [])
        known, downloads = await self._json(f"https://api.npmjs.org/downloads/point/last-week/{quote(name, safe='@')}")
        if known and downloads.get("downloads", LOW_WEEKLY_DOWNLOADS) < LOW_WEEKLY_DOWNLOADS:
            report.signals.append("low_adoption")
        return report


async def inspect(registry: RegistryClient, ecosystem: str, name: str, *, ledger: set[tuple[str, str]] | None = None,
                  now: datetime | None = None) -> PackageReport:  # fmt: skip
    moment = now or datetime.now(UTC)
    report = await (registry.pypi(name, moment) if ecosystem == "pypi" else registry.npm(name, moment))
    key = normalize_pypi(name) if ecosystem == "pypi" else name.lower()
    if (ecosystem, key) in (load_ledger() if ledger is None else ledger):
        report.signals.append("ledger_hit")
    similar = lookalike_of(key, ecosystem) if report.found is not False else None
    if similar:
        report.signals.append("lookalike")
        report.detail["similar_to"] = similar
    return report


def npm_dependencies(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for group in NPM_GROUPS:
        section = data.get(group) if isinstance(data, dict) else None
        if isinstance(section, dict):
            names |= {str(name) for name, spec in section.items() if not LOCAL_SPEC.match(str(spec))}
    return names


def requirements_dependencies(text: str) -> set[str]:
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line or line.startswith((".", "/")):
            continue
        match = REQUIREMENT.match(line)
        if match:
            names.add(normalize_pypi(match[1]))
    return names


def pyproject_dependencies(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    project = data.get("project") or {}
    specs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group)
    return requirements_dependencies("\n".join(str(spec) for spec in specs))


def manifest_dependencies(snapshot: Snapshot) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {"npm": set(), "pypi": set()}
    for path, text in snapshot.files.items():
        name = path.split("/")[-1]
        if "node_modules/" in path:
            continue
        if name == "package.json":
            found["npm"] |= npm_dependencies(text)
        elif name == "pyproject.toml":
            found["pypi"] |= pyproject_dependencies(text)
        elif name.startswith("requirements") and name.endswith(".txt"):
            found["pypi"] |= requirements_dependencies(text)
    return found


def uses_private_registry(snapshot: Snapshot) -> bool:
    """A name missing from the public registry proves nothing if the project has its own."""
    for path, text in snapshot.files.items():
        name = path.split("/")[-1]
        if name in (".npmrc", ".yarnrc.yml", "pip.conf", ".pypirc") and "registry" in text + name:
            return True
        if name == "pyproject.toml" and re.search(r"\[\[?tool\.(?:uv\.index|poetry\.source)", text):
            return True
        if name.startswith("requirements") and re.search(r"--(?:extra-)?index-url", text):
            return True
    return False


async def run(ctx: EvidenceContext) -> list[Check]:
    submission = ctx.stage.submission
    if not submission.is_pull_request or ctx.registry is None:
        return []
    manifests = [path for path in submission.changed_files if path.split("/")[-1] in MANIFEST_NAMES
                 or "requirements" in path.split("/")[-1]]  # fmt: skip
    if not manifests:
        return []
    head, base = await ctx.primary(), await ctx.snapshot(await ctx.default_ref())
    if not head.complete or not base.complete:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="could not read the manifests")]
    now_deps, old_deps = manifest_dependencies(head), manifest_dependencies(base)
    private = uses_private_registry(head)
    checks: list[Check] = []
    for ecosystem in ("pypi", "npm"):
        for name in sorted(now_deps[ecosystem] - old_deps[ecosystem]):
            report = await inspect(ctx.registry, ecosystem, name)
            checks.append(_to_check(report, private))
    return checks


def _to_check(report: PackageReport, private: bool) -> Check:
    detail = {"ecosystem": report.ecosystem, "package": report.name, "signals": report.signals,
              "risk": report.risk, **report.detail}  # fmt: skip
    if report.found is None:
        return make_check(MODULE, CheckStatus.UNVERIFIED, **{"reason": "registry gave no answer", **detail})
    if report.found:
        return make_check(MODULE, CheckStatus.CONFIRMED, **detail)
    if private or report.name.startswith("@"):
        return make_check(MODULE, CheckStatus.UNVERIFIED, reason="may live on a private registry", **detail)
    return make_check(MODULE, CheckStatus.CONTRADICTED, strength=Strength.HARD, **detail)
