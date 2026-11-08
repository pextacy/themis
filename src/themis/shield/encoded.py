"""Instructions hidden in encodings, terminal escapes and image URLs.

Encoded data is ordinary in reports, so decoding alone finds nothing. A finding needs decoded
text that reads as an instruction, an escape that really hides text, or an image that would
carry opaque data to an unknown host just by being rendered.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import math
import re
from collections import Counter
from collections.abc import Iterator
from urllib.parse import parse_qsl, unquote, urlsplit

from themis.models import FindingSeverity, ShieldFinding
from themis.shield import sanitize

MAX_TEXT = 200_000
MAX_FRAGMENT = 4096
MAX_CANDIDATES = 64
MAX_DEPTH = 2
MAX_ESCAPES = 10
MIN_PRINTABLE = 0.9
MIN_OPAQUE = 40
MIN_ENTROPY = 4.2
MIN_HIDDEN_WORDS = 4

BASE64 = re.compile(rf"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{{16,{MAX_FRAGMENT}}}={{0,2}}")
HEX = re.compile(rf"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{{2}}){{8,{MAX_FRAGMENT // 2}}}(?![0-9a-fA-F])")
BACKSLASH_HEX = re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}")
PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
ENTITY = re.compile(r"&#?[0-9a-zA-Z]+;")
ROT13_SENTINELS = re.compile(r"vtaber|qvfertneq|vafgehpgvbaf?|flfgrz\s+cebzcg", re.IGNORECASE)
ROT13_WINDOW = 300

CSI = re.compile(r"\x1b\[([0-9;?]*)[ -/]*([@-~])")
OSC_LINK = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")
HIDING_FINALS = frozenset("AJ")
LINE_BREAKS = re.compile(r"[\r\n]")

IMAGE = re.compile(r"!\[[^\]]{0,500}\]\(\s*(https?://[^)\s]+)|<img\b[^>]*\bsrc\s*=\s*[\"']?(https?://[^\s\"'>]+)", re.I)
LINK = re.compile(r"(?<!!)\[[^\]]{0,500}\]\(\s*(https?://[^)\s]+)", re.I)
TRUSTED_HOSTS = (
    "github.com", "githubusercontent.com", "githubassets.com", "shields.io", "codecov.io", "coveralls.io",
    "readthedocs.org", "readthedocs.io", "gitlab.com", "codeberg.org",
)  # fmt: skip


def _printable(raw: bytes) -> str | None:
    """Readable text, or None for binary data."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    readable = sum(char.isprintable() or char.isspace() for char in text)
    return text if readable / len(text) >= MIN_PRINTABLE else None


def _from_base64(fragment: str) -> str | None:
    cleaned = fragment.rstrip("=").replace("-", "+").replace("_", "/")
    try:
        return _printable(base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True))
    except (binascii.Error, ValueError):
        return None


def _from_hex(fragment: str) -> str | None:
    try:
        return _printable(bytes.fromhex(fragment.replace("\\x", "")))
    except ValueError:
        return None


def decodings(text: str) -> Iterator[tuple[str, str]]:
    """Every (encoding, decoded text) one step down. Bounded by the caller."""
    for match in BASE64.finditer(text):
        if decoded := _from_base64(match[0]):
            yield "base64", decoded
    for pattern in (HEX, BACKSLASH_HEX):
        for match in pattern.finditer(text):
            if decoded := _from_hex(match[0]):
                yield "hex", decoded
    for word in text.split() if "%" in text else ():
        if PERCENT.search(word):
            yield "url", unquote(word[:MAX_FRAGMENT])
    if ENTITY.search(text):
        unescaped = html.unescape(text)
        if unescaped != text:
            yield "html_entities", unescaped
    for match in ROT13_SENTINELS.finditer(text):
        window = text[max(match.start() - ROT13_WINDOW, 0) : match.end() + ROT13_WINDOW]
        yield "rot13", codecs.decode(window, "rot13")


def _instruction(text: str) -> str | None:
    normalized, _ = sanitize._normalize(text)
    for pattern in sanitize.INSTRUCTION_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return match[0]
    return None


def scan_decoded(text: str, name: str) -> list[ShieldFinding]:
    """Walk at most two encodings deep. The first hit per encoding path is enough."""
    findings: list[ShieldFinding] = []
    seen: set[str] = set()
    budget = MAX_CANDIDATES
    level = [("", text[:MAX_TEXT])]
    for _ in range(MAX_DEPTH):
        following: list[tuple[str, str]] = []
        for path, current in level:
            for encoding, decoded in decodings(current):
                if decoded in seen or budget == 0:
                    continue
                seen.add(decoded)
                budget -= 1
                route = f"{path}+{encoding}" if path else encoding
                hit = _instruction(decoded)
                if hit:
                    span = (0, len(text))
                    excerpt = f"{route}: {hit}"
                    findings.append(
                        sanitize._finding("encoded_instruction", FindingSeverity.TAINT, name, span, excerpt)
                    )
                else:
                    following.append((route, decoded))
        level = following
    return findings


def _hidden_before(text: str, start: int) -> bool:
    """True when a whole sentence sits on the line this escape wipes."""
    line = LINE_BREAKS.split(text[:start])[-1]
    return len(CSI.sub("", line).split()) >= MIN_HIDDEN_WORDS


def _conceals(match: re.Match[str], text: str) -> bool:
    params = [item for item in match[1].split(";") if item]
    final = match[2]
    if final == "m":
        return "8" in params or ("30" in params and "40" in params)
    wipes_line = final == "K" and "2" in params
    return (wipes_line or final in HIDING_FINALS) and _hidden_before(text, match.start())


def scan_ansi(text: str, name: str) -> list[ShieldFinding]:
    """Colors in a pasted log are honest. Hiding text from whoever reads it is not."""
    if "\x1b" not in text:
        return []
    findings: list[ShieldFinding] = []
    for match in list(CSI.finditer(text))[:MAX_ESCAPES]:
        severity = FindingSeverity.TAINT if _conceals(match, text) else FindingSeverity.NOTE
        findings.append(sanitize._finding("ansi_escape", severity, name, match.span(), "terminal escape sequence"))
    for match in list(OSC_LINK.finditer(text))[:MAX_ESCAPES]:
        findings.append(
            sanitize._finding("ansi_escape", FindingSeverity.TAINT, name, match.span(), "terminal hyperlink")
        )
    return findings


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(count / len(value) * math.log2(count / len(value)) for count in counts.values())


def _trusted(host: str) -> bool:
    return any(host == item or host.endswith(f".{item}") for item in TRUSTED_HOSTS)


def carries_data(url: str) -> bool:
    """A query value long and random enough to be someone's data, sent to an unknown host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if _trusted((parts.hostname or "").lower()):
        return False
    values = [value for _, value in parse_qsl(parts.query, keep_blank_values=True)]
    return any(len(value) >= MIN_OPAQUE and _entropy(value) >= MIN_ENTROPY for value in values)


def scan_urls(text: str, name: str) -> list[ShieldFinding]:
    """An image loads by itself, so it taints. A link needs a click, so it is only a note."""
    findings: list[ShieldFinding] = []
    for match in IMAGE.finditer(text):
        url = match[1] or match[2]
        if carries_data(url):
            host = urlsplit(url).hostname or "unknown host"
            excerpt = f"image sends data to {host}"
            findings.append(sanitize._finding("exfil_image", FindingSeverity.TAINT, name, match.span(), excerpt))
    for match in LINK.finditer(text):
        if carries_data(match[1]):
            host = urlsplit(match[1]).hostname or "unknown host"
            excerpt = f"link sends data to {host}"
            findings.append(sanitize._finding("exfil_link", FindingSeverity.NOTE, name, match.span(), excerpt))
    return findings


def scan(text: str, name: str) -> list[ShieldFinding]:
    bounded = text[:MAX_TEXT]
    return [*scan_decoded(bounded, name), *scan_ansi(bounded, name), *scan_urls(bounded, name)]
