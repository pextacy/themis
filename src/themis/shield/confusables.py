"""Look-alike letters used to slip instructions past the sanitizer's patterns.

Folding happens word by word. A word only counts as suspicious when it mixes plain Latin with
look-alikes: whole words in Russian, Greek, Turkish, Chinese or Arabic are ordinary writing
and produce no finding. A word made only of look-alikes is folded too, but it can only matter
when the folded text spells an instruction, which honest text in those languages does not.
"""

from __future__ import annotations

import re
import unicodedata

from themis.models import FindingSeverity, ShieldFinding
from themis.shield import sanitize

MAX_NOTES = 20
WORD = re.compile(r"[^\W\d_]+")
LOOKALIKES = str.maketrans(
    {
        # Cyrillic
        "а": "a", "в": "b", "с": "c", "ԁ": "d", "е": "e", "һ": "h", "і": "i", "ј": "j", "к": "k", "ӏ": "l",
        "м": "m", "о": "o", "р": "p", "ԛ": "q", "ѕ": "s", "т": "t", "ѵ": "v", "ԝ": "w", "х": "x", "у": "y",
        "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J", "К": "K", "М": "M", "О": "O",
        "Р": "P", "Ѕ": "S", "Т": "T", "Х": "X", "У": "Y",
        # Greek
        "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
        "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
        # Armenian
        "օ": "o", "ս": "u", "հ": "h",
    }
)  # fmt: skip
TABLE = frozenset(chr(code) for code in LOOKALIKES)
# Latin letters that never make a word suspicious, but must not hide an instruction either.
LATIN_EXTRAS = {"ı": "i"}


def _width_folded(original: str, folded: str) -> bool:
    """A fullwidth or math letter that NFKC turned into a single ASCII letter."""
    return (
        not original.isascii()
        and folded.isascii()
        and folded.isalpha()
        and len(unicodedata.normalize("NFKC", original)) == 1
    )


def _is_lookalike(char: str, original: str) -> bool:
    return char in TABLE or _width_folded(original, char)


def _fold_word(word: str) -> str:
    return word.translate(LOOKALIKES)


def fold(normalized: str, offsets: list[int], text: str) -> tuple[str, set[int], list[tuple[int, int]]]:
    """Return the folded text, the positions changed by folding, and the mixed-script words."""
    chars = list(normalized)
    changed: set[int] = set()
    mixed: list[tuple[int, int]] = []
    for match in WORD.finditer(normalized):
        start, word = match.start(), match[0]
        alike = [i for i, char in enumerate(word, start) if _is_lookalike(char, text[offsets[i]])]
        extras = [i for i, char in enumerate(word, start) if char in LATIN_EXTRAS]
        plain = len(word) - len(alike) - len(extras)
        latin = sum(char.isascii() for i, char in enumerate(word, start) if i not in alike)
        if alike and latin:
            mixed.append(match.span())
        if alike and (latin or plain == 0):
            for index in alike:
                chars[index] = _fold_word(chars[index])
                changed.add(index)
        for index in extras:
            chars[index] = LATIN_EXTRAS[chars[index]]
            changed.add(index)
    return "".join(chars), changed, mixed


def _instruction_hits(folded: str, changed: set[int]) -> list[tuple[int, int]]:
    """Only hits that folding produced. The sanitizer already reports the rest."""
    hits: list[tuple[int, int]] = []
    for pattern in sanitize.INSTRUCTION_PATTERNS:
        for match in pattern.finditer(folded):
            if any(index in changed for index in range(*match.span())):
                hits.append(match.span())
    return hits


def scan(text: str, name: str) -> list[ShieldFinding]:
    normalized, offsets = sanitize._normalize(text)
    if not normalized:
        return []
    folded, changed, mixed = fold(normalized, offsets, text)

    def span(start: int, end: int) -> tuple[int, int]:
        return offsets[start], offsets[end - 1] + 1

    hits = _instruction_hits(folded, changed)
    findings = [
        sanitize._finding("homoglyph_instruction", FindingSeverity.TAINT, name, span(start, end), folded[start:end])
        for start, end in hits
    ]
    quiet = [(start, end) for start, end in mixed if not any(start < stop and begin < end for begin, stop in hits)]
    findings += [
        sanitize._finding("homoglyph_word", FindingSeverity.NOTE, name, span(start, end), folded[start:end])
        for start, end in quiet[:MAX_NOTES]
    ]
    return sorted(findings, key=lambda item: item.span or (0, 0))
