"""The TDX Web API token: issued by the SSO sign-in of the person's own browser profile.

The Web API's ``GET <base_path>/auth/loginsso`` "logs in the current session using single
sign-on" and answers with the token as text. Opened in the tool's TDX browser profile, it
issues a token for the person signed in there, with that person's own rights.

The Web API is a sign-in service provider of its own, apart from the TDX web pages: the
endpoint sends the page to the identity provider, which may ask the person to sign in again
(MFA included) even while the TDX session is fine. So there are two ways to ask:
- **with the person** (``tdx login`` right after the TDX sign-in when ``tdx.api.enabled`` is
  set, and ``tdx api token --headed``): the endpoint opens in the window they see, and the
  wait ends when the token arrives, up to ``tdx.api.token_sign_in_minutes``. The person signs
  in; this code never fills in or submits the identity provider's pages;
- **headless** (``tdx api token``, and the API reader when the stored token is about to expire
  or was refused), with the saved profile and the read-only request guard. When the identity
  provider wants a person, it gives up with ``needs_sign_in``: a token matter, never an
  expired TDX session, so it never writes the session log or the watch loop's signed-out
  state.

The token is a JSON Web Token. Its ``exp`` claim is read without checking the signature: it is
used only to know when to ask again. Tokens expire 24 hours after issue.

The token is a secret:
- it lives only in ``keyring`` (service ``ops-suite``, user ``tdx-api-token``). Windows' Credential
  Manager holds at most about 1,280 characters per entry, so a longer token is split over
  ``tdx-api-token-1``, ``-2``, ... and the main entry says how many;
- it is never printed, logged, written to a file, put in config or an exception, or shown by
  ``repr()``. Messages and logs carry reason codes and the expiry time only;
- it is wiped from the keychain once it has expired, and when the API answers 401.

No other credential is ever used: no user name and password and no service-account keys (see
``tdx/guard.py``, ``REFUSED_CREDENTIAL_PATHS``).
"""

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import keyring
import keyring.errors

from ops_suite.common import paths
from ops_suite.common.config import Settings, TdxSettings
from ops_suite.tdx.browser import BrowserStartError, TdxBrowser
from ops_suite.tdx.guard import BUILT_IN_ALLOWED, READ_METHODS, parse_allowed
from ops_suite.tdx.session import describe_path, on_tdx, tdx_origin

log = logging.getLogger(__name__)

KEYRING_SERVICE = "ops-suite"  # the same service as the webhooks and the Anthropic key
KEYRING_TOKEN_USER = "tdx-api-token"
TOKEN_PATH = "/auth/loginsso"
# A token with less than this left is replaced before it is used.
REFRESH_BEFORE = timedelta(minutes=30)
# The Web API's tokens expire this long after they are issued.
TOKEN_LIFETIME = timedelta(hours=24)
# Characters per keychain entry: under Windows' limit of 2,560 bytes (UTF-16) per credential.
PART_CHARS = 1000
_PARTS_PREFIX = "parts:"
_SEGMENT = r"[A-Za-z0-9_-]+"
_JWT = re.compile(rf"{_SEGMENT}\.{_SEGMENT}\.[A-Za-z0-9_-]*")
# Headless: a page that stays off the TDX host this long without the token waits for a person.
OFF_HOST_SECONDS = 10
# With the person: tdx.api.token_sign_in_minutes is counted in these.
MINUTE_MS = 60_000


class TokenError(Exception):
    """No usable token. ``reason`` is a fixed code; the message never holds the token."""

    def __init__(self, reason: str, *, blocked: tuple[str, ...] = ()) -> None:
        super().__init__(f"TDX API token: {reason}")
        self.reason = reason
        # "METHOD /masked/path" of requests the guard refused while asking for the token.
        self.blocked = blocked


@dataclass(frozen=True, slots=True, repr=False)
class ApiToken:
    """A token and its expiry (aware, UTC). ``value`` is a secret: it never leaves this
    object except into the ``Authorization`` header and the keychain."""

    value: str = field(repr=False)
    expires: datetime

    def __repr__(self) -> str:
        return f"ApiToken(expires={self.expires.isoformat()!r})"

    __str__ = __repr__


# --- the token's shape --------------------------------------------------------------------


def _segment_json(segment: str) -> object:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def parse_token(text: str) -> ApiToken:
    """The token in an answer of the token endpoint: the bare token, or a JSON string holding
    it. TokenError ``not_a_jwt`` or ``no_expiry``; the message never quotes the text."""
    candidate = text.strip()
    if candidate.startswith('"'):
        try:
            loaded = json.loads(candidate)
        except ValueError:
            loaded = None
        candidate = loaded.strip() if isinstance(loaded, str) else ""
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    if not _JWT.fullmatch(candidate):
        raise TokenError("not_a_jwt")
    header_part, payload_part, _ = candidate.split(".")
    try:
        header = _segment_json(header_part)
        payload = _segment_json(payload_part)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise TokenError("not_a_jwt") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenError("not_a_jwt")
    expiry = payload.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int | float):
        raise TokenError("no_expiry")
    try:
        expires = datetime.fromtimestamp(expiry, UTC)
    except (OverflowError, OSError, ValueError):
        raise TokenError("no_expiry") from None
    return ApiToken(candidate, expires)


def needs_refresh(token: ApiToken, now: datetime) -> bool:
    """True when ``token`` has less than ``REFRESH_BEFORE`` left (or has expired)."""
    return token.expires - now < REFRESH_BEFORE


def expiry_text(token: ApiToken, tz: ZoneInfo) -> str:
    """``Wed 09:14``: the expiry in the desk's time zone, for messages."""
    return f"{token.expires.astimezone(tz):%a %H:%M}"


# --- the keychain -------------------------------------------------------------------------


def _part_user(number: int) -> str:
    return f"{KEYRING_TOKEN_USER}-{number}"


def _read_stored() -> str | None:
    head = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_USER)
    if not head or not head.startswith(_PARTS_PREFIX):
        return head or None
    try:
        count = int(head[len(_PARTS_PREFIX) :])
    except ValueError:
        return None
    parts = [keyring.get_password(KEYRING_SERVICE, _part_user(n)) for n in range(1, count + 1)]
    if count < 1 or any(not part for part in parts):
        return None
    return "".join(part for part in parts if part)


def _delete(user: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, user)
    except keyring.errors.PasswordDeleteError:
        pass  # nothing stored under it


def wipe_token() -> None:
    """Remove the stored token, every part of it included. Nothing stored is not an error."""
    head = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_USER)
    count = 0
    if head and head.startswith(_PARTS_PREFIX):
        try:
            count = int(head[len(_PARTS_PREFIX) :])
        except ValueError:
            count = 0
    _delete(KEYRING_TOKEN_USER)
    for number in range(1, count + 1):
        _delete(_part_user(number))


def save_token(token: ApiToken) -> None:
    """Store ``token`` in the system keychain, replacing any earlier one."""
    wipe_token()
    value = token.value
    if len(value) <= PART_CHARS:
        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_USER, value)
        return
    parts = [value[start : start + PART_CHARS] for start in range(0, len(value), PART_CHARS)]
    for number, part in enumerate(parts, start=1):
        keyring.set_password(KEYRING_SERVICE, _part_user(number), part)
    keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_USER, f"{_PARTS_PREFIX}{len(parts)}")


def load_token(now: datetime) -> ApiToken | None:
    """The stored token while it is valid. An expired or unreadable one is wiped: None."""
    stored = _read_stored()
    if stored is None:
        return None
    try:
        token = parse_token(stored)
    except TokenError as error:
        log.info("stored api token unusable, wiped", extra={"reason": error.reason})
        wipe_token()
        return None
    if token.expires <= now:
        log.info("stored api token expired, wiped")
        wipe_token()
        return None
    return token


# --- asking for a token ------------------------------------------------------------------


def _is_token(text: str) -> bool:
    try:
        parse_token(text)
    except TokenError:
        return False
    return True


def token_url(tdx: TdxSettings) -> str:
    """The token endpoint's address on ``tdx.base_url``'s host."""
    return tdx_origin(tdx.base_url) + tdx.api.base_path + TOKEN_PATH


def fetch_token(browser: TdxBrowser, tdx: TdxSettings, *, interactive: bool = False) -> ApiToken:
    """Open the token endpoint in ``browser`` and read the token from its answer.

    The Web API is a sign-in service provider of its own: the endpoint may send the page to
    the identity provider, which may ask the person to sign in again, MFA included.

    Headless (``interactive`` False), in a new page: the saved profile must be enough. A page
    that stays off the TDX host for ``OFF_HOST_SECONDS`` waits for a person, and so does a
    401 or 403 from the endpoint: TokenError ``needs_sign_in``. That is about the API's own
    sign-in, not the TDX session, which may be fine.

    ``interactive``: the person is at ``browser``'s window. The endpoint opens in the page
    they see, with no limit off the TDX host, and the wait ends by itself when the token
    arrives on the TDX host, else after ``tdx.api.token_sign_in_minutes``
    (``sign_in_timeout``) or when the window is closed (``window_closed``). This code only
    waits on the page: it never fills in, clicks or submits anything there.

    Either way an error page off the TDX host ends the wait at once: ``idp_refused``. Other
    TokenError reasons: ``http_<status>``, ``blocked_request`` (the guard refused a request
    on the way), ``not_a_jwt``, ``no_answer``. The answer's text is read for the token only
    and dropped.
    """
    url = token_url(tdx)
    blocked_before = len(browser.blocked_requests())
    seen_before = len(browser.requests_seen())
    if interactive:
        answer = browser.read_document(
            url,
            path_suffix=tdx.api.base_path + TOKEN_PATH,
            accept=_is_token,
            timeout_ms=tdx.api.token_sign_in_minutes * MINUTE_MS,
            off_host_seconds=None,
            in_first_page=True,
            accept_any_path=True,
        )
    else:
        answer = browser.read_document(
            url,
            path_suffix=tdx.api.base_path + TOKEN_PATH,
            accept=_is_token,
            timeout_ms=tdx.page_timeout_seconds * 1000,
            off_host_seconds=OFF_HOST_SECONDS,
        )
    blocked = tuple(record.describe() for record in browser.blocked_requests()[blocked_before:])
    log.info(
        "api token page read",
        extra={
            "interactive": interactive,
            "status": answer.status,
            "ended": answer.ended,
            "refused_status": answer.refused_status,
            "path": describe_path(answer.url),
            "on_tdx": on_tdx(answer.url, tdx.base_url),
            "blocked": list(blocked),
            # TDX-host writes on the way (unguarded only), as "METHOD /masked/path": what a
            # guarded sign-in would have to allow.
            "non_get": [
                record.describe()
                for record in browser.requests_seen()[seen_before:]
                if record.method.upper() not in READ_METHODS
            ],
        },
    )
    if answer.status == 200:
        return parse_token(answer.text)
    if answer.ended == "refused":
        raise TokenError("idp_refused")
    if answer.status in (401, 403) and not interactive:
        raise TokenError("needs_sign_in")
    if answer.status is not None:
        raise TokenError(f"http_{answer.status}", blocked=blocked)
    if blocked:
        raise TokenError("blocked_request", blocked=blocked)
    if answer.ended == "closed":
        raise TokenError("window_closed")
    if interactive and answer.ended == "timeout":
        raise TokenError("sign_in_timeout")
    if not on_tdx(answer.url, tdx.base_url):
        raise TokenError("needs_sign_in")
    raise TokenError("no_answer")


def browser_fetcher(settings: Settings) -> Callable[[], ApiToken]:
    """Ask for a fresh token headless, with the saved profile and the request guard."""
    tdx = settings.tdx

    def fetch() -> ApiToken:
        allowed = BUILT_IN_ALLOWED + parse_allowed(tdx.allowed_requests)
        with TdxBrowser(
            paths.tdx_profile_dir(settings),
            tdx.base_url,
            headless=True,
            allowed_requests=allowed,
            launch_timeout_seconds=tdx.browser_launch_timeout_seconds,
        ) as browser:
            return fetch_token(browser, tdx)

    return fetch


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TokenSource:
    """The token for the next API call.

    The token already in use, else the stored one, while it has ``REFRESH_BEFORE`` left; else
    a fresh one from ``fetch`` (stored before it is used). If asking fails while the old one
    is still valid, the old one is used and the failure logged. ``rejected`` (after a 401)
    wipes the stored token, so the next ``get`` asks again. ``fetch`` None: stored tokens only.
    """

    def __init__(
        self,
        fetch: Callable[[], ApiToken] | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._token: ApiToken | None = None
        self.fetched = 0  # fresh tokens asked for

    @property
    def can_refresh(self) -> bool:
        return self._fetch is not None

    def get(self) -> ApiToken:
        now = self._clock()
        token = self._token
        if token is None or token.expires <= now:
            token = load_token(now)
        if token is not None and not needs_refresh(token, now):
            self._token = token
            return token
        if self._fetch is None:
            if token is not None:
                self._token = token
                return token
            raise TokenError("no_token")
        try:
            fresh = self._fetch()
        except (TokenError, BrowserStartError) as error:
            if token is None:
                raise
            log.info(
                "api token refresh failed; the current one is still valid",
                extra={"error": type(error).__name__, "reason": getattr(error, "reason", "")},
            )
            self._token = token
            return token
        save_token(fresh)
        self.fetched += 1
        self._token = fresh
        log.info("api token saved", extra={"expires": fresh.expires.isoformat()})
        return fresh

    def rejected(self) -> None:
        """The API answered 401 with the current token: wipe it everywhere."""
        self._token = None
        wipe_token()
        log.info("api token refused by the api, wiped")
