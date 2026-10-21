"""Ticket rows from the TDX dashboard widgets. Pure functions; no network and no Playwright.

The selectors follow docs/tdx-page-structure.md: each widget is a Kendo UI grid inside a
TDWorkManagement dashboard widget container. Only stable tag, role and class names are used,
never ids, and never text except the column headers and the widget title (which comes from
config).

Personal data rule: titles, requestor and responsible names and ticket links stay inside
``TicketRow`` objects in memory. They never go into an exception message, a log record or
``repr()``. A date cell that cannot be read leaves this module only as its ``date_shape``.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

WidgetKind = Literal["unclaimed", "claimed"]

# --- selectors (see docs/tdx-page-structure.md) ------------------------------------------
CONTAINER_SELECTOR = ".tdx-dashboard__widget-container"
TITLE_SELECTOR = ".tdx-control-bar__title"
GRID_SELECTOR = ".k-grid"
HEADER_ROW_SELECTOR = "thead tr"
ROW_SELECTOR = "tbody tr.k-master-row"
NO_RECORDS_SELECTOR = "tbody tr.k-grid-norecords"
LOADING_SELECTOR = ".tdx-loading-animation, .k-loading-mask"
# The pager's "next page" button: a nav button that is neither "first" nor "last", with the
# right-pointing caret icon. The extra icon names cover older Kendo themes.
NEXT_SELECTOR = (
    ".k-pager .k-pager-nav:not(.k-pager-first):not(.k-pager-last)"
    ":has(.k-svg-i-caret-alt-right, .k-i-caret-alt-right, .k-i-arrow-60-right, .k-i-arrow-e)"
)
CURRENT_PAGE_SELECTOR = ".k-pager .k-pager-numbers .k-selected"

REQUIRED_COLUMNS: dict[WidgetKind, frozenset[str]] = {
    "unclaimed": frozenset({"ID", "Created", "Title", "Requestor", "Resp Group"}),
    "claimed": frozenset(
        {"ID", "Modified", "Title", "Requestor", "Resp Group", "Status", "Prim Resp"}
    ),
}
# Every column the captures showed. Error messages name these and only count the others.
KNOWN_COLUMNS = frozenset(
    {
        "ID",
        "Created",
        "Title",
        "Requestor",
        "Affiliation",
        "Resp Group",
        "Acct/Dept",
        "Priority",
        "Due",
        "Service",
        "Modified",
        "Status",
        "Prim Resp",
    }
)

_SHAPE_LIMIT = 40
_SORT_WORDS = re.compile(r"\b(?:sort(?:ed)?(?:\s+by)?|ascending|descending)\b", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_TICKET_ID = re.compile(r"\d{1,12}")


@dataclass(frozen=True, slots=True, repr=False)
class TicketRow:
    widget: WidgetKind
    ticket_id: int
    title: str
    requestor: str
    resp_group: str
    created: datetime | None  # aware, in display_timezone; None if absent or unreadable
    modified: datetime | None  # claimed only
    prim_resp: str | None  # claimed only; None when blank
    status: str | None  # claimed only
    detail_url: str | None  # absolute URL of the ID/title link; raw, never logged

    def __repr__(self) -> str:
        return f"TicketRow(widget={self.widget!r}, ticket_id={self.ticket_id})"


@dataclass(frozen=True, slots=True)
class WidgetPage:
    rows: tuple[TicketRow, ...]
    unreadable_dates: tuple[str, ...]  # date_shape() of each date cell that could not be read
    has_next: bool  # the pager shows a usable "next" control
    rows_without_id: int = 0  # data rows skipped because their ID cell held no number
    page_marker: str = ""  # the pager's current page number, used to see a page turn


class StructureError(Exception):
    """The widget's markup is not what the captures showed. The message names only
    the widget kind, column names and structural parts, never page text."""


# --- text helpers ------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def normalise_header(text: str) -> str:
    """Collapse whitespace, strip sort-indicator text, case-fold for matching.

    TDX sort links repeat the label for screen readers ("ID Sort by ID Ascending"), so a
    label that is the same words twice is kept once.
    """
    words = _collapse(_SORT_WORDS.sub(" ", text)).casefold().split()
    half = len(words) // 2
    if len(words) % 2 == 0 and half and words[:half] == words[half:]:
        words = words[:half]
    return " ".join(words)


def normalise_title(text: str) -> str:
    """Widget titles match case-insensitively after collapsing whitespace."""
    return _collapse(text).casefold()


def date_shape(text: str) -> str:
    """Each digit becomes 9 and each letter a; spaces and punctuation stay; 40 chars at most."""
    shape = "".join("9" if c.isdigit() else "a" if c.isalpha() else c for c in _collapse(text))
    return shape[:_SHAPE_LIMIT] if shape else "(blank)"


# --- dates -------------------------------------------------------------------------------

_WEEKDAYS = frozenset(
    {
        "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu", "thur", "thurs",
        "thursday", "fri", "friday", "sat", "saturday", "sun", "sunday",
    }
)  # fmt: skip
_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_LEADING_WORD = re.compile(r"([A-Za-z]+)\.?,?\s+")
_TIME = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?"
_TIME_SEPARATOR = r"(?:\s*,\s*|\s+(?:at\s+)?|T)"
_DATE_FORMATS = [
    # 10/2/26, 10/02/2026
    re.compile(
        rf"(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{4}}|\d{{2}})"
        rf"(?:{_TIME_SEPARATOR}{_TIME})?"
    ),
    # 2026-10-02
    re.compile(
        rf"(?P<year>\d{{4}})-(?P<month>\d{{1,2}})-(?P<day>\d{{1,2}})(?:{_TIME_SEPARATOR}{_TIME})?"
    ),
    # Oct 2, 2026
    re.compile(
        rf"(?P<month_name>[A-Za-z]+)\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})"
        rf"(?:{_TIME_SEPARATOR}{_TIME})?"
    ),
]


def parse_tdx_datetime(text: str, tz: ZoneInfo, now: datetime) -> datetime | None:
    """A TDX date cell as an aware datetime in ``tz``; None if the format is not known.

    US month/day order; two-digit years are 20xx; a date without a time is midnight. ``now``
    is for relative formats ("Today"), which no capture has shown yet.
    """
    text = _collapse(text)
    leading = _LEADING_WORD.match(text)
    if leading and leading.group(1).casefold() in _WEEKDAYS:
        text = text[leading.end() :]
    for pattern in _DATE_FORMATS:
        match = pattern.fullmatch(text)
        if match:
            return _build_datetime(match, tz)
    return None


def _build_datetime(match: re.Match[str], tz: ZoneInfo) -> datetime | None:
    parts = match.groupdict()
    if parts.get("month_name") is not None:
        month = _MONTHS.get(parts["month_name"].casefold())
        if month is None:
            return None
    else:
        month = int(parts["month"])
    year = int(parts["year"])
    if year < 100:
        year += 2000
    hour = int(parts["hour"] or 0)
    ampm = parts["ampm"]
    if ampm is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm[0] in "Pp" else 0)
    try:
        return datetime(
            year,
            month,
            int(parts["day"]),
            hour,
            int(parts["minute"] or 0),
            int(parts["second"] or 0),
            tzinfo=tz,
        )
    except ValueError:
        return None


# --- widgets -----------------------------------------------------------------------------


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _container(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    found = soup.select_one(CONTAINER_SELECTOR)
    return found if found is not None else soup


def widget_title(html: str) -> str | None:
    """The normalised title of a widget container, or None if it has no title element."""
    title = _container(_soup(html)).select_one(TITLE_SELECTOR)
    return normalise_title(title.get_text(" ")) if title is not None else None


def widget_ready(html: str) -> bool:
    """True once the widget shows a grid with a header row and no loading indicator."""
    container = _container(_soup(html))
    grid = container.select_one(GRID_SELECTOR)
    if grid is None or container.select_one(LOADING_SELECTOR) is not None:
        return False
    header = grid.select_one(HEADER_ROW_SELECTOR)
    return header is not None and header.find("th") is not None


def _header_label(cell: Tag) -> str:
    title = cell.select_one(".k-column-title")
    return (title or cell).get_text(" ")


def _column_map(grid: Tag, kind: WidgetKind) -> dict[str, int]:
    header = grid.select_one(HEADER_ROW_SELECTOR)
    cells = header.find_all("th") if header is not None else []
    if not cells:
        raise StructureError(f"{kind} widget: the grid has no header row")
    columns: dict[str, int] = {}
    for index, cell in enumerate(cells):
        columns.setdefault(normalise_header(_header_label(cell)), index)
    missing = sorted(name for name in REQUIRED_COLUMNS[kind] if name.casefold() not in columns)
    if missing:
        known = {name.casefold(): name for name in KNOWN_COLUMNS}
        found = [known[label] for label in columns if label in known]
        others = len(columns) - len(found)
        found_text = ", ".join(found) or "none of the known columns"
        if others:
            found_text += f" and {others} other column(s)"
        raise StructureError(
            f"{kind} widget: required column(s) missing: {', '.join(missing)} (found: {found_text})"
        )
    return columns


def _cell_text(cells: list[Tag], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return _collapse(cells[index].get_text(" "))


def _link(cells: list[Tag], index: int | None, page_url: str) -> str | None:
    if index is None or index >= len(cells):
        return None
    anchor = cells[index].find("a", href=True)
    if anchor is None:
        return None
    href = str(anchor["href"]).strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None
    absolute = urljoin(page_url, href)
    return absolute if urlsplit(absolute).scheme in ("http", "https") else None


def _is_disabled(element: Tag) -> bool:
    return (
        "k-disabled" in (element.get("class") or [])
        or element.has_attr("disabled")
        or str(element.get("aria-disabled", "")).lower() == "true"
    )


def parse_widget(
    html: str, kind: WidgetKind, page_url: str, tz: ZoneInfo, now: datetime
) -> WidgetPage:
    """Rows of one page of one widget, from the widget container's HTML."""
    container = _container(_soup(html))
    grid = container.select_one(GRID_SELECTOR)
    if grid is None:
        raise StructureError(f"{kind} widget: no Kendo grid (.k-grid) inside the widget")
    columns = _column_map(grid, kind)

    def column(name: str) -> int | None:
        return columns.get(name.casefold())

    def read_date(cells: list[Tag], name: str) -> datetime | None:
        index = column(name)
        if index is None:
            return None
        text = _cell_text(cells, index)
        parsed = parse_tdx_datetime(text, tz, now)
        if parsed is None:
            unreadable.append(date_shape(text))
        return parsed

    rows: list[TicketRow] = []
    unreadable: list[str] = []
    data_rows = grid.select(ROW_SELECTOR)
    without_id = 0
    claimed = kind == "claimed"
    for row in data_rows:
        cells = row.find_all("td", recursive=False)
        id_text = _cell_text(cells, column("ID"))
        if not _TICKET_ID.fullmatch(id_text):
            without_id += 1
            continue
        rows.append(
            TicketRow(
                widget=kind,
                ticket_id=int(id_text),
                title=_cell_text(cells, column("Title")),
                requestor=_cell_text(cells, column("Requestor")),
                resp_group=_cell_text(cells, column("Resp Group")),
                created=read_date(cells, "Created"),
                modified=read_date(cells, "Modified") if claimed else None,
                prim_resp=(_cell_text(cells, column("Prim Resp")) or None) if claimed else None,
                status=(_cell_text(cells, column("Status")) or None) if claimed else None,
                detail_url=_link(cells, column("ID"), page_url)
                or _link(cells, column("Title"), page_url),
            )
        )
    if data_rows and without_id * 2 > len(data_rows):
        raise StructureError(
            f"{kind} widget: {without_id} of {len(data_rows)} rows have no readable ticket id"
        )
    if not data_rows and grid.select("tbody tr") and not grid.select(NO_RECORDS_SELECTOR):
        raise StructureError(f"{kind} widget: the grid has rows, but none marked k-master-row")

    next_button = container.select_one(NEXT_SELECTOR)
    marker = container.select_one(CURRENT_PAGE_SELECTOR)
    return WidgetPage(
        rows=tuple(rows),
        unreadable_dates=tuple(unreadable),
        has_next=next_button is not None and not _is_disabled(next_button),
        rows_without_id=without_id,
        page_marker=_collapse(marker.get_text(" ")) if marker is not None else "",
    )
