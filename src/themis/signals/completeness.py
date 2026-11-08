"""Does a report hold what a maintainer needs? Judges the content, never the author.

Generous on purpose: code fences, version numbers and error output count in any language, and
hidden text does not, so a template comment cannot fill in a section.
"""

from __future__ import annotations

import re

from themis.models import Signal, SignalSeverity, SubmissionKind
from themis.shield import sanitize
from themis.signals.base import SignalContext, make_signal

MODULE = "completeness"
MIN_LIST_ITEMS = 2
REPORT_KINDS = frozenset({SubmissionKind.BUG_REPORT, SubmissionKind.SECURITY_REPORT})

VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+(?:\.\d+)*(?:[-+.]?[A-Za-z0-9]+)*\b|\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
FENCE = re.compile(r"^\s*(?:```|~~~)|^(?: {4}|\t)\S|^\s*[$>] \S", re.MULTILINE)
LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+\S", re.MULTILINE)
STEP_WORDS = re.compile(
    r"\b(?:steps?|repro\w*|adım\w*|tekrar\w*|pasos?|reproducir|étapes?|schritte?|reproduzier\w*)\b|再現|复现|重现|步骤|手順",
    re.IGNORECASE,
)
EXPECTED = re.compile(
    r"\b(?:expected|expect|should|supposed|beklen\w*|esperado|esperaba|attendu|erwartet\w*)\b|期待|预期|应该",
    re.IGNORECASE,
)
ACTUAL = re.compile(
    r"\b(?:actual\w*|instead|got|returns?|gerçekleşen|oluyor|obtuve|obtenu|tatsächlich)\b|実際|实际", re.IGNORECASE
)
ERROR_OUTPUT = re.compile(
    r"Traceback \(most recent call last\)|\b\w*(?:Error|Exception)\b:|\bpanic:|^\s+at \S+.*:\d+", re.MULTILINE
)
ENVIRONMENT = re.compile(
    r"\b(?:linux|ubuntu|debian|fedora|arch|alpine|centos|macos|mac os|darwin|windows|win1[01]|android|ios|docker|"
    r"kubernetes|python\s*\d|cpython|pypy|node(?:js|\.js)?\s*v?\d|go\s?1\.\d+|rustc?\s*\d|java\s*\d|jdk|ruby\s*\d|"
    r"chrome|firefox|safari|edge|x86_64|amd64|arm64|aarch64|gcc|clang|environment|ortam|entorno|umgebung)\b|环境|環境",
    re.IGNORECASE,
)
IMPACT = re.compile(
    r"\b(?:impact|attackers?|severity|cvss|exfiltrat\w*|rce|remote code|privilege|xss|sqli|injection|denial|dos|"
    r"bypass\w*|leak\w*|etki\w*|saldırgan\w*|impacto|atacante|auswirkung\w*)\b|影响|影響",
    re.IGNORECASE,
)
COMPONENT = re.compile(
    r"`[^`\n]+`|[\w-]+(?:/[\w.-]+)+|\b[\w-]+\.[a-z]{1,5}\b|\b\w+\(\)|\b(?:endpoint|module|component|function|bileşen|"
    r"componente|komponente)\b",
    re.IGNORECASE,
)
POC = re.compile(r"\b(?:poc|proof[- ]of[- ]concept|exploit|payload|curl\s)", re.IGNORECASE)

ASKS = {
    "missing_version": "Could you add the version or commit where you saw this?",
    "missing_steps": "Could you share the steps or a short script that shows it?",
    "missing_expected_actual": "Could you say what you expected and what happened instead?",
    "missing_environment": "Could you mention your environment, such as the OS and runtime?",
    "missing_impact": "Could you describe the impact, meaning what an attacker could do?",
    "missing_component": "Could you name the affected file, function or endpoint?",
    "missing_poc": "Could you include a proof of concept we can run?",
}
TITLES = {
    "missing_version": "No version or commit found",
    "missing_steps": "No reproduction steps or code found",
    "missing_expected_actual": "No expected and actual behavior found",
    "missing_environment": "No environment found",
    "missing_impact": "No impact description found",
    "missing_component": "No affected component found",
    "missing_poc": "No proof of concept found",
}


def _has_steps(text: str) -> bool:
    if FENCE.search(text) or STEP_WORDS.search(text):
        return True
    return len(LIST_ITEM.findall(text)) >= MIN_LIST_ITEMS


def _has_outcome(text: str) -> bool:
    """Error output shows what went wrong by itself."""
    return ERROR_OUTPUT.search(text) is not None or (
        EXPECTED.search(text) is not None and ACTUAL.search(text) is not None
    )


def _has_poc(text: str) -> bool:
    return POC.search(text) is not None or FENCE.search(text) is not None


def _common(text: str) -> dict[str, bool]:
    return {
        "missing_version": VERSION.search(text) is not None,
        "missing_steps": _has_steps(text),
        "missing_expected_actual": _has_outcome(text),
        "missing_environment": ENVIRONMENT.search(text) is not None,
    }


def _security(text: str) -> dict[str, bool]:
    return {
        "missing_impact": IMPACT.search(text) is not None,
        "missing_component": COMPONENT.search(text) is not None,
        "missing_poc": _has_poc(text),
    }


def scan(ctx: SignalContext) -> list[Signal]:
    submission = ctx.submission
    if submission.is_pull_request or ctx.kind not in REPORT_KINDS:
        return []
    text = sanitize.scan(f"{submission.title}\n\n{submission.body}", "report").clean
    present = _common(text) | (_security(text) if ctx.kind is SubmissionKind.SECURITY_REPORT else {})
    return [
        make_signal(MODULE, code, SignalSeverity.INFO, TITLES[code]) for code, found in present.items() if not found
    ]


def checklist(signals: list[Signal]) -> list[str]:
    """Polite one-line asks for the contributor, in a stable order, without repeats."""
    return list(dict.fromkeys(ASKS[s.code] for s in signals if s.module == MODULE and s.code in ASKS))
