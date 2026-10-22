"""The read-only request guard's decision. Pure functions; no network and no Playwright.

``tdx rows`` routes every request the browser makes through ``request_allowed``. Reads
(``GET``, ``HEAD``, ``OPTIONS``) go anywhere. Any other method is allowed only when it goes to
the TDX host and matches an ``AllowedRequest``: method, resource type and the URL path with
long digit runs masked as ``#`` (fnmatch, case-sensitive). Everything else is aborted.

Under the Web API's auth path on the TDX host only ``BUILT_IN_AUTH_READS`` passes: the
token for the signed-in person's own session. Everything else there is refused with every
method, GET included, so the credential endpoints (``REFUSED_CREDENTIAL_PATHS``: a user name
and password, or a service account's keys) never are reached, and no ``tdx.allowed_requests``
entry may match them. The API client (``tdx/api_client.py``) refuses the same list.

Each built-in entry is justified in docs/tdx-page-structure.md. Entries from the
``tdx.allowed_requests`` setting are added only after a person has reviewed each one: an
allowed write is a write to TDX.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from ops_suite.tdx.session import on_tdx
from ops_suite.tdx.skeleton import mask_digits

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Script-initiated requests. A "document" POST is a form postback and is never allowlisted
# by default.
SCRIPT_RESOURCE_TYPES = frozenset({"fetch", "xhr"})

_METHOD = re.compile(r"[A-Z]+")
_PATTERN = re.compile(r"/[^\s?]*")


# The Web API's credential endpoints, as path endings after the API's base path (normally
# "/TDWebApi/api"), compared ignoring case because the server ignores it too. They take a user
# name and password, or a service account's keys: new credentials this tool never uses.
REFUSED_CREDENTIAL_PATHS: tuple[str, ...] = (
    "/api/auth",
    "/api/auth/login",
    "/api/auth/loginadmin",
)


def is_credential_path(path: str) -> bool:
    """True if ``path`` (a URL path, query left out) ends in a refused credential endpoint."""
    folded = path.casefold().rstrip("/")
    return any(folded.endswith(ending) for ending in REFUSED_CREDENTIAL_PATHS)


@dataclass(frozen=True, slots=True)
class AllowedRequest:
    method: str  # upper case; in BUILT_IN_ALLOWED never "GET", "HEAD" or "OPTIONS"
    path_pattern: str  # fnmatch pattern on the URL path after mask_digits
    reason: str  # one line
    resource_types: frozenset[str] = SCRIPT_RESOURCE_TYPES


BUILT_IN_ALLOWED: tuple[AllowedRequest, ...] = (
    AllowedRequest(
        method="POST",
        path_pattern="/TDWorkManagement/WorkManagement/Widgets/#",
        reason=(
            "loads one dashboard widget's report rows (Kendo grid read); one per widget on load, "
            "likely also paging"
        ),
    ),
    AllowedRequest(
        method="POST",
        path_pattern="/TDNext/Apps/Feed/Search",
        reason="loads the ticket's activity feed (search by ticket)",
    ),
)

# Under the Web API's auth path (any base path, ignoring case) a guarded browser may make only
# these requests, and nothing else there, GET included: the token for the signed-in person's
# own session, opened as a page by "tdx api token" (docs/tdx-page-structure.md, "Web API
# token"). The credential endpoints that take a password or keys are under the same path.
AUTH_PATH_PATTERNS: tuple[str, ...] = ("*/api/auth", "*/api/auth/*")
BUILT_IN_AUTH_READS: tuple[AllowedRequest, ...] = (
    AllowedRequest(
        method="GET",
        path_pattern="*/api/auth/loginsso",
        reason="the API token for the signed-in person's own session (tdx login, tdx api token)",
        resource_types=frozenset({"document"}),
    ),
)


# SignalR's live updates on the ticket page. Guarded runs refuse every request under this path
# (GET included) and every WebSocket, and count them apart from blocked requests: a read-only
# audit needs no live updates (docs/tdx-page-structure.md, "Live updates").
LIVE_UPDATE_PREFIX = "/TDSignalR/"


def is_live_update(url: str) -> bool:
    """True for a request to SignalR's live-update path, on any host and with any method."""
    try:
        path = mask_digits(urlsplit(url).path)
    except ValueError:
        return False
    return path.startswith(LIVE_UPDATE_PREFIX)


def parse_allowed(entries: Sequence[str]) -> tuple[AllowedRequest, ...]:
    """Config strings ``"METHOD /path-pattern"``; ValueError naming the first bad entry."""
    parsed: list[AllowedRequest] = []
    for entry in entries:
        parts = entry.split()
        if len(parts) != 2:
            raise ValueError(f"{entry!r} is not 'METHOD /path-pattern'")
        method, pattern = parts[0].upper(), parts[1]
        if not _METHOD.fullmatch(method):
            raise ValueError(f"{entry!r}: the method must be letters only")
        if method in READ_METHODS:
            raise ValueError(f"{entry!r}: {method} is always allowed; list other methods only")
        if not _PATTERN.fullmatch(pattern):
            raise ValueError(f"{entry!r}: the pattern must be a path starting with / (no query)")
        if _may_match_credentials(pattern):
            raise ValueError(f"{entry!r}: it could allow a Web API credential endpoint")
        parsed.append(AllowedRequest(method, pattern, "from tdx.allowed_requests"))
    return tuple(parsed)


def _may_match_credentials(pattern: str) -> bool:
    """Whether an allowlist pattern matches a credential endpoint under some base path."""
    folded = pattern.casefold()
    return any(
        fnmatchcase(prefix + ending, folded)
        for ending in REFUSED_CREDENTIAL_PATHS
        for prefix in ("", "/tdwebapi", "/sbtdwebapi", "/#")
    )


def _is_auth_path(path: str) -> bool:
    folded = path.casefold().rstrip("/")
    return any(fnmatchcase(folded, pattern) for pattern in AUTH_PATH_PATTERNS)


def request_allowed(
    method: str,
    url: str,
    allowed: Sequence[AllowedRequest],
    *,
    base_url: str,
    resource_type: str,
) -> bool:
    """True if the request may go ahead; see the module docstring for the rules."""
    try:
        path: str | None = mask_digits(urlsplit(url).path)
    except ValueError:
        path = None
    method = method.upper()
    if path is not None and on_tdx(url, base_url) and _is_auth_path(path):
        return any(
            entry.method == method
            and resource_type in entry.resource_types
            and fnmatchcase(path.casefold().rstrip("/"), entry.path_pattern)
            for entry in BUILT_IN_AUTH_READS
        )
    if method in READ_METHODS:
        return True
    if path is None or not on_tdx(url, base_url):
        return False
    return any(
        entry.method == method
        and resource_type in entry.resource_types
        and fnmatchcase(path, entry.path_pattern)
        for entry in allowed
    )
