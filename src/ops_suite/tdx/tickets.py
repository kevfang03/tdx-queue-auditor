"""Open tickets one at a time, read-only, and read their activity feeds.

Each ticket opens in a new page of the guarded browser (``TdxBrowser`` with the request guard,
service workers blocked and SignalR's live updates refused), at the grid's ID link or,
failing that, at ``tdx.ticket_url_template``. Every frame of that page is searched for the
feed, as the dashboard reader does for widgets, and the feed frame's HTML goes to the pure
parser in ``tdx/feed.py``, together with one read-only ``frame.evaluate`` that reports which private
markers the browser actually renders. The page is closed after reading, and the next ticket
waits ``tdx.feed_delay_seconds``.

Event request forms: for the tickets in ``form_ticket_ids``, every copy of the description
in the feed frame's HTML (the container's DOM with its hidden parts, the rest of its
section, and a copy kept as source text in a text box, an attribute or a script) is turned
into text by ``description_to_text``, the same step the Web API reader uses, and handed to
``read_event_form``, which uses the most complete "Response Summary" block; only the
``EventForm`` is kept. The browser's rendered text (``innerText``) is not used: it leaves out
whatever the page hides, so a long description shown as a shortened preview would lose its
later answers, the services and the space among them. The description is part of the
server-rendered page, so no extra request is made and nothing is expanded. One more
read-only ``frame.evaluate`` reports the container's shape as counts (text blocks, hidden
blocks, clipped or not, toggle-like controls) and where the space and services labels
appear, as place names, never text.

Read-only: this module clicks, types, fills and presses nothing, and its scripts only read
layout, computed styles and where two form labels appear. Its only navigation is one
``open_page`` per ticket address. Ticket ids may be logged; addresses only through
``describe_path``, and names, titles, feed text and description text never.
"""

import json
import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request

from ops_suite.common.config import TdxSettings
from ops_suite.tdx.browser import FrameHtml, TdxBrowser
from ops_suite.tdx.dashboard import DashboardStructureError, NotSignedInError
from ops_suite.tdx.event_form import (
    DESCRIPTION_SELECTORS,
    PART_SOURCES,
    READ_FIELDS,
    EventForm,
    EventFormOptions,
    FormRead,
    description_parts,
    read_event_form,
)
from ops_suite.tdx.feed import (
    FeedDetails,
    empty_marker_stats,
    feed_state,
    read_feed_frames,
)
from ops_suite.tdx.feed_types import TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.session import describe_path, on_tdx

log = logging.getLogger(__name__)

FEED_SEARCH_PATH = "/TDNext/Apps/Feed/Search"
POLL_MS = 250
FAILURES_IN_A_ROW = 3  # tickets in a row with no feed frame before the run stops
TEMPLATE_ID = "{id}"
# Read-only: for each private marker of the frame, in document order, whether it is rendered
# (has a layout box and is not visibility: hidden). Also whether the feed's entries are laid
# out at all: a frame in a hidden tab has no boxes, and then the flags mean nothing.
MARKER_VISIBILITY_SCRIPT = """() => {
  const markers = Array.from(document.querySelectorAll(".feed-private"));
  const entries = Array.from(document.querySelectorAll('.feed [role="group"]'));
  const rendered = entries.some((entry) => entry.getClientRects().length > 0);
  const visible = markers.map((marker) =>
    marker.getClientRects().length > 0 && getComputedStyle(marker).visibility === "visible");
  return { rendered, visible };
}"""
# The form labels whose place on the page the shape script reports, by the words it looks
# for (lower case, spaces collapsed): the two answers the outreach rules depend on.
FORM_LABELS = {"space": "what type of space", "services": "please select all that apply"}
# Where the shape script looks for them: the rendered text; the container's DOM text; the
# rest of the description's section; text boxes; template contents; any attribute; scripts;
# and anywhere in the frame's HTML.
LABEL_PLACES = (
    "shown",
    "container",
    "outside",
    "text box",
    "template",
    "attribute",
    "script",
    "anywhere",
)
# Read-only: the description container's shape as counts and flags, never its text: its
# text-bearing elements and how many of them are not rendered (no layout box, or not
# visible), whether the container clips its content, the toggle-like controls in its
# section (buttons, links without a real address), and in which of LABEL_PLACES each of
# FORM_LABELS appears.
DESCRIPTION_SHAPE_SCRIPT = """() => {
  const norm = (text) => (text || "").replace(/&nbsp;|&#0*160;|&#x0*a0;/gi, " ")
    .replace(/\\s+/g, " ").toLowerCase();
  for (const selector of SELECTORS) {
    const element = document.querySelector(selector);
    if (!element) continue;
    const blocks = Array.from(element.querySelectorAll("*")).filter(
      (node) => node.children.length === 0 && (node.textContent || "").trim() !== "");
    const hidden = blocks.filter((node) => node.getClientRects().length === 0
      || getComputedStyle(node).visibility !== "visible").length;
    const clipped = element.scrollHeight > element.clientHeight + 1
      && getComputedStyle(element).overflowY !== "visible";
    const scope = element.closest("#divDescription") || element.parentElement || element;
    const toggles = Array.from(scope.querySelectorAll('a, button, [role="button"]')).filter(
      (node) => {
        const href = node.getAttribute("href");
        return node.tagName !== "A" || !href || href === "#"
          || href.toLowerCase().startsWith("javascript:");
      }).length;
    const above = scope.parentElement;
    const section = above && /description$/i.test(above.id || "") ? above : scope;
    const rest = section.cloneNode(true);
    const inner = rest.querySelector(selector);
    if (inner) inner.remove();
    const attributes = [];
    for (const node of document.querySelectorAll("*")) {
      for (const attribute of node.attributes) attributes.push(attribute.value);
    }
    const texts = {
      shown: element.innerText,
      container: element.textContent,
      outside: rest.textContent,
      "text box": Array.from(document.querySelectorAll("textarea"))
        .map((box) => box.value + " " + box.textContent).join(" "),
      attribute: attributes.join(" "),
      template: Array.from(document.querySelectorAll("template"))
        .map((template) => template.content.textContent).join(" "),
      script: Array.from(document.scripts).map((script) => script.textContent).join(" "),
      anywhere: document.documentElement.outerHTML,
    };
    const labels = {};
    for (const [key, words] of Object.entries(LABELS)) {
      labels[key] = Object.keys(texts).filter((place) => norm(texts[place]).includes(words));
    }
    return { blocks: blocks.length, hidden, clipped, toggles, labels };
  }
  return null;
}""".replace("SELECTORS", json.dumps(list(DESCRIPTION_SELECTORS))).replace(
    "LABELS", json.dumps(FORM_LABELS)
)
# Counted over a run's form tickets: descriptions whose shape was read, those with hidden
# text blocks, those whose container clips, those with a toggle; then, per form label and
# place, the descriptions where the label appears there ("services_text box"); then, from
# the reader's own parse, the "Response Summary" blocks found, the forms whose block had all
# the read answers' labels, and the forms read from each place ("from_container").
DESCRIPTION_SHAPE_KEYS = (
    "read",
    "hidden",
    "clipped",
    "toggle",
    *(f"{label}_{place}" for label in FORM_LABELS for place in LABEL_PLACES),
    "forms",
    "blocks",
    "complete",
    *(f"from_{source}" for source in PART_SOURCES),
)

Progress = Callable[[int, int], None]  # (tickets done, tickets to read); ids never passed


@dataclass(frozen=True, slots=True)
class FeedRead:
    feeds: dict[int, TicketFeed]
    failed: tuple[int, ...]  # ticket ids whose feed could not be read (no link, no feed, timeout)
    deferred: tuple[int, ...]  # beyond max_feeds_per_run
    blocked: tuple[str, ...]  # "METHOD /masked/path" for each request the guard aborted
    allowed_non_get: tuple[str, ...]
    unreadable_shapes: tuple[str, ...] = ()  # feed.py's shapes of unreadable entries
    # "WS /path" and "GET /TDSignalR/..." lines: SignalR's live updates, refused on purpose.
    live_updates_blocked: tuple[str, ...] = ()
    # feed.MARKER_STAT_KEYS summed over the feeds read, plus tickets.py's own fallbacks.
    marker_stats: Mapping[str, int] = field(default_factory=empty_marker_stats)
    # The parsed event request forms of the form tickets whose page was read;
    # a read page with no readable form has no entry. Flags and dates only, never text.
    forms: dict[int, EventForm] = field(default_factory=dict)
    # DESCRIPTION_SHAPE_KEYS counted over the descriptions read; empty for the Web API.
    description_shapes: Mapping[str, int] = field(default_factory=dict)


def ticket_addresses(row: TicketRow | None, ticket_id: int, settings: TdxSettings) -> list[str]:
    """Where to look for the feed, in order: the grid's link, then the template. TDX host only."""
    addresses: list[str] = []
    if row is not None and row.detail_url:
        addresses.append(row.detail_url)
    template = settings.ticket_url_template.strip()
    if template and TEMPLATE_ID in template:
        addresses.append(template.replace(TEMPLATE_ID, str(ticket_id)))
    unique = list(dict.fromkeys(addresses))
    return [address for address in unique if on_tdx(address, settings.base_url)]


def read_feeds(
    browser: TdxBrowser,
    settings: TdxSettings,
    rows_by_id: Mapping[int, TicketRow],
    ticket_ids: Sequence[int],
    now: datetime,
    progress: Progress | None = None,
    *,
    form_ticket_ids: Collection[int] = (),
    form_options: EventFormOptions | None = None,
) -> FeedRead:
    """The feeds of ``ticket_ids``, in order, up to ``max_feeds_per_run``; the rest deferred.

    For the tickets in ``form_ticket_ids``, the event request form in the description too.
    """
    tz = ZoneInfo(settings.display_timezone)
    options = form_options or EventFormOptions()
    to_read = list(ticket_ids[: settings.max_feeds_per_run])
    deferred = tuple(ticket_ids[settings.max_feeds_per_run :])
    blocked_before = len(browser.blocked_requests())
    allowed_before = len(browser.allowed_non_get_requests())
    live_before = len(browser.live_updates_blocked())
    feeds: dict[int, TicketFeed] = {}
    forms: dict[int, EventForm] = {}
    failed: list[int] = []
    shapes: list[str] = []
    stats = empty_marker_stats()
    shapes_seen = dict.fromkeys(DESCRIPTION_SHAPE_KEYS, 0)
    no_feed_in_a_row = 0

    for index, ticket_id in enumerate(to_read):
        if index and settings.feed_delay_seconds:
            browser.wait(int(settings.feed_delay_seconds * 1000))
        if progress is not None and index:
            progress(index, len(to_read))
        addresses = ticket_addresses(rows_by_id.get(ticket_id), ticket_id, settings)
        if not addresses:
            failed.append(ticket_id)
            log.info("feed not read", extra={"ticket_id": ticket_id, "reason": "no_address"})
            continue
        details = None
        form_options = options if ticket_id in form_ticket_ids else None
        row = rows_by_id.get(ticket_id)
        requester = row.requestor if row is not None else None
        read: FormRead | None = None
        shape: DescriptionShape | None = None
        for address in addresses:
            details, read, shape = _read_one(
                browser, settings, address, ticket_id, tz, now, form_options, requester
            )
            if details is not None:
                break
        if details is None:
            failed.append(ticket_id)
            no_feed_in_a_row += 1
            log.info("feed not read", extra={"ticket_id": ticket_id, "reason": "no_feed"})
            if no_feed_in_a_row >= FAILURES_IN_A_ROW:
                raise DashboardStructureError(
                    f"no activity feed was found on {no_feed_in_a_row} tickets in a row (step: "
                    f"open each ticket and wait up to {settings.page_timeout_seconds} s for its "
                    "feed)",
                    _since(browser.blocked_requests(), blocked_before),
                )
            continue
        no_feed_in_a_row = 0
        feeds[ticket_id] = details.feed
        form = read.form if read is not None else None
        if form is not None:
            forms[ticket_id] = form
        if form_options is not None:
            log.info(
                "form read",
                extra={
                    "ticket_id": ticket_id,
                    "readable": form is not None,
                    "blocks": read.blocks if read is not None else 0,
                    "answers": read.answers if read is not None else 0,
                    "source": read.source if read is not None else None,
                },
            )
        if read is not None:
            shapes_seen["forms"] += 1
            shapes_seen["blocks"] += read.blocks
            shapes_seen["complete"] += read.answers == len(READ_FIELDS)
            if read.source is not None:
                shapes_seen[f"from_{read.source}"] += 1
        if shape is not None:
            shapes_seen["read"] += 1
            shapes_seen["hidden"] += shape.hidden > 0
            shapes_seen["clipped"] += shape.clipped
            shapes_seen["toggle"] += shape.toggles > 0
            for label, places in shape.labels.items():
                for place in places:
                    shapes_seen[f"{label}_{place}"] += 1
            log.info("description shape", extra={"ticket_id": ticket_id, **shape.as_log()})
        shapes.extend(details.unreadable_shapes)
        for key, value in details.marker_stats.items():
            stats[key] += value
        log.info(
            "feed read",
            extra={
                "ticket_id": ticket_id,
                "entries": len(details.feed.entries),
                "unreadable": details.feed.unreadable,
                "markers": dict(details.marker_stats),
            },
        )
    if progress is not None and to_read:
        progress(len(to_read), len(to_read))

    return FeedRead(
        feeds=feeds,
        failed=tuple(failed),
        deferred=deferred,
        blocked=_since(browser.blocked_requests(), blocked_before),
        allowed_non_get=_since(browser.allowed_non_get_requests(), allowed_before),
        unreadable_shapes=tuple(shapes),
        live_updates_blocked=_since(browser.live_updates_blocked(), live_before),
        marker_stats=stats,
        forms=forms,
        description_shapes=shapes_seen,
    )


def _since(records: Sequence, start: int) -> tuple[str, ...]:
    return tuple(record.describe() for record in records[start:])


def _read_one(
    browser: TdxBrowser,
    settings: TdxSettings,
    address: str,
    ticket_id: int,
    tz: ZoneInfo,
    now: datetime,
    form_options: EventFormOptions | None = None,
    requester: str | None = None,
) -> tuple[FeedDetails | None, FormRead | None, "DescriptionShape | None"]:
    """Open ``address`` in a new page, wait for its feed, read it and close the page.

    With ``form_options``, also read the event request form from the feed frame's description
    (None without a description) and the description's shape (counts and place names only;
    None when it could not be read).
    ``requester``: the row's requester, who never becomes desk through the desk signature.
    """
    searched = _SearchWatch()
    timeout_ms = settings.page_timeout_seconds * 1000
    page = browser.open_page(address, timeout_ms=timeout_ms, on_request_finished=searched)
    try:
        feed_frame = _wait_for_feed(browser, page, settings, searched)
        if feed_frame is None:
            log.info(
                "no feed on the ticket page",
                extra={"ticket_id": ticket_id, "path": describe_path(address)},
            )
            return None, None, None
        flags = _marker_visibility(browser, page, feed_frame)
        details = read_feed_frames(
            [feed_frame.html],
            ticket_id,
            tz,
            now,
            private_marker_visible=flags,
            desk_signatures=settings.rules.desk_signatures,
            requester=requester,
        )
        if details is not None and flags is None:
            stats = dict(details.marker_stats)
            stats["visibility_fallback"] = 1
            details = FeedDetails(details.feed, details.unreadable_shapes, stats)
        read = shape = None
        if details is not None and form_options is not None:
            read = _read_form(feed_frame, tz, now, form_options)
            shape = _description_shape(browser, page, feed_frame)
        return details, read, shape
    finally:
        browser.close_page(page)


def _read_form(
    frame: FrameHtml, tz: ZoneInfo, now: datetime, options: EventFormOptions
) -> FormRead | None:
    """The event request form in the frame's description; None without a description.

    Every copy of the description in the frame's HTML (hidden parts, a text box, an
    attribute or a script included) goes through ``description_to_text`` to the parser,
    which uses the most complete "Response Summary" block; the text is then dropped.
    """
    parts = description_parts(frame.html)
    return read_event_form(parts, tz, now, options) if parts is not None else None


@dataclass(frozen=True, slots=True)
class DescriptionShape:
    """The description container's shape on the page: counts and place names, never text."""

    blocks: int  # text-bearing elements
    hidden: int  # of those, not rendered
    clipped: bool  # the container clips its content (overflow with a capped height)
    toggles: int  # buttons and address-less links in the description's section
    # For each of FORM_LABELS, the LABEL_PLACES where it appears.
    labels: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def as_log(self) -> dict[str, object]:
        return {
            "blocks": self.blocks,
            "hidden": self.hidden,
            "clipped": self.clipped,
            "toggles": self.toggles,
            "labels": {label: list(places) for label, places in self.labels.items()},
        }


def _description_shape(
    browser: TdxBrowser, page: Page, frame: FrameHtml
) -> DescriptionShape | None:
    """The description's shape on the rendered page; None if it cannot be read."""
    try:
        result = browser.evaluate_in_frame(page, frame.frame_index, DESCRIPTION_SHAPE_SCRIPT)
    except PlaywrightError as error:
        log.info("description shape not read", extra={"error": type(error).__name__})
        return None
    if not isinstance(result, dict):
        return None
    blocks, hidden, clipped, toggles = (
        result.get(key) for key in ("blocks", "hidden", "clipped", "toggles")
    )
    # type() rather than isinstance(): a bool is an int, and a count must not be one.
    if type(blocks) is not int or type(hidden) is not int or type(toggles) is not int:
        return None
    if not isinstance(clipped, bool):
        return None
    return DescriptionShape(blocks, hidden, clipped, toggles, _label_places(result.get("labels")))


def _label_places(found: object) -> dict[str, tuple[str, ...]]:
    """The script's label places, kept only where they are known label and place names."""
    if not isinstance(found, dict):
        return {}
    return {
        label: tuple(place for place in LABEL_PLACES if place in places)
        for label, places in found.items()
        if label in FORM_LABELS and isinstance(places, list)
    }


def _marker_visibility(browser: TdxBrowser, page: Page, frame: FrameHtml) -> list[bool] | None:
    """Whether each private marker of the feed frame is rendered; None if that is unknown.

    Unknown when the script fails, returns something unexpected, or the frame's entries are
    not laid out (a hidden tab), so that the caller falls back to marker presence.
    """
    try:
        result = browser.evaluate_in_frame(page, frame.frame_index, MARKER_VISIBILITY_SCRIPT)
    except PlaywrightError as error:
        log.info("marker visibility not read", extra={"error": type(error).__name__})
        return None
    if not isinstance(result, dict):
        return None
    visible = result.get("visible")
    if not isinstance(visible, list) or not all(isinstance(flag, bool) for flag in visible):
        return None
    if visible and not result.get("rendered"):
        log.info("marker visibility not read", extra={"error": "not_rendered"})
        return None
    return visible


class _SearchWatch:
    """Counts finished feed searches on one page (its frames included)."""

    def __init__(self) -> None:
        self.finished = 0

    def __call__(self, request: Request) -> None:
        try:
            if urlsplit(request.url).path == FEED_SEARCH_PATH:
                self.finished += 1
        except ValueError:
            pass


def _check_signed_in(page: Page, base_url: str) -> None:
    url = page.url
    # A page that has not loaded yet (about:blank, an error page) is not a sign-in page.
    if url.lower().startswith(("http://", "https://")) and not on_tdx(url, base_url):
        raise NotSignedInError()


def _wait_for_feed(
    browser: TdxBrowser, page: Page, settings: TdxSettings, searched: _SearchWatch
) -> FrameHtml | None:
    """The feed frame once it shows a settled feed; None on timeout.

    Entries count once the feed frame's HTML is the same on two polls in a row. An empty feed
    counts only after the page's feed search has finished, and only if it stays empty for two
    polls taken after that.
    """
    end = time.monotonic() + settings.page_timeout_seconds
    previous: str | None = None
    searches_seen = 0
    while True:
        _check_signed_in(page, settings.base_url)
        try:
            frames = browser.page_frames(page)
        except PlaywrightError:
            frames = []
        states = [feed_state(frame.html) for frame in frames]
        if searched.finished != searches_seen:
            searches_seen = searched.finished
            previous = None  # start the settling over from the first poll after the search
        found = next((f for f, s in zip(frames, states, strict=True) if s == "entries"), None)
        if found is None and searches_seen:
            found = next((f for f, s in zip(frames, states, strict=True) if s == "empty"), None)
        if found is not None and found.html == previous:
            return found
        previous = found.html if found is not None else None
        if time.monotonic() > end:
            return None
        try:
            page.wait_for_timeout(POLL_MS)
        except PlaywrightError:
            return None
