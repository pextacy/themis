"""TEST DOUBLES for the evidence engine. Nothing here is evidence."""

from __future__ import annotations

import io
import tarfile
from typing import Any

import httpx

from tests.conftest import Gateway, make_settings
from themis.config import RepoPolicy
from themis.context import StageContext
from themis.evidence.base import EvidenceContext
from themis.evidence.dependencies import RegistryClient
from themis.intake.github import GitHubClient
from themis.models import Claim, ClaimSet, ClaimType, Submission, SubmissionKind

REPO = "acme/tinyhttp"
SOURCE = {
    "tinyhttp/__init__.py": "from .ranges import parse_range\n",
    "tinyhttp/ranges.py": "import os\n\nclass Client:\n    def get(self, timeout=1):\n        return timeout\n\n"
    "def parse_range(header, size):\n    return (0, size + 1)\n",
    "tests/test_ranges.py": "def test_ok():\n    assert True\n",
    "pyproject.toml": '[project]\nname = "tinyhttp"\nversion = "0.4.0"\ndependencies = ["httpx>=0.27"]\n',
    "cli.py": 'import argparse\nparser.add_argument("--verbose")\n',
}


def tarball(files: dict[str, str], *, big: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(f"acme-tinyhttp-abc/{path}")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        if big:
            raw = b"x" * (1024 * 1024 + 1)
            info = tarfile.TarInfo("acme-tinyhttp-abc/huge.js")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def github_transport(refs: dict[str, bytes | None], tags: list[str] | None = None, issues: tuple[int, ...] = ()):  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/tags"):
            return httpx.Response(200, json=[{"name": tag} for tag in tags or []])
        if "/tarball/" in path:
            archive = refs.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, content=archive) if archive is not None else httpx.Response(404)
        if "/issues/" in path:
            return httpx.Response(200 if int(path.rsplit("/", 1)[1]) in issues else 404, json={})
        if path == "/search/issues":
            return httpx.Response(200, json={"items": [
                {"number": 3, "title": "parse_range returns wrong byte count"}, {"number": 9, "title": "unrelated docs"},
            ]})  # fmt: skip
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def claim_for(text: str, quote: str, kind: ClaimType, target: str | None, index: int = 1) -> Claim:
    start = text.index(quote)
    return Claim(id=f"c{index}", type=kind, text=quote, span=(start, start + len(quote)), normalized_target=target)


def evidence_context(
    gateway: Gateway,
    body: str,
    claims: list[tuple[str, ClaimType, str | None]],
    *,
    kind: SubmissionKind = SubmissionKind.BUG_REPORT,
    refs: dict[str, bytes | None] | None = None,
    tags: list[str] | None = None,
    issues: tuple[int, ...] = (),
    registry: httpx.MockTransport | None = None,
    test_results: dict[str, Any] | None = None,
    **fields: Any,
) -> EvidenceContext:
    title = fields.pop("title", "Report")
    settings = make_settings(model_prover="acme/deepseek-coder")
    submission = Submission(id="acme-tinyhttp-1", repo=REPO, number=1, title=title, body=body,
                            contributor_key="gh:7", **fields)  # fmt: skip
    source = f"{title}\n\n{body}"
    made = [claim_for(source, quote, claim_type, target, index + 1)
            for index, (quote, claim_type, target) in enumerate(claims)]  # fmt: skip
    stage = StageContext(settings, RepoPolicy(), gateway.client(settings), submission)
    github = GitHubClient(settings, transport=github_transport(refs or {"main": tarball(SOURCE)}, tags, issues))
    return EvidenceContext(
        stage=stage,
        github=github,
        claims=ClaimSet(kind=kind, claims=made),
        registry=RegistryClient(transport=registry) if registry else None,
        test_results=test_results,
    )
