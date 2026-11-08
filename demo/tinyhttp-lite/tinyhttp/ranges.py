"""Byte range parsing for the HTTP Range header."""

UNIT = "bytes="


def parse_range(header, size):
    """Return (start, end) with end exclusive, or None for an invalid range."""
    if not header.startswith(UNIT):
        return None
    spec = header[len(UNIT) :]
    if "," in spec or "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    if first == "":
        if not last.isdigit() or int(last) == 0:
            return None
        return (max(size - int(last), 0), size)
    if not first.isdigit():
        return None
    start = int(first)
    if start >= size:
        return None
    if last == "":
        return (start, size + 1)
    if not last.isdigit() or int(last) < start:
        return None
    return (start, min(int(last) + 1, size))


def content_range(start, end, size):
    """Format a Content-Range value for an exclusive end."""
    return f"bytes {start}-{end - 1}/{size}"


def slice_body(body, header):
    """Return (status, body) for a request body and a Range header."""
    if header is None:
        return 200, body
    window = parse_range(header, len(body))
    if window is None:
        return 416, b""
    return 206, body[window[0] : window[1]]
