"""Asking for the API token in a real Chromium, against two local servers: a stand-in TDX host
and a stand-in identity provider. Headless and guarded as the background refresh runs, and
unguarded for the sign-in with a person (headless here too, so it runs anywhere; a timer on
the fake sign-in page stands in for the person). Synthetic tokens only; no real site."""

import functools
import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from api_fakes import make_jwt

from ops_suite import cli
from ops_suite.cli import main
from ops_suite.common.config import TdxApiSettings, TdxSettings
from ops_suite.tdx import api_token
from ops_suite.tdx.api_token import TokenError, fetch_token
from ops_suite.tdx.browser import TdxBrowser
from ops_suite.tdx.guard import BUILT_IN_ALLOWED

TOKEN = make_jwt(datetime.now(UTC) + timedelta(hours=24))
TOKEN_PATH = "/TDWebApi/api/auth/loginsso"
ACS_PATH = "/TDWebApi/SAML/Acs"
# The fake person clicks "Sign in" this long after the page shows: longer than the headless
# wait off the TDX host (1 s here), so only the sign-in with a person gets there.
CLICK_AFTER_MS = 2500


class _Servers:
    mode = "token"
    tdx = ""
    signin = ""
    posted: list[str] = []


class _Tdx(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path != TOKEN_PATH:
            return self._send(404, "text/plain", "")
        mode = _Servers.mode
        if mode == "token" or "back" in query or "step" in query:
            return self._send(200, "application/json", json.dumps(TOKEN))
        if mode == "page_first":
            page = '<html><script>location.href = "?step=2";</script>Signing you in</html>'
            return self._send(200, "text/html", page)
        self.send_response(302)
        self.send_header("Location", f"{_Servers.signin}/{mode}")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        _Servers.posted.append(self.path)
        if self.path == ACS_PATH and _Servers.mode == "idp_acs_answers":
            return self._send(200, "application/json", json.dumps(TOKEN))
        if self.path == ACS_PATH:  # the SAML answer: back to the token endpoint, signed in
            self.send_response(302)
            self.send_header("Location", f"{TOKEN_PATH}?back=1")
            self.end_headers()
            return None
        self._send(200, "text/plain", "posted")

    def _send(self, status: int, kind: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _SignIn(_Tdx):
    def do_POST(self) -> None:  # noqa: N802
        _Servers.posted.append(f"idp {self.path}")
        # Signed in: the SAML answer posts itself back to the Web API on the TDX host.
        form = (
            f'<form method="post" action="{_Servers.tdx}{ACS_PATH}">'
            '<input name="SAMLResponse" value="x"></form>'
            "<script>document.forms[0].submit();</script>"
        )
        self._send(200, "text/html", form)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/idp", "/idp_acs_answers"):  # asks the person to sign in again
            page = (
                '<html><form method="post" action="/idp/login">User name <input name="u">'
                "<button>Sign in</button></form><script>setTimeout(() => "
                f"document.querySelector('button').click(), {CLICK_AFTER_MS});</script></html>"
            )
            self._send(200, "text/html", page)
        elif self.path == "/idp_error":
            self._send(403, "text/html", "<html><h1>Access denied</h1></html>")
        elif self.path == "/redirect":  # signed in already: straight back with a ticket
            self.send_response(302)
            self.send_header("Location", f"{_Servers.tdx}{TOKEN_PATH}?back=1")
            self.end_headers()
        elif self.path == "/post_back":  # signed in: posts the answer back to the TDX host
            form = (
                f'<form method="post" action="{_Servers.tdx}/SSO/Acs/12345">'
                '<input name="SAMLResponse" value="x"></form>'
                "<script>document.forms[0].submit();</script>"
            )
            self._send(200, "text/html", form)
        else:  # waits for a person to sign in
            self._send(200, "text/html", "<html><form>User name <input></form></html>")


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Iterator[TdxSettings]:
    tdx_server, _Servers.tdx = _serve(_Tdx)
    signin_server, _Servers.signin = _serve(_SignIn)
    _Servers.posted = []
    monkeypatch.setattr(api_token, "OFF_HOST_SECONDS", 1)
    try:
        yield TdxSettings(
            base_url=f"{_Servers.tdx}/TDNext/",
            page_timeout_seconds=10,
            api=TdxApiSettings(enabled=True),
        )
    finally:
        for server in (tdx_server, signin_server):
            server.shutdown()
            server.server_close()


def _fetch(tdx: TdxSettings, tmp_path: Path, mode: str, *, interactive: bool = False):
    """Headless and guarded (the background refresh), or ``interactive``: unguarded, in the
    first page, as with a person at the window."""
    _Servers.mode = mode
    allowed = None if interactive else BUILT_IN_ALLOWED
    with TdxBrowser(
        tmp_path / "profile", tdx.base_url, headless=True, allowed_requests=allowed
    ) as browser:
        try:
            return fetch_token(browser, tdx, interactive=interactive), browser.blocked_requests()
        except TokenError as error:
            return error, browser.blocked_requests()


def _short_sign_in(tdx: TdxSettings, monkeypatch: pytest.MonkeyPatch, ms: int) -> TdxSettings:
    """token_sign_in_minutes = 1, with a "minute" of ``ms`` milliseconds."""
    monkeypatch.setattr(api_token, "MINUTE_MS", ms)
    return tdx.model_copy(update={"api": TdxApiSettings(enabled=True, token_sign_in_minutes=1)})


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["token", "redirect", "page_first"])
def test_the_token_is_read_after_redirects_and_pages_on_the_way(
    mode: str, servers: TdxSettings, tmp_path: Path
) -> None:
    token, blocked = _fetch(servers, tmp_path, mode)

    assert getattr(token, "value", None) == TOKEN, repr(token)
    assert blocked == []


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["waits", "idp"])
def test_headless_a_sign_in_page_waiting_for_a_person_needs_sign_in(
    mode: str, servers: TdxSettings, tmp_path: Path
) -> None:
    error, _ = _fetch(servers, tmp_path, mode)

    assert isinstance(error, TokenError), repr(error)
    assert error.reason == "needs_sign_in"
    assert api_token.load_token(datetime.now(UTC)) is None


@pytest.mark.browser
def test_a_sign_in_that_posts_back_is_blocked_by_the_guard_and_reported(
    servers: TdxSettings, tmp_path: Path
) -> None:
    error, blocked = _fetch(servers, tmp_path, "post_back")

    assert isinstance(error, TokenError)
    assert error.reason == "blocked_request"
    assert error.blocked == ("POST /SSO/Acs/#",)
    assert [record.describe() for record in blocked] == ["POST /SSO/Acs/#"]
    assert _Servers.posted == []  # the guard stopped it before it left the browser


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["idp", "idp_acs_answers"])
def test_with_a_person_the_second_sign_in_ends_with_the_token(
    mode: str, servers: TdxSettings, tmp_path: Path
) -> None:
    """The identity provider waits for a later "Sign in" click, then posts the answer back
    to the TDX host, which answers with the token (from the endpoint, or straight away)."""
    started = time.monotonic()

    token, blocked = _fetch(servers, tmp_path, mode, interactive=True)

    assert getattr(token, "value", None) == TOKEN, repr(token)
    assert time.monotonic() - started >= CLICK_AFTER_MS / 1000  # it waited for the person
    assert blocked == []
    assert _Servers.posted == ["idp /idp/login", ACS_PATH]


@pytest.mark.browser
def test_with_a_person_the_wait_is_in_the_window_they_see(
    servers: TdxSettings, tmp_path: Path
) -> None:
    _Servers.mode = "idp"
    with TdxBrowser(tmp_path / "profile", servers.base_url, headless=True) as browser:
        browser.open_start_page()
        fetch_token(browser, servers, interactive=True)

        urls = browser.current_urls()
    assert len(urls) == 1  # no new tab: the first page navigated, and it is still open
    assert urlsplit(urls[0]).path == TOKEN_PATH


@pytest.mark.browser
def test_with_a_person_a_sign_in_never_finished_times_out(
    servers: TdxSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tdx = _short_sign_in(servers, monkeypatch, 2000)

    error, _ = _fetch(tdx, tmp_path, "waits", interactive=True)

    assert isinstance(error, TokenError), repr(error)
    assert error.reason == "sign_in_timeout"
    assert api_token.load_token(datetime.now(UTC)) is None


@pytest.mark.browser
@pytest.mark.parametrize("interactive", [False, True])
def test_an_error_page_at_the_identity_provider_ends_at_once(
    interactive: bool, servers: TdxSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_token, "OFF_HOST_SECONDS", 30)
    tdx = _short_sign_in(servers, monkeypatch, 30_000)
    started = time.monotonic()

    error, _ = _fetch(tdx, tmp_path, "idp_error", interactive=interactive)

    assert isinstance(error, TokenError), repr(error)
    assert error.reason == "idp_refused"
    assert time.monotonic() - started < 15  # no wait for the off-host limit or the timeout


@pytest.mark.browser
def test_the_guard_refuses_the_identity_providers_form_post(
    servers: TdxSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the sign-in with a person runs unguarded, as tdx login does: the read-only guard
    refuses every write off the TDX host, the sign-in form's post included."""
    tdx = _short_sign_in(servers, monkeypatch, 5000)
    _Servers.mode = "idp"
    with TdxBrowser(
        tmp_path / "profile", tdx.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    ) as browser:
        with pytest.raises(TokenError) as raised:
            fetch_token(browser, tdx, interactive=True)

    assert raised.value.reason == "blocked_request"
    assert raised.value.blocked == ("POST /idp/login",)
    assert _Servers.posted == []


@pytest.mark.browser
@pytest.mark.parametrize(
    ("mode", "interactive"),
    [("idp", True), ("idp_acs_answers", True), ("idp", False), ("token", False)],
)
def test_the_token_never_reaches_the_log_or_an_error(
    mode: str,
    interactive: bool,
    servers: TdxSettings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ops_suite")

    result, _ = _fetch(servers, tmp_path, mode, interactive=interactive)

    text = " ".join(
        [caplog.text, repr(result), str(result)]
        + [repr(record.__dict__) for record in caplog.records]
    )
    assert TOKEN not in text
    assert TOKEN.split(".")[1] not in text


@pytest.mark.browser
def test_api_token_headed_end_to_end_saves_it_and_prints_only_the_expiry(
    servers: TdxSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command as the person runs it (headless here, so it runs anywhere): the second
    sign-in, then the token in the keychain; the token is in no output and no log line."""
    monkeypatch.setenv("OPS_SUITE_TDX__BASE_URL", servers.base_url)
    monkeypatch.setenv("OPS_SUITE_TDX__API__ENABLED", "true")
    monkeypatch.setattr(cli, "TdxBrowser", functools.partial(TdxBrowser, headless=True))
    _Servers.mode = "idp"

    code = main(["tdx", "api", "token", "--headed"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out.splitlines()[-1].startswith("TDX API token: saved, expires ")
    stored = api_token.load_token(datetime.now(UTC))
    assert stored is not None and stored.value == TOKEN
    log_text = (tmp_path / "applogs" / "ops-suite.log").read_text(encoding="utf-8")
    for text in (captured.out, captured.err, log_text):
        assert TOKEN not in text
        assert TOKEN.split(".")[1] not in text
