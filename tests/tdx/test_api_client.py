"""The read-only TDX Web API client: the allowlist (checked before any network), the token
header, 401 and 429 handling, pacing and reason codes. A fake opener stands in for the network,
except for one test against a local HTTP server that checks redirects are refused."""

import http.server
import logging
import threading
import urllib.error
from collections.abc import Iterator
from datetime import timedelta

import pytest
from api_fakes import (
    API,
    APP,
    NOW,
    ORIGIN,
    Answer,
    FakeApi,
    FakeClock,
    api_client,
    api_tdx,
    make_token,
    stored_tokens,
)

from ops_suite.common.config import TdxSettings
from ops_suite.tdx.api_client import (
    API_ALLOWLIST,
    RATE_LIMIT_MAX_WAIT_SECONDS,
    ApiError,
    RequestRefused,
    TdxApiClient,
    check_request,
)
from ops_suite.tdx.api_token import KEYRING_SERVICE, KEYRING_TOKEN_USER, TokenSource, load_token
from ops_suite.tdx.guard import REFUSED_CREDENTIAL_PATHS

ALLOWED = [
    f"{API}/auth/getuser",
    f"{API}/reports",
    f"{API}/reports/11?withData=true",
    f"{API}/tickets/5501001",
    f"{API}/{APP}/tickets/5501001",
    f"{API}/{APP}/tickets/5501001/feed",
    f"{API}/feed/987654",
]


# --- the allowlist ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALLOWED)
def test_each_allowed_get_passes(path: str) -> None:
    assert check_request("GET", ORIGIN + path, api_tdx()) in API_ALLOWLIST


def test_the_allowlist_ignores_case_like_the_server() -> None:
    assert check_request("get", f"{ORIGIN}/tdwebapi/API/Reports/11", api_tdx())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", ALLOWED[:2] + [f"{API}/{APP}/tickets/search"])
def test_every_other_method_is_refused(method: str, path: str) -> None:
    with pytest.raises(RequestRefused) as raised:
        check_request(method, ORIGIN + path, api_tdx())

    assert raised.value.reason == "method"


@pytest.mark.parametrize("ending", REFUSED_CREDENTIAL_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_credential_endpoints_are_refused_with_any_method(ending: str, method: str) -> None:
    for path in (f"/TDWebApi{ending}", f"/TDWebApi{ending.upper()}", f"/TDWebApi{ending}/"):
        with pytest.raises(RequestRefused) as raised:
            check_request(method, ORIGIN + path, api_tdx())
        assert raised.value.reason == "credential_endpoint"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"https://elsewhere.example.edu{API}/reports", "off_host"),
        (f"http://tdx.example.edu{API}/reports", "off_host"),  # another scheme is another origin
        (f"{ORIGIN}/TDNext/Apps/43/Tickets/TicketDet.aspx", "outside_api"),
        (f"{ORIGIN}{API}/people/search", "not_allowed"),
        (f"{ORIGIN}{API}/{APP}/tickets/feed", "not_allowed"),  # the app-wide feed search
        (f"{ORIGIN}{API}/{APP}/tickets/5501001/feed/../..", "not_allowed"),
        (f"{ORIGIN}{API}/reports/11%2F..", "not_allowed"),
        (f"{ORIGIN}{API}/reports/11?withData=true&dataSortExpression=x", "query"),
        (f"{ORIGIN}{API}/{APP}/tickets/5501001/feed?top=1", "query"),
        (f"{ORIGIN}{API}/auth/loginsso", "not_allowed"),  # only ever opened in the browser
    ],
)
def test_requests_off_the_list_are_refused(url: str, reason: str) -> None:
    with pytest.raises(RequestRefused) as raised:
        check_request("GET", url, api_tdx())

    assert raised.value.reason == reason


def test_http_is_refused_except_to_this_computer() -> None:
    plain = TdxSettings(base_url="http://tdx.example.edu/", api=api_tdx().api)
    local = TdxSettings(base_url="http://127.0.0.1:8123/", api=api_tdx().api)

    with pytest.raises(RequestRefused) as raised:
        check_request("GET", f"http://tdx.example.edu{API}/reports", plain)
    assert raised.value.reason == "insecure"
    assert check_request("GET", f"http://127.0.0.1:8123{API}/reports", local)


def test_a_refused_request_never_reaches_the_network_or_asks_for_a_token() -> None:
    api = FakeApi()
    asked: list[int] = []
    tokens = TokenSource(lambda: asked.append(1) or make_token(NOW), clock=lambda: NOW)
    client = api_client(api, tokens=tokens)

    for path in ("/auth/login", "/auth/loginadmin", "/auth", "/people/search"):
        with pytest.raises(RequestRefused):
            client.get(path)
    with pytest.raises(RequestRefused):
        client.get("/reports/11", {"withData": "true", "extra": "1"})

    assert api.calls == [] and asked == []


def test_the_client_has_no_way_to_send_anything_but_get() -> None:
    public = {name for name in dir(TdxApiClient) if not name.startswith("_")}

    assert public == {"get", "url"}


# --- requests -----------------------------------------------------------------------------


def test_get_sends_the_bearer_token_and_returns_the_json() -> None:
    api = FakeApi({f"{API}/auth/getuser": {"SecurityRoleName": "Technician"}})
    client = api_client(api)

    assert client.get("/auth/getuser") == {"SecurityRoleName": "Technician"}

    method, url, headers = api.calls[0]
    token = load_token(NOW)
    assert token is not None
    assert (method, url) == ("GET", f"{ORIGIN}{API}/auth/getuser")
    assert headers["Authorization"] == f"Bearer {token.value}"
    assert headers["Accept"] == "application/json"
    assert client.requests == {"GET /auth/getuser": 1}


def test_a_query_is_encoded() -> None:
    api = FakeApi({f"{API}/reports/11": {"ID": 11}})

    api_client(api).get("/reports/11", {"withData": "true"})

    assert api.calls[0][1] == f"{ORIGIN}{API}/reports/11?withData=true"


def test_bad_json_is_a_reason_code() -> None:
    api = FakeApi({f"{API}/reports": Answer(200, raw=b"<html>not json</html>")})

    with pytest.raises(ApiError) as raised:
        api_client(api).get("/reports")

    assert (raised.value.reason, raised.value.endpoint) == ("bad_json", "GET /reports")


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "forbidden"), (404, "not_found"), (500, "http_500"), (302, "redirect")],
)
def test_http_errors_have_reason_codes(status: int, reason: str) -> None:
    api = FakeApi({f"{API}/reports": Answer(status, {"Message": "student data could be here"})})

    with pytest.raises(ApiError) as raised:
        api_client(api).get("/reports")

    assert raised.value.reason == reason
    assert str(raised.value) == f"TDX API request GET /reports failed ({reason})"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError(TimeoutError()), "timeout"),
        (TimeoutError(), "timeout"),
        (urllib.error.URLError("refused"), "connection"),
        (ConnectionResetError(), "connection"),
    ],
)
def test_network_failures_have_reason_codes(error: Exception, reason: str) -> None:
    def fail(request: object) -> object:
        raise error

    with pytest.raises(ApiError) as raised:
        api_client(FakeApi({f"{API}/reports": fail})).get("/reports")

    assert raised.value.reason == reason
    assert raised.value.__cause__ is None and raised.value.__context__ is None


def test_a_401_wipes_the_token_and_retries_once_with_a_fresh_one(memory_keyring) -> None:
    fresh = make_token(NOW + timedelta(hours=23))
    answers = [Answer(401), {"SecurityRoleName": "Technician"}]
    api = FakeApi({f"{API}/auth/getuser": lambda request: answers.pop(0)})
    client = api_client(api, tokens=stored_tokens(fetch=lambda: fresh))

    assert client.get("/auth/getuser") == {"SecurityRoleName": "Technician"}

    assert api.calls[1][2]["Authorization"] == f"Bearer {fresh.value}"
    assert memory_keyring.passwords == {(KEYRING_SERVICE, KEYRING_TOKEN_USER): fresh.value}


def test_a_second_401_is_unauthorised_and_leaves_no_token(memory_keyring) -> None:
    api = FakeApi({f"{API}/auth/getuser": Answer(401)})
    client = api_client(
        api, tokens=stored_tokens(fetch=lambda: make_token(NOW + timedelta(hours=5)))
    )

    with pytest.raises(ApiError) as raised:
        client.get("/auth/getuser")

    assert raised.value.reason == "unauthorised"
    assert len(api.calls) == 2
    assert memory_keyring.passwords == {}


def test_a_401_without_a_way_to_refresh_is_not_retried(memory_keyring) -> None:
    api = FakeApi({f"{API}/auth/getuser": Answer(401)})

    with pytest.raises(ApiError) as raised:
        api_client(api, tokens=stored_tokens()).get("/auth/getuser")

    assert raised.value.reason == "unauthorised"
    assert len(api.calls) == 1
    assert memory_keyring.passwords == {}


@pytest.mark.parametrize(
    ("reset", "wait"),
    [
        ("Wed, 21 Oct 2026 16:00:20 GMT", 20.0),  # until the reset
        ("Wed, 21 Oct 2026 16:00:01 GMT", 5.0),  # at least 5 s
        ("Wed, 21 Oct 2026 16:10:00 GMT", RATE_LIMIT_MAX_WAIT_SECONDS),  # at most 65 s
        (None, 5.0),
        ("not a date", 5.0),
    ],
)
def test_a_429_waits_for_the_reset_and_retries_once(reset: str | None, wait: float) -> None:
    headers = {"X-RateLimit-Reset": reset} if reset else {}
    answers = [Answer(429, headers=headers), [{"ID": 1}]]
    clock = FakeClock()
    client = api_client(FakeApi({f"{API}/reports": lambda r: answers.pop(0)}), clock=clock)

    assert client.get("/reports") == [{"ID": 1}]
    assert wait in clock.slept


def test_a_second_429_is_rate_limited() -> None:
    api = FakeApi({f"{API}/reports": Answer(429)})

    with pytest.raises(ApiError) as raised:
        api_client(api).get("/reports")

    assert raised.value.reason == "rate_limited"
    assert len(api.calls) == 2


def test_requests_are_paced_by_min_request_interval() -> None:
    clock = FakeClock()
    api = FakeApi({f"{API}/reports": []})
    client = api_client(api, tdx=api_tdx(min_request_interval_ms=1500), clock=clock)

    client.get("/reports")
    clock.now += 0.5  # half a second of work between two requests
    client.get("/reports")
    client.get("/reports")

    assert clock.slept == [pytest.approx(1.0), pytest.approx(1.5)]


def test_nothing_is_sent_after_the_deadline_and_the_timeout_shrinks_to_the_time_left() -> None:
    clock = FakeClock()
    timeouts: list[float | None] = []
    api = FakeApi({f"{API}/reports": []})
    real_open = api.open

    def open_(request, timeout=None):
        timeouts.append(timeout)
        return real_open(request, timeout)

    api.open = open_  # type: ignore[method-assign]
    client = TdxApiClient(
        api_tdx(),
        stored_tokens(),
        opener=api,  # type: ignore[arg-type]
        clock=clock,
        wall=lambda: NOW,
        sleep=clock.sleep,
        deadline=clock.now + 12,
    )

    client.get("/reports")
    clock.now += 10
    client.get("/reports")  # 2 s left: the request waits no longer than that
    clock.now += 5
    with pytest.raises(ApiError) as caught:
        client.get("/reports")

    assert caught.value.reason == "time_box"
    assert len(api.calls) == 2
    assert timeouts == [pytest.approx(12.0), pytest.approx(2.0)]  # never past the deadline
    assert sum(client.requests.values()) == 2


def test_the_interval_cannot_be_set_under_one_second() -> None:
    with pytest.raises(ValueError):
        api_tdx(min_request_interval_ms=500)


def test_the_token_is_never_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="ops_suite")
    api = FakeApi({f"{API}/reports": Answer(500), f"{API}/auth/getuser": Answer(401)})
    client = api_client(api)
    token = load_token(NOW)
    assert token is not None
    errors = []
    for path in ("/reports", "/auth/getuser", "/auth/login"):
        with pytest.raises(ApiError) as raised:
            client.get(path)
        errors.append(raised.value)

    shown = caplog.text + repr([r.__dict__ for r in caplog.records]) + repr(errors)
    shown += " ".join(str(error) for error in errors)
    assert token.value not in shown
    assert token.value.split(".")[1] not in shown


# --- the real opener ----------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/reports"):
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.example.edu/steal")
            self.end_headers()
            return
        body = b'{"SecurityRoleName": "Technician"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_api() -> Iterator[str]:
    _Handler.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_opener_sends_the_token_and_refuses_redirects(local_api: str) -> None:
    tdx = TdxSettings(base_url=f"{local_api}/TDNext/", api=api_tdx().api)
    client = TdxApiClient(tdx, stored_tokens(), sleep=lambda seconds: None)

    assert client.get("/auth/getuser") == {"SecurityRoleName": "Technician"}
    with pytest.raises(ApiError) as raised:
        client.get("/reports")

    assert raised.value.reason == "redirect"
    assert [path for path, _ in _Handler.seen] == [f"{API}/auth/getuser", f"{API}/reports"]
    assert all(auth and auth.startswith("Bearer ") for _, auth in _Handler.seen)
