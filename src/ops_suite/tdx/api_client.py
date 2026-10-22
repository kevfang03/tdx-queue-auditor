"""A read-only client for the TDX Web API: GET on a short allowlist, nothing else.

Every request is checked against ``API_ALLOWLIST`` **before** anything goes on the network:
- the method is ``GET``: never ``POST``, ``PUT``, ``PATCH`` or ``DELETE``. Not even the
  read-only searches (``POST .../search``) are used: the reports give the rows;
- the path, below ``tdx.api.base_path`` on ``tdx.base_url``'s host, is one of the endpoints
  listed, each confirmed in the TDX Web API reference (the instance's ``/TDWebApi/swagger``),
  with only the query parameters listed for it;
- never a credential endpoint (``tdx/guard.py``, ``REFUSED_CREDENTIAL_PATHS``), whatever the
  method;
- https, except to the computer itself (for tests).
A request that fails a check raises ``RequestRefused`` and is never sent.

Requests carry ``Authorization: Bearer <token>`` from a ``TokenSource``, refuse redirects (the
token goes to the TDX host or nowhere), verify TLS against the operating system's trust store
and are paced at least ``tdx.api.min_request_interval_ms`` apart. A 401 wipes the token; with a
way to ask for a fresh one, the request is tried once more. A 429 waits until the
``X-RateLimit-Reset`` time the API sends (at least 5 s, as its rate-limiting guide advises, at
most 65 s) and is tried once more.

A client given a ``deadline`` (on its own clock) sends nothing once it has passed: the request
fails with ``time_box``, and a request sent before it waits no longer than the time left.

Errors carry a reason code (``unauthorised``, ``forbidden``, ``not_found``, ``rate_limited``,
``timeout``, ``connection``, ``redirect``, ``bad_json``, ``time_box``, ``http_<n>``) and the
endpoint's pattern, never the token, a response body or a query value. Response bodies hold
student data: they are parsed in memory and handed to the caller, never logged or written
anywhere.
"""

import http.client
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit

import truststore

from ops_suite.common.config import TdxSettings
from ops_suite.tdx.api_token import TokenSource
from ops_suite.tdx.guard import is_credential_path
from ops_suite.tdx.session import on_tdx, tdx_origin

log = logging.getLogger(__name__)

RATE_LIMIT_MIN_WAIT_SECONDS = 5
RATE_LIMIT_MAX_WAIT_SECONDS = 65
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NUMBER = "{n}"
_PATH_CHARS = re.compile(r"[A-Za-z0-9/]*")


@dataclass(frozen=True, slots=True)
class ApiEndpoint:
    method: str
    pattern: str  # below the base path; "{n}" is a whole number (an id)
    query: frozenset[str]  # the query parameters it may carry
    reason: str  # one line

    def matches(self, path: str) -> bool:
        regex = "/".join(
            "[0-9]+" if part == _NUMBER else re.escape(part) for part in self.pattern.split("/")
        )
        return re.fullmatch(regex, path, re.IGNORECASE) is not None

    def describe(self) -> str:
        return f"{self.method} {self.pattern}"


# Each one checked against the TDX Web API reference: method, path, parameters and the
# response fields the reader uses (docs/USAGE.md, "TDX API (proof of concept)").
GET_USER = ApiEndpoint("GET", "/auth/getuser", frozenset(), "who the token belongs to")
LIST_REPORTS = ApiEndpoint("GET", "/reports", frozenset(), "the reports the person can see")
GET_REPORT = ApiEndpoint("GET", "/reports/{n}", frozenset({"withData"}), "a report and its rows")
FIND_TICKET = ApiEndpoint("GET", "/tickets/{n}", frozenset(), "a ticket in any ticketing app")
GET_TICKET = ApiEndpoint("GET", "/{n}/tickets/{n}", frozenset(), "a ticket and its description")
GET_FEED = ApiEndpoint("GET", "/{n}/tickets/{n}/feed", frozenset(), "a ticket's feed entries")
GET_ENTRY = ApiEndpoint("GET", "/feed/{n}", frozenset(), "a feed entry with its replies")
API_ALLOWLIST: tuple[ApiEndpoint, ...] = (
    GET_USER,
    LIST_REPORTS,
    GET_REPORT,
    FIND_TICKET,
    GET_TICKET,
    GET_FEED,
    GET_ENTRY,
)


class ApiError(Exception):
    """An API request failed. ``reason`` is a fixed code; the message names the endpoint's
    pattern and the code only."""

    def __init__(
        self,
        reason: str,
        endpoint: str = "",
        *,
        status: int | None = None,
        message: str | None = None,
    ) -> None:
        where = f" {endpoint}" if endpoint else ""
        super().__init__(message or f"TDX API request{where} failed ({reason})")
        self.reason = reason
        self.endpoint = endpoint
        self.status = status


class RequestRefused(ApiError):
    """The request is not on the allowlist; it was never sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, message=f"TDX API request refused before sending ({reason})")


def check_request(method: str, url: str, tdx: TdxSettings) -> ApiEndpoint:
    """The allowlist entry ``method url`` matches; RequestRefused (with a reason code) if none.

    Pure: no network. Reasons: ``credential_endpoint``, ``method``, ``off_host``, ``insecure``,
    ``outside_api``, ``not_allowed``, ``query``.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise RequestRefused("not_allowed") from None
    if is_credential_path(parts.path):
        raise RequestRefused("credential_endpoint")
    if method.upper() != "GET":
        raise RequestRefused("method")
    if not on_tdx(url, tdx.base_url):
        raise RequestRefused("off_host")
    if parts.scheme.lower() != "https" and host not in _LOOPBACK_HOSTS:
        raise RequestRefused("insecure")
    base = tdx.api.base_path
    if parts.path[: len(base)].casefold() != base.casefold():
        raise RequestRefused("outside_api")
    rest = parts.path[len(base) :]
    if not _PATH_CHARS.fullmatch(rest) or parts.fragment:
        raise RequestRefused("not_allowed")
    endpoint = next((entry for entry in API_ALLOWLIST if entry.matches(rest)), None)
    if endpoint is None:
        raise RequestRefused("not_allowed")
    keys = {pair.split("=", 1)[0] for pair in parts.query.split("&") if pair}
    if not keys <= endpoint.query:
        raise RequestRefused("query")
    return endpoint


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the token goes to the TDX host's API or nowhere."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _tls_context() -> ssl.SSLContext:
    """Verify certificates against the operating system's trust store, as the Teams client
    does; Python builds without a CA bundle of their own fail otherwise. Fully verifying."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=_tls_context())
    )


@dataclass(frozen=True, slots=True)
class _Answer:
    status: int
    reset: str | None  # X-RateLimit-Reset, RFC 1123
    body: bytes


def _reason(status: int) -> str:
    if 300 <= status < 400:
        return "redirect"
    return {401: "unauthorised", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(
        status, f"http_{status}"
    )


class TdxApiClient:
    """GET requests on ``API_ALLOWLIST``, as JSON. ``opener``, ``clock``, ``wall`` and
    ``sleep`` let tests run without network or real time. ``deadline``: a ``clock`` time after
    which no request is sent (``time_box``)."""

    def __init__(
        self,
        tdx: TdxSettings,
        tokens: TokenSource,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        deadline: float | None = None,
    ) -> None:
        tdx_origin(tdx.base_url)  # ValueError early if base_url is unusable
        self._tdx = tdx
        self._tokens = tokens
        self._opener = opener or default_opener()
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._deadline = deadline
        self._last: float | None = None
        self.requests: Counter[str] = Counter()  # endpoint pattern -> requests sent

    def url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        """The full address of ``path`` (below the base path)."""
        address = tdx_origin(self._tdx.base_url) + self._tdx.api.base_path + path
        return f"{address}?{urlencode(dict(query))}" if query else address

    def get(self, path: str, query: Mapping[str, str] | None = None) -> object:
        """GET ``path`` (below the base path) and return its JSON; ApiError if it fails."""
        url = self.url(path, query)
        endpoint = check_request("GET", url, self._tdx)
        name = endpoint.describe()
        answer = self._send(url, name)
        if answer.status == 401:
            self._tokens.rejected()
            if self._tokens.can_refresh:
                answer = self._send(url, name)
                if answer.status == 401:
                    self._tokens.rejected()
        if answer.status == 429:
            self._sleep(self._rate_limit_wait(answer.reset))
            answer = self._send(url, name)
        if not 200 <= answer.status < 300:
            raise ApiError(_reason(answer.status), name, status=answer.status)
        try:
            return json.loads(answer.body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", name, status=answer.status) from None

    def _rate_limit_wait(self, reset: str | None) -> float:
        seconds = float(RATE_LIMIT_MIN_WAIT_SECONDS)
        if reset:
            try:
                until = parsedate_to_datetime(reset)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                seconds = max(seconds, (until - self._wall()).total_seconds())
            except (TypeError, ValueError):
                pass
        return min(seconds, RATE_LIMIT_MAX_WAIT_SECONDS)

    def _check_deadline(self, name: str) -> None:
        if self._deadline is not None and self._clock() >= self._deadline:
            log.info("api request not sent", extra={"endpoint": name, "reason": "time_box"})
            raise ApiError("time_box", name)

    def _pace(self) -> None:
        interval = self._tdx.api.min_request_interval_ms / 1000
        if self._last is not None:
            wait = interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _send(self, url: str, name: str) -> _Answer:
        """One request with the current token; ApiError on a timeout or connection error."""
        self._check_deadline(name)
        token = self._tokens.get()
        self._pace()
        timeout: float = self._tdx.api.timeout_seconds
        if self._deadline is not None:
            self._check_deadline(name)
            timeout = max(1.0, min(timeout, self._deadline - self._clock()))
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {token.value}", "Accept": "application/json"},
        )
        self.requests[name] += 1
        reason = "connection"
        try:
            with self._opener.open(request, timeout=timeout) as response:
                answer = _Answer(
                    response.status, response.headers.get("X-RateLimit-Reset"), response.read()
                )
        except urllib.error.HTTPError as error:
            reset = error.headers.get("X-RateLimit-Reset") if error.headers else None
            error.close()  # the body is never read: it may echo student data
            answer = _Answer(error.code, reset, b"")
        except urllib.error.URLError as error:
            reason = "timeout" if isinstance(error.reason, TimeoutError) else "connection"
            answer = None
        except TimeoutError:
            reason, answer = "timeout", None
        except (OSError, http.client.HTTPException):
            answer = None
        if answer is None:
            # Raised outside the except blocks, so no chained exception carries the request.
            log.info("api request failed", extra={"endpoint": name, "reason": reason})
            raise ApiError(reason, name)
        log.info("api request answered", extra={"endpoint": name, "status": answer.status})
        return answer
