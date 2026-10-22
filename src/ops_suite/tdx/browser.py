"""The TDX browser: Playwright's bundled Chromium with a dedicated, persistent profile.

``tdx login``, ``tdx capture`` and ``tdx api token --headed`` run it headed and unguarded,
because the person signs in and navigates by hand. ``tdx rows`` and ``tdx audit`` may run it
headless, and always with the read-only request guard (``tdx/guard.py``) installed on the
context before the first navigation, with service workers blocked so that every request passes
through the guard, and SignalR's live updates refused: every request under ``/TDSignalR/`` and
every WebSocket.

This module never clicks, types, fills or submits; apart from ``open_url``, ``open_page`` and
``read_document`` (the Web API's token endpoint) it never navigates; its only script
(``evaluate_in_frame``) reads, never changes, the page;
and it changes nothing about how the browser presents itself (no stealth or anti-bot
changes). The pager click of ``tdx rows`` lives in ``tdx/dashboard.py``.

The profile folder holds live session cookies. Its path may be printed; its contents are never
read, logged, copied or uploaded by this code.
"""

import logging
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    WebSocketRoute,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ops_suite.common.hints import RUN
from ops_suite.tdx.guard import (
    READ_METHODS,
    AllowedRequest,
    is_live_update,
    request_allowed,
)
from ops_suite.tdx.session import describe_path, describe_url, on_tdx, tdx_origin

log = logging.getLogger(__name__)

START_PAGE_TIMEOUT_MS = 60_000
POLL_MS = 250
# Seconds Chromium gets to start (tdx.browser_launch_timeout_seconds); Playwright's own is 30.
LAUNCH_TIMEOUT_SECONDS = 60
_EVERY_URL = re.compile(".*")
# What Chromium says when another browser already has the profile open.
_PROFILE_IN_USE_HINTS = ("processsingleton", "singletonlock", "already in use", "profile in use")
# What Playwright says when its Chromium has not been downloaded.
_NOT_INSTALLED_HINTS = ("executable doesn't exist", "playwright install")
# What Playwright and Chromium say when a headed browser has no screen to open on.
_NO_DISPLAY_HINTS = ("xserver", "x server", "$display", "missing display", "cannot open display")
PROFILE_IN_USE_MESSAGE = (
    "the TDX browser profile is already in use by another browser window; close any open "
    f'"{RUN} tdx login" or "{RUN} tdx capture" window and try again'
)


StartFailure = Literal["profile_in_use", "not_installed", "launch_timeout", "no_display", "other"]
# Why ``read_document`` stopped waiting: an accepted answer; the time ran out; the page stayed
# off the TDX host too long; a refused answer had nothing newer; an error page off the TDX host
# (the sign-in service refused); or the page or window was closed.
DocumentEnd = Literal["answer", "timeout", "off_host", "settled", "refused", "closed"]


class BrowserStartError(RuntimeError):
    """Chromium could not be launched (not installed, profile in use, no display).

    ``reason`` is a fixed code, safe to print and log. The message may hold Playwright's first
    line, which can contain the profile path (and so the user name): only interactive commands
    show it, and ``tdx watch`` never prints or logs it.
    """

    def __init__(self, message: str, *, reason: StartFailure = "other") -> None:
        super().__init__(message)
        self.reason: StartFailure = reason


def start_failure(error: PlaywrightError) -> StartFailure:
    """The reason code for a Playwright error raised while the browser started."""
    if isinstance(error, PlaywrightTimeoutError):
        return "launch_timeout"
    message = str(error).lower()
    if any(hint in message for hint in _PROFILE_IN_USE_HINTS):
        return "profile_in_use"
    if any(hint in message for hint in _NOT_INSTALLED_HINTS):
        return "not_installed"
    if any(hint in message for hint in _NO_DISPLAY_HINTS):
        return "no_display"
    return "other"


def profile_in_use(profile_dir: Path) -> bool:
    """True if a running Chromium holds the profile's lock.

    A headed Chromium (the ``tdx login`` or ``tdx capture`` window) keeps ``lockfile`` open on
    Windows and a ``SingletonLock`` symlink to ``<host>-<pid>`` on macOS and Linux. The headless
    shell takes no lock and a second headless launch is not refused by Chromium, so this check
    runs before every launch. Only lock files are looked at, never profile contents.
    """
    lockfile = profile_dir / "lockfile"
    if sys.platform == "win32":
        if not lockfile.exists():
            return False
        try:
            with lockfile.open("a"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False
    singleton = profile_dir / "SingletonLock"
    if not singleton.is_symlink():
        return False
    host, _, pid_text = os.readlink(singleton).rpartition("-")
    if host != socket.gethostname() or not pid_text.isdigit():
        return True  # held from another machine, or a format we do not know: do not share it
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False  # a stale lock from a browser that crashed; Chromium replaces it
    except PermissionError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class FrameHtml:
    page_index: int
    frame_index: int  # 0 is the page's main frame
    url: str  # raw; only ever written out through describe_url
    html: str  # raw; never logged or written out


@dataclass(frozen=True, slots=True)
class RequestRecord:
    method: str
    resource_type: str
    url: str  # raw; only ever written out through describe_url or describe_path

    def describe(self) -> str:
        """``METHOD /masked/path``: no host, query or fragment; safe to log and print."""
        return f"{self.method.upper()} {describe_path(self.url)}"


@dataclass(frozen=True, slots=True, repr=False)
class DocumentAnswer:
    """What ``read_document`` saw. ``text`` may hold a secret (the API token): never log or
    print it, and drop it once read."""

    url: str  # raw: the answering document's address, else where the page ended up
    status: int | None  # None: no document answered from the expected path
    text: str = field(default="", repr=False)
    ended: DocumentEnd = "answer"
    # "refused": the HTTP status of the error page off the TDX host.
    refused_status: int | None = None

    def __repr__(self) -> str:
        return f"DocumentAnswer(status={self.status!r}, ended={self.ended!r})"


class TdxBrowser:
    def __init__(
        self,
        profile_dir: Path,
        base_url: str,
        *,
        headless: bool = False,
        allowed_requests: Sequence[AllowedRequest] | None = None,  # None = no guard
        launch_timeout_seconds: float = LAUNCH_TIMEOUT_SECONDS,
    ) -> None:
        tdx_origin(base_url)  # ValueError early if base_url is unusable
        self.profile_dir = profile_dir
        self.base_url = base_url
        self.headless = headless
        self.launch_timeout_seconds = launch_timeout_seconds
        self.skipped_frames = 0  # frames the last collect_frames() could not read
        self._allowed = None if allowed_requests is None else tuple(allowed_requests)
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._requests: list[RequestRecord] = []
        self._blocked: list[RequestRecord] = []
        self._allowed_non_get: list[RequestRecord] = []
        self._pending_sockets: list[WebSocketRoute] = []
        self._live_updates: list[RequestRecord] = []

    def __enter__(self) -> "TdxBrowser":
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if profile_in_use(self.profile_dir):
            raise BrowserStartError(PROFILE_IN_USE_MESSAGE, reason="profile_in_use")
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
                # Headed: let the page follow the window size, as in an everyday browser.
                no_viewport=not self.headless,
                # Read-only use: nothing a page offers for download is saved.
                accept_downloads=False,
                # Guarded runs: a service worker's requests would bypass context.route.
                service_workers="allow" if self._allowed is None else "block",
                timeout=self.launch_timeout_seconds * 1000,
            )
            if self._allowed is not None:
                self._context.route("**/*", self._guard)
                self._context.route_web_socket(_EVERY_URL, self._close_web_socket)
        except PlaywrightError as error:
            self.__exit__(None, None, None)
            reason = start_failure(error)
            if reason == "profile_in_use":
                raise BrowserStartError(PROFILE_IN_USE_MESSAGE, reason=reason) from error
            first_line = (str(error).strip().splitlines() or [type(error).__name__])[0]
            raise BrowserStartError(first_line, reason=reason) from error
        self._context.on("request", self._record_request)
        log.info(
            "browser started",
            extra={"headless": self.headless, "guarded": self._allowed is not None},
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                # The person may already have closed the window.
                log.debug("browser context was already closed")
            self._context = None
        self._stop_playwright()
        log.info("browser closed")

    def open_start_page(self) -> None:
        """Go to ``base_url`` in the first page. A page that fails to load is not fatal."""
        self.open_url(self.base_url)

    def open_url(self, url: str, *, timeout_ms: int = START_PAGE_TIMEOUT_MS) -> None:
        """Go to ``url`` in the first page. A page that fails to load is not fatal."""
        context = self._require_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as error:
            log.warning(
                "page did not finish loading",
                extra={"url": describe_url(url), "error": type(error).__name__},
            )

    def open_page(
        self,
        url: str,
        *,
        timeout_ms: int = START_PAGE_TIMEOUT_MS,
        on_request_finished: Callable[[Request], None] | None = None,
    ) -> Page:
        """Go to ``url`` in a new page and return it. A page that fails to load is not fatal.

        ``on_request_finished`` sees every request of the page and its frames that finished,
        from the first one on. The caller closes the page with ``close_page``.
        """
        page = self._require_context().new_page()
        if on_request_finished is not None:
            page.on("requestfinished", on_request_finished)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as error:
            log.warning(
                "page did not finish loading",
                extra={"url": describe_url(url), "error": type(error).__name__},
            )
        return page

    def read_document(
        self,
        url: str,
        *,
        path_suffix: str,
        accept: Callable[[str], bool] = lambda text: True,
        timeout_ms: int = START_PAGE_TIMEOUT_MS,
        off_host_seconds: float | None = 10,
        settle_seconds: float = 5,
        in_first_page: bool = False,
        accept_any_path: bool = False,
    ) -> DocumentAnswer:
        """Open ``url`` in a new page and return a main-frame document that answered from a
        TDX-host path ending in ``path_suffix`` (ignoring case), after any redirects and
        sign-in pages on the way: the first whose text ``accept`` takes, else the last one.

        Gives up after ``timeout_ms``; once the page has stayed off the TDX host for
        ``off_host_seconds`` (a sign-in page waiting for a person; None: no limit, a person is
        signing in); at once when a page off the TDX host answers with an HTTP error (the
        sign-in service refused); when the page is closed; or once an answer that ``accept``
        refused is ``settle_seconds`` old with nothing newer. ``ended`` says which.

        ``in_first_page``: navigate the first open page (the window the person sees) and leave
        it open, instead of a new page that is closed before returning. ``accept_any_path``:
        a TDX-host document on any path whose text ``accept`` takes also answers (a sign-in
        that ends on a page of its own). The text is kept only in the answer.
        """
        context = self._require_context()
        page = context.pages[0] if in_first_page and context.pages else context.new_page()
        answers: list[Response] = []
        others: list[Response] = []
        # Main-frame documents off the TDX host, latest last: the HTTP status of an error
        # page, else 0.
        off_host: list[int] = []
        suffix = path_suffix.casefold()

        def on_response(response: Response) -> None:
            try:
                request = response.request
                if request.resource_type != "document" or request.frame != page.main_frame:
                    return
                if 300 <= response.status < 400:
                    return  # a redirect on the way (to the sign-in and back)
                path = urlsplit(response.url).path.casefold()
            except (PlaywrightError, ValueError):
                return
            if not on_tdx(response.url, self.base_url):
                off_host.append(response.status if response.status >= 400 else 0)
            elif path.endswith(suffix):
                answers.append(response)
            elif accept_any_path:
                others.append(response)

        page.on("response", on_response)
        try:
            if in_first_page:
                page.bring_to_front()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightError as error:
                log.info(
                    "document did not finish loading",
                    extra={"url": describe_url(url), "error": type(error).__name__},
                )
            deadline = time.monotonic() + timeout_ms / 1000
            off_since: float | None = None
            read = 0  # answers already looked at
            read_others = 0
            last: DocumentAnswer | None = None
            last_at = 0.0
            while time.monotonic() < deadline:
                while read_others < len(others):
                    response = others[read_others]
                    read_others += 1
                    text = _response_text(response)
                    if accept(text):
                        return DocumentAnswer(response.url, response.status, text)
                while read < len(answers):
                    response = answers[read]
                    read += 1
                    text = _response_text(response)
                    last, last_at = (
                        DocumentAnswer(response.url, response.status, text),
                        time.monotonic(),
                    )
                    if accept(text):
                        return last
                if last is not None and time.monotonic() - last_at >= settle_seconds:
                    return _ended(last, "settled")
                if on_tdx(page.url, self.base_url):
                    off_since = None
                else:
                    if off_host and off_host[-1]:
                        return DocumentAnswer(
                            page.url, None, ended="refused", refused_status=off_host[-1]
                        )
                    off_since = off_since or time.monotonic()
                    if (
                        off_host_seconds is not None
                        and time.monotonic() - off_since >= off_host_seconds
                    ):
                        return _ended(last, "off_host", page.url)
                try:
                    page.wait_for_timeout(POLL_MS)
                except PlaywrightError:
                    return _ended(last, "closed", page.url)
            return _ended(last, "timeout", page.url)
        finally:
            if in_first_page:
                page.remove_listener("response", on_response)
            else:
                self.close_page(page)

    def close_page(self, page: Page) -> None:
        self._close_pending_sockets()
        try:
            page.close()
        except PlaywrightError:
            log.debug("page was already closed")

    def current_urls(self) -> list[str]:
        return [page.url for page in self._open_pages()]

    def collect_frames(self) -> list[FrameHtml]:
        """The HTML of every frame of every open page; unreadable frames are counted."""
        collected: list[FrameHtml] = []
        self.skipped_frames = 0
        for page_index, page in enumerate(self._open_pages()):
            collected.extend(self._page_frames(page, page_index))
        return collected

    def page_frames(self, page: Page) -> list[FrameHtml]:
        """The HTML of every frame of one page, main frame first; unreadable frames are counted."""
        self._close_pending_sockets()
        self.skipped_frames = 0
        return self._page_frames(page, 0)

    def _page_frames(self, page: Page, page_index: int) -> list[FrameHtml]:
        collected: list[FrameHtml] = []
        main = page.main_frame
        frames = [main, *(frame for frame in page.frames if frame is not main)]
        for frame_index, frame in enumerate(frames):
            try:
                if frame.is_detached():
                    raise PlaywrightError("frame is detached")
                collected.append(FrameHtml(page_index, frame_index, frame.url, frame.content()))
            except PlaywrightError as error:
                self.skipped_frames += 1
                log.info(
                    "frame skipped",
                    extra={
                        "frame": f"{page_index}-{frame_index}",
                        "url": describe_url(frame.url),
                        "error": type(error).__name__,
                    },
                )
        return collected

    def requests_seen(self) -> list[RequestRecord]:
        """Requests to the TDX host since the context opened, in the order they were made."""
        self._catch_up()
        return list(self._requests)

    def blocked_requests(self) -> list[RequestRecord]:
        """Requests the guard aborted, in order. Show them only through ``describe()``."""
        self._catch_up()
        return list(self._blocked)

    def live_updates_blocked(self) -> list[RequestRecord]:
        """SignalR's live updates refused in a guarded run, in order: every request under
        ``/TDSignalR/`` (GET included) and every WebSocket (method ``WS``, closed unconnected).

        Kept apart from ``blocked_requests``: every ticket page asks for them, and refusing
        them is expected, not a sign that the page missed something it needed.
        """
        self._catch_up()
        return list(self._live_updates)

    def evaluate_in_frame(self, page: Page, frame_index: int, script: str) -> object:
        """Run a read-only script in one frame of ``page`` (``FrameHtml.frame_index``).

        For reading layout and computed styles only: the scripts passed here never change the
        page. PlaywrightError if the frame is gone.
        """
        main = page.main_frame
        frames = [main, *(frame for frame in page.frames if frame is not main)]
        if not 0 <= frame_index < len(frames):
            raise PlaywrightError("frame is gone")
        return frames[frame_index].evaluate(script)

    def allowed_non_get_requests(self) -> list[RequestRecord]:
        """Non-read requests the guard let through (allowlisted), in order."""
        self._catch_up()
        return list(self._allowed_non_get)

    def live_frames(self) -> list[Frame]:
        """Every frame of every open page, each page's main frame first."""
        frames: list[Frame] = []
        for page in self._open_pages():
            main = page.main_frame
            frames.extend([main, *(frame for frame in page.frames if frame is not main)])
        return frames

    def wait(self, milliseconds: int) -> None:
        """Let the browser run (and the guard answer requests) for a while.

        A plain ``time.sleep`` would stall every request waiting on the guard, because the
        route handler only runs while Playwright handles a call.
        """
        for page in self._open_pages():
            try:
                page.wait_for_timeout(milliseconds)
                break
            except PlaywrightError:
                continue
        self._close_pending_sockets()

    # --- helpers ------------------------------------------------------------------------

    def _catch_up(self) -> None:
        """Process the browser events that arrived while Python was blocked (in ``input()``).

        Page URLs, the page list and the request log are updated only while Playwright handles
        a call to the browser. A zero-length wait is such a call and touches no page; its reply
        comes after every earlier event, so the cached state is current once it returns. A call
        on a page that has closed meanwhile is also answered, so the next page is tried.
        """
        for page in self._require_context().pages:
            try:
                page.wait_for_timeout(0)
                break
            except PlaywrightError:
                continue
        self._close_pending_sockets()

    def _guard(self, route: Route) -> None:
        request = route.request
        record = RequestRecord(request.method, request.resource_type, request.url)
        if is_live_update(request.url):
            self._live_updates.append(record)
            log.info("live update refused", extra={"request": record.describe()})
            try:
                route.abort("blockedbyclient")
            except PlaywrightError:
                log.debug("route already handled", extra={"request": record.describe()})
            return
        allowed = request_allowed(
            request.method,
            request.url,
            self._allowed or (),
            base_url=self.base_url,
            resource_type=request.resource_type,
        )
        try:
            if not allowed:
                self._blocked.append(record)
                log.info(
                    "request blocked",
                    extra={"request": record.describe(), "resource_type": record.resource_type},
                )
                route.abort("blockedbyclient")
                return
            if request.method.upper() not in READ_METHODS:
                self._allowed_non_get.append(record)
                log.info(
                    "non-GET request allowed",
                    extra={"request": record.describe(), "resource_type": record.resource_type},
                )
            route.continue_()
        except PlaywrightError:
            # The page or frame went away while the request was pending.
            log.debug("route already handled", extra={"request": record.describe()})

    def _close_web_socket(self, socket_route: WebSocketRoute) -> None:
        """Guarded runs: a page opened a WebSocket. It is never connected to the server.

        Closing it here would deadlock: the handler runs inside Playwright's event dispatch,
        where a blocking call cannot get its answer. So it is queued and closed by
        ``_close_pending_sockets`` at the next call this class makes to the browser.
        """
        record = RequestRecord("WS", "websocket", socket_route.url)
        self._live_updates.append(record)
        self._pending_sockets.append(socket_route)
        log.info("websocket refused", extra={"request": record.describe()})

    def _close_pending_sockets(self) -> None:
        while self._pending_sockets:
            socket_route = self._pending_sockets.pop(0)
            try:
                socket_route.close()
            except PlaywrightError:
                log.debug("websocket already gone")

    def _record_request(self, request: Request) -> None:
        if on_tdx(request.url, self.base_url):
            self._requests.append(RequestRecord(request.method, request.resource_type, request.url))

    def _open_pages(self) -> list[Page]:
        self._catch_up()
        return [page for page in self._require_context().pages if not page.is_closed()]

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("TdxBrowser is not open; use it in a with-block")
        return self._context

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


def _response_text(response: Response) -> str:
    try:
        return response.text()
    except PlaywrightError:
        return ""


def _ended(last: DocumentAnswer | None, why: DocumentEnd, url: str = "") -> DocumentAnswer:
    """``last`` (the latest answer from the expected path) ended ``why``; without one, no
    answer, at ``url``."""
    if last is None:
        return DocumentAnswer(url, None, ended=why)
    return DocumentAnswer(last.url, last.status, last.text, ended=why)
