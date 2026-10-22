"""The queue read through the TDX Web API: the same rows, feeds and event forms as the browser.

A second source behind the same types (``TicketRow``, ``TicketFeed``, ``EventForm``, and the
``DashboardRead`` / ``FeedRead`` summaries), so the rules, the summaries and the cards do not
change. The browser stays the default source; this one is a read-only proof of concept.

- **Rows** come from two Report Builder reports (``tdx.api.unclaimed_report_id`` and
  ``claimed_report_id``) that show the same tickets and columns as the two dashboard widgets:
  ``GET /reports/{id}?withData=true``. Columns are found by their header text, as on the
  dashboard (ID, Created, Title, Requestor, Resp Group, Modified, Status, Prim Resp), and a
  missing one stops the run with a message that names it. The report answers in one piece (no
  paging); a report that returned as many rows as its ``MaxResults`` may have been cut short
  and is flagged like a widget that hit its page limit.
- **Feeds** come from ``GET /{app}/tickets/{id}/feed`` for the tickets ``plan_feeds`` picks.
  That endpoint leaves out replies to an entry (only their count), so an entry with replies is
  read once more, ``GET /feed/{id}``. Entries map to ``FeedEntry`` kinds:
  - **private** from the entry's own private flag;
  - **system** for a merge (update type 6) or a comment that opens with a merge notice or
    reassignment (their authors count as desk, as on the feed page), for the ticket's creation
    and "My Work" changes (types 4 and 5), and for an entry with no visible comment, such as a
    status change;
  - **reply** otherwise.
  Replies carry no private flag of their own: a reply under a private entry is private when the
  entry's author wrote it, as on the feed page. The desk-signature check runs on each reply's
  text, and the newest reply's text becomes ``latest_reply_text``; nothing else keeps it.
- **Event forms** come from ``GET /{app}/tickets/{id}``'s description, through
  ``description_to_text`` (the same text step as the browser reader's) into
  ``parse_event_form``, which cuts the Recipient Data block off first.
- **Ticket links** for the cards: ``tdx.ticket_url_template``, else the ticket page the
  dashboard's links open (``/TDNext/Apps/<app>/Tickets/TicketDet.aspx?TicketID=<id>``).
- **The app id**: ``tdx.api.app_id``, else the ``/Apps/<id>/`` of ``tdx.ticket_url_template``,
  else the first ticket's own record (``GET /tickets/{id}``, which searches every ticketing app).

Dates from the API are in UTC (the reference's dates guide); they are shown in the desk's time
zone like the dashboard's. Responses hold student data: they are mapped straight to the types
above and dropped. Logs and errors carry counts, ticket ids, reason codes and endpoint
patterns only.
"""

import html
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ops_suite.common.config import ConfigProblem, TdxSettings
from ops_suite.tdx.api_client import ApiError, TdxApiClient
from ops_suite.tdx.dashboard import KINDS, DashboardRead, DashboardStructureError, WidgetRead
from ops_suite.tdx.event_form import (
    EventForm,
    EventFormOptions,
    description_to_text,
    parse_event_form,
)
from ops_suite.tdx.feed import FeedDetails, comment_facts, comment_text, empty_marker_stats
from ops_suite.tdx.feed_types import EntryKind, FeedEntry, TicketFeed
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import (
    KNOWN_COLUMNS,
    REQUIRED_COLUMNS,
    TicketRow,
    WidgetKind,
    date_shape,
    normalise_header,
)
from ops_suite.tdx.rules import RuleSettings, plan_feeds
from ops_suite.tdx.session import tdx_origin
from ops_suite.tdx.tickets import TEMPLATE_ID, FeedRead, Progress

log = logging.getLogger(__name__)

FAILURES_IN_A_ROW = 3  # tickets in a row whose feed could not be read before the run stops
UPDATE_TYPE_CREATED = 4
UPDATE_TYPE_MY_WORK = 5
UPDATE_TYPE_MERGE = 6
HTML_COLUMN = 11  # the reference's ColumnDataType "Html": sanitised HTML strings
# The ticket page the dashboard's ticket links open (docs/tdx-page-structure.md, frames).
TICKET_PAGE = "/TDNext/Apps/{app}/Tickets/TicketDet.aspx?TicketID={id}"
_APP_IN_LINK = re.compile(r"/Apps/(\d+)/Tickets/", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_FRACTION = re.compile(r"(\.\d{6})\d+")
_TICKET_ID = re.compile(r"\d{1,12}")
# Ticket failures that leave the run going (like a ticket page without a feed).
_TICKET_FAILURES = frozenset(
    {"not_found", "forbidden", "bad_json", "bad_shape", "timeout", "connection"}
)


@dataclass(frozen=True, slots=True)
class ApiQueue:
    """What one API read gives: the same summaries as the browser read, plus the app id."""

    dashboard: DashboardRead
    feeds: FeedRead
    rows: tuple[TicketRow, ...]
    app_id: int
    app_id_source: str  # "setting", "ticket_url_template" or "first ticket"
    requests: Mapping[str, int] = field(default_factory=dict)  # endpoint pattern -> count


# --- values -----------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _text(value: object, *, is_html: bool = False) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int | float):
        return str(value)
    if not isinstance(value, str):
        return ""
    if is_html:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    return _collapse(value)


def text_value(value: object) -> str:
    """A text field of an answer, whitespace collapsed; "" if it is not text or a number."""
    return _text(value)


def parse_api_datetime(value: object, tz: ZoneInfo) -> datetime | None:
    """An API date (ISO 8601, UTC when no offset is given) in ``tz``; None if unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(tz)


def _ticket_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _TICKET_ID.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# --- rows -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Column:
    name: str  # the key in each data row
    is_html: bool


def _columns(report: Mapping[str, object], kind: WidgetKind) -> dict[str, _Column]:
    """Header text (normalised) -> column; DashboardStructureError naming a missing one."""
    displayed = report.get("DisplayedColumns")
    columns: dict[str, _Column] = {}
    for column in displayed if isinstance(displayed, list) else []:
        if not isinstance(column, dict):
            continue
        header, name = column.get("HeaderText"), column.get("ColumnName")
        if isinstance(header, str) and isinstance(name, str) and name:
            columns.setdefault(
                normalise_header(header), _Column(name, column.get("DataType") == HTML_COLUMN)
            )
    missing = sorted(name for name in REQUIRED_COLUMNS[kind] if name.casefold() not in columns)
    if missing:
        known = {name.casefold(): name for name in KNOWN_COLUMNS}
        found = [known[label] for label in columns if label in known]
        others = len(columns) - len(found)
        found_text = ", ".join(found) or "none of the known columns"
        if others:
            found_text += f" and {others} other column(s)"
        raise DashboardStructureError(
            f"{kind} report: required column(s) missing: {', '.join(missing)} (found: "
            f"{found_text}). Add them to the report in TDX, or pick another report"
        )
    return columns


def map_report(
    report: object,
    kind: WidgetKind,
    tz: ZoneInfo,
    link: Callable[[int], str | None] = lambda ticket_id: None,
) -> WidgetRead:
    """The rows of one report as the matching widget's rows. Pure; no network."""
    if not isinstance(report, dict):
        raise DashboardStructureError(f"{kind} report: the answer is not a report")
    columns = _columns(report, kind)
    data = report.get("DataRows")
    if not isinstance(data, list):
        raise DashboardStructureError(
            f"{kind} report: the answer has no rows (it must be read with its data)"
        )
    claimed = kind == "claimed"
    unreadable: list[str] = []

    def value(row: Mapping[str, object], header: str) -> object:
        column = columns.get(header.casefold())
        return row.get(column.name) if column is not None else None

    def text(row: Mapping[str, object], header: str) -> str:
        column = columns.get(header.casefold())
        return _text(value(row, header), is_html=column is not None and column.is_html)

    def date(row: Mapping[str, object], header: str) -> datetime | None:
        if header.casefold() not in columns:
            return None
        raw = value(row, header)
        parsed = parse_api_datetime(raw, tz)
        if parsed is None:
            unreadable.append(date_shape(raw if isinstance(raw, str) else ""))
        return parsed

    rows: list[TicketRow] = []
    seen: set[int] = set()
    duplicates = without_id = 0
    for item in data:
        ticket_id = _ticket_id(value(item, "ID")) if isinstance(item, dict) else None
        if ticket_id is None or not isinstance(item, dict):
            without_id += 1
            continue
        if ticket_id in seen:
            duplicates += 1
            continue
        seen.add(ticket_id)
        rows.append(
            TicketRow(
                widget=kind,
                ticket_id=ticket_id,
                title=text(item, "Title"),
                requestor=text(item, "Requestor"),
                resp_group=text(item, "Resp Group"),
                created=date(item, "Created"),
                modified=date(item, "Modified") if claimed else None,
                prim_resp=(text(item, "Prim Resp") or None) if claimed else None,
                status=(text(item, "Status") or None) if claimed else None,
                detail_url=link(ticket_id),
            )
        )
    if data and without_id * 2 > len(data):
        raise DashboardStructureError(
            f"{kind} report: {without_id} of {len(data)} rows have no readable ticket id"
        )
    limit = _int(report.get("MaxResults"))
    return WidgetRead(
        kind=kind,
        rows=tuple(rows),
        pages=1,
        unreadable_dates=tuple(unreadable),
        hit_page_limit=bool(limit) and len(data) >= limit,
        duplicates=duplicates,
        rows_without_id=without_id,
    )


def column_count(report: object) -> int:
    """How many columns a report shows."""
    displayed = report.get("DisplayedColumns") if isinstance(report, dict) else None
    return len(displayed) if isinstance(displayed, list) else 0


@dataclass(frozen=True, slots=True)
class ReportInfo:
    """One report the person can see: its id, name and source (interface wording only)."""

    report_id: int
    name: str
    source: str


def report_list(answer: object) -> list[ReportInfo]:
    """The answer of ``GET /reports``, by name; owners' names and the rest are left out."""
    found = [
        ReportInfo(
            _int(item.get("ID")),
            _text(item.get("Name"))[:80],
            _text(item.get("ReportSourceName"))[:40],
        )
        for item in (answer if isinstance(answer, list) else [])
        if isinstance(item, dict) and _int(item.get("ID")) > 0
    ]
    return sorted(found, key=lambda report: (report.name.casefold(), report.report_id))


def report_ids(tdx: TdxSettings) -> dict[WidgetKind, int]:
    """The two report ids; ConfigProblem while one is not set."""
    ids: dict[WidgetKind, int] = {
        "unclaimed": tdx.api.unclaimed_report_id,
        "claimed": tdx.api.claimed_report_id,
    }
    unset = [kind for kind in KINDS if not ids[kind]]
    if unset:
        names = " and ".join(f"tdx.api.{kind}_report_id" for kind in unset)
        raise ConfigProblem(f'{names} not set; "tdx api reports" lists the reports you can see')
    return ids


def ticket_link(tdx: TdxSettings, app_id: int) -> Callable[[int], str | None]:
    """The address a card links to for a ticket: the template, else the ticket page."""
    template = tdx.ticket_url_template.strip()
    if template and TEMPLATE_ID in template:
        return lambda ticket_id: template.replace(TEMPLATE_ID, str(ticket_id))
    if not app_id:
        return lambda ticket_id: None
    origin = tdx_origin(tdx.base_url)
    return lambda ticket_id: origin + TICKET_PAGE.format(app=app_id, id=ticket_id)


def resolve_app_id(
    tdx: TdxSettings, client: TdxApiClient, first_ticket: int | None
) -> tuple[int, str]:
    """(app id, where it came from). DashboardStructureError when there is no way to tell."""
    if tdx.api.app_id:
        return tdx.api.app_id, "setting"
    match = _APP_IN_LINK.search(tdx.ticket_url_template)
    if match:
        return int(match.group(1)), "ticket_url_template"
    if first_ticket is None:
        raise DashboardStructureError(
            "no ticket to learn the app id from; set tdx.api.app_id (the number after /Apps/ "
            "in a ticket's address)"
        )
    ticket = client.get(f"/tickets/{first_ticket}")
    app_id = _int(ticket.get("AppID")) if isinstance(ticket, dict) else 0
    if app_id <= 0:
        raise DashboardStructureError("the first ticket's record has no app id; set tdx.api.app_id")
    return app_id, "first ticket"


def read_reports(
    tdx: TdxSettings, client: TdxApiClient
) -> tuple[dict[WidgetKind, WidgetRead], int, str]:
    """Both reports as widget reads, with ticket links; (widgets, app id, its source)."""
    tz = ZoneInfo(tdx.display_timezone)
    ids = report_ids(tdx)
    widgets: dict[WidgetKind, WidgetRead] = {}
    for kind in KINDS:
        report = client.get(f"/reports/{ids[kind]}", {"withData": "true"})
        widgets[kind] = map_report(report, kind, tz)
    first = next((row.ticket_id for kind in KINDS for row in widgets[kind].rows), None)
    app_id, source = resolve_app_id(tdx, client, first)
    link = ticket_link(tdx, app_id)
    linked = {
        kind: replace(
            read, rows=tuple(replace(row, detail_url=link(row.ticket_id)) for row in read.rows)
        )
        for kind, read in widgets.items()
    }
    log.info(
        "api reports read",
        extra={
            "rows": {kind: len(read.rows) for kind, read in linked.items()},
            "app_id_source": source,
        },
    )
    return linked, app_id, source


# --- feeds ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    author: str
    created: datetime | None
    kind: EntryKind
    body: str = ""  # a reply's comment HTML, read for text only if it is the newest reply


def _body(item: Mapping[str, object]) -> str:
    """An entry's or reply's body as HTML: rich text as given, plain text escaped."""
    body = item.get("Body")
    if not isinstance(body, str):
        return ""
    if item.get("IsRichHtml") is False:
        return "<br>".join(html.escape(line) for line in body.split("\n"))
    return body


class _FeedMapper:
    def __init__(self, tz: ZoneInfo, signatures: Sequence[str], requester: str | None) -> None:
        self.tz = tz
        # The desk-signature rule needs the requester, who never becomes desk through it.
        self.signatures = tuple(signatures) if requester else ()
        self.requester = requester or ""
        self.entries: list[_Entry] = []
        self.shapes: list[str] = []
        self.stats = empty_marker_stats()
        self.desk_authors: list[str] = []

    def _add(self, author: str, stamp: object, kind: EntryKind, body: str = "") -> None:
        if not author:
            self.shapes.append("no author")
            return
        created = parse_api_datetime(stamp, self.tz)
        if created is None:
            self.shapes.append(
                f"undated: {date_shape(stamp) if isinstance(stamp, str) else 'no timestamp'}"
            )
        self.entries.append(_Entry(author, created, kind, body if kind == "reply" else ""))

    def _desk(self, author: str) -> None:
        if author and not any(same_person(author, known) for known in self.desk_authors):
            self.desk_authors.append(author)

    def _signed(self, author: str, kind: EntryKind, signed: bool) -> None:
        if kind != "reply" or not signed or not author or same_person(author, self.requester):
            return
        self.stats["signature_desk_replies"] += 1
        self._desk(author)

    def entry(self, item: Mapping[str, object], replies: object) -> None:
        author = _text(item.get("CreatedFullName"))
        body = _body(item)
        facts = comment_facts(body, self.signatures)
        update_type = item.get("UpdateType")
        note = "merge" if update_type == UPDATE_TYPE_MERGE else facts.note
        private = item.get("IsPrivate") is True
        self.stats["entries"] += 1
        self.stats["private_present"] += private
        self.stats["private_visible"] += private
        if note is not None:
            self.stats["merge_notes" if note == "merge" else "reassignments"] += 1
            if author:
                self.desk_authors.append(author)  # only staff merge or reassign tickets
            self._add(author, item.get("CreatedDate"), "system")
            private = False  # replies under a TDX notice inherit nothing
        else:
            if private:
                kind: EntryKind = "private"
            elif update_type in (UPDATE_TYPE_CREATED, UPDATE_TYPE_MY_WORK) or not facts.visible:
                kind = "system"
            else:
                kind = "reply"
            self._add(author, item.get("CreatedDate"), kind, body)
            self._signed(author, kind, facts.signed)
        expected = _int(item.get("RepliesCount"))
        if not expected:
            return
        if not isinstance(replies, list):
            self.shapes.append("replies not read")
            return
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            self.stats["entries"] += 1
            self.stats["nested"] += 1
            reply_author = _text(reply.get("CreatedFullName"))
            reply_body = _body(reply)
            reply_facts = comment_facts(reply_body, self.signatures)
            # A reply has no private flag of its own: under a private entry it is private when
            # the entry's author wrote it, as the feed page shows it.
            inherited = private and same_person(reply_author, author)
            self.stats["inherited_private"] += inherited
            if inherited:
                reply_kind: EntryKind = "private"
            else:
                reply_kind = "reply" if reply_facts.visible else "system"
            self._add(reply_author, reply.get("CreatedDate"), reply_kind, reply_body)
            if note != "merge":  # nothing inside a merge notice counts
                self._signed(reply_author, reply_kind, reply_facts.signed)


def _newest_first(entries: list[_Entry]) -> list[_Entry]:
    """Dated entries sorted newest first; undated ones keep their place, as on the feed page."""
    dated = iter(sorted((e for e in entries if e.created), key=lambda e: e.created, reverse=True))
    return [entry if entry.created is None else next(dated) for entry in entries]


def map_feed(
    ticket_id: int,
    entries: object,
    replies: Mapping[int, object],
    tz: ZoneInfo,
    *,
    desk_signatures: Sequence[str] = (),
    requester: str | None = None,
) -> FeedDetails:
    """A ticket's feed from the feed endpoint's entries and, by entry id, the replies read
    from ``GET /feed/{id}``. Pure; no network. DashboardStructureError if ``entries`` is not
    a list."""
    if not isinstance(entries, list):
        raise DashboardStructureError("a ticket's feed answer is not a list of entries")
    mapper = _FeedMapper(tz, desk_signatures, requester)
    for item in entries:
        if isinstance(item, dict):
            mapper.entry(item, replies.get(_int(item.get("ID"))))
    ordered = _newest_first(mapper.entries)
    newest = next((entry for entry in ordered if entry.kind == "reply"), None)
    feed = TicketFeed(
        ticket_id=ticket_id,
        entries=tuple(FeedEntry(e.author, e.created, e.kind) for e in ordered),
        unreadable=len(mapper.shapes),
        desk_authors=tuple(mapper.desk_authors),
        latest_reply_text=comment_text(newest.body) if newest is not None else None,
    )
    return FeedDetails(feed, tuple(mapper.shapes), dict(mapper.stats))


def read_feed(
    client: TdxApiClient,
    app_id: int,
    ticket_id: int,
    tz: ZoneInfo,
    *,
    desk_signatures: Sequence[str] = (),
    requester: str | None = None,
) -> FeedDetails:
    """One ticket's feed, replies included. ApiError if the feed itself cannot be read; a
    reply list that cannot be read is counted as unreadable."""
    entries = client.get(f"/{app_id}/tickets/{ticket_id}/feed")
    replies: dict[int, object] = {}
    for item in entries if isinstance(entries, list) else []:
        entry_id = _int(item.get("ID")) if isinstance(item, dict) else 0
        if not entry_id or not _int(item.get("RepliesCount")):
            continue
        try:
            full = client.get(f"/feed/{entry_id}")
        except ApiError as error:
            if error.reason in ("unauthorised", "time_box"):
                raise
            log.info("replies not read", extra={"ticket_id": ticket_id, "reason": error.reason})
            continue
        replies[entry_id] = full.get("Replies") if isinstance(full, dict) else None
    return map_feed(
        ticket_id, entries, replies, tz, desk_signatures=desk_signatures, requester=requester
    )


def read_form(
    client: TdxApiClient,
    app_id: int,
    ticket_id: int,
    tz: ZoneInfo,
    now: datetime,
    options: EventFormOptions,
) -> EventForm | None:
    """The event request form in the ticket's description; None if it has none."""
    ticket = client.get(f"/{app_id}/tickets/{ticket_id}")
    description = ticket.get("Description") if isinstance(ticket, dict) else None
    if not isinstance(description, str) or not description.strip():
        return None
    rich_html = not (isinstance(ticket, dict) and ticket.get("IsRichHtml") is False)
    text = description_to_text(description, rich_html=rich_html)
    return parse_event_form(text, tz, now, options)


def read_feeds(
    client: TdxApiClient,
    tdx: TdxSettings,
    app_id: int,
    rows_by_id: Mapping[int, TicketRow],
    ticket_ids: Sequence[int],
    now: datetime,
    progress: Progress | None = None,
    *,
    form_ticket_ids: Collection[int] = (),
    form_options: EventFormOptions | None = None,
) -> FeedRead:
    """The feeds of ``ticket_ids`` up to ``max_feeds_per_run``, the rest deferred, as the
    browser's ticket reader gives them; for ``form_ticket_ids`` the event form too."""
    tz = ZoneInfo(tdx.display_timezone)
    options = form_options or EventFormOptions()
    to_read = list(ticket_ids[: tdx.max_feeds_per_run])
    deferred = tuple(ticket_ids[tdx.max_feeds_per_run :])
    feeds: dict[int, TicketFeed] = {}
    forms: dict[int, EventForm] = {}
    failed: list[int] = []
    shapes: list[str] = []
    stats = empty_marker_stats()
    failures_in_a_row = 0
    for index, ticket_id in enumerate(to_read):
        if progress is not None and index:
            progress(index, len(to_read))
        row = rows_by_id.get(ticket_id)
        try:
            details = read_feed(
                client,
                app_id,
                ticket_id,
                tz,
                desk_signatures=tdx.rules.desk_signatures,
                requester=row.requestor if row is not None else None,
            )
            form = None
            if ticket_id in form_ticket_ids:
                form = read_form(client, app_id, ticket_id, tz, now, options)
                log.info("form read", extra={"ticket_id": ticket_id, "readable": form is not None})
        except (ApiError, DashboardStructureError) as error:
            reason = getattr(error, "reason", "bad_shape")
            if reason not in _TICKET_FAILURES and not reason.startswith("http_5"):
                raise
            failed.append(ticket_id)
            failures_in_a_row += 1
            log.info("feed not read", extra={"ticket_id": ticket_id, "reason": reason})
            if failures_in_a_row >= FAILURES_IN_A_ROW:
                raise ApiError(reason, "ticket feeds") from None
            continue
        failures_in_a_row = 0
        feeds[ticket_id] = details.feed
        if form is not None:
            forms[ticket_id] = form
        shapes.extend(details.unreadable_shapes)
        for key, count in details.marker_stats.items():
            stats[key] += count
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
        blocked=(),
        allowed_non_get=(),
        unreadable_shapes=tuple(shapes),
        marker_stats=stats,
        forms=forms,
    )


def read_queue(
    tdx: TdxSettings,
    client: TdxApiClient,
    now: datetime,
    rules: RuleSettings,
    options: EventFormOptions,
    progress: Progress | None = None,
) -> ApiQueue:
    """Both reports, then the feeds (and event forms) the rules need, as ``tdx audit`` reads
    them through the browser."""
    tz = ZoneInfo(tdx.display_timezone)
    widgets, app_id, source = read_reports(tdx, client)
    dashboard = DashboardRead(widgets=widgets, blocked=())
    rows = [row for widget in widgets.values() for row in widget.rows]
    plan = plan_feeds(rows, now, rules, tz)
    feeds = read_feeds(
        client,
        tdx,
        app_id,
        {row.ticket_id: row for row in rows},
        plan.feed_ticket_ids,
        now,
        progress,
        form_ticket_ids=set(plan.form_ticket_ids),
        form_options=options,
    )
    return ApiQueue(
        dashboard=dashboard,
        feeds=feeds,
        rows=tuple(rows),
        app_id=app_id,
        app_id_source=source,
        requests=dict(client.requests),
    )
