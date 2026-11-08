"""Query string parsing and encoding."""

from urllib.parse import quote, unquote_plus


def parse_query(text):
    """Parse a query string into a dict of lists."""
    result = {}
    for pair in text.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        result.setdefault(unquote_plus(name), []).append(unquote_plus(value))
    return result


def encode_query(values):
    """Encode a dict of values or lists of values."""
    parts = []
    for name, value in values.items():
        for item in value if isinstance(value, list) else [value]:
            parts.append(f"{quote(str(name), safe='')}={quote(str(item), safe='')}")
    return "&".join(parts)
