"""The ``tdx demo`` queue: 27 invented tickets, generated at start with times relative to now.

Every title is generic and every name an obvious placeholder, so the demo's output, card and
screenshot can be shared. The tickets are built so each rule fires once or twice; ``expect``
names the outcome each ticket should get from the rules (with ``--no-llm``), and
``EXPECTED_FINDINGS`` sums them for the tests and the README.

``demo_pages`` turns the tickets into the widget grids, feeds and event request form
descriptions that the synthetic app serves (``widgets/<widget id>-<page>.html``,
``feeds/<ticket id>.html`` and ``descriptions/<ticket id>.html``), in the structure of the
reviewed captures (docs/tdx-page-structure.md).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Literal
from zoneinfo import ZoneInfo

UNCLAIMED_WIDGET = 40101
CLAIMED_WIDGET = 40102
CLOSED_WIDGET = 40103
TASKS_WIDGET = 40104
ROWS_PER_PAGE = 10
DESK = "Dana Desk"
FULL_TIMER = "Robin Fulltime"
FORM_TITLE = "CCI Event Request Form"
# The event request form's options, as the real form words them.
HOST_EVENT = (
    "To host an event at CCI, including reserving space, marketing and other logistical support."
)
MARKETING_ONLY = (
    "Marketing/advertising for an occasion or event that does not require CCI space or other "
    "logistical support"
)
# The college is now the SCIS (formerly CCI). One demo form already shows the new name, so the
# demo shows both are matched (tdx.events.form_titles, known_option_prefixes).
SCIS_FORM_TITLE = "SCIS Event Request Form"
SCIS_HOST_EVENT = (
    "To host an event at SCIS, including reserving space, marketing and other logistical support."
)
LOBBY = "Lobby Area (10th and/or 11th floor lobby area)"
CLASSROOM = "Classroom Space (available during non-class time)"
OTHER_SPACE = "Other (please explain)"

UNCLAIMED_COLUMNS = (
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
)
CLAIMED_COLUMNS = (
    "Resp Group",
    "Modified",
    "ID",
    "Title",
    "Requestor",
    "Acct/Dept",
    "Status",
    "Priority",
    "Prim Resp",
    "Due",
    "Service",
)

# The rules' outcome per ticket with --no-llm: a finding "kind/reason", or a count key.
Expect = Literal[
    "client_waiting/no_reply_yet",
    "client_waiting/requestor_last",
    "needs_review/other_last",
    "event_over/event_over",
    "location_tbd_soon/tbd_soon",
    "location_tbd_soon/location_missing",
    "event_outreach_due/outreach_due",
    "event_details_pending/details_pending",
    "client_waiting/event_requester_replied",
    "ready_to_close/desk_replied_last",
    "event_marketing_only",
    "event_no_outreach",
    "event_in_progress",
    "event_recurring",
    "unclaimed_recent",
    "desk_replied_last",
    "claimed",
]
# Findings per kind with --no-llm. With the LLM, the two requestor_last tickets whose client
# says they will follow up or needs no reply become follow_up_with_client.
EXPECTED_FINDINGS = {
    "client_waiting": 6,
    "follow_up_with_client": 0,
    "event_outreach_due": 1,
    "event_details_pending": 1,
    "event_over": 1,
    "location_tbd_soon": 2,
    "needs_review": 1,
    "ready_to_close": 1,
}
# The shift summary's possible duplicate: the newer ticket -> the older one.
EXPECTED_DUPLICATES = {1000023: 1000001}
# The shift summary's "Events this week", in date order: ticket id -> state code. One of each
# state.
EXPECTED_WEEK_EVENTS = {
    1000026: "requester_replied",
    1000025: "location_missing",
    1000007: "location_tbd",
    1000027: "no_outreach",
    1000024: "in_progress",
    1000020: "waiting_on_requester",
    1000019: "no_outreach_yet",
    1000021: "marketing_only",
}
# The verdict a working classifier should give each requestor_last ticket's last message.
EXPECTED_VERDICTS = {
    1000002: "expects_reply",
    1000003: "client_will_follow_up",
    1000004: "no_reply_needed",
}


@dataclass(frozen=True, slots=True)
class DemoEntry:
    author: str
    at: datetime
    text: str
    private: bool = False


@dataclass(frozen=True, slots=True)
class DemoForm:
    """The answers of an invented event request form; rendered into the ticket description."""

    day: datetime
    looking: str
    space: str
    services: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DemoTicket:
    ticket_id: int
    widget: Literal["unclaimed", "claimed"]
    title: str
    requestor: str
    created: datetime
    expect: Expect
    entries: tuple[DemoEntry, ...] = ()  # oldest first
    modified: datetime | None = None
    status: str = "New"
    prim_resp: str = ""
    service: str = "General Support"
    form: DemoForm | None = None


def _at_day(now: datetime, days: int, hour: int, minute: int = 0) -> datetime:
    """``days`` from now's date at a fixed local time, for event dates."""
    day = (now + timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)


def _short(when: datetime) -> str:
    """``10/20/26``, the date shape desk staff type into event titles."""
    return f"{when.month}/{when.day}/{when:%y}"


def demo_tickets(now: datetime) -> list[DemoTicket]:
    """The 21 unclaimed and 6 claimed tickets, relative to ``now`` (timezone-aware)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.replace(second=0, microsecond=0)

    def ago(hours: float) -> datetime:
        return now - timedelta(hours=hours)

    over = (_at_day(now, -6, 15), _at_day(now, -5, 15))
    tbd = _at_day(now, 2, 15)
    panel = _at_day(now, 4, 14)
    workshop = _at_day(now, 1, 18)
    town_hall = _at_day(now, 1, 11)
    return [
        # Client waiting, no reply yet: nobody has answered for three days.
        DemoTicket(
            1000001,
            "unclaimed",
            "Printer in lab will not connect",
            "Sam Sample",
            ago(74),
            "client_waiting/no_reply_yet",
            service="Printing",
        ),
        # Requestor wrote last, three times: expects a reply, will follow up, needs no reply.
        DemoTicket(
            1000002,
            "unclaimed",
            "VPN keeps disconnecting off campus",
            "Alex Example",
            ago(122),
            "client_waiting/requestor_last",
            (
                DemoEntry(
                    DESK,
                    ago(100),
                    "Hi Alex,\n\nCould you reinstall the VPN client from the software page "
                    "and let us know if it still drops?\n\nDana\nIT Service Desk",
                ),
                DemoEntry(
                    "Alex Example",
                    ago(76),
                    "Hi Dana,\n\nI reinstalled it but it still drops every few minutes. Can "
                    "someone look at my laptop this week?\n\nThanks,\nAlex",
                ),
            ),
            service="Network and VPN",
        ),
        DemoTicket(
            1000003,
            "unclaimed",
            "Software install request for a course",
            "Jordan Placeholder",
            ago(121),
            "client_waiting/requestor_last",
            (
                DemoEntry(
                    DESK,
                    ago(99),
                    "Hi Jordan,\n\nThe installer is ready in the software portal. Please "
                    "try it and tell us how it goes.\n\nDana\nIT Service Desk",
                ),
                DemoEntry(
                    "Jordan Placeholder",
                    ago(75),
                    "Hi Dana,\n\nThanks for the steps. I am away until Monday, so I will try "
                    "the install then and let you know how it goes.\n\nBest,\nJordan",
                ),
            ),
            service="Software",
        ),
        DemoTicket(
            1000004,
            "unclaimed",
            "Cannot print from personal laptop",
            "Casey Testcase",
            ago(120),
            "client_waiting/requestor_last",
            (
                DemoEntry(
                    DESK,
                    ago(98),
                    "Hi Casey,\n\nPlease add the lab printer with the steps in the attached "
                    "guide.\n\nDana\nIT Service Desk",
                ),
                DemoEntry(
                    "Casey Testcase",
                    ago(74),
                    "That worked, I can print again. Thank you!",
                ),
            ),
            service="Printing",
        ),
        # Needs review: an outside contact wrote last, three days ago.
        DemoTicket(
            1000005,
            "unclaimed",
            "Projector bulb replacement in room 101",
            "Riley Sample",
            ago(120),
            "needs_review/other_last",
            (
                DemoEntry(
                    DESK,
                    ago(100),
                    "Hi Riley,\n\nWe asked the vendor for a replacement bulb.\n\nDana",
                ),
                DemoEntry(
                    "Vic Vendor",
                    ago(73),
                    "The replacement bulb ships next week.",
                ),
            ),
            service="Classroom Technology",
        ),
        # Events: over (multi-date title), location TBD two days out, and a recurring one.
        DemoTicket(
            1000006,
            "unclaimed",
            f"EVENT [{_short(over[0])}, {_short(over[1])} 3:00 PM - 5:00 PM Room 101] "
            "Robotics club demo day",
            "Taylor Example",
            ago(400),
            "event_over/event_over",
            service="Event Support",
        ),
        DemoTicket(
            1000007,
            "unclaimed",
            f"EVENT [{_short(tbd)} 3:00 PM TBD] Guest lecture on cloud careers",
            "Jamie Sample",
            ago(150),
            "location_tbd_soon/tbd_soon",
            service="Event Support",
        ),
        DemoTicket(
            1000008,
            "unclaimed",
            "EVENT [Weekly, Mon 5:00 PM Room 210] Coding club meetup",
            "Quinn Placeholder",
            ago(600),
            "event_recurring",
            service="Event Support",
        ),
        # Recent: under 48 hours old, so no feed is opened.
        DemoTicket(
            1000009,
            "unclaimed",
            "Laptop loaner request",
            "Avery Example",
            ago(5),
            "unclaimed_recent",
            service="Equipment Loans",
        ),
        DemoTicket(
            1000010,
            "unclaimed",
            "Monitor flickering in the study room",
            "Drew Sample",
            ago(20),
            "unclaimed_recent",
            service="Desktop Support",
        ),
        # The desk replied last: nothing to do yet (under close_after_days).
        DemoTicket(
            1000011,
            "unclaimed",
            "Email not syncing on phone",
            "Morgan Example",
            ago(96),
            "desk_replied_last",
            (
                DemoEntry("Morgan Example", ago(95), "Mail stopped syncing on my phone."),
                DemoEntry(
                    DESK,
                    ago(60),
                    "Hi Morgan,\n\nWe reset the mail profile. It should sync within the "
                    "hour.\n\nDana",
                ),
            ),
            service="Email",
        ),
        DemoTicket(
            1000012,
            "unclaimed",
            "Request for a shared drive folder",
            "Jesse Placeholder",
            ago(97),
            "desk_replied_last",
            (
                DemoEntry("Jesse Placeholder", ago(96), "Our group needs a shared folder."),
                DemoEntry(FULL_TIMER, ago(80), "Checked the group's storage quota.", True),
                DemoEntry(
                    DESK,
                    ago(70),
                    "Hi Jesse,\n\nThe folder is ready; you should see it after signing in "
                    "again.\n\nDana",
                ),
            ),
            service="Storage",
        ),
        # The desk replied four days ago and the client has been quiet since: ready to close.
        DemoTicket(
            1000022,
            "unclaimed",
            "Wi-Fi password reset on a tablet",
            "Sydney Sample",
            ago(150),
            "ready_to_close/desk_replied_last",
            (
                DemoEntry("Sydney Sample", ago(149), "My tablet will not join the Wi-Fi."),
                DemoEntry(
                    DESK,
                    ago(100),
                    "Hi Sydney,\n\nWe reset the device's Wi-Fi registration. Please forget the "
                    "network and join it again.\n\nDana",
                ),
            ),
            service="Network and VPN",
        ),
        # The same requester as 1000001, with a similar title: a possible duplicate in the
        # shift summary, pointing at the older 1000001.
        DemoTicket(
            1000023,
            "unclaimed",
            "Lab printer will not connect",
            "Sam Sample",
            ago(50),
            "client_waiting/no_reply_yet",
            service="Printing",
        ),
        # Renamed events: the desk renames at first handling, so the form and the
        # feed tell the state. Four days out, the requester answered and the desk confirmed:
        # "in progress", no finding.
        DemoTicket(
            1000024,
            "unclaimed",
            f"EVENT [{_short(panel)} 2:00 PM - 4:00 PM Room 210] Alumni career panel",
            "Reese Example",
            ago(300),
            "event_in_progress",
            (
                DemoEntry(
                    DESK,
                    ago(100),
                    "Hi Reese,\n\nThanks for your event request. Could you confirm the room "
                    "booking and tell us whether you want it recorded?\n\nDana\nIT Service Desk",
                ),
                DemoEntry("Reese Example", ago(70), "Room 210 is confirmed. No recording."),
                DemoEntry(DESK, ago(30), "Thanks, you are all set.\n\nDana\nIT Service Desk"),
            ),
            service="Event Support",
            form=DemoForm(panel, HOST_EVENT, CLASSROOM, ("A/V & Technical Support",)),
        ),
        # Tomorrow, with a date and a time but no room in the title, and no form (made by
        # hand): "location missing", like TBD.
        DemoTicket(
            1000025,
            "unclaimed",
            f"EVENT [{_short(workshop)} 6:00 PM - 7:00 PM] Resume review workshop",
            "Quinn Placeholder",
            ago(40),
            "location_tbd_soon/location_missing",
            service="Event Support",
        ),
        # Tomorrow, and the requester answered the desk three hours ago: the desk owes a
        # reply, so client waiting at once (high, within near_event_days).
        DemoTicket(
            1000026,
            "unclaimed",
            f"EVENT [{_short(town_hall)} 11:00 AM - 12:00 PM Room 101] Faculty town hall",
            "Avery Sample",
            ago(200),
            "client_waiting/event_requester_replied",
            (
                DemoEntry(
                    DESK,
                    ago(30),
                    "Hi Avery,\n\nCould you send the Zoom link and add the desk mailbox as an "
                    "alternate host?\n\nDana\nIT Service Desk",
                ),
                DemoEntry("Avery Sample", ago(3), "Here is the link; the host is added."),
            ),
            service="Event Support",
            form=DemoForm(town_hall, HOST_EVENT, CLASSROOM, ("A/V & Technical Support",)),
        ),
        # Event request forms: A/V six days out and nobody has reached out yet;
        # a classroom five days out where the desk asked three days ago (on a form that already
        # says SCIS); a lobby with catering only, three days out; marketing only, six days out.
        DemoTicket(
            1000019,
            "unclaimed",
            FORM_TITLE,
            "Charlie Example",
            ago(20),
            "event_outreach_due/outreach_due",
            service="Event Support",
            form=DemoForm(_at_day(now, 6, 15), HOST_EVENT, LOBBY, ("A/V & Technical Support",)),
        ),
        DemoTicket(
            1000020,
            "unclaimed",
            SCIS_FORM_TITLE,
            "Frankie Sample",
            ago(100),
            "event_details_pending/details_pending",
            (
                DemoEntry(
                    DESK,
                    ago(72),
                    "Hi Frankie,\n\nThanks for your event request. Could you confirm the room "
                    "booking, send the Zoom link if it is hybrid, add the desk mailbox as a Zoom "
                    "alternate host and tell us whether you want it recorded?\n\nDana\n"
                    "IT Service Desk",
                ),
            ),
            service="Event Support",
            form=DemoForm(
                _at_day(now, 5, 15), SCIS_HOST_EVENT, CLASSROOM, ("SCIS Operational Assistance",)
            ),
        ),
        DemoTicket(
            1000021,
            "unclaimed",
            FORM_TITLE,
            "Harper Placeholder",
            ago(30),
            "event_marketing_only",
            service="Event Support",
            form=DemoForm(_at_day(now, 6, 18), MARKETING_ONLY, OTHER_SPACE, ("Marketing Support",)),
        ),
        DemoTicket(
            1000027,
            "unclaimed",
            FORM_TITLE,
            "Rowan Placeholder",
            ago(26),
            "event_no_outreach",
            service="Event Support",
            form=DemoForm(_at_day(now, 3, 12), HOST_EVENT, LOBBY, ("Event Catering",)),
        ),
        # Claimed by full-timers: counted only, never opened.
        *(
            DemoTicket(
                ticket_id,
                "claimed",
                title,
                requestor,
                ago(hours + 48),
                "claimed",
                modified=ago(hours),
                status=status,
                prim_resp=prim_resp,
            )
            for ticket_id, title, requestor, hours, status, prim_resp in (
                (1000013, "New staff laptop setup", "Kim Example", 30, "In Process", FULL_TIMER),
                (1000014, "Lab image update for next term", "Lee Sample", 70, "New", FULL_TIMER),
                (1000015, "Classroom microphone replacement", "Pat Example", 6, "In Process", DESK),
                (1000016, "Shared mailbox permissions", "Sky Placeholder", 90, "In Process", DESK),
                (1000017, "Badge access to the server room", "Ari Sample", 12, "New", FULL_TIMER),
                (1000018, "Department printer lease renewal", "Rowan Example", 200, "New", DESK),
            )
        ),
    ]


def expected_findings(tickets: list[DemoTicket]) -> Counter[str]:
    """Findings per kind that ``tickets`` should give with --no-llm."""
    return Counter(ticket.expect.split("/")[0] for ticket in tickets if "/" in ticket.expect)


# --- pages -------------------------------------------------------------------------------


def _grid_time(when: datetime, tz: ZoneInfo) -> str:
    """``Fri 10/2/26 3:30 PM``, the grid's and the feed's date shape."""
    local = when.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{local:%a} {local.month}/{local.day}/{local:%y} {hour}:{local:%M} {local:%p}"


def _ago(when: datetime, now: datetime) -> str:
    hours = int((now - when).total_seconds() // 3600)
    if hours < 24:
        return f"({hours} hour{'' if hours == 1 else 's'} ago)"
    days = hours // 24
    return f"({days} day{'' if days == 1 else 's'} ago)"


def _cell(text: str = "") -> str:
    return f'<td class="k-table-td" role="gridcell">{escape(text)}</td>'


def _link_cell(href: str, text: str) -> str:
    return (
        '<td class="k-table-td" role="gridcell"><a class="tdx-link tdx-link--inline-block" '
        f'href="{escape(href)}">{escape(text)}</a></td>'
    )


def _ticket_link(ticket_id: int) -> str:
    return f"/TDWorkManagement/Apps/43/Tickets/TicketDet?TicketID={ticket_id}"


def _person_cell(name: str) -> str:
    return _link_cell(f"/TDWorkManagement/People/Details?Name={name.replace(' ', '+')}", name)


def _row_cells(ticket: DemoTicket, tz: ZoneInfo) -> list[str]:
    link = _ticket_link(ticket.ticket_id)
    values = {
        "ID": _link_cell(link, str(ticket.ticket_id)),
        "Created": _cell(_grid_time(ticket.created, tz)),
        "Modified": _cell(_grid_time(ticket.modified or ticket.created, tz)),
        "Title": _link_cell(link, ticket.title),
        "Requestor": _person_cell(ticket.requestor),
        "Affiliation": _cell("Student"),
        "Resp Group": _cell("IT Service Desk"),
        "Acct/Dept": _cell("Example College"),
        "Status": _cell(ticket.status),
        "Priority": _cell("Medium"),
        "Prim Resp": _person_cell(ticket.prim_resp) if ticket.prim_resp else _cell(),
        "Due": _cell(),
        "Service": _cell(ticket.service),
    }
    columns = UNCLAIMED_COLUMNS if ticket.widget == "unclaimed" else CLAIMED_COLUMNS
    return [values[column] for column in columns]


def _pager_button(icon: str, extra: str, disabled: bool) -> str:
    state = " k-disabled" if disabled else ""
    return (
        '<button class="k-pager-nav k-button k-button-flat k-button-flat-base k-icon-button'
        f'{extra} k-button-md{state}" role="button"><span class="k-button-icon k-icon '
        f'k-svg-icon k-svg-i-{icon}"><svg></svg></span></button>'
    )


def _pager(page: int, pages: int, total: int) -> str:
    first = page == 1
    last = page == pages
    numbers = "".join(
        '<button class="k-button k-button-md k-button-flat k-button-flat-primary'
        f'{" k-selected" if number == page else ""}" role="button">'
        f'<span class="k-button-text">{number}</span></button>'
        for number in range(1, pages + 1)
    )
    options = "".join(f"<option>{number}</option>" for number in range(1, pages + 1))
    start = (page - 1) * ROWS_PER_PAGE + 1 if total else 0
    end = min(page * ROWS_PER_PAGE, total)
    return (
        '<div class="k-pager k-grid-pager k-pager-md" role="application">'
        '<div class="k-pager-numbers-wrap">'
        + _pager_button("caret-alt-to-left", " k-pager-first", first)
        + _pager_button("caret-alt-left", "", first)
        + '<select class="k-dropdown k-picker k-picker-solid k-dropdown-list k-rounded-md '
        f'k-picker-md">{options}</select><div class="k-pager-numbers">{numbers}</div>'
        + _pager_button("caret-alt-right", "", last)
        + _pager_button("caret-alt-to-right", " k-pager-last", last)
        + f'</div><span class="k-pager-info k-label">{start} - {end} of {total} items</span>'
        "</div>"
    )


def _grid(
    columns: tuple[str, ...], rows: list[list[str]], page: int, pages: int, total: int
) -> str:
    header = "".join(
        '<th rowspan="1" class="k-table-th k-header" role="columnheader"><span '
        'class="k-cell-inner"><span class="k-link"><span class="k-column-title">'
        f"{escape(column)}</span></span></span></th>"
        for column in columns
    )
    body = "".join(
        f'<tr class="k-table-row k-master-row" role="row">{"".join(cells)}</tr>' for cells in rows
    )
    return (
        "<!-- tdx demo: an invented widget grid, generated at start. -->\n"
        '<div class="k-overflow-x-auto k-grid k-grid-md"><table class="k-grid-table k-table '
        f'k-table-md" role="grid"><colgroup>{"<col>" * len(columns)}</colgroup>'
        '<thead class="k-table-thead k-grid-header" role="rowgroup"><tr class="k-table-row" '
        f'role="row">{header}</tr></thead><tbody class="k-table-tbody" role="rowgroup">'
        f"{body}</tbody></table>{_pager(page, pages, total)}</div>"
    )


def _widget_pages(
    widget: int, columns: tuple[str, ...], tickets: list[DemoTicket], tz: ZoneInfo
) -> dict[str, bytes]:
    total = len(tickets)
    pages = max(1, -(-total // ROWS_PER_PAGE))
    result = {}
    for page in range(1, pages + 1):
        chunk = tickets[(page - 1) * ROWS_PER_PAGE : page * ROWS_PER_PAGE]
        rows = [_row_cells(ticket, tz) for ticket in chunk]
        html = _grid(columns, rows, page, pages, total)
        result[f"widgets/{widget}-{page}.html"] = html.encode("utf-8")
    return result


def _initials(name: str) -> str:
    return "".join(part[0] for part in name.split()[:2]).upper()


def _entry(entry: DemoEntry, number: int, now: datetime, tz: ZoneInfo) -> str:
    private = '\n      <span class="feed-private">Private</span>' if entry.private else ""
    text = "<br>".join(escape(line) for line in entry.text.split("\n"))
    return f"""<div class="feed-item" role="group">
  <div class="feed-profile-picture"><div class="profile-image clickable gray-bg initials" \
role="link">{_initials(entry.author)}</div></div>
  <div class="feed-body">
    <div class="feed-heading">
      <span class="feed-participant-name"><a href="/TDNext/Apps/People/Details?U={number}">\
{escape(entry.author)}</a></span>{private}
    </div>
    <div class="feed-breadcrumbs gray wrap-text feed-context"></div>
    <div class="wrap-text feed-comment-text">{text}</div>
    <div class="feed-context"><span class="feed-timestamp gray"><span>\
{_grid_time(entry.at, tz)}</span><span>{_ago(entry.at, now)}</span></span></div>
  </div>
</div>
"""


def _description(ticket: DemoTicket, form: DemoForm) -> bytes:
    """The ticket description as the form's notification writes it: an invented Recipient
    Data block (a documentation address, TEST-NET-2), then the Response Summary."""
    day = form.day
    start = f"{day.hour % 12 or 12}:{day:%M} {day:%p}"
    end_hour = day.hour + 2
    end = f"{end_hour % 12 or 12}:{day:%M} {'PM' if end_hour >= 12 else 'AM'}"
    fields = (
        ("Are you:", "A student organization"),
        ("Organization Name:", "Example Student Society"),
        ("Your Name:", ticket.requestor),
        ("Event Type:", "Workshop"),
        ("Event Title:", "Member showcase"),
        ("Event Description:", "An invented event for the demo."),
        ("On-Site Contact:", ticket.requestor),
        ("Is this a recurring event:", "No"),
        ("What is your proposed event date(s):", f"{day.month}/{day.day}/{day.year}"),
        ("What is your event proposed start/end times:", f"{start} - {end}"),
        ("Please confirm that you have reviewed the event date(s) ...", "Yes"),
        ("I am looking:", form.looking),
        ("What type of space are you looking for:", form.space),
        ("Who is your anticipated audience ...", "Students"),
        ("What is your estimated event headcount?", "30"),
        ("Will this event include minors? ...", "No"),
        ("Please select all that apply:", "\n".join(form.services)),
        ("Please describe your A/V needs in detail ...", "None beyond the room's own."),
        ("By submitting this request ...", "I agree"),
    )
    lines = [
        "<b>Recipient Data:</b>",
        "IP: 198.51.100.24",
        f"ResponseID: R_demo{ticket.ticket_id}",
        "Link to View Results: https://survey.example.edu/reports/demo",
        "<br>",
        "<b>Response Summary:</b>",
    ]
    for label, answer in fields:
        lines.append(f"<b>{escape(label)}</b>")
        lines.extend(escape(line) for line in answer.split("\n"))
    head = f"<!-- tdx demo: the invented description of ticket {ticket.ticket_id}. -->\n"
    return (head + "".join(f"<div>{line}</div>\n" for line in lines)).encode("utf-8")


def _feed(ticket: DemoTicket, now: datetime, tz: ZoneInfo) -> bytes:
    """The feed, newest first as TDX shows it; an empty search for a ticket with none."""
    people: dict[str, int] = {}
    entries = [
        _entry(entry, people.setdefault(entry.author, len(people) + 1), now, tz)
        for entry in ticket.entries
    ]
    head = f"<!-- tdx demo: the invented feed of ticket {ticket.ticket_id}. -->\n"
    return (head + "".join(reversed(entries))).encode("utf-8")


def demo_pages(tickets: list[DemoTicket], now: datetime, tz: ZoneInfo) -> dict[str, bytes]:
    """Everything the synthetic app serves for ``tickets``, by relative name."""
    unclaimed = [ticket for ticket in tickets if ticket.widget == "unclaimed"]
    claimed = [ticket for ticket in tickets if ticket.widget == "claimed"]
    pages = {
        **_widget_pages(UNCLAIMED_WIDGET, UNCLAIMED_COLUMNS, unclaimed, tz),
        **_widget_pages(CLAIMED_WIDGET, CLAIMED_COLUMNS, claimed, tz),
        # The dashboard's other two widgets load too; the audit never reads them.
        **_widget_pages(CLOSED_WIDGET, ("ID", "Title", "Closed"), [], tz),
        **_widget_pages(TASKS_WIDGET, ("Title", "Due"), [], tz),
    }
    for ticket in unclaimed:
        pages[f"feeds/{ticket.ticket_id}.html"] = _feed(ticket, now, tz)
        if ticket.form is not None:
            pages[f"descriptions/{ticket.ticket_id}.html"] = _description(ticket, ticket.form)
    return pages
