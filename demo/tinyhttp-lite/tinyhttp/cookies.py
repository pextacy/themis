"""Cookie header helpers."""


def parse_cookies(header):
    """Parse a Cookie header into a dict. Later values win."""
    cookies = {}
    for part in (header or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            cookies[name] = value.strip('"')
    return cookies


def serialize_cookie(name, value, *, path="/", max_age=None, secure=False, http_only=True):
    """Build a Set-Cookie value."""
    if any(char in f"{name}{value}" for char in ";\r\n"):
        raise ValueError("cookie contains a forbidden character")
    parts = [f"{name}={value}", f"Path={path}"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)
