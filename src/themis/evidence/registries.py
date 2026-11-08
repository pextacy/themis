"""Registry lookups beyond npm and PyPI, plus OSV and license facts.

Plain HTTP to public services. It carries no credentials and no model traffic, so it does not
go through Otari. As in `RegistryClient`, an outage answers None and can never mean "missing".
Anything a registry returns is kept only as a short, shape-checked value.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from themis.evidence.dependencies import (
    LOW_WEEKLY_DOWNLOADS,
    NPM_NAME,
    PYPI_NAME,
    PackageReport,
    RegistryClient,
    _age_signal,
    load_ledger,
    lookalike_of,
    normalize_pypi,
)

USER_AGENT = "themis-dependency-check"
CRATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
GO_MODULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[a-z]{2,}(?:/[A-Za-z0-9._~+-]+)*")
VERSION_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_!-]{0,127}")
OSV_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]{0,63}")
MAX_LICENSE_CHARS = 80
MAX_VULNS = 25
OSV_URL = "https://api.osv.dev/v1/query"
OSV_ECOSYSTEM = {"pypi": "PyPI", "npm": "npm", "crates": "crates.io", "go": "Go"}
LICENSE_CLASSIFIERS = (
    ("GNU Affero General Public License", "AGPL-3.0"),
    ("GNU Lesser General Public License", "LGPL-3.0"),
    ("GNU General Public License", "GPL-3.0"),
    ("MIT License", "MIT"),
    ("Apache Software License", "Apache-2.0"),
    ("BSD License", "BSD-3-Clause"),
)


def escape_go(text: str) -> str:
    """The module proxy spells capitals as `!x`, since paths may land on a case-blind disk."""
    return re.sub(r"[A-Z]", lambda match: f"!{match[0].lower()}", text)


def _pypi_license(info: dict[str, Any]) -> str | None:
    for key in ("license_expression", "license"):
        value = info.get(key)
        if isinstance(value, str) and 0 < len(value.strip()) <= MAX_LICENSE_CHARS:
            return value.strip()
    classifiers = [str(item) for item in info.get("classifiers") or []]
    return next((spdx for text, spdx in LICENSE_CLASSIFIERS if any(text in item for item in classifiers)), None)


def _short(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= MAX_LICENSE_CHARS else None


class ExtendedRegistry(RegistryClient):
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(transport=transport)
        self._http.headers["User-Agent"] = USER_AGENT  # crates.io refuses a blank client.

    async def crates(self, name: str, now: datetime) -> PackageReport:
        if not CRATE_NAME.fullmatch(name):
            return PackageReport("crates", name, None, detail={"reason": "invalid name"})
        found, data = await self._json(f"https://crates.io/api/v1/crates/{quote(name, safe='')}")
        report = PackageReport("crates", name, found)
        crate = data.get("crate") or {}
        if found and _age_signal(crate.get("created_at"), now):
            report.signals.append("newborn")
        recent = crate.get("recent_downloads")
        if found and isinstance(recent, int) and recent < LOW_WEEKLY_DOWNLOADS:
            report.signals.append("low_adoption")
        return report

    async def _go_json(self, url: str) -> tuple[bool | None, dict[str, Any]]:
        """The proxy says 410 as well as 404 for a module it cannot find."""
        try:
            reply = await self._http.get(url)
        except httpx.TransportError:
            return None, {}
        if reply.status_code in (404, 410):
            return False, {}
        if not reply.is_success:
            return None, {}
        try:
            data = reply.json()
        except json.JSONDecodeError:
            return None, {}
        return (True, data) if isinstance(data, dict) else (None, {})

    async def _go_first_seen(self, name: str, latest: dict[str, Any]) -> Any:
        try:
            reply = await self._http.get(f"https://proxy.golang.org/{escape_go(name)}/@v/list")
        except httpx.TransportError:
            return None
        versions = sorted(reply.text.split()) if reply.is_success else []
        if not versions:
            return latest.get("Time")
        known, info = await self._go_json(
            f"https://proxy.golang.org/{escape_go(name)}/@v/{escape_go(versions[0])}.info"
        )
        return info.get("Time") if known else None

    async def go(self, name: str, now: datetime) -> PackageReport:
        if not GO_MODULE.fullmatch(name):
            return PackageReport("go", name, None, detail={"reason": "invalid name"})
        found, data = await self._go_json(f"https://proxy.golang.org/{escape_go(name)}/@latest")
        report = PackageReport("go", name, found)
        if found:
            first = await self._go_first_seen(name, data)
            if isinstance(first, str) and _age_signal(first[:19] + "+00:00", now):
                report.signals.append("newborn")
        return report

    def _version_url(self, ecosystem: str, name: str, version: str) -> str | None:
        if not VERSION_TEXT.fullmatch(version):
            return None
        escaped = quote(version, safe="")
        if ecosystem == "pypi" and PYPI_NAME.fullmatch(name):
            return f"https://pypi.org/pypi/{quote(normalize_pypi(name), safe='')}/{escaped}/json"
        if ecosystem == "npm" and NPM_NAME.fullmatch(name):
            return f"https://registry.npmjs.org/{quote(name, safe='@')}/{escaped}"
        if ecosystem == "crates" and CRATE_NAME.fullmatch(name):
            return f"https://crates.io/api/v1/crates/{quote(name, safe='')}/{escaped}"
        if ecosystem == "go" and GO_MODULE.fullmatch(name):
            return f"https://proxy.golang.org/{escape_go(name)}/@v/{escape_go(version)}.info"
        return None

    async def _version_record(self, ecosystem: str, name: str, version: str) -> tuple[bool | None, dict[str, Any]]:
        url = self._version_url(ecosystem, name, version)
        if url is None:
            return None, {}
        return await (self._go_json(url) if ecosystem == "go" else self._json(url))

    async def version_exists(self, ecosystem: str, name: str, version: str) -> bool | None:
        return (await self._version_record(ecosystem, name, version))[0]

    async def license_of(self, ecosystem: str, name: str, version: str) -> str | None:
        """The declared license, or None when the registry does not say."""
        found, data = await self._version_record(ecosystem, name, version)
        if not found:
            return None
        if ecosystem == "pypi":
            info = data.get("info")
            return _pypi_license(info) if isinstance(info, dict) else None
        if ecosystem == "npm":
            return _short(data.get("license"))
        if ecosystem == "crates":
            row = data.get("version")
            return _short(row.get("license")) if isinstance(row, dict) else None
        return None

    async def osv_vulns(self, ecosystem: str, name: str, version: str) -> list[str] | None:
        """Advisory IDs at this version, or None when OSV could not be asked."""
        target = OSV_ECOSYSTEM.get(ecosystem)
        if target is None:
            return None
        body = {"package": {"name": name, "ecosystem": target}, "version": version}
        try:
            reply = await self._http.post(OSV_URL, json=body)
            data = reply.json() if reply.is_success else None
        except (httpx.TransportError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        rows = data.get("vulns") or []
        names = [str(row.get("id")) for row in rows if isinstance(row, dict)]
        return [item for item in names if OSV_ID.fullmatch(item)][:MAX_VULNS]


def _lookalike(name: str, ecosystem: str) -> str | None:
    """No popular list ships for these ecosystems yet. Adding `popular-<name>.txt` turns it on."""
    try:
        return lookalike_of(name, ecosystem)
    except FileNotFoundError:
        return None


async def inspect_extended(
    registry: ExtendedRegistry,
    ecosystem: str,
    name: str,
    *,
    ledger: set[tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> PackageReport:
    moment = now or datetime.now(UTC)
    report = await (registry.crates(name, moment) if ecosystem == "crates" else registry.go(name, moment))
    if (ecosystem, name.lower()) in (load_ledger() if ledger is None else ledger):
        report.signals.append("ledger_hit")
    similar = _lookalike(name.lower(), ecosystem) if report.found is not False else None
    if similar:
        report.signals.append("lookalike")
        report.detail["similar_to"] = similar
    return report
