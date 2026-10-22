"""Synthetic TDX Web API answers for the API tests: invented data in the shapes the TDX Web
API reference documents (Report, DisplayColumn, ItemUpdate, ItemUpdateReply, Ticket, User).

``FakeApi`` stands in for urllib's opener, so no test touches the network. Tokens are built
here at run time from invented claims; none is a real token.
"""

import base64
import email.message
import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ops_suite.common.config import TdxApiSettings, TdxSettings
from ops_suite.tdx.api_client import TdxApiClient
from ops_suite.tdx.api_token import ApiToken, TokenSource, parse_token, save_token
from ops_suite.tdx.demo.app import PAGES_DIR
from ops_suite.tdx.demo.dataset import (
    CLAIMED_COLUMNS,
    UNCLAIMED_COLUMNS,
    DemoTicket,
    demo_pages,
    demo_tickets,
)
from ops_suite.tdx.event_form import EventFormOptions, description_text, parse_event_form
from ops_suite.tdx.feed import parse_feed
from ops_suite.tdx.rows import parse_widget

BASE = "https://tdx.example.edu/TDNext/Home/Desktop/Default.aspx"
ORIGIN = "https://tdx.example.edu"
API = "/TDWebApi/api"
APP = 43
UNCLAIMED_REPORT = 11
CLAIMED_REPORT = 12
NOW = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
TZ = ZoneInfo("America/New_York")

# The report columns as Report Builder names them; the reader finds them by HeaderText.
COLUMN_NAMES = {
    "ID": "TicketID",
    "Created": "CreatedDate",
    "Modified": "LastModifiedDate",
    "Title": "Title",
    "Requestor": "CustomerName",
    "Affiliation": "Affiliation",
    "Resp Group": "ResponsibleGroupName",
    "Acct/Dept": "AccountName",
    "Status": "StatusName",
    "Priority": "PriorityName",
    "Prim Resp": "ResponsibleFullName",
    "Due": "DueDate",
    "Service": "ServiceName",
}
DATE_COLUMNS = frozenset({"Created", "Modified", "Due"})


def _segment(data: Mapping[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(
    expires: datetime, *, filler: int = 0, claims: Mapping[str, object] | None = None
) -> str:
    """An invented, unsigned-looking JWT with ``exp`` = ``expires``. ``filler`` pads the
    payload, for tokens longer than one keychain entry."""
    payload: dict[str, object] = {"exp": int(expires.timestamp()), "sub": "synthetic"}
    if filler:
        payload["pad"] = "x" * filler
    if claims:
        payload.update(claims)
    return f"{_segment({'alg': 'HS256', 'typ': 'JWT'})}.{_segment(payload)}.c3ludGhldGlj"


def make_token(expires: datetime, **kwargs: object) -> ApiToken:
    return parse_token(make_jwt(expires, **kwargs))  # type: ignore[arg-type]


def api_tdx(**api: object) -> TdxSettings:
    settings: dict[str, object] = {
        "enabled": True,
        "app_id": APP,
        "unclaimed_report_id": UNCLAIMED_REPORT,
        "claimed_report_id": CLAIMED_REPORT,
    }
    settings.update(api)
    return TdxSettings(base_url=BASE, api=TdxApiSettings(**settings))


@dataclass
class Answer:
    """A canned HTTP answer: a status, JSON (or raw bytes) and headers."""

    status: int = 200
    body: object = None
    headers: Mapping[str, str] = field(default_factory=dict)
    raw: bytes | None = None


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value


Route = object | Answer | Callable[[urllib.request.Request], object]


class FakeApi:
    """An opener answering from ``routes`` (path -> JSON, Answer or callable); unknown paths
    answer 404. Every request is recorded: (method, url, headers)."""

    def __init__(self, routes: Mapping[str, Route] | None = None) -> None:
        self.routes: dict[str, Route] = dict(routes or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        self.calls.append((request.get_method(), url, dict(request.header_items())))
        route = self.routes.get(urlsplit(url).path, Answer(404))
        if callable(route) and not isinstance(route, Answer):
            route = route(request)
        answer = route if isinstance(route, Answer) else Answer(200, route)
        body = answer.raw if answer.raw is not None else json.dumps(answer.body).encode("utf-8")
        if not 200 <= answer.status < 300:
            headers = email.message.Message()
            for key, value in answer.headers.items():
                headers[key] = value
            raise urllib.error.HTTPError(url, answer.status, "fake", headers, io.BytesIO(body))
        return FakeResponse(answer.status, body, answer.headers)

    def paths(self) -> list[str]:
        return [urlsplit(url).path for _, url, _ in self.calls]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def stored_tokens(
    expires: datetime | None = None,
    *,
    fetch: Callable[[], ApiToken] | None = None,
    now: datetime = NOW,
) -> TokenSource:
    """A token source with a synthetic token stored in the (test) keychain."""
    save_token(make_token(expires or now + timedelta(hours=20)))
    return TokenSource(fetch, clock=lambda: now)


def api_client(
    api: FakeApi,
    tdx: TdxSettings | None = None,
    tokens: TokenSource | None = None,
    clock: FakeClock | None = None,
) -> TdxApiClient:
    clock = clock or FakeClock()
    return TdxApiClient(
        tdx or api_tdx(),
        tokens or stored_tokens(),
        opener=api,  # type: ignore[arg-type]
        clock=clock,
        wall=lambda: NOW,
        sleep=clock.sleep,
    )


# --- documented shapes ------------------------------------------------------------------


def iso(when: datetime) -> str:
    """``2026-10-21T16:00:00Z``: the API's dates are in UTC."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def report(headers: tuple[str, ...], rows: list[Mapping[str, object]], **extra: object) -> dict:
    """A Report with DisplayedColumns for ``headers`` and DataRows keyed by column name."""
    columns = [
        {
            "HeaderText": header,
            "ColumnName": COLUMN_NAMES[header],
            "DataType": 7 if header in DATE_COLUMNS else 1,
        }
        for header in headers
    ]
    data = [{COLUMN_NAMES[header]: value for header, value in row.items()} for row in rows]
    return {
        "ID": 1,
        "Name": "Synthetic report",
        "MaxResults": 0,
        "DisplayedColumns": columns,
        "DataRows": data,
        **extra,
    }


def entry(
    entry_id: int,
    author: str,
    when: datetime,
    body: str,
    *,
    private: bool = False,
    update_type: int = 1,
    replies: int = 0,
    rich: bool = True,
) -> dict:
    """An ItemUpdate as the ticket feed endpoint returns it (no Replies, only the count)."""
    return {
        "ID": entry_id,
        "CreatedFullName": author,
        "CreatedDate": iso(when),
        "LastUpdatedDate": iso(when),
        "Body": body,
        "IsRichHtml": rich,
        "IsPrivate": private,
        "UpdateType": update_type,
        "ItemType": 9,
        "RepliesCount": replies,
        "NotifiedList": "",
    }


def reply(reply_id: int, author: str, when: datetime, body: str) -> dict:
    """An ItemUpdateReply (no private flag of its own)."""
    return {
        "ID": reply_id,
        "CreatedFullName": author,
        "CreatedDate": iso(when),
        "Body": body,
        "IsRichHtml": True,
    }


# --- the demo dataset as API answers -----------------------------------------------------


def _row(ticket: DemoTicket) -> dict[str, object]:
    return {
        "ID": ticket.ticket_id,
        "Created": iso(ticket.created),
        "Modified": iso(ticket.modified or ticket.created),
        "Title": ticket.title,
        "Requestor": ticket.requestor,
        "Affiliation": "Student",
        "Resp Group": "IT Service Desk",
        "Acct/Dept": "Example College",
        "Status": ticket.status,
        "Priority": "Medium",
        "Prim Resp": ticket.prim_resp,
        "Due": None,
        "Service": ticket.service,
    }


def demo_routes(tickets: list[DemoTicket], now: datetime, tz: ZoneInfo) -> dict[str, Route]:
    """Every API answer ``tdx audit --source api`` asks for, for the demo's tickets: the same
    tickets, people, times and texts as the demo's widget pages, feeds and descriptions."""
    pages = demo_pages(tickets, now, tz)
    unclaimed = [ticket for ticket in tickets if ticket.widget == "unclaimed"]
    claimed = [ticket for ticket in tickets if ticket.widget == "claimed"]
    routes: dict[str, Route] = {
        f"{API}/reports/{UNCLAIMED_REPORT}": report(
            UNCLAIMED_COLUMNS, [{h: _row(t)[h] for h in UNCLAIMED_COLUMNS} for t in unclaimed]
        ),
        f"{API}/reports/{CLAIMED_REPORT}": report(
            CLAIMED_COLUMNS, [{h: _row(t)[h] for h in CLAIMED_COLUMNS} for t in claimed]
        ),
    }
    for ticket in unclaimed:
        entries = [
            entry(
                ticket.ticket_id * 100 + number,
                item.author,
                item.at,
                "<br>".join(escape(line) for line in item.text.split("\n")),
                private=item.private,
            )
            for number, item in enumerate(ticket.entries, start=1)
        ]
        routes[f"{API}/{APP}/tickets/{ticket.ticket_id}/feed"] = list(reversed(entries))
        description = pages.get(f"descriptions/{ticket.ticket_id}.html")
        routes[f"{API}/{APP}/tickets/{ticket.ticket_id}"] = {
            "ID": ticket.ticket_id,
            "AppID": APP,
            "Title": ticket.title,
            "Description": description.decode("utf-8") if description else "",
            "IsRichHtml": True,
            "RequestorName": ticket.requestor,
        }
    return routes


# --- the demo dataset as the browser reader parses it ------------------------------------

FEED_END = '<div class="text-center NoPrint js-feed-end">'
DESCRIPTION_MARK = "<!-- ticket description -->"
PAGE_URL = "http://127.0.0.1:1/TDWorkManagement/WorkManagement/Dashboard/Dashboard"


def demo_browser_side(now: datetime, tz: ZoneInfo, options: EventFormOptions | None = None):
    """The demo's tickets as the browser reader parses its widget pages, feeds and forms."""
    tickets = demo_tickets(now)
    pages = demo_pages(tickets, now, tz)
    rows = []
    for name, page in pages.items():
        for widget, kind in (("40101", "unclaimed"), ("40102", "claimed")):
            if name.startswith(f"widgets/{widget}-"):
                rows += parse_widget(page.decode("utf-8"), kind, PAGE_URL, tz, now).rows
    shell = (PAGES_DIR / "ticket.html").read_text(encoding="utf-8")
    feeds, forms = {}, {}
    for ticket in tickets:
        fragment = pages.get(f"feeds/{ticket.ticket_id}.html")
        if fragment is None:
            continue
        description = pages.get(f"descriptions/{ticket.ticket_id}.html") or b""
        frame = shell.replace(DESCRIPTION_MARK, description.decode("utf-8")).replace(
            FEED_END, fragment.decode("utf-8") + FEED_END
        )
        feeds[ticket.ticket_id] = parse_feed(
            [frame],
            ticket.ticket_id,
            tz,
            now,
            requester=ticket.requestor,
            desk_signatures=("SCIS Commons Support", "CCI Commons Support"),
        )
        text = description_text(frame)
        form = parse_event_form(text, tz, now, options or EventFormOptions()) if text else None
        if form is not None:
            forms[ticket.ticket_id] = form
    return tickets, rows, feeds, forms
