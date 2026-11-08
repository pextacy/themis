"""Credentials in added lines. No value, or any part of one, leaves this module."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from themis.evidence.tests_results import is_test_path
from themis.models import Signal, SignalSeverity
from themis.signals.base import AddedLine, FilePatch, SignalContext, make_signal

MODULE = "secrets"
CODE = "secret_leak"
FINGERPRINT_CHARS = 12
MAX_FINDINGS = 50
MIN_GENERIC_ENTROPY = 3.5
MIN_KNOWN_ENTROPY = 3.0
PRIVATE_KEY = "private_key"

KNOWN: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("aws_access_key", "AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", "GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_token", "GitHub token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("otari_key", "Otari key", re.compile(r"\btk-[A-Za-z0-9_-]{16,}")),
    ("otari_key", "Otari key", re.compile(r"\botk_[A-Za-z0-9_]{16,}")),
    ("slack_token", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("slack_webhook", "Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    ("stripe_key", "Stripe live key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("google_api_key", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    (PRIVATE_KEY, "private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
)
GENERIC = re.compile(
    r"([\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|auth[_-]?key|private[_-]?key|access[_-]?key)[\w.-]*)"
    r"[\"']?\s*(?::=|=>|[:=])\s*[\"']([^\"'\s]{16,})[\"']",
    re.IGNORECASE,
)
# Checked against the value only. A comment beside a real key must not be able to hide it.
PLACEHOLDER = re.compile(
    r"x{3,}|\*{3,}|\.{3}|your[-_]?(?:api[-_]?)?(?:key|token|secret)|change[-_]?me|example|placeholder|dummy|fake|"
    r"redacted|sample|test|\$\{|\{\{|<[^>]*>",
    re.IGNORECASE,
)
FAKE_MARKER = re.compile(r"\b(?:fake|dummy|fixture|not\s+a\s+real)\b", re.IGNORECASE)
FIXTURE_PATH = re.compile(r"(?:^|/)(?:fixtures?|testdata|test_data)/", re.IGNORECASE)
NOT_A_VALUE = re.compile(r"^(?:https?://|/|\./|\$)")
NOT_A_SECRET_NAME = re.compile(r"hash|digest|sha\d*|checksum|file|path|url", re.IGNORECASE)


def fingerprint(value: str) -> str:
    """Lets a maintainer match two findings without seeing the value."""
    return hashlib.sha256(value.encode()).hexdigest()[:FINGERPRINT_CHARS]


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    counts = Counter(value)
    return -sum(count / len(value) * math.log2(count / len(value)) for count in counts.values()) if value else 0.0


def _placeholder(value: str, kind: str) -> bool:
    if PLACEHOLDER.search(value):
        return True
    return kind != PRIVATE_KEY and entropy(value) < MIN_KNOWN_ENTROPY


def _severity(path: str, line: str) -> SignalSeverity:
    """A value marked as fake in a test file still shows, without the critical label."""
    fixture = is_test_path(path) and FAKE_MARKER.search(line) is not None
    return SignalSeverity.WARN if fixture or FIXTURE_PATH.search(path) else SignalSeverity.CRITICAL


def _known(line: str) -> list[tuple[str, str, str]]:
    found = []
    for kind, label, pattern in KNOWN:
        found += [(kind, label, match[0]) for match in pattern.finditer(line) if not _placeholder(match[0], kind)]
    return found


def _generic(line: str) -> list[str]:
    found = []
    for match in GENERIC.finditer(line):
        name, value = match[1], match[2]
        skip = NOT_A_SECRET_NAME.search(name) or NOT_A_VALUE.match(value) or PLACEHOLDER.search(value)
        if not skip and entropy(value) >= MIN_GENERIC_ENTROPY:
            found.append(value)
    return found


def _scan_line(patch: FilePatch, added: AddedLine, seen: set[str], partial: bool) -> list[Signal]:
    known = _known(added.text)
    labelled = [(kind, f"Possible {label} in the added lines", value, _severity(patch.path, added.text))
                for kind, label, value in known]  # fmt: skip
    if not known and not is_test_path(patch.path):
        labelled += [("high_entropy", "High-entropy value assigned to a credential name", value, SignalSeverity.WARN)
                     for value in _generic(added.text)]  # fmt: skip
    signals = []
    for kind, title, value, severity in labelled:
        mark = fingerprint(value)
        if mark in seen:
            continue
        seen.add(mark)
        extra = {"partial": True} if partial else {}
        signals.append(
            make_signal(MODULE, CODE, severity, title, file=patch.path, line=added.number,
                        kind=kind, fingerprint=mark, **extra)
        )  # fmt: skip
    return signals


def scan(ctx: SignalContext) -> list[Signal]:
    signals: list[Signal] = []
    for patch in ctx.patches:
        seen: set[str] = set()
        for added in patch.added:
            signals += _scan_line(patch, added, seen, ctx.truncated)
    return signals[:MAX_FINDINGS]
