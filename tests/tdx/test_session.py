import pytest

from ops_suite.tdx.session import describe_url, on_tdx, tdx_origin

BASE = "https://tdx.example.edu/TDNext/"


def test_tdx_origin() -> None:
    assert tdx_origin(BASE) == "https://tdx.example.edu"
    assert tdx_origin("HTTP://TDX.Example.edu:8080/x?y=1") == "http://tdx.example.edu:8080"
    assert tdx_origin("https://tdx.example.edu:443/") == "https://tdx.example.edu"


@pytest.mark.parametrize(
    "bad", ["", "   ", "file:///C:/tdx/index.html", "ftp://tdx.example.edu/", "tdx.example.edu"]
)
def test_tdx_origin_rejects_non_web_urls(bad: str) -> None:
    with pytest.raises(ValueError):
        tdx_origin(bad)


def test_on_tdx_for_a_deep_link_on_the_same_host() -> None:
    url = "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID=3119232#feed"

    assert on_tdx(url, BASE)
    assert on_tdx("https://TDX.example.edu/", BASE)


@pytest.mark.parametrize(
    "url",
    [
        "https://login.example.edu/idp/profile/SAML2/Redirect/SSO",
        "https://login.microsoftonline.example/common/oauth2",
        "http://tdx.example.edu/TDNext/",
        "https://tdx.example.edu:8443/TDNext/",
        "https://tdx.example.edu.evil.example/TDNext/",
        "about:blank",
        "",
    ],
)
def test_on_tdx_is_false_elsewhere(url: str) -> None:
    assert not on_tdx(url, BASE)


def test_on_tdx_is_false_for_an_unusable_base_url() -> None:
    assert not on_tdx("https://tdx.example.edu/", "")


def test_describe_url_drops_query_and_masks_digits() -> None:
    url = (
        "https://alex:secret@tdx.example.edu/TDNext/Apps/43/Tickets/3119232/Det?TicketID=3119232#x"
    )

    assert describe_url(url) == "tdx.example.edu/TDNext/Apps/43/Tickets/#/Det"


def test_describe_url_keeps_a_port_and_shows_an_empty_path() -> None:
    assert describe_url("http://127.0.0.1:8123/widget.html?ReportID=88125") == (
        "127.0.0.1:8123/widget.html"
    )
    assert describe_url("https://tdx.example.edu?x=1") == "tdx.example.edu/"


def test_describe_url_hides_the_body_of_non_web_urls() -> None:
    assert describe_url("data:text/html,<p>Alex Example</p>") == "data:"
    assert describe_url("blob:https://tdx.example.edu/1234-5678") == "blob:"
    assert describe_url("about:blank") == "about:blank"
    assert describe_url("TicketDet?TicketID=3119232") == "[relative url]"
