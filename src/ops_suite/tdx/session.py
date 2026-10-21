"""URL checks for the TDX session. Pure functions; no network and no Playwright."""

from urllib.parse import urlsplit

from ops_suite.tdx.skeleton import mask_digits

_WEB_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_parts(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) URL with a host, else None."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _WEB_SCHEMES or not parts.hostname:
        return None
    return scheme, parts.hostname, port or _DEFAULT_PORTS[scheme]


def tdx_origin(base_url: str) -> str:
    """``scheme://host[:port]`` of ``base_url``; ValueError if it is not an http(s) URL."""
    parts = _origin_parts(base_url)
    if parts is None:
        raise ValueError("tdx.base_url must be an http:// or https:// URL with a host")
    scheme, host, port = parts
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}" if port == _DEFAULT_PORTS[scheme] else f"{scheme}://{host}:{port}"


def on_tdx(url: str, base_url: str) -> bool:
    """True if ``url`` has the same scheme, host and port as ``base_url``."""
    base = _origin_parts(base_url)
    return base is not None and _origin_parts(url) == base


def describe_url(url: str) -> str:
    """Host and path only, with long digit runs masked; safe to log and to write out.

    Query, fragment and user info are dropped. Non-web URLs keep only their scheme, because a
    ``data:`` or ``blob:`` URL can carry page content.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "[unreadable url]"
    scheme = parts.scheme.lower()
    if scheme == "about":
        return f"about:{mask_digits(parts.path)}"
    if scheme not in _WEB_SCHEMES | {"ws", "wss"}:
        return f"{scheme}:" if scheme else "[relative url]"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return f"{netloc}{mask_digits(parts.path) or '/'}"


def describe_path(url: str) -> str:
    """The path only, with long digit runs masked: no host, query or fragment.

    For the guard's reports (``POST /TDWorkManagement/WorkManagement/Widgets/#``). Non-web URLs
    keep only their scheme.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "[unreadable url]"
    scheme = parts.scheme.lower()
    if scheme not in _WEB_SCHEMES | {"ws", "wss"}:
        return f"{scheme}:" if scheme else "[relative url]"
    return mask_digits(parts.path) or "/"
