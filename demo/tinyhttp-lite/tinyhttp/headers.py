"""Case-insensitive HTTP headers."""


class Headers:
    """A header map that keeps every value and ignores name case."""

    def __init__(self, items=None):
        self._items = []
        for name, value in items or []:
            self.add(name, value)

    def add(self, name, value):
        self._items.append((name, value))

    def set(self, name, value):
        self.remove(name)
        self.add(name, value)

    def remove(self, name):
        wanted = name.lower()
        self._items = [item for item in self._items if item[0].lower() != wanted]

    def get(self, name, default=None):
        wanted = name.lower()
        for key, value in self._items:
            if key.lower() == wanted:
                return value
        return default

    def get_all(self, name):
        wanted = name.lower()
        return [value for key, value in self._items if key.lower() == wanted]

    def items(self):
        return list(self._items)

    def __contains__(self, name):
        return self.get(name) is not None

    def __len__(self):
        return len(self._items)


def parse_headers(text):
    """Parse header lines into Headers. Lines without a colon are skipped."""
    headers = Headers()
    for line in text.split("\r\n"):
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if separator and name.strip():
            headers.add(name.strip(), value.strip())
    return headers


def format_headers(headers):
    """Serialize Headers to wire format, without the final blank line."""
    return "".join(f"{name}: {value}\r\n" for name, value in headers.items())
