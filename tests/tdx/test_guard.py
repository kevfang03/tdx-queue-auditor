import pytest

from ops_suite.tdx.guard import (
    BUILT_IN_ALLOWED,
    AllowedRequest,
    is_live_update,
    parse_allowed,
    request_allowed,
)

BASE = "https://tdx.example.edu/TDWorkManagement/"
WIDGET_POST = "https://tdx.example.edu/TDWorkManagement/WorkManagement/Widgets/40102"


def _allowed(method: str, url: str, resource_type: str = "fetch", allowed=BUILT_IN_ALLOWED):
    return request_allowed(method, url, allowed, base_url=BASE, resource_type=resource_type)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
@pytest.mark.parametrize(
    "url",
    [
        "https://tdx.example.edu/TDWorkManagement/WorkManagement/Dashboard/Dashboard",
        "https://cdn.example.com/lib.js",
        "https://login.example.edu/idp/SSO",
    ],
)
def test_reads_go_to_any_host(method: str, url: str) -> None:
    assert _allowed(method, url, "document")
    assert _allowed(method, url, "script", allowed=())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_to_the_tdx_host_are_blocked_without_an_entry(method: str) -> None:
    url = "https://tdx.example.edu/TDWorkManagement/api/12345/feed"

    assert not _allowed(method, url)
    assert not _allowed(method, WIDGET_POST, allowed=())


def test_the_widget_read_is_allowed_for_fetch_and_xhr_only() -> None:
    assert _allowed("POST", WIDGET_POST, "fetch")
    assert _allowed("POST", WIDGET_POST, "xhr")
    assert _allowed("POST", WIDGET_POST + "?page=2", "fetch")
    # A form postback to the same path, and other methods, stay blocked.
    assert not _allowed("POST", WIDGET_POST, "document")
    assert not _allowed("PUT", WIDGET_POST, "fetch")
    assert not _allowed("DELETE", WIDGET_POST, "xhr")


@pytest.mark.parametrize(
    "path",
    [
        "/TDWorkManagement/WorkManagement/Widgets/40102/Delete",
        "/TDWorkManagement/WorkManagement/Widgets/12",  # a short id is not masked
        "/TDWorkManagement/WorkManagement/Widgets/abc",
        "/TDWorkManagement/WorkManagement/Widgets/",
        "/tdworkmanagement/workmanagement/widgets/40102",
        "/TDWorkManagement/WorkManagement/Widgets/40102/../../Tickets/Edit",
    ],
)
def test_the_widget_pattern_matches_the_exact_masked_path_only(path: str) -> None:
    assert not _allowed("POST", f"https://tdx.example.edu{path}")


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example.edu/TDWorkManagement/WorkManagement/Widgets/40102",
        "http://tdx.example.edu/TDWorkManagement/WorkManagement/Widgets/40102",
        "https://tdx.example.edu:8443/TDWorkManagement/WorkManagement/Widgets/40102",
        "data:text/plain,hello",
        "not a url",
    ],
)
def test_an_allowlisted_pattern_on_another_host_is_blocked(url: str) -> None:
    assert not _allowed("POST", url)


def test_masked_digit_matching() -> None:
    allowed = (AllowedRequest("POST", "/TDWorkManagement/api/#/search/#", "test"),)

    assert _allowed(
        "POST", "https://tdx.example.edu/TDWorkManagement/api/123/search/4567", "xhr", allowed
    )
    assert not _allowed(
        "POST", "https://tdx.example.edu/TDWorkManagement/api/12/search/4567", "xhr", allowed
    )


def test_wildcards_in_patterns() -> None:
    allowed = parse_allowed(["post /TDWorkManagement/api/*/search"])

    assert allowed == (
        AllowedRequest("POST", "/TDWorkManagement/api/*/search", "from tdx.allowed_requests"),
    )
    assert _allowed(
        "POST", "https://tdx.example.edu/TDWorkManagement/api/x/search", "fetch", allowed
    )
    assert not _allowed(
        "POST", "https://tdx.example.edu/TDWorkManagement/api/x/search", "document", allowed
    )


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "POST",
        "/TDWorkManagement/x",
        "POST /a /b",
        "GET /TDWorkManagement/x",
        "head /TDWorkManagement/x",
        "OPTIONS /TDWorkManagement/x",
        "P0ST /TDWorkManagement/x",
        "POST TDWorkManagement/x",
        "POST https://tdx.example.edu/TDWorkManagement/x",
        "POST /TDWorkManagement/x?id=1",
    ],
)
def test_bad_config_entries_raise(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_allowed([entry])


def test_built_in_entries_are_justified_writes() -> None:
    assert BUILT_IN_ALLOWED
    for entry in BUILT_IN_ALLOWED:
        assert entry.method not in {"GET", "HEAD", "OPTIONS"}
        assert entry.method == entry.method.upper()
        assert entry.path_pattern.startswith("/")
        assert entry.reason and "\n" not in entry.reason
        assert "document" not in entry.resource_types


FEED_SEARCH = "https://tdx.example.edu/TDNext/Apps/Feed/Search"


def test_the_feed_search_is_allowed_for_xhr_and_fetch_only() -> None:
    assert _allowed("POST", FEED_SEARCH, "xhr")
    assert _allowed("POST", FEED_SEARCH, "fetch")
    assert _allowed("POST", FEED_SEARCH + "?ticketId=1", "xhr")
    assert not _allowed("POST", FEED_SEARCH, "document")
    assert not _allowed("PUT", FEED_SEARCH, "xhr")


@pytest.mark.parametrize(
    "url",
    [
        "https://tdx.example.edu/TDNext/Apps/Feed/Search/Delete",
        "https://tdx.example.edu/TDNext/Apps/Feed/Searches",
        "https://tdx.example.edu/TDNext/Apps/Feed/Update",
        "https://tdx.example.edu/tdnext/apps/feed/search",
        "https://tdx.example.edu/TDNext/Apps/43/Feed/Search",
        "https://elsewhere.example.edu/TDNext/Apps/Feed/Search",
    ],
)
def test_the_feed_search_entry_is_the_exact_path_on_the_tdx_host(url: str) -> None:
    assert not _allowed("POST", url, "xhr")


@pytest.mark.parametrize(
    ("url", "live"),
    [
        ("https://tdx.example.edu/TDSignalR/signalr/hubs", True),
        ("https://tdx.example.edu/TDSignalR/signalr/negotiate?clientProtocol=2.1", True),
        ("wss://tdx.example.edu/TDSignalR/signalr/connect?transport=webSockets", True),
        ("https://tdx.example.edu/TDSignalR/signalr/poll", True),
        ("https://tdx.example.edu/TDNext/Apps/Feed/Search", False),
        ("https://tdx.example.edu/TDSignalRx/hubs", False),
        ("https://tdx.example.edu/tdsignalr/signalr/hubs", False),
        ("not a url", False),
    ],
)
def test_live_updates_are_recognised_by_path(url: str, live: bool) -> None:
    assert is_live_update(url) is live
