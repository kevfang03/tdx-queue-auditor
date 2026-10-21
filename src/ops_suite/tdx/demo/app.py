"""A local stand-in for the TDWorkManagement app, serving synthetic pages only.

The app's own pages (the shell, the dashboard frame, the ticket pages and the sign-in page)
are in ``pages/`` next to this file. The widget grids, ticket feeds and descriptions come from a
``PageSource``: the tests serve fixed files from ``tests/fixtures/tdx_synthetic/workmgmt/``
(``FolderSource``), and ``tdx demo`` serves a dataset generated at start (a plain dict).
Names are relative, such as ``widgets/40101-1.html``, ``feeds/5501002.html`` or
``descriptions/5501012.html``.

It listens on 127.0.0.1 only and never reaches the network.
"""

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

PAGES_DIR = Path(__file__).parent / "pages"
WIDGET_PATH = re.compile(r"/TDWorkManagement/WorkManagement/Widgets/(\d+)")
FEED_SEARCH_PATH = "/TDNext/Apps/Feed/Search"
TICKET_SHELL_PATH = "/TDWorkManagement/Apps/43/Tickets/TicketDet"
TICKET_PAGE_PATH = "/TDNext/Apps/43/Tickets/TicketDet.aspx"
PAGES = {
    "/TDWorkManagement/": "shell.html",
    "/TDWorkManagement/WorkManagement/Dashboard/Dashboard": "dashboard.html",
    TICKET_SHELL_PATH: "ticket-shell.html",
    TICKET_PAGE_PATH: "ticket.html",
    "/signin": "signin.html",
}
# Where the ticket page's server-rendered description goes.
DESCRIPTION_MARK = b"<!-- ticket description -->"
# Paths that redirect to the sign-in host when opened with ?variant=signin.
SIGNIN_PATHS = frozenset({"/TDWorkManagement/", TICKET_SHELL_PATH, TICKET_PAGE_PATH})


class PageSource(Protocol):
    def get(self, name: str, /) -> bytes | None:
        """The page called ``name`` (``widgets/...``, ``feeds/...`` or ``descriptions/...``),
        or None."""
        ...


class FolderSource:
    """Pages read from a folder, as the tests' fixtures keep them."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def get(self, name: str, /) -> bytes | None:
        path = self.folder / name
        return path.read_bytes() if path.is_file() else None


@dataclass
class WorkMgmtServer:
    base_url: str  # http://127.0.0.1:<port>/TDWorkManagement/
    port: int
    received: list[tuple[str, str]] = field(default_factory=list)  # (method, path) seen

    def shell(self, query: str = "") -> str:
        """The app's start page; without ``d=`` it opens the homepage dashboard."""
        return f"{self.base_url}?{query}" if query else self.base_url

    def dashboard(self, variant: str = "") -> str:
        """The shell, opened on the IT Tickets dashboard (both widgets)."""
        return self.shell(f"d=2&variant={variant}" if variant else "d=2")

    def frame(self, query: str) -> str:
        """The inner dashboard frame's own address, as tdx set-dashboard saves it."""
        return f"{self.base_url}WorkManagement/Dashboard/Dashboard?{query}"

    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ticket_shell(self, ticket_id: int, variant: str = "") -> str:
        """The grid's ID link: the shell with the ticket open as a tab, dashboard behind it."""
        query = f"TicketID={ticket_id}" + (f"&variant={variant}" if variant else "")
        return f"{self.origin()}{TICKET_SHELL_PATH}?{query}"

    def ticket_page(self, ticket_id: int, variant: str = "") -> str:
        """The TDNext ticket page on its own."""
        query = f"TicketID={ticket_id}" + (f"&variant={variant}" if variant else "")
        return f"{self.origin()}{TICKET_PAGE_PATH}?{query}"


def _handler(server_state: WorkMgmtServer, source: PageSource) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/html") -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            server_state.received.append(("GET", parts.path))
            variant = parse_qs(parts.query).get("variant", [""])[0]
            if parts.path in SIGNIN_PATHS and variant == "signin":
                # An expired session: TDX sends the browser to the sign-in host.
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{server_state.port}/signin")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            name = PAGES.get(parts.path)
            if name is None:
                self._send(404)
                return
            body = (PAGES_DIR / name).read_bytes()
            if parts.path == TICKET_PAGE_PATH:
                body = body.replace(DESCRIPTION_MARK, self._description(parts.query))
            self._send(200, body)

        def _description(self, query: str) -> bytes:
            """The ticket's description fragment, rendered into the page like TDX does."""
            ticket = parse_qs(query).get("TicketID", [""])[0]
            body = source.get(f"descriptions/{ticket}.html") if ticket.isdigit() else None
            return body or b""

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            server_state.received.append(("POST", parts.path))
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if parts.path == FEED_SEARCH_PATH:
                self._feed_search(raw)
                return
            match = WIDGET_PATH.fullmatch(parts.path)
            if match is None:
                self._send(200, b"{}", "application/json")
                return
            try:
                request = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                request = {}
            page = int(request.get("page", 1))
            variant = str(request.get("variant", ""))
            if variant == "stuck":
                page = 1
            widget = match.group(1)
            # A variant's own page if there is one, else the default page.
            names = [f"{widget}-{page}-{variant}.html"] if variant else []
            names.append(f"{widget}-{page}.html")
            for name in names:
                body = source.get(f"widgets/{name}")
                if body is not None:
                    self._send(200, body)
                    return
            self._send(404)

        def _feed_search(self, raw: bytes) -> None:
            """The ticket's feed entries; 404 (a feed that never loads) if there is none."""
            try:
                ticket_id = int(json.loads(raw or b"{}").get("TicketID", 0))
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                ticket_id = 0
            body = source.get(f"feeds/{ticket_id}.html") if ticket_id else None
            if body is None:
                self._send(404)
            else:
                self._send(200, body)

    return Handler


@contextmanager
def serve(source: PageSource) -> Iterator[WorkMgmtServer]:
    """Serve the synthetic TDWorkManagement app on a free port of 127.0.0.1 until exit."""
    state = WorkMgmtServer(base_url="", port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state, source))
    state.port = server.server_address[1]
    state.base_url = f"http://127.0.0.1:{state.port}/TDWorkManagement/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
