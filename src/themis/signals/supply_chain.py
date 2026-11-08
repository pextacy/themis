"""Changes that run code at install or CI time, or that move where packages come from."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from themis.models import Signal, SignalSeverity
from themis.signals.base import FilePatch, SignalContext, make_signal
from themis.signals.summary import parse_summary

MODULE = "supply_chain"
CODE = "risky_change"
LONG_LINE = 1000
WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
DOC_FILE = re.compile(r"\.(?:md|rst|txt|adoc)$", re.IGNORECASE)
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?([\w.-]+/[\w.-]+)(?:/[^@\s\"']*)?@([^\s\"'#]+)")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
FIRST_PARTY = frozenset({"actions", "github"})
BINARY = re.compile(r"\.(?:exe|dll|so|dylib|bin|jar|whl|pyc|class|wasm|node|o|a)$", re.IGNORECASE)
LONG_LINE_OK = re.compile(
    r"(?:^|/)(?:package-lock\.json|yarn\.lock|uv\.lock|poetry\.lock|Cargo\.lock)$|\.(?:svg|ipynb|csv)$"
)
WATCHED_FILES = frozenset({"CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".github/themis.yml"})
KNOWN_HIDDEN_DIRS = frozenset({".github", ".vscode", ".devcontainer", ".circleci", ".gitlab", ".idea"})

WORKFLOW_RULES = (
    ("pull_request_target", "Workflow adds the pull_request_target trigger", re.compile(r"\bpull_request_target\b")),
    ("write_all", "Workflow asks for write-all permissions", re.compile(r"\bpermissions:\s*write-all\b")),
    ("secret_echo", "Workflow sends a secret to output or the network", re.compile(
        r"\b(?:echo|printf|curl|wget|tee)\b.*\$\{\{\s*(?:secrets\.|toJSON\(secrets\))", re.IGNORECASE)),
)  # fmt: skip
INSTALL_RULES = (
    (re.compile(r"(?:^|/)package\.json$"), "npm install-time script added",
     re.compile(r"\"(?:preinstall|install|postinstall)\"\s*:")),
    (re.compile(r"(?:^|/)setup\.py$"), "setup.py runs custom code at install time",
     re.compile(r"\bcmdclass\b|\bsubprocess\b|\bos\.system\b|\burllib\b|\brequests\.")),
    (re.compile(r"(?:^|/)build\.rs$"), "build.rs reaches the network or a shell",
     re.compile(r"\breqwest\b|\bureq\b|\bTcpStream\b|\bstd::net\b|\bhyper::|Command::new\(\"(?:curl|wget|sh|bash)\"")),
)  # fmt: skip
ANY_FILE_RULES = (
    ("pipe_to_shell", "Downloaded script piped into a shell",
     re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")),
    ("decoded_exec", "Decoded data passed to eval or exec", re.compile(
        r"\b(?:eval|exec)\s*\(.*(?:b64decode|base64|atob|fromhex)|base64\s+(?:-d|--decode)\b.*\|\s*(?:ba)?sh\b")),
)  # fmt: skip


@dataclass(frozen=True)
class Lockfile:
    entry: re.Pattern[str]
    allowed: tuple[str, ...]  # host + path prefixes


_PYPI = ("pypi.org/", "files.pythonhosted.org/")
_PY_LOCK = re.compile(r"^\s*(?:url|registry)\s*=\s*\"(https?://[^\"]+)\"|\burl\s*=\s*\"(https?://[^\"]+)\"")
LOCKFILES = {
    "package-lock.json": Lockfile(re.compile(r"\"resolved\":\s*\"(https?://[^\"]+)\""), ("registry.npmjs.org/",)),
    "yarn.lock": Lockfile(re.compile(r"\bresolved\s+\"(https?://[^\"]+)\""),
                          ("registry.yarnpkg.com/", "registry.npmjs.org/")),
    "uv.lock": Lockfile(_PY_LOCK, _PYPI),
    "poetry.lock": Lockfile(_PY_LOCK, _PYPI),
    "Cargo.lock": Lockfile(re.compile(r"\bsource\s*=\s*\"(?:registry\+|sparse\+|git\+)?(\w+://[^\"]+)\""),
                           ("github.com/rust-lang/crates.io-index", "index.crates.io/")),
}  # fmt: skip


def _is_workflow_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _workflow_rules(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    hits: list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]] = []
    for added in patch.added:
        if _is_workflow_comment(added.text):
            continue
        for rule, title, pattern in WORKFLOW_RULES:
            if pattern.search(added.text):
                hits.append((rule, title, SignalSeverity.CRITICAL, added.number, {}))
    return hits + _action_swaps(patch)


def _action_swaps(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    """A tag bump of the same action is routine. Leaving a pinned hash, or changing action, is not."""
    old = {match[1]: match[2] for line in patch.removed if (match := USES.match(line))}
    hits: list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]] = []
    for added in patch.added:
        match = USES.match(added.text)
        if match is None or match[1].split("/")[0] in FIRST_PARTY or FULL_SHA.match(match[2]):
            continue
        action = {"action": match[1]}
        if match[1] in old and not FULL_SHA.match(old[match[1]]):
            continue
        if match[1] in old or old:
            title = "Third-party action changed to an unpinned reference"
            hits.append(("action_swap", title, SignalSeverity.CRITICAL, added.number, action))
        else:
            title = "New third-party action is not pinned to a commit"
            hits.append(("action_unpinned", title, SignalSeverity.WARN, added.number, action))
    return hits


def _install_rules(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    hits: list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]] = []
    for path, title, pattern in INSTALL_RULES:
        if path.search(patch.path):
            hits += [("install_hook", title, SignalSeverity.CRITICAL, added.number, {})
                     for added in patch.added if pattern.search(added.text)]  # fmt: skip
    return hits


def _any_file_rules(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    """Text in a document cannot run, so there it is shown without the critical label."""
    severity = SignalSeverity.WARN if DOC_FILE.search(patch.path) else SignalSeverity.CRITICAL
    hits: list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]] = []
    for added in patch.added:
        hits += [(rule, title, severity, added.number, {})
                 for rule, title, pattern in ANY_FILE_RULES if pattern.search(added.text)]  # fmt: skip
    return hits


def _lock_urls(rule: Lockfile, lines: Iterable[str]) -> list[str]:
    return [url for line in lines if (match := rule.entry.search(line)) for url in match.groups() if url]


def _foreign(url: str, allowed: tuple[str, ...]) -> str | None:
    """The host of a URL outside the official registry. Parsed, so `npmjs.org@evil` is caught."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return None if f"{host}{parsed.path or '/'}".startswith(allowed) else host


def _lockfile_rules(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    rule = LOCKFILES.get(patch.path.split("/")[-1])
    if rule is None:
        return []
    # A host the lockfile already used is the project's own mirror, not a new source.
    known = {urlparse(url).hostname for url in _lock_urls(rule, patch.removed)}
    hits: list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]] = []
    reported: set[str] = set()
    for added in patch.added:
        for url in _lock_urls(rule, [added.text]):
            host = _foreign(url, rule.allowed)
            if host is not None and host not in known and host not in reported:
                reported.add(host)
                title = "Lockfile entry leaves the official registry"
                hits.append(("lockfile_host", title, SignalSeverity.CRITICAL, added.number, {"host": host}))
    return hits


def _long_lines(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    if LONG_LINE_OK.search(patch.path):
        return []
    line = next((item for item in patch.added if len(item.text) > LONG_LINE), None)
    if line is None:
        return []
    return [
        ("minified_line", "Very long single line added", SignalSeverity.WARN, line.number, {"chars": len(line.text)})
    ]


def _patch_hits(patch: FilePatch) -> list[tuple[str, str, SignalSeverity, int | None, dict[str, Any]]]:
    hits = _workflow_rules(patch) if WORKFLOW.match(patch.path) else []
    hits += _install_rules(patch) + _any_file_rules(patch) + _lockfile_rules(patch) + _long_lines(patch)
    return hits


def _hidden_dir(path: str) -> bool:
    parts = path.split("/")[:-1]
    return any(part.startswith(".") and part not in KNOWN_HIDDEN_DIRS for part in parts)


def _path_hits(ctx: SignalContext) -> list[tuple[str, str, str]]:
    stats = parse_summary(ctx.submission.diff_summary)
    hits: list[tuple[str, str, str]] = []
    for path in ctx.submission.changed_files:
        removed = path in stats and stats[path].status == "removed"
        if BINARY.search(path) and not removed:
            hits.append(("binary_blob", "Binary file in the change", path))
        if _hidden_dir(path) and not removed:
            hits.append(("hidden_directory", "File added under a hidden directory", path))
        if path in WATCHED_FILES:
            hits.append(("review_rules", "Change to review rules or THEMIS policy", path))
    return hits


def scan(ctx: SignalContext) -> list[Signal]:
    extra = {"partial": True} if ctx.truncated else {}
    signals = [
        make_signal(MODULE, CODE, SignalSeverity.WARN, title, file=path, rule=rule, **extra)
        for rule, title, path in _path_hits(ctx)
    ]
    for patch in ctx.patches:
        signals += [
            make_signal(MODULE, CODE, severity, title, file=patch.path, line=line, rule=rule, **detail, **extra)
            for rule, title, severity, line, detail in _patch_hits(patch)
        ]
    return signals
