"""Build HTTP responses."""

import json

from tinyhttp.headers import Headers, format_headers

REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    206: "Partial Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    416: "Range Not Satisfiable",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class Response:
    """An HTTP response with a status, headers and a body."""

    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = Headers(headers)

    def set_header(self, name, value):
        """Set a header. The value is not sanitized."""
        self.headers.set(name, value)

    @classmethod
    def json(cls, data, status=200):
        response = cls(status, json.dumps(data))
        response.set_header("Content-Type", "application/json")
        return response

    @classmethod
    def redirect(cls, location, status=302):
        response = cls(status)
        response.set_header("Location", location)
        return response

    def to_bytes(self):
        reason = REASONS.get(self.status, "Unknown")
        self.headers.set("Content-Length", str(len(self.body)))
        head = f"HTTP/1.1 {self.status} {reason}\r\n{format_headers(self.headers)}\r\n"
        return head.encode("latin-1") + self.body
