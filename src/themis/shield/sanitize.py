"""Deterministic scan for text hidden from humans or aimed at AI reviewers.

Hidden text alone is common and honest: issue templates use HTML comments, emoji use ZWJ,
RTL languages use bidi marks. So those are NOTE findings. A finding only taints when hidden
text carries instructions, or uses a channel with no honest use in a report.

Known limit: a person quoting an injection string in a report is quarantined for human
review. That costs a maintainer a look, and a false negative costs a hijacked reviewer.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

from themis.models import FindingSeverity, ShieldFinding
from themis.store.redact import redact

EXCERPT_CHARS = 120
ZERO_WIDTH = frozenset("​‌‍⁠﻿᠎­")
BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")
TAG_START, TAG_END = 0xE0000, 0xE007F
BLACK_FLAG = "\U0001f3f4"
ZERO_WIDTH_RUN_TAINT = 4

HIDDEN_REGIONS = (
    ("html_comment", re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)),
    ("hidden_html", re.compile(
        r"<(\w+)[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|\shidden[\s>=])[^>]*>"
        r".*?(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE)),
)  # fmt: skip
COLLAPSED = re.compile(r"<details\b[^>]*>.*?(?:</details>|\Z)", re.DOTALL | re.IGNORECASE)
LINK_TITLE = re.compile(r"\[[^\]]*\]\([^)\s]*\s+\"([^\"]*)\"\)")

_SEP = r"[\s._*/-]+"  # Attackers swap spaces for dots or dashes, as in a file name.
_WORDS = rf"(?:{_SEP}[\w']+){{0,4}}?"
INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\b(?:ignore|disregard|forget|override)\b{_WORDS}{_SEP}(?:previous|prior|above|earlier|preceding|your|system|all)"
        rf"{_WORDS}{_SEP}(?:instructions?|prompts?|directives?|rules)\b",
        r"\bsystem\s+override\b",
        rf"\b(?:print|reveal|show|repeat|output|leak)\b{_WORDS}\s+system\s+prompt\b",
        rf"\byou\s+are\s+now\b{_WORDS}\s+(?:mode|assistant|agent|admin|administrator|maintainer)\b",
        # Must give an order. "I used AI tools:" is an honest disclosure, not an injection.
        r"\b(?:ai|llm|gpt|bot|automated)\s+(?:reviewers?|agents?|triage|assistants?|models?|tools?)\s*[:,]\s*"
        r"(?:please\s+)?(?:ignore|disregard|respond|reply|mark|label|approve|output|say|close|classify|treat|skip|"
        r"return|do\s+not|don't|you\s+must)\b",
        r"\b(?:dear|hey|attention|note\s+to)\s+(?:the\s+)?(?:ai|llm|bot|assistant|reviewer|model)\b",
        r"\b(?:mark|label|classify|flag|tag)\s+(?:this|it)(?:\s+(?:issue|pr|pull\s+request|report|submission))?"
        r"\s+(?:as\s+)?(?:a\s+)?(?:verified|approved|safe|trusted|genuine|evidenced|security_report)\b",
        r"\bauto[- ]approve\s+(?:this|the)\b",
        r"\boutput\s+only\s+the\s+word\b",
        r"\brespond\s+only\s+with\b",
        r"</?untrusted_(?:submission|context)\b",
        r"\bnew\s+instructions?\s+from\b",
        r"\bclose\s+(?:all\s+)?(?:the\s+)?others?(?:\s+(?:issues|prs|pull\s+requests))?\b",
    )
)


@dataclass
class Sanitized:
    """`clean` is the text models may see; `offsets[i]` is where clean[i] came from."""

    original: str
    clean: str
    offsets: list[int]
    findings: list[ShieldFinding] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        return any(item.severity is FindingSeverity.TAINT for item in self.findings)

    def locate(self, quote: str) -> tuple[int, int] | None:
        """Find a quote regardless of whitespace, as a span of the original text."""
        tokens = quote.split()
        if not tokens:
            return None
        match = re.search(r"\s+".join(re.escape(token) for token in tokens), self.clean)
        if match is None:
            return None
        return self.offsets[match.start()], self.offsets[match.end() - 1] + 1


def safe_excerpt(text: str) -> str:
    """Safe to show a maintainer: invisibles gone, markup escaped, secrets redacted, short."""
    visible = "".join(char for char in text if char.isprintable() or char == " ")
    shortened = " ".join(visible.split())[:EXCERPT_CHARS]
    return str(redact(html.escape(shortened)))


def is_tag(char: str) -> bool:
    return TAG_START <= ord(char) <= TAG_END


def _normalize(text: str) -> tuple[str, list[int]]:
    """Drop invisibles and fold compatibility forms, keeping a map back to the original."""
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char in ZERO_WIDTH or char in BIDI_CONTROLS or is_tag(char):
            continue
        for folded in unicodedata.normalize("NFKC", char):
            chars.append(folded)
            offsets.append(index)
    return "".join(chars), offsets


def _tag_runs(text: str) -> list[tuple[int, int]]:
    """Runs of tag characters, leaving out the sequences that spell a subdivision flag."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if not is_tag(text[index]):
            index += 1
            continue
        start = index
        while index < len(text) and is_tag(text[index]):
            index += 1
        flag = start > 0 and text[start - 1] == BLACK_FLAG and text[index - 1] == chr(TAG_END)
        if not flag:
            runs.append((start, index))
    return runs


def _finding(kind: str, severity: FindingSeverity, name: str, span: tuple[int, int], text: str) -> ShieldFinding:
    return ShieldFinding(
        source="sanitizer", kind=kind, severity=severity, field=name, span=span, excerpt_redacted=safe_excerpt(text)
    )


def _scan_unicode(text: str, name: str) -> list[ShieldFinding]:
    findings: list[ShieldFinding] = []
    for start, end in _tag_runs(text):
        decoded = "".join(chr(ord(char) - TAG_START) for char in text[start:end])
        findings.append(_finding("unicode_tag", FindingSeverity.TAINT, name, (start, end), decoded))

    for match in re.finditer(f"[{''.join(BIDI_CONTROLS)}]+", text):
        has_rtl = any(unicodedata.bidirectional(char) in ("R", "AL") for char in text)
        severity = FindingSeverity.NOTE if has_rtl else FindingSeverity.TAINT
        findings.append(_finding("bidi_control", severity, name, match.span(), f"{len(match[0])} control(s)"))

    for match in re.finditer(f"[{''.join(ZERO_WIDTH)}]+", text):
        if match.start() == 0 and match[0] == "\ufeff":
            continue  # A byte order mark from a pasted file.
        before, after = text[max(match.start() - 1, 0)], text[min(match.end(), len(text) - 1)]
        if len(match[0]) >= ZERO_WIDTH_RUN_TAINT:
            findings.append(
                _finding("zero_width", FindingSeverity.TAINT, name, match.span(), f"run of {len(match[0])}")
            )
        elif before.isascii() and after.isascii():
            findings.append(_finding("zero_width", FindingSeverity.NOTE, name, match.span(), "inside plain text"))
    return findings


def _regions(normalized: str) -> list[tuple[str, int, int]]:
    found = [(kind, *match.span()) for kind, pattern in HIDDEN_REGIONS for match in pattern.finditer(normalized)]
    found += [("collapsed_details", *match.span()) for match in COLLAPSED.finditer(normalized)]
    found += [("link_title", *match.span(1)) for match in LINK_TITLE.finditer(normalized)]
    return found


def scan(text: str, name: str) -> Sanitized:
    normalized, offsets = _normalize(text)
    regions = _regions(normalized)
    findings = _scan_unicode(text, name)

    def original_span(start: int, end: int) -> tuple[int, int]:
        return offsets[start], offsets[end - 1] + 1

    instruction_hits: list[tuple[int, int]] = []
    for pattern in INSTRUCTION_PATTERNS:
        for match in pattern.finditer(normalized):
            instruction_hits.append(match.span())
            inside = next((kind for kind, start, end in regions if start <= match.start() < end), None)
            kind = f"hidden_instruction:{inside}" if inside else "bot_instruction"
            span = original_span(*match.span())
            findings.append(_finding(kind, FindingSeverity.TAINT, name, span, match[0]))

    for start, end in _tag_runs(text):
        decoded = "".join(chr(ord(char) - TAG_START) for char in text[start:end])
        if any(pattern.search(decoded) for pattern in INSTRUCTION_PATTERNS):
            findings.append(
                _finding("hidden_instruction:unicode_tag", FindingSeverity.TAINT, name, (start, end), decoded)
            )

    for kind, start, end in regions:
        if end > start and not any(start <= hit < end for hit, _ in instruction_hits):
            findings.append(
                _finding(kind, FindingSeverity.NOTE, name, original_span(start, end), normalized[start:end])
            )

    removed = [(start, end) for kind, start, end in regions if kind in ("html_comment", "hidden_html")]
    keep = [index for index in range(len(normalized)) if not any(start <= index < end for start, end in removed)]
    return Sanitized(
        original=text,
        clean="".join(normalized[index] for index in keep),
        offsets=[offsets[index] for index in keep],
        findings=sorted(findings, key=lambda item: item.span or (0, 0)),
        hidden=[normalized[start:end] for kind, start, end in regions if kind != "link_title"],
    )
