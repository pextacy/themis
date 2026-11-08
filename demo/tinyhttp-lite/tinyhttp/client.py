"""A minimal blocking HTTP client built on sockets."""

import socket
from urllib.parse import urlsplit

from tinyhttp.headers import Headers, format_headers, parse_headers

DEFAULT_TIMEOUT = 10.0
BUFFER = 65536


def build_request(method, target, host, headers=None, body=b""):
    """Serialize a request to bytes."""
    merged = Headers(headers)
    merged.set("Host", host)
    merged.set("Connection", "close")
    if body:
        merged.set("Content-Length", str(len(body)))
    head = f"{method} {target} HTTP/1.1\r\n{format_headers(merged)}\r\n"
    return head.encode("latin-1") + body


def parse_response(raw):
    """Return (status, headers, body) from raw response bytes."""
    head, _, body = raw.partition(b"\r\n\r\n")
    line, _, rest = head.decode("latin-1").partition("\r\n")
    return int(line.split(" ")[1]), parse_headers(rest), body


class Client:
    """Sends one request per connection."""

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    def request(self, method, url, headers=None, body=b"", timeout=None):
        parts = urlsplit(url)
        port = parts.port or 80
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        payload = build_request(method, target, parts.hostname, headers, body)
        with socket.create_connection((parts.hostname, port), timeout=timeout or self.timeout) as connection:
            connection.sendall(payload)
            chunks = []
            while chunk := connection.recv(BUFFER):
                chunks.append(chunk)
        return parse_response(b"".join(chunks))

    def get(self, url, headers=None, timeout=None):
        return self.request("GET", url, headers, timeout=timeout)

    def post(self, url, body, headers=None, timeout=None):
        return self.request("POST", url, headers, body, timeout)
