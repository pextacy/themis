"""Do the files, symbols and flags a claim names exist? Deterministic."""

from __future__ import annotations

import builtins
import re
import sys
from dataclasses import dataclass
from typing import Any

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.snapshot import Snapshot, imported_roots
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength

MODULE = "symbols"
PATH_LIKE = re.compile(r"[\w.@-]+/(?:[\w.@-]+/?)*|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,8}")
LINE_REF = re.compile(r"^(?P<path>[\w./@-]+\.\w+):(?P<line>\d+)$")
FLAG = re.compile(r"^--?[A-Za-z][\w-]*$")
DOTTED = re.compile(r"^[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*$")
CALL = re.compile(r"^([A-Za-z_][\w.:]*)\(.*\)?$", re.DOTALL)
MEDIA_TYPES = ("application/", "text/", "image/", "audio/", "video/", "multipart/", "font/")
NON_SYMBOLS = frozenset({"NaN", "nan", "inf", "Infinity", "null", "NULL", "nil", "undefined", "true", "false", "None"})
BUILTINS = frozenset(dir(builtins))
TEST_NODE = re.compile(r"^(?P<path>[\w./-]+\.py)::(?P<name>[\w:]+)$")
MIN_NAME_LENGTH = 3


@dataclass(frozen=True)
class Reference:
    kind: str  # file, symbol, flag or line
    value: str
    path: str | None = None
    line: int | None = None


def parse_target(target: str, claim_type: ClaimType) -> Reference | None:
    """Turn a claim's target into something checkable. Anything else is left alone."""
    text = target.strip()
    call = CALL.match(text)
    if call:
        text = call[1]
    if node := TEST_NODE.match(text):
        return Reference("file", node["path"])
    if (line := LINE_REF.match(text)) and ".." not in text:
        return Reference("line", text, path=line["path"], line=int(line["line"]))
    if FLAG.match(text):
        return Reference("flag", text)
    if DOTTED.match(text) and claim_type is ClaimType.SYMBOL_EXISTS:
        return Reference("symbol", text.replace("::", "."))
    if claim_type is ClaimType.FILE_EXISTS and PATH_LIKE.fullmatch(text) and ".." not in text:
        return Reference("file", text)
    return None


def _symbol_parts(value: str) -> list[str]:
    return [part for part in value.split(".") if part]


def missing_parts(reference: Reference, snapshot: Snapshot) -> list[str]:
    """What this snapshot lacks. An empty list means everything named is there."""
    if reference.kind == "file":
        if snapshot.has_path(reference.value) or "/" not in reference.value and reference.value in snapshot.words:
            return []
        return [] if _dotted_symbol_present(reference.value, snapshot) else [reference.value]
    if reference.kind == "line":
        path = snapshot.resolve(reference.path or "")
        if path is None:
            return [reference.path or ""]
        return [] if (reference.line or 0) <= snapshot.files[path].count("\n") + 1 else [reference.value]
    if reference.kind == "flag":
        return [] if snapshot.contains(reference.value) else [reference.value]
    return [part for part in _symbol_parts(reference.value) if part not in snapshot.words]


def _dotted_symbol_present(value: str, snapshot: Snapshot) -> bool:
    """A model can file `Flask.run` under file_exists, because `.run` looks like an extension."""
    return "/" not in value and all(part in snapshot.words for part in _symbol_parts(value))


def is_external(reference: Reference, snapshot: Snapshot) -> bool:
    """`os.path.join`, `asyncio.TaskPool` or `OverflowError` belongs to someone else's code."""
    if reference.kind != "symbol":
        return False
    root = reference.value.split(".", 1)[0]
    if root in BUILTINS or root in NON_SYMBOLS or root.endswith(("Error", "Exception", "Warning")):
        return True
    return "." in reference.value and (root in sys.stdlib_module_names or root in imported_roots(snapshot))


def provable(reference: Reference) -> bool:
    """Whether absence of this kind of reference can mean the claim is false.

    A bare `poc.py` may be the author's file, `api/users` may be a route, `--items` may be the
    author's own option, and a new `test_x` may be the regression test they just wrote.
    """
    if reference.kind == "flag":
        return False
    if reference.kind in ("file", "line"):
        path = reference.path or reference.value
        directory, _, name = path.rstrip("/").rpartition("/")
        return bool(directory) and (path.endswith("/") or "." in name) and not path.startswith(MEDIA_TYPES)
    return not reference.value.split(".")[-1].startswith("test_")


def anchored_to_project(ctx: EvidenceContext, claim: Claim, reference: Reference) -> bool:
    """A bare `UserDict` or `FAILURES` could be anything. It needs dots or the text's own label."""
    if reference.kind != "symbol" or "." in reference.value:
        return True
    name = re.escape(reference.value)
    context = ctx.claim_context(claim)
    return (
        re.search(rf"\b(?:class|function|method)\s+`?{name}\b|\b{name}`?\s+(?:class|function|method)\b", context)
        is not None
    )


async def check_claim(ctx: EvidenceContext, claim: Claim) -> Check:
    reference = parse_target(claim.normalized_target or "", claim.type)
    if reference is None:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="no checkable target")
    if len(reference.value) < MIN_NAME_LENGTH:
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="target too short to search")

    searched: list[str] = []
    missing: list[str] = []
    complete = True
    for ref in await ctx.search_refs():
        snapshot = await ctx.snapshot(ref)
        searched.append(ref)
        missing = missing_parts(reference, snapshot)
        if not missing:
            return make_check(MODULE, CheckStatus.CONFIRMED, claim, ref=ref, target=reference.value)
        complete = complete and snapshot.complete and not is_external(reference, snapshot)

    detail: dict[str, Any] = {"target": reference.value, "searched": searched, "missing": missing}
    provable_here = complete and provable(reference) and anchored_to_project(ctx, claim, reference)
    if not provable_here or not ctx.may_be_hard(claim) or ctx.defined_by_author(missing[0].split(".")[-1]):
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason="absence not provable", **detail)
    return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=Strength.HARD, **detail)


async def run(ctx: EvidenceContext) -> list[Check]:
    wanted = (ClaimType.SYMBOL_EXISTS, ClaimType.FILE_EXISTS)
    return [await check_claim(ctx, claim) for claim in ctx.claims.claims if claim.type in wanted]
