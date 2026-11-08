"""Does a pasted stack trace fit the project's code? Deterministic and SOFT at most.

A trace is the most expensive thing to read and the easiest to invent. Each frame that belongs
to this project is checked across the same refs as symbols: the file exists, the line is in
range, the function is named in the file. An old release can differ from every ref we read,
so a mismatch is never HARD. Frame text is untrusted: it is matched, truncated and stored as
data, and it never picks a path outside the snapshots held in memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from themis.evidence.base import EvidenceContext, make_check
from themis.evidence.snapshot import Snapshot
from themis.models import Check, CheckStatus, Claim, ClaimType, Strength

MODULE = "tracebacks"
MAX_FRAMES = 40
MAX_LINE_CHARS = 1000
MAX_SHOWN = 5
MAX_DETAIL_CHARS = 200

PYTHON = re.compile(r'^\s*File "([^"\n]{1,400})", line (\d{1,7})(?:, in (\S{1,200}))?')
JAVA = re.compile(r"^\s*at ([\w$.<>]{1,300})\(([\w$]{1,200}\.(?:java|kt|scala)):(\d{1,7})\)")
NODE = re.compile(r"^\s*at (?:async )?(?:(.{1,200}?) \()?((?:file://)?[^\s()]{1,400}):(\d{1,7}):\d{1,7}\)?\s*$")
GO_PLACE = re.compile(r"^\s+(\S{1,400}\.go):(\d{1,7})(?: \+0x[0-9a-f]+)?\s*$")
GO_CALL = re.compile(r"^([\w./*()-]{1,300})\(.*\)\s*$")
RUST_CALL = re.compile(r"^\s*\d+:\s+(\S.{0,300}?)\s*$")
RUST_PANIC = re.compile(r"panicked at .{0,300}?([\w./-]{1,300}\.rs):(\d{1,7}):\d{1,7}")
SCRIPT_FILE = re.compile(r"\.(?:[cm]?[jt]s|[jt]sx|rs)$")
JAVA_EXTERNAL = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "org.junit.", "kotlin.", "scala.")
EXTERNAL_PATH = re.compile(
    r"site-packages|dist-packages|node_modules|/lib/python|python\d\.\d+/|/usr/|/opt/|/rustc/|/\.cargo/|/\.rustup/|"
    r"/pkg/mod/|/runtime/|^runtime/|^internal/|^node:|^<|https?://|/\.?venv/|/\.pyenv/|/Library/|/vendor/|^vendor/|"
    r"(?:^|/)(?:dist|build|target)/|\.min\.js$"
)
NAME = re.compile(r"[A-Za-z_$][\w$]*")
UNNAMED = frozenset({"module", "lambda", "listcomp", "dictcomp", "setcomp", "genexpr", "anonymous", "init", "clinit",
                     "closure", "main_body", "unknown"})  # fmt: skip
GENERATED_NAME = re.compile(r"^(?:h[0-9a-f]{16}|func\d+|lambda\$.*|gowrap\d+)$")


@dataclass(frozen=True)
class Frame:
    path: str
    line: int
    function: str | None = None


def simple_name(raw: str | None) -> str | None:
    """The bare function name, or None for frames with no name to look for."""
    if not raw:
        return None
    text = re.sub(r"\s*\[as .*\]$", "", raw.strip())
    names = [item for item in NAME.findall(text) if item not in ("new", "async") and not GENERATED_NAME.match(item)]
    return None if not names or names[-1] in UNNAMED else names[-1]


def _python(line: str) -> Frame | None:
    match = PYTHON.match(line)
    return Frame(match[1], int(match[2]), simple_name(match[3])) if match else None


def _java(line: str) -> Frame | None:
    match = JAVA.match(line)
    if match is None or match[1].startswith(JAVA_EXTERNAL):
        return None
    parts = match[1].split(".")
    return Frame("/".join([*parts[:-2], match[2]]), int(match[3]), simple_name(parts[-1]))


def _node(line: str, previous: str) -> Frame | None:
    match = NODE.match(line)
    if match is None or not SCRIPT_FILE.search(match[2]):
        return None
    called = match[1]
    if match[2].endswith(".rs") and (rust := RUST_CALL.match(previous)):
        called = rust[1]
    return Frame(match[2].removeprefix("file://"), int(match[3]), simple_name(called))


def _go(line: str, previous: str) -> Frame | None:
    match = GO_PLACE.match(line)
    if match is None:
        return None
    call = GO_CALL.match(previous)
    return Frame(match[1], int(match[2]), simple_name(call[1]) if call else None)


def _rust_panic(line: str) -> Frame | None:
    match = RUST_PANIC.search(line)
    return Frame(match[1], int(match[2])) if match else None


def parse_frames(text: str) -> list[Frame]:
    """Every frame found, in order and without repeats. Long lines are skipped, not parsed."""
    frames: list[Frame] = []
    previous = ""
    for line in text.splitlines():
        if len(line) <= MAX_LINE_CHARS:
            frame = _python(line) or _java(line) or _node(line, previous) or _go(line, previous) or _rust_panic(line)
            if frame:
                frames.append(frame)
            previous = line
    return list(dict.fromkeys(frames))


def is_project_frame(frame: Frame) -> bool:
    path = frame.path.replace("\\", "/")
    return EXTERNAL_PATH.search(path) is None and ".." not in path.split("/")


def candidates(path: str) -> list[str]:
    """Longest first. An absolute path is someone's machine, so only its tail can match."""
    clean = path.replace("\\", "/")
    drive = re.match(r"^[A-Za-z]:", clean)
    clean = clean[2:] if drive else clean
    absolute = bool(drive) or clean.startswith("/")
    parts = [part for part in clean.split("/") if part and part != "."]
    tails = ["/".join(parts[index:]) for index in range(len(parts)) if len(parts) - index >= 2]
    return tails if absolute or len(parts) > 1 else ["/".join(parts)]


def locate(frame: Frame, snapshot: Snapshot) -> str | None:
    for candidate in candidates(frame.path):
        found = (
            snapshot.resolve(candidate) if "/" in candidate else (candidate if candidate in snapshot.files else None)
        )
        if found:
            return found
    return None


def problem_in(frame: Frame, text: str) -> str | None:
    if frame.line < 1 or frame.line > text.count("\n") + 1:
        return "line past the end of the file"
    if frame.function and not re.search(rf"(?<![\w$]){re.escape(frame.function)}(?![\w$])", text):
        return "function not in the file"
    return None


def check_frame(frame: Frame, snapshots: list[Snapshot]) -> tuple[str, str | None]:
    """("ok", None), ("mismatch", why), or ("unknown", None) when no ref holds the file."""
    problem: str | None = None
    for snapshot in snapshots:
        path = locate(frame, snapshot)
        if path is None:
            continue
        problem = problem_in(frame, snapshot.files[path])
        if problem is None:
            return "ok", None
    return ("mismatch", problem) if problem else ("unknown", None)


def _claim(ctx: EvidenceContext) -> Claim | None:
    return next((claim for claim in ctx.claims.claims if claim.type is ClaimType.BEHAVIOR), None)


def _shown(frame: Frame, problem: str | None) -> dict[str, Any]:
    return {"path": frame.path[:MAX_DETAIL_CHARS], "line": frame.line, "problem": problem}


def summarize(ctx: EvidenceContext, frames: list[Frame], snapshots: list[Snapshot]) -> Check:
    results = [(frame, *check_frame(frame, snapshots)) for frame in frames]
    wrong = [_shown(frame, why) for frame, state, why in results if state == "mismatch"]
    good = sum(state == "ok" for _, state, _ in results)
    claim = _claim(ctx)
    detail: dict[str, Any] = {"frames": len(frames), "matched": good, "unknown": len(frames) - good - len(wrong)}
    if wrong and all(snapshot.complete for snapshot in snapshots):
        reason = "the trace does not fit the code at any ref read"
        strength = Strength.SOFT
        return make_check(MODULE, CheckStatus.CONTRADICTED, claim, strength=strength, reason=reason,
                          mismatches=wrong[:MAX_SHOWN], **detail)  # fmt: skip
    if wrong:
        reason = "part of the repository went unread"
        return make_check(MODULE, CheckStatus.UNVERIFIED, claim, reason=reason, mismatches=wrong[:MAX_SHOWN], **detail)
    if good:
        return make_check(MODULE, CheckStatus.CONFIRMED, claim, target="trace matches the code", **detail)
    return make_check(
        MODULE, CheckStatus.UNVERIFIED, claim, reason="no frame names a file in this repository", **detail
    )


async def run(ctx: EvidenceContext) -> list[Check]:
    found = parse_frames(ctx.stage.submission.body)
    frames = [
        frame
        for frame in found
        if is_project_frame(frame) and not (frame.function and ctx.defined_by_author(frame.function))
    ][:MAX_FRAMES]
    if not frames:
        return []
    snapshots = [await ctx.snapshot(ref) for ref in await ctx.search_refs()]
    return [summarize(ctx, frames, snapshots)]
