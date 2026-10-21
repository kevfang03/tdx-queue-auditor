"""Read the two ticket widgets of the TDX dashboard, every page, read-only.

The widgets live in a frame of the TDWorkManagement shell. They are found by searching every
frame for widget containers and matching the titles from config, never by frame index, URL or
position. Each container's HTML goes to the pure parser in ``tdx/rows.py``.

TDX opens each person's homepage dashboard, which may lack a widget, and switching dashboards
changes only the inner frame. ``find_dashboard_frame`` (for ``tdx set-dashboard``) returns the
address of the frame that holds both widgets after the person switched by hand, and
``check_dashboard`` tests that the address shows both widgets when opened on its own.

Read-only: the pager's "next page" button is the only element this code ever clicks. It never
types, fills, selects or presses anything, and never navigates except the single ``open_url``.
Every request the browser makes passes the guard installed by ``TdxBrowser``.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame

from ops_suite.common.config import TdxSettings
from ops_suite.tdx.browser import TdxBrowser
from ops_suite.tdx.rows import (
    CONTAINER_SELECTOR,
    NEXT_SELECTOR,
    StructureError,
    TicketRow,
    WidgetKind,
    WidgetPage,
    normalise_title,
    parse_widget,
    widget_ready,
    widget_title,
)
from ops_suite.tdx.session import on_tdx

log = logging.getLogger(__name__)

POLL_MS = 250
# After the person presses Enter in set-dashboard, frames that are still loading get this long.
FIND_WAIT_SECONDS = 5
KINDS: tuple[WidgetKind, ...] = ("unclaimed", "claimed")
_OUTER_HTML = "elements => elements.map(element => element.outerHTML)"


@dataclass(frozen=True, slots=True)
class WidgetRead:
    kind: WidgetKind
    rows: tuple[TicketRow, ...]
    pages: int
    unreadable_dates: tuple[str, ...]
    hit_page_limit: bool
    duplicates: int = 0  # ticket ids seen again on a later page, kept once
    rows_without_id: int = 0


@dataclass(frozen=True, slots=True)
class DashboardRead:
    widgets: dict[WidgetKind, WidgetRead]
    blocked: tuple[str, ...]  # "METHOD /masked/path" for each aborted request
    allowed_non_get: tuple[str, ...] = field(default=())  # same, for allowlisted non-GETs


class NotSignedInError(Exception):
    """The dashboard URL ended up off the TDX host, normally on the sign-in page."""


class DashboardStructureError(Exception):
    """The page did not look like the captures. The message is safe to print and log."""

    def __init__(self, message: str, blocked: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.blocked = blocked


class WidgetNotFoundError(DashboardStructureError):
    """A configured widget title is not on the page (for example the homepage dashboard)."""


@dataclass(frozen=True, slots=True)
class _Widget:
    frame: Frame
    index: int  # position among the frame's widget containers
    url: str  # the frame's URL; raw, only used to make ticket links absolute
    html: str


class _Deadline:
    def __init__(self, seconds: int) -> None:
        self._end = time.monotonic() + seconds

    def passed(self) -> bool:
        return time.monotonic() > self._end


def _titles(settings: TdxSettings) -> dict[WidgetKind, str]:
    return {
        "unclaimed": settings.widgets.unclaimed_title,
        "claimed": settings.widgets.claimed_title,
    }


def _named(titles: dict[WidgetKind, str], kinds: list[WidgetKind]) -> str:
    """Config titles with their setting names; safe to print (they come from config)."""
    return "; ".join(f'"{titles[kind]}" (tdx.widgets.{kind}_title)' for kind in kinds)


def read_dashboard(
    browser: TdxBrowser, settings: TdxSettings, now: datetime, *, start_url: str
) -> DashboardRead:
    tz = ZoneInfo(settings.display_timezone)
    titles = _titles(settings)
    browser.open_url(start_url, timeout_ms=settings.page_timeout_seconds * 1000)
    found = _wait_for_widgets(browser, settings, titles)
    widgets = {
        kind: _read_widget(browser, kind, titles[kind], found[kind], settings, tz, now)
        for kind in KINDS
    }
    return DashboardRead(
        widgets=widgets,
        blocked=_blocked(browser),
        allowed_non_get=tuple(r.describe() for r in browser.allowed_non_get_requests()),
    )


def _blocked(browser: TdxBrowser) -> tuple[str, ...]:
    return tuple(record.describe() for record in browser.blocked_requests())


def _structure_error(browser: TdxBrowser, message: str) -> DashboardStructureError:
    return DashboardStructureError(message, _blocked(browser))


def _scan_frames(
    frames: list[Frame], titles: dict[WidgetKind, str]
) -> list[dict[WidgetKind, _Widget]]:
    """For each readable frame, in order, the first container matching each config title."""
    wanted = {normalise_title(title): kind for kind, title in titles.items()}
    per_frame: list[dict[WidgetKind, _Widget]] = []
    for frame in frames:
        try:
            if frame.is_detached():
                continue
            url = frame.url
            htmls = frame.locator(CONTAINER_SELECTOR).evaluate_all(_OUTER_HTML)
        except PlaywrightError:
            continue  # the frame navigated or went away; the next poll looks again
        found: dict[WidgetKind, _Widget] = {}
        for index, html in enumerate(htmls):
            kind = wanted.get(widget_title(html) or "")
            if kind is not None and kind not in found:
                found[kind] = _Widget(frame, index, url, html)
        per_frame.append(found)
    return per_frame


def _scan(frames: list[Frame], titles: dict[WidgetKind, str]) -> dict[WidgetKind, _Widget]:
    """The first widget container in ``frames`` whose title matches each config title."""
    found: dict[WidgetKind, _Widget] = {}
    for in_frame in _scan_frames(frames, titles):
        for kind, widget in in_frame.items():
            found.setdefault(kind, widget)
    return found


def find_dashboard_frame(browser: TdxBrowser, settings: TdxSettings) -> str:
    """The raw address of the one frame that shows both configured widgets.

    For ``tdx set-dashboard``, after the person has opened the right dashboard by hand. If
    several frames qualify, the first one on the most recently opened page wins.
    """
    titles = _titles(settings)
    deadline = _Deadline(FIND_WAIT_SECONDS)
    while True:
        per_frame = _scan_frames(browser.live_frames(), titles)
        both = [found for found in per_frame if all(kind in found for kind in KINDS)]
        if both or deadline.passed():
            break
        browser.wait(POLL_MS)
    if both:
        last_page = both[-1]["unclaimed"].frame.page
        chosen = next(found for found in both if found["unclaimed"].frame.page == last_page)
        log.info("dashboard frame found", extra={"qualifying_frames": len(both)})
        return chosen["unclaimed"].url
    seen = [kind for kind in KINDS if any(kind in found for found in per_frame)]
    if len(seen) == len(KINDS):
        raise WidgetNotFoundError(
            f"both widgets were found, but not in the same frame: {_named(titles, seen)}"
        )
    missing = [kind for kind in KINDS if kind not in seen]
    message = f"widget not found: {_named(titles, missing)}"
    if seen:
        message += f" (found: {_named(titles, seen)})"
    raise WidgetNotFoundError(message)


def check_dashboard(browser: TdxBrowser, settings: TdxSettings, url: str) -> None:
    """Open ``url`` on its own and check that both widgets load and parse (page 1 only)."""
    tz = ZoneInfo(settings.display_timezone)
    now = datetime.now(tz)
    titles = _titles(settings)
    browser.open_url(url, timeout_ms=settings.page_timeout_seconds * 1000)
    found = _wait_for_widgets(browser, settings, titles)
    for kind in KINDS:
        _parse(found[kind], kind, tz, now, browser)
    log.info("dashboard address checked")


def _check_signed_in(browser: TdxBrowser, base_url: str) -> None:
    urls = browser.current_urls()
    web = [url for url in urls if url.lower().startswith(("http://", "https://"))]
    # A page that has not loaded yet (about:blank, an error page) is not a sign-in page.
    if web and not any(on_tdx(url, base_url) for url in web):
        raise NotSignedInError()


def _wait_for_widgets(
    browser: TdxBrowser, settings: TdxSettings, titles: dict[WidgetKind, str]
) -> dict[WidgetKind, _Widget]:
    """Both widgets, once each shows its grid and has stopped changing."""
    deadline = _Deadline(settings.page_timeout_seconds)
    previous: dict[WidgetKind, str] = {}
    found: dict[WidgetKind, _Widget] = {}
    while True:
        _check_signed_in(browser, settings.base_url)
        found = _scan(browser.live_frames(), titles)
        settled = all(
            kind in found
            and widget_ready(found[kind].html)
            and previous.get(kind) == found[kind].html
            for kind in KINDS
        )
        if settled:
            log.info("widgets found", extra={"frame_index": _frame_indexes(browser, found)})
            return found
        if deadline.passed():
            break
        previous = {kind: widget.html for kind, widget in found.items()}
        browser.wait(POLL_MS)

    if not any(on_tdx(url, settings.base_url) for url in browser.current_urls()):
        raise _structure_error(browser, "the dashboard page did not load")
    missing = [kind for kind in KINDS if kind not in found]
    if missing:
        raise WidgetNotFoundError(f"widget not found: {_named(titles, missing)}", _blocked(browser))
    loading = [kind for kind in KINDS if not widget_ready(found[kind].html)]
    raise _structure_error(
        browser,
        f"the {' and '.join(loading) or 'dashboard'} widget did not finish loading its grid "
        f"within {settings.page_timeout_seconds} s",
    )


def _frame_indexes(browser: TdxBrowser, found: dict[WidgetKind, _Widget]) -> dict[str, int]:
    frames = browser.live_frames()
    return {kind: frames.index(w.frame) for kind, w in found.items() if w.frame in frames}


def _parse(
    widget: _Widget, kind: WidgetKind, tz: ZoneInfo, now: datetime, browser: TdxBrowser
) -> WidgetPage:
    try:
        return parse_widget(widget.html, kind, widget.url, tz, now)
    except StructureError as error:
        raise _structure_error(browser, str(error)) from error


def _page_signature(page: WidgetPage) -> tuple[int | None, str]:
    return (page.rows[0].ticket_id if page.rows else None, page.page_marker)


def _read_widget(
    browser: TdxBrowser,
    kind: WidgetKind,
    title: str,
    widget: _Widget,
    settings: TdxSettings,
    tz: ZoneInfo,
    now: datetime,
) -> WidgetRead:
    page = _parse(widget, kind, tz, now, browser)
    pages = 1
    rows: dict[int, TicketRow] = {}
    unreadable: list[str] = []
    duplicates = 0
    without_id = 0

    def collect(page: WidgetPage) -> None:
        nonlocal duplicates, without_id
        for row in page.rows:
            if row.ticket_id in rows:
                duplicates += 1
            else:
                rows[row.ticket_id] = row
        unreadable.extend(page.unreadable_dates)
        without_id += page.rows_without_id

    collect(page)
    while page.has_next and pages < settings.max_pages_per_widget:
        widget, page = _next_page(browser, kind, title, widget, page, pages, settings, tz, now)
        pages += 1
        collect(page)
    hit_limit = page.has_next
    log.info(
        "widget read",
        extra={
            "widget": kind,
            "tickets": len(rows),
            "pages": pages,
            "duplicates": duplicates,
            "rows_without_id": without_id,
            "unreadable_dates": len(unreadable),
            "hit_page_limit": hit_limit,
        },
    )
    return WidgetRead(
        kind=kind,
        rows=tuple(rows.values()),
        pages=pages,
        unreadable_dates=tuple(unreadable),
        hit_page_limit=hit_limit,
        duplicates=duplicates,
        rows_without_id=without_id,
    )


def _next_page(
    browser: TdxBrowser,
    kind: WidgetKind,
    title: str,
    widget: _Widget,
    page: WidgetPage,
    pages: int,
    settings: TdxSettings,
    tz: ZoneInfo,
    now: datetime,
) -> tuple[_Widget, WidgetPage]:
    """Click "next" once and wait until the widget shows a different, settled page."""
    before = _page_signature(page)
    next_button = widget.frame.locator(CONTAINER_SELECTOR).nth(widget.index).locator(NEXT_SELECTOR)
    try:
        # The only click in the code base: the pager's "next page" button.
        next_button.first.click(timeout=settings.page_timeout_seconds * 1000)
    except PlaywrightError as error:
        raise _structure_error(
            browser, f"could not click the next-page button of the {kind} widget on page {pages}"
        ) from error

    deadline = _Deadline(settings.page_timeout_seconds)
    previous = ""
    while not deadline.passed():
        browser.wait(POLL_MS)
        found = _scan([widget.frame], {kind: title}).get(kind)
        if found is None or not widget_ready(found.html):
            previous = ""
            continue
        if found.html == previous:
            new_page = _parse(found, kind, tz, now, browser)
            if _page_signature(new_page) != before:
                return found, new_page
        previous = found.html
    raise _structure_error(
        browser,
        f"paging did not advance for the {kind} widget after page {pages} "
        f"(waited {settings.page_timeout_seconds} s)",
    )
