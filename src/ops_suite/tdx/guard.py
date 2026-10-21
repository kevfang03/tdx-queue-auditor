"""The read-only request guard's decision. Pure functions; no network and no Playwright.

``tdx rows`` routes every request the browser makes through ``request_allowed``. Reads
(``GET``, ``HEAD``, ``OPTIONS``) go anywhere. Any other method is allowed only when it goes to
the TDX host and matches an ``AllowedRequest``: method, resource type and the URL path with
long digit runs masked as ``#`` (fnmatch, case-sensitive). Everything else is aborted.

Each built-in entry is justified in docs/tdx-page-structure.md. Entries from the
``tdx.allowed_requests`` setting are added only after the architect session reviews them.
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


@dataclass(frozen=True, slots=True)
class AllowedRequest:
    method: str  # upper case, never "GET", "HEAD" or "OPTIONS" (those are always allowed)
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
        parsed.append(AllowedRequest(method, pattern, "from tdx.allowed_requests"))
    return tuple(parsed)


def request_allowed(
    method: str,
    url: str,
    allowed: Sequence[AllowedRequest],
    *,
    base_url: str,
    resource_type: str,
) -> bool:
    """True if the request may go ahead; see the module docstring for the rules."""
    method = method.upper()
    if method in READ_METHODS:
        return True
    if not on_tdx(url, base_url):
        return False
    try:
        path = mask_digits(urlsplit(url).path)
    except ValueError:
        return False
    return any(
        entry.method == method
        and resource_type in entry.resource_types
        and fnmatchcase(path, entry.path_pattern)
        for entry in allowed
    )
