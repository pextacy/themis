import pytest
from tinyhttp import (
    Headers,
    ParseError,
    Response,
    Router,
    build_request,
    encode_query,
    parse_cookies,
    parse_headers,
    parse_query,
    parse_range,
    parse_request,
    serialize_cookie,
)
from tinyhttp.ranges import content_range, slice_body
from tinyhttp.server import handle_bytes


def test_closed_range():
    assert parse_range("bytes=0-9", 100) == (0, 10)


def test_suffix_range():
    assert parse_range("bytes=-10", 100) == (90, 100)


@pytest.mark.parametrize("header", ["items=0-1", "bytes=a-b", "bytes=5-2", "bytes=0-1,2-3", "bytes=200-"])
def test_invalid_ranges(header):
    assert parse_range(header, 100) is None


def test_content_range():
    assert content_range(0, 10, 100) == "bytes 0-9/100"


def test_slice_body():
    assert slice_body(b"hello", "bytes=1-2") == (206, b"el")
    assert slice_body(b"hello", None) == (200, b"hello")
    assert slice_body(b"hello", "bytes=9-") == (416, b"")


def test_headers_ignore_case():
    headers = Headers([("Accept", "a"), ("accept", "b")])
    assert headers.get("ACCEPT") == "a" and headers.get_all("accept") == ["a", "b"]


def test_parse_headers_skips_junk():
    assert len(parse_headers("A: 1\r\nnonsense\r\nB: 2")) == 2


def test_parse_request():
    request = parse_request(b"POST /a?x=1 HTTP/1.1\r\nContent-Length: 2\r\n\r\nhiEXTRA")
    assert (request.method, request.path, request.query, request.body) == ("POST", "/a", {"x": ["1"]}, b"hi")


@pytest.mark.parametrize(
    "raw", [b"GET /\r\n\r\n", b"BREW / HTTP/1.1\r\n\r\n", b"GET / HTTP/2\r\n\r\n", b"GET / HTTP/1.1"]
)
def test_bad_requests(raw):
    with pytest.raises(ParseError):
        parse_request(raw)


def test_query_round_trip():
    assert parse_query(encode_query({"a": ["1", "2"], "b c": "d"})) == {"a": ["1", "2"], "b c": ["d"]}


def test_cookies():
    assert parse_cookies('a=1; b="2"') == {"a": "1", "b": "2"}
    assert serialize_cookie("a", "1", max_age=5).startswith("a=1; Path=/; Max-Age=5")
    with pytest.raises(ValueError):
        serialize_cookie("a", "1;evil")


def test_response_bytes():
    assert Response(200, "ok").to_bytes().startswith(b"HTTP/1.1 200 OK\r\nContent-Length: 2")


def test_router_params_and_errors():
    router = Router()
    router.get("/users/:id", lambda request: Response(200, request.params["id"]))
    assert b"\r\n\r\n42" in handle_bytes(router, b"GET /users/42 HTTP/1.1\r\n\r\n")
    assert handle_bytes(router, b"GET /nope HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 404")
    assert handle_bytes(router, b"POST /users/1 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 405")
    assert handle_bytes(router, b"junk").startswith(b"HTTP/1.1 400")


def test_handler_crash_is_a_500():
    router = Router()
    router.get("/", lambda request: 1 / 0)
    assert handle_bytes(router, b"GET / HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 500")


def test_build_request():
    assert build_request("GET", "/", "x.test").startswith(b"GET / HTTP/1.1\r\nHost: x.test")
