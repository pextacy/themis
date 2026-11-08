"""Parse raw HTTP/1.1 request text."""

from dataclasses import dataclass, field

from tinyhttp.headers import Headers, parse_headers
from tinyhttp.query import parse_query

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MAX_REQUEST_BYTES = 1024 * 1024


class ParseError(ValueError):
    """The request could not be parsed."""


@dataclass
class Request:
    method: str
    path: str
    version: str
    headers: Headers
    body: bytes = b""
    query: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)


def split_target(target):
    """Split a request target into its path and parsed query."""
    path, _, query = target.partition("?")
    return path, parse_query(query)


def parse_request_line(line):
    parts = line.split(" ")
    if len(parts) != 3:
        raise ParseError(f"bad request line: {line!r}")
    method, target, version = parts
    if method not in METHODS:
        raise ParseError(f"unknown method: {method}")
    if not version.startswith("HTTP/1."):
        raise ParseError(f"unsupported version: {version}")
    return method, target, version


def parse_request(raw):
    """Parse bytes into a Request. Raises ParseError on anything malformed."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise ParseError("request too large")
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ParseError("no end of headers")
    text = head.decode("latin-1")
    line, _, rest = text.partition("\r\n")
    method, target, version = parse_request_line(line)
    path, query = split_target(target)
    headers = parse_headers(rest)
    length = headers.get("Content-Length")
    if length is not None:
        if not length.isdigit():
            raise ParseError("bad content length")
        body = body[: int(length)]
    return Request(method, path, version, headers, body, query)
