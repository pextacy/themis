"""Reproduce behavior claims in Otari's sandbox.

The model writes only a probe. We assemble the script around the project's real source and
read the result from a marker carrying a nonce the model never saw, so REPRODUCED comes from
execution and not from anything a model said. Not reproducing never counts against a claim.
"""

from __future__ import annotations

import ast
import re
import secrets

from themis.agents import prover
from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.snapshot import MAX_AST_BYTES, Snapshot
from themis.models import Check, CheckStatus, Claim, ClaimType
from themis.otari.errors import OtariBudgetExceeded, OtariConfigError, OtariError

MODULE = "repro"
MAX_CLAIMS = 2
MAX_SOURCE_CHARS = 12_000
RELATIVE_IMPORT = re.compile(r"^\s*from\s+\.+[\w.]*\s+import\s+.*$|^\s*import\s+\.+.*$", re.MULTILINE)
FORBIDDEN_MODULES = frozenset({"socket", "subprocess", "urllib", "http", "requests", "httpx", "ftplib", "ctypes"})
REPRODUCED_KINDS = ("bug_report", "security_report", "bugfix_pr")


def defined_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return set()
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}


def calls_source(probe: str, names: set[str]) -> bool:
    """A probe that never calls the project's code cannot reproduce anything."""
    try:
        tree = ast.parse(probe)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return bool(called & names)


def uses_forbidden_module(probe: str) -> bool:
    tree = ast.parse(probe)
    for node in ast.walk(tree):
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        if any(name.split(".")[0] in FORBIDDEN_MODULES for name in names):
            return True
    return False


def assemble(source: str, probe: str, nonce: str) -> str:
    """The probe runs in a function so it cannot print over the marker from the top level."""
    body = "\n".join(f"    {line}" for line in probe.splitlines())
    return (
        f"{source}\n\n"
        "def __themis_probe():\n"
        "    reproduced = None\n"
        f"{body}\n"
        "    return reproduced\n\n"
        f"print('THEMIS_RESULT_{nonce}=' + repr(__themis_probe()))\n"
    )


def read_marker(stdout: str, nonce: str) -> bool | None:
    found = re.findall(rf"^THEMIS_RESULT_{nonce}=(True|False)$", stdout, re.MULTILINE)
    return found[-1] == "True" if len(found) == 1 else None


def select_source(claim: Claim, snapshot: Snapshot) -> str | None:
    """The file defining the claim's target, minus imports that cannot resolve in a sandbox."""
    name = (claim.normalized_target or "").split("(")[0].split(".")[-1]
    path = snapshot.defining_file(name) if name else None
    if path is None:
        return None
    text = RELATIVE_IMPORT.sub("", snapshot.files[path])
    return text if len(text) <= min(MAX_SOURCE_CHARS, MAX_AST_BYTES) else None


async def check_claim(ctx: EvidenceContext, claim: Claim, snapshot: Snapshot) -> Check:
    source = select_source(claim, snapshot)
    names = defined_names(source or "")
    if source is None or not names:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="no self-contained source for this claim")
    probe, request_id = await prover.write_probe(ctx.stage, claim, source)
    if probe is None:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="invalid Prover output after one repair")
    if not calls_source(probe, names):
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="probe does not call the project's code")
    if uses_forbidden_module(probe):
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="probe uses a forbidden module")
    nonce = secrets.token_hex(8)
    result, call = await ctx.stage.client.code_execute(
        assemble(source, probe, nonce), user=ctx.stage.submission.contributor_key
    )
    observed = read_marker(result.stdout, nonce) if result.return_code == 0 else None
    status = CheckStatus.REPRODUCED if observed is True else CheckStatus.UNVERIFIED
    check = make_check(MODULE, status, claim, exit_code=result.return_code, observed=observed,
                       stdout=result.stdout[:500], ref=snapshot.ref)  # fmt: skip
    return check.model_copy(update={"otari_request_id": call.request_id or request_id})


async def run(ctx: EvidenceContext) -> list[Check]:
    if ctx.claims.kind.value not in REPRODUCED_KINDS:
        return []
    claims = [claim for claim in ctx.claims.claims if claim.type is ClaimType.BEHAVIOR][:MAX_CLAIMS]
    if not claims:
        return []
    snapshot = await ctx.primary()
    checks: list[Check] = []
    for claim in claims:
        try:
            checks.append(await check_claim(ctx, claim, snapshot))
        except (OtariBudgetExceeded, OtariConfigError):
            raise
        except OtariError as error:
            checks.append(make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason=str(error)))
    return checks
