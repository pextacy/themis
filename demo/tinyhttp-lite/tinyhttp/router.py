"""Route requests to handlers by method and path pattern."""

from tinyhttp.response import Response


class Router:
    """Matches `/users/:id` style patterns in the order they were added."""

    def __init__(self):
        self._routes = []

    def add(self, method, pattern, handler):
        self._routes.append((method, pattern.strip("/").split("/"), handler))

    def get(self, pattern, handler):
        self.add("GET", pattern, handler)

    def post(self, pattern, handler):
        self.add("POST", pattern, handler)

    @staticmethod
    def match(pattern, segments):
        """Return captured params, or None when the path does not match."""
        if len(pattern) != len(segments):
            return None
        params = {}
        for expected, actual in zip(pattern, segments, strict=True):
            if expected.startswith(":"):
                params[expected[1:]] = actual
            elif expected != actual:
                return None
        return params

    def dispatch(self, request):
        """Call the first matching handler, or answer 404 or 405."""
        segments = request.path.strip("/").split("/")
        allowed = []
        for method, pattern, handler in self._routes:
            params = self.match(pattern, segments)
            if params is None:
                continue
            if method == request.method:
                request.params = params
                return handler(request)
            allowed.append(method)
        if allowed:
            return Response(405, headers=[("Allow", ", ".join(sorted(set(allowed))))])
        return Response(404)
