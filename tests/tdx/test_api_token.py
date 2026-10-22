"""The TDX Web API token: its shape, the keychain, asking for one and when to ask again.
Synthetic tokens only, built at run time; the keychain is the tests' in-memory one."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from api_fakes import NOW, TZ, api_tdx, make_jwt, make_token

from ops_suite.tdx import api_token
from ops_suite.tdx.api_token import (
    KEYRING_SERVICE,
    KEYRING_TOKEN_USER,
    PART_CHARS,
    ApiToken,
    TokenError,
    TokenSource,
    expiry_text,
    fetch_token,
    load_token,
    needs_refresh,
    parse_token,
    save_token,
    token_url,
    wipe_token,
)
from ops_suite.tdx.browser import DocumentAnswer, RequestRecord

LATER = NOW + timedelta(hours=20)


# --- the shape --------------------------------------------------------------------------


def test_a_bare_token_and_its_expiry() -> None:
    text = make_jwt(LATER)

    token = parse_token(f"  {text}\n")

    assert token.value == text
    assert token.expires == LATER.replace(microsecond=0)


@pytest.mark.parametrize("wrap", ['"{}"', "Bearer {}", ' "{}" '])
def test_a_token_in_a_json_string_or_after_bearer(wrap: str) -> None:
    text = make_jwt(LATER)

    assert parse_token(wrap.format(text)).value == text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "not_a_jwt"),
        ("<html><body>Sign in</body></html>", "not_a_jwt"),
        ("abc.def", "not_a_jwt"),
        ("a.b.c", "not_a_jwt"),  # segments that are not JSON
        ('"not a token"', "not_a_jwt"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig", "no_expiry"),  # no exp claim
        ("eyJhbGciOiJIUzI1NiJ9.eyJleHAiOiJ0b21vcnJvdyJ9.sig", "no_expiry"),  # exp not a number
    ],
)
def test_text_that_is_not_a_token_is_refused_without_quoting_it(text: str, reason: str) -> None:
    with pytest.raises(TokenError) as raised:
        parse_token(text)

    assert raised.value.reason == reason
    assert str(raised.value) == f"TDX API token: {reason}"


def test_repr_and_str_never_show_the_token() -> None:
    token = make_token(LATER)

    for shown in (repr(token), str(token), f"{token}", repr([token])):
        assert token.value not in shown
        assert "expires" in shown


def test_refresh_is_due_within_thirty_minutes_of_expiry() -> None:
    token = make_token(NOW + timedelta(minutes=31))

    assert not needs_refresh(token, NOW)
    assert needs_refresh(token, NOW + timedelta(minutes=2))
    assert needs_refresh(token, NOW + timedelta(hours=1))


def test_expiry_text_is_weekday_and_time_in_the_desk_zone() -> None:
    token = make_token(datetime(2026, 10, 21, 13, 14, tzinfo=UTC))

    assert expiry_text(token, TZ) == "Wed 09:14"


# --- the keychain -----------------------------------------------------------------------


def test_save_load_and_wipe(memory_keyring) -> None:
    token = make_token(LATER)

    save_token(token)

    assert memory_keyring.passwords == {(KEYRING_SERVICE, KEYRING_TOKEN_USER): token.value}
    assert load_token(NOW) == token
    wipe_token()
    assert memory_keyring.passwords == {}
    assert load_token(NOW) is None


def test_a_long_token_is_split_over_entries_and_put_back_together(memory_keyring) -> None:
    token = make_token(LATER, filler=2500)
    assert len(token.value) > 2 * PART_CHARS

    save_token(token)

    stored = memory_keyring.passwords
    assert stored[(KEYRING_SERVICE, KEYRING_TOKEN_USER)] == "parts:4"
    assert all(len(value) <= PART_CHARS for value in stored.values())
    assert load_token(NOW) == token
    save_token(make_token(LATER))  # a shorter one replaces every part
    assert len(memory_keyring.passwords) == 1
    wipe_token()
    assert memory_keyring.passwords == {}


def test_an_expired_or_broken_stored_token_is_wiped(memory_keyring) -> None:
    save_token(make_token(NOW - timedelta(minutes=1)))

    assert load_token(NOW) is None
    assert memory_keyring.passwords == {}

    memory_keyring.passwords[(KEYRING_SERVICE, KEYRING_TOKEN_USER)] = "not a token"
    assert load_token(NOW) is None
    assert memory_keyring.passwords == {}


# --- asking for one ---------------------------------------------------------------------


class FakeBrowser:
    """Answers ``read_document`` with a canned answer, the guard refusing ``blocked`` on the
    way; records what was asked."""

    def __init__(
        self,
        answer: DocumentAnswer,
        blocked: list[RequestRecord] | None = None,
        seen: list[RequestRecord] | None = None,
    ):
        self.answer = answer
        self.to_block = blocked or []
        self.to_see = seen or []
        self.blocked: list[RequestRecord] = []
        self.seen: list[RequestRecord] = []
        self.asked: list[tuple[str, str]] = []
        self.options: dict[str, object] = {}

    def read_document(self, url: str, *, path_suffix: str, **kwargs: object) -> DocumentAnswer:
        self.asked.append((url, path_suffix))
        self.options = kwargs
        self.blocked.extend(self.to_block)
        self.seen.extend(self.to_see)
        return self.answer

    def blocked_requests(self) -> list[RequestRecord]:
        return list(self.blocked)

    def requests_seen(self) -> list[RequestRecord]:
        return list(self.seen)


TOKEN_PAGE = "https://tdx.example.edu/TDWebApi/api/auth/loginsso"


def test_the_token_endpoint_address_follows_the_settings() -> None:
    assert token_url(api_tdx()) == TOKEN_PAGE
    assert token_url(api_tdx(base_path="/SBTDWebApi/api")).endswith("/SBTDWebApi/api/auth/loginsso")


def test_fetch_reads_the_token_from_the_answer() -> None:
    text = make_jwt(LATER)
    browser = FakeBrowser(DocumentAnswer(TOKEN_PAGE, 200, text))

    token = fetch_token(browser, api_tdx())  # type: ignore[arg-type]

    assert token.value == text
    assert browser.asked == [(TOKEN_PAGE, "/TDWebApi/api/auth/loginsso")]


@pytest.mark.parametrize(
    "answer",
    [
        DocumentAnswer(TOKEN_PAGE, 401, "", ended="settled"),
        DocumentAnswer(TOKEN_PAGE, 403, "", ended="settled"),
        DocumentAnswer("https://login.example.edu/idp/profile/SAML2/Redirect/SSO", None),
    ],
)
def test_headless_fetch_that_needs_a_person_is_needs_sign_in(answer: DocumentAnswer) -> None:
    """The Web API's own sign-in wants a person: a token matter, not an expired session."""
    with pytest.raises(TokenError) as raised:
        fetch_token(FakeBrowser(answer), api_tdx())  # type: ignore[arg-type]

    assert raised.value.reason == "needs_sign_in"


def test_headless_fetch_waits_off_the_host_briefly_in_a_new_page() -> None:
    browser = FakeBrowser(DocumentAnswer(TOKEN_PAGE, 200, make_jwt(LATER)))

    fetch_token(browser, api_tdx())  # type: ignore[arg-type]

    assert browser.options["off_host_seconds"] == api_token.OFF_HOST_SECONDS
    assert not browser.options.get("in_first_page")


def test_interactive_fetch_waits_in_the_window_for_the_sign_in_minutes() -> None:
    browser = FakeBrowser(DocumentAnswer(TOKEN_PAGE, 200, make_jwt(LATER)))

    fetch_token(browser, api_tdx(token_sign_in_minutes=7), interactive=True)  # type: ignore[arg-type]

    assert browser.options["timeout_ms"] == 7 * api_token.MINUTE_MS
    assert browser.options["off_host_seconds"] is None
    assert browser.options["in_first_page"] is True
    assert browser.options["accept_any_path"] is True


@pytest.mark.parametrize(
    ("answer", "interactive", "reason"),
    [
        (
            DocumentAnswer("https://login.example.edu/sso", None, ended="timeout"),
            True,
            "sign_in_timeout",
        ),
        (
            DocumentAnswer("https://login.example.edu/sso", None, ended="closed"),
            True,
            "window_closed",
        ),
        (
            DocumentAnswer(
                "https://login.example.edu/err", None, ended="refused", refused_status=403
            ),
            True,
            "idp_refused",
        ),
        (
            DocumentAnswer(
                "https://login.example.edu/err", None, ended="refused", refused_status=500
            ),
            False,
            "idp_refused",
        ),
        (DocumentAnswer(TOKEN_PAGE, 401, "", ended="settled"), True, "http_401"),
    ],
)
def test_sign_in_outcomes_have_reason_codes(
    answer: DocumentAnswer, interactive: bool, reason: str
) -> None:
    with pytest.raises(TokenError) as raised:
        fetch_token(FakeBrowser(answer), api_tdx(), interactive=interactive)  # type: ignore[arg-type]

    assert raised.value.reason == reason
    assert str(raised.value) == f"TDX API token: {reason}"


def test_writes_seen_on_the_way_are_logged_by_masked_path_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ops_suite")
    seen = [
        RequestRecord("GET", "document", "https://tdx.example.edu/TDWebApi/api/auth/loginsso"),
        RequestRecord("POST", "document", "https://tdx.example.edu/TDWebApi/SAML/Acs/12345?x=s"),
    ]
    browser = FakeBrowser(DocumentAnswer(TOKEN_PAGE, 200, make_jwt(LATER)), seen=seen)

    fetch_token(browser, api_tdx(), interactive=True)  # type: ignore[arg-type]

    (record,) = [r for r in caplog.records if r.getMessage() == "api token page read"]
    assert record.non_get == ["POST /TDWebApi/SAML/Acs/#"]  # type: ignore[attr-defined]


def test_fetch_reports_blocked_requests_by_masked_path_only() -> None:
    record = RequestRecord("POST", "document", "https://tdx.example.edu/SSO/Acs/12345?x=secret")
    browser = FakeBrowser(DocumentAnswer("https://login.example.edu/saml", None), [record])

    with pytest.raises(TokenError) as raised:
        fetch_token(browser, api_tdx())  # type: ignore[arg-type]

    assert raised.value.reason == "blocked_request"
    assert raised.value.blocked == ("POST /SSO/Acs/#",)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (DocumentAnswer(TOKEN_PAGE, 500, "oops"), "http_500"),
        (DocumentAnswer(TOKEN_PAGE, None), "no_answer"),
        (DocumentAnswer(TOKEN_PAGE, 200, "<html>a page, not a token</html>"), "not_a_jwt"),
    ],
)
def test_fetch_failures_have_reason_codes(answer: DocumentAnswer, reason: str) -> None:
    with pytest.raises(TokenError) as raised:
        fetch_token(FakeBrowser(answer), api_tdx())  # type: ignore[arg-type]

    assert raised.value.reason == reason


def test_the_document_answer_repr_leaves_the_text_out() -> None:
    text = make_jwt(LATER)

    assert text not in repr(DocumentAnswer(TOKEN_PAGE, 200, text))


# --- the token source ---------------------------------------------------------------------


class Fetcher:
    def __init__(self, *tokens: ApiToken | Exception) -> None:
        self.tokens = list(tokens)
        self.calls = 0

    def __call__(self) -> ApiToken:
        self.calls += 1
        item = self.tokens.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_a_stored_token_with_time_left_is_used_without_asking() -> None:
    stored = make_token(LATER)
    save_token(stored)
    fetch = Fetcher()

    assert TokenSource(fetch, clock=lambda: NOW).get() == stored
    assert fetch.calls == 0


def test_a_token_close_to_expiry_is_replaced_and_stored(memory_keyring) -> None:
    save_token(make_token(NOW + timedelta(minutes=20)))
    fresh = make_token(LATER)
    fetch = Fetcher(fresh)
    source = TokenSource(fetch, clock=lambda: NOW)

    assert source.get() == fresh
    assert source.get() == fresh  # kept in use, not asked again
    assert fetch.calls == 1 and source.fetched == 1
    assert load_token(NOW) == fresh


def test_no_token_and_no_way_to_ask_raises_no_token() -> None:
    with pytest.raises(TokenError) as raised:
        TokenSource(None, clock=lambda: NOW).get()

    assert raised.value.reason == "no_token"


def test_an_expired_token_and_a_refresh_that_needs_a_sign_in_raise_it(memory_keyring) -> None:
    save_token(make_token(NOW - timedelta(hours=1)))
    source = TokenSource(Fetcher(TokenError("needs_sign_in")), clock=lambda: NOW)

    with pytest.raises(TokenError) as raised:
        source.get()
    assert raised.value.reason == "needs_sign_in"
    assert memory_keyring.passwords == {}  # the expired one was wiped


def test_a_refresh_that_needs_a_sign_in_keeps_using_a_still_valid_token() -> None:
    """The API reader's refresh across a fake clock: while the old token lasts, a refresh
    that needs a sign-in is no failure at all; once it has expired, it is needs_sign_in."""
    stored = make_token(NOW + timedelta(minutes=10))
    save_token(stored)
    clock = [NOW]
    fetch = Fetcher(TokenError("needs_sign_in"), TokenError("needs_sign_in"))
    source = TokenSource(fetch, clock=lambda: clock[0])

    assert source.get() == stored
    clock[0] = NOW + timedelta(minutes=11)
    with pytest.raises(TokenError) as raised:
        source.get()
    assert raised.value.reason == "needs_sign_in"
    assert fetch.calls == 2


def test_rejected_wipes_the_token_and_the_next_get_asks_again(memory_keyring) -> None:
    save_token(make_token(LATER))
    fresh = make_token(LATER + timedelta(hours=1))
    fetch = Fetcher(fresh)
    source = TokenSource(fetch, clock=lambda: NOW)
    source.get()

    source.rejected()

    assert memory_keyring.passwords == {}
    assert source.get() == fresh
    assert fetch.calls == 1


def test_the_token_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="ops_suite")
    old = make_token(NOW + timedelta(minutes=5))
    fresh = make_token(LATER)
    save_token(old)
    source = TokenSource(Fetcher(fresh), clock=lambda: NOW)

    source.get()
    source.rejected()
    save_token(make_token(NOW - timedelta(hours=1)))
    load_token(NOW)

    text = caplog.text + " ".join(repr(record.__dict__) for record in caplog.records)
    for token in (old, fresh):
        assert token.value not in text
        assert token.value.split(".")[1] not in text
