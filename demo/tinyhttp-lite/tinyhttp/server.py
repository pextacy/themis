"""A small single-threaded development server."""

import socket

from tinyhttp.parser import ParseError, parse_request
from tinyhttp.response import Response

BACKLOG = 16
BUFFER = 65536


def handle_bytes(router, raw):
    """Turn request bytes into response bytes. Never raises for bad input."""
    try:
        request = parse_request(raw)
    except ParseError:
        return Response(400, "bad request").to_bytes()
    try:
        return router.dispatch(request).to_bytes()
    except Exception:  # noqa: BLE001 - a handler bug must not stop the server
        return Response(500, "internal error").to_bytes()


class HttpServer:
    """Serves one connection at a time. For development only."""

    def __init__(self, router, host="127.0.0.1", port=8000):
        self.router = router
        self.address = (host, port)

    def serve_once(self, listener):
        connection, _ = listener.accept()
        with connection:
            connection.sendall(handle_bytes(self.router, connection.recv(BUFFER)))

    def serve_forever(self):
        with socket.create_server(self.address, backlog=BACKLOG) as listener:
            while True:
                self.serve_once(listener)
