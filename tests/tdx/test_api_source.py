"""The API reader: report rows, feeds and event forms mapped to the browser path's types, and
the rules giving the same result over both sources. Synthetic answers in the documented
shapes (``api_fakes``); no network."""

from datetime import UTC, datetime, timedelta

import pytest
from api_fakes import (
    API,
    APP,
    NOW,
    ORIGIN,
    TZ,
    Answer,
    FakeApi,
    api_client,
    api_tdx,
    demo_browser_side,
    demo_routes,
    entry,
    iso,
    reply,
    report,
)

from ops_suite.common.config import ConfigProblem
from ops_suite.tdx import api_source
from ops_suite.tdx.api_client import ApiError
from ops_suite.tdx.dashboard import DashboardStructureError
from ops_suite.tdx.event_form import EventFormOptions
from ops_suite.tdx.rows import REQUIRED_COLUMNS
from ops_suite.tdx.rules import RuleSettings, evaluate

UNCLAIMED = ("ID", "Created", "Title", "Requestor", "Resp Group", "Service")
CLAIMED = ("Resp Group", "Modified", "ID", "Title", "Requestor", "Status", "Prim Resp")
EARLIER = NOW - timedelta(days=3)


def _unclaimed_row(ticket_id: int, **values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ID": ticket_id,
        "Created": iso(EARLIER),
        "Title": "Printer offline",
        "Requestor": "Alex Example",
        "Resp Group": "IT Service Desk",
        "Service": "Printing",
    }
    row.update(values)
    return row


# --- rows -------------------------------------------------------------------------------


def test_report_rows_map_to_ticket_rows_by_header() -> None:
    answer = report(UNCLAIMED, [_unclaimed_row(5501001), _unclaimed_row(5501002, Title="VPN")])

    read = api_source.map_report(answer, "unclaimed", TZ, lambda i: f"link {i}")

    first = read.rows[0]
    assert (first.widget, first.ticket_id, first.title, first.requestor) == (
        "unclaimed",
        5501001,
        "Printer offline",
        "Alex Example",
    )
    assert first.resp_group == "IT Service Desk"
    assert first.created == EARLIER.astimezone(TZ) and first.created.tzinfo == TZ
    assert (first.modified, first.prim_resp, first.status) == (None, None, None)
    assert first.detail_url == "link 5501001"
    assert (read.pages, read.unreadable_dates, read.hit_page_limit) == (1, (), False)


def test_claimed_report_rows_carry_modified_status_and_prim_resp() -> None:
    row = {
        "ID": "4409100",  # ids may come as text
        "Modified": "2026-10-19T13:30:00",  # no offset: UTC, as the API's dates are
        "Title": "Laptop repair",
        "Requestor": "Example, Alex",
        "Resp Group": "IT Service Desk",
        "Status": "In Process",
        "Prim Resp": "Robin Fulltime",
    }

    (read_row,) = api_source.map_report(report(CLAIMED, [row]), "claimed", TZ).rows

    assert read_row.ticket_id == 4409100
    assert read_row.modified == datetime(2026, 10, 19, 13, 30, tzinfo=UTC).astimezone(TZ)
    assert (read_row.status, read_row.prim_resp, read_row.created) == (
        "In Process",
        "Robin Fulltime",
        None,
    )


def test_headers_match_like_the_dashboard_and_extra_columns_are_ignored() -> None:
    answer = report(UNCLAIMED + ("Priority", "Due"), [_unclaimed_row(1, Priority="Low", Due=None)])
    for column in answer["DisplayedColumns"]:
        column["HeaderText"] = f"  {column['HeaderText'].upper()}  Sort by {column['HeaderText']}"

    assert [row.ticket_id for row in api_source.map_report(answer, "unclaimed", TZ).rows] == [1]


@pytest.mark.parametrize("kind", ["unclaimed", "claimed"])
def test_a_missing_column_is_named(kind: str) -> None:
    headers = UNCLAIMED if kind == "unclaimed" else CLAIMED
    answer = report(tuple(h for h in headers if h != "Requestor"), [])

    with pytest.raises(DashboardStructureError) as raised:
        api_source.map_report(answer, kind, TZ)  # type: ignore[arg-type]

    message = str(raised.value)
    assert f"{kind} report: required column(s) missing: Requestor" in message
    assert "Add them to the report in TDX" in message
    assert REQUIRED_COLUMNS[kind] - {"Requestor"} <= set(message.replace(",", " ,").split(" ")) | {
        "Resp Group",
        "Prim Resp",
    }


def test_rows_without_an_id_and_duplicates_are_counted() -> None:
    rows = [_unclaimed_row(7), _unclaimed_row(7), _unclaimed_row(8), _unclaimed_row(None)]  # type: ignore[arg-type]

    read = api_source.map_report(report(UNCLAIMED, rows), "unclaimed", TZ)

    assert [row.ticket_id for row in read.rows] == [7, 8]
    assert (read.duplicates, read.rows_without_id) == (1, 1)


def test_mostly_unreadable_ids_stop_the_run() -> None:
    rows = [_unclaimed_row(None), _unclaimed_row("x"), _unclaimed_row(3)]  # type: ignore[arg-type]

    with pytest.raises(DashboardStructureError, match="2 of 3 rows have no readable ticket id"):
        api_source.map_report(report(UNCLAIMED, rows), "unclaimed", TZ)


def test_an_unreadable_date_leaves_only_its_shape() -> None:
    answer = report(UNCLAIMED, [_unclaimed_row(1, Created="Oct 2nd, 9ish")])

    read = api_source.map_report(answer, "unclaimed", TZ)

    assert read.rows[0].created is None
    assert read.unreadable_dates == ("aaa 9aa, 9aaa",)


def test_fractional_seconds_and_offsets_are_read() -> None:
    seven_digits = api_source.parse_api_datetime("2026-10-21T16:00:00.1234567Z", TZ)
    assert seven_digits == (NOW + timedelta(microseconds=123456)).astimezone(TZ)
    assert api_source.parse_api_datetime("2026-10-21T12:00:00-04:00", TZ) == NOW.astimezone(TZ)
    assert api_source.parse_api_datetime(None, TZ) is None


def test_a_report_at_its_row_limit_is_flagged() -> None:
    answer = report(UNCLAIMED, [_unclaimed_row(1), _unclaimed_row(2)], MaxResults=2)

    assert api_source.map_report(answer, "unclaimed", TZ).hit_page_limit


def test_html_columns_become_text() -> None:
    answer = report(UNCLAIMED, [_unclaimed_row(1, Title="<b>Printer</b>&nbsp;offline")])
    for column in answer["DisplayedColumns"]:
        if column["HeaderText"] == "Title":
            column["DataType"] = 11

    assert api_source.map_report(answer, "unclaimed", TZ).rows[0].title == "Printer offline"


def test_a_report_read_without_its_data_is_an_error() -> None:
    answer = report(UNCLAIMED, [])
    answer["DataRows"] = None

    with pytest.raises(DashboardStructureError, match="it must be read with its data"):
        api_source.map_report(answer, "unclaimed", TZ)


def test_report_ids_must_be_set() -> None:
    with pytest.raises(ConfigProblem, match="tdx.api.claimed_report_id not set"):
        api_source.report_ids(api_tdx(claimed_report_id=0))


def test_reports_are_read_with_their_data_and_linked_to_the_ticket_page() -> None:
    api = FakeApi(
        {
            f"{API}/reports/11": report(UNCLAIMED, [_unclaimed_row(5501001)]),
            f"{API}/reports/12": report(CLAIMED, []),
        }
    )

    widgets, app_id, source = api_source.read_reports(api_tdx(), api_client(api))

    assert (app_id, source) == (APP, "setting")
    assert widgets["unclaimed"].rows[0].detail_url == (
        f"{ORIGIN}/TDNext/Apps/{APP}/Tickets/TicketDet.aspx?TicketID=5501001"
    )
    assert api.paths() == [f"{API}/reports/11", f"{API}/reports/12"]
    assert all(url.endswith("?withData=true") for _, url, _ in api.calls)


def test_the_app_id_comes_from_the_template_or_the_first_ticket() -> None:
    template = f"{ORIGIN}/TDNext/Apps/77/Tickets/TicketDet?TicketID={{id}}"
    api = FakeApi({f"{API}/tickets/5501001": {"ID": 5501001, "AppID": 55}})
    client = api_client(api)

    from_template = api_source.resolve_app_id(
        api_tdx(app_id=0).model_copy(update={"ticket_url_template": template}), client, 5501001
    )
    from_ticket = api_source.resolve_app_id(api_tdx(app_id=0), client, 5501001)

    assert from_template == (77, "ticket_url_template")
    assert from_ticket == (55, "first ticket")
    with pytest.raises(DashboardStructureError, match="set tdx.api.app_id"):
        api_source.resolve_app_id(api_tdx(app_id=0), client, None)


def test_ticket_links_prefer_the_template() -> None:
    tdx = api_tdx().model_copy(update={"ticket_url_template": f"{ORIGIN}/T?id={{id}}"})

    assert api_source.ticket_link(tdx, APP)(9) == f"{ORIGIN}/T?id=9"
    assert api_source.ticket_link(api_tdx(), 0)(9) is None


def test_the_report_list_keeps_ids_names_and_sources_only() -> None:
    answer = [
        {"ID": 2, "Name": "Zeta queue", "ReportSourceName": "Tickets", "CreatedFullName": "X Y"},
        {"ID": 1, "Name": "alpha  queue", "ReportSourceName": "Tickets"},
        {"Name": "no id"},
    ]

    found = api_source.report_list(answer)

    assert [(r.report_id, r.name, r.source) for r in found] == [
        (1, "alpha queue", "Tickets"),
        (2, "Zeta queue", "Tickets"),
    ]


# --- feeds ------------------------------------------------------------------------------

REQUESTER = "Alex Example"
DESK = "Pat Helpdesk"
CHANGE = "Changed Status from <b>New</b> to <b>In Process</b>."


def _at(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def test_entries_map_to_private_system_and_reply_newest_first() -> None:
    entries = [
        entry(1, DESK, _at(30), "Looking into it."),
        entry(2, DESK, _at(10), "Internal: waiting on a part.", private=True),
        entry(3, DESK, _at(20), CHANGE, update_type=2),
        entry(4, REQUESTER, _at(5), "Any news?"),
        entry(5, REQUESTER, _at(50), "My printer is offline.", update_type=4),
        entry(6, DESK, _at(40), "", update_type=5),
    ]

    details = api_source.map_feed(5501001, entries, {}, TZ, requester=REQUESTER)

    kinds = [(e.author, e.kind) for e in details.feed.entries]
    assert kinds == [
        (REQUESTER, "reply"),
        (DESK, "private"),
        (DESK, "system"),
        (DESK, "reply"),
        (DESK, "system"),
        (REQUESTER, "system"),  # the ticket's creation is not a comment
    ]
    assert details.feed.entries[0].created == _at(5).astimezone(TZ)
    assert details.feed.latest_reply_text == "Any news?"
    assert details.marker_stats["private_present"] == details.marker_stats["private_visible"] == 1


def test_a_status_change_with_a_comment_is_a_reply() -> None:
    entries = [entry(1, DESK, _at(3), f"{CHANGE}<br>The part arrived, try again please.")]

    (feed_entry,) = api_source.map_feed(1, entries, {}, TZ).feed.entries

    assert feed_entry.kind == "reply"


def test_plain_text_bodies_are_read_as_text() -> None:
    entries = [entry(1, REQUESTER, _at(3), "Line one <b>\nLine two", rich=False)]

    details = api_source.map_feed(1, entries, {}, TZ)

    assert details.feed.latest_reply_text == "Line one <b>\nLine two"


def test_merges_and_reassignments_are_system_and_mark_desk() -> None:
    entries = [
        entry(1, "Riley Testperson", _at(2), "Reassigned this incident from A to B.", private=True),
        entry(2, DESK, _at(4), "Merged incident 5509999 (Room 204, ...): <div>Hi</div>"),
        entry(3, "Sam Merger", _at(5), "", update_type=6),
        entry(4, REQUESTER, _at(6), "<blockquote>Merged incident 1</blockquote>Why merged?"),
    ]

    details = api_source.map_feed(1, entries, {}, TZ)

    assert [e.kind for e in details.feed.entries] == ["system", "system", "system", "reply"]
    assert details.feed.desk_authors == ("Riley Testperson", DESK, "Sam Merger")
    assert (details.marker_stats["reassignments"], details.marker_stats["merge_notes"]) == (1, 2)


def test_replies_are_read_and_inherit_private_from_their_own_author_only() -> None:
    entries = [
        entry(10, DESK, _at(30), "Private note.", private=True, replies=2),
        entry(20, DESK, _at(25), "We replaced the toner.", replies=1),
    ]
    replies = {
        10: [reply(11, DESK, _at(29), "Follow-up note."), reply(12, REQUESTER, _at(28), "Hi?")],
        20: [reply(21, REQUESTER, _at(2), "Thanks, it <b>works</b> now.")],
    }

    details = api_source.map_feed(1, entries, replies, TZ, requester=REQUESTER)

    assert [(e.author, e.kind) for e in details.feed.entries] == [
        (REQUESTER, "reply"),
        (DESK, "reply"),
        (REQUESTER, "reply"),
        (DESK, "private"),
        (DESK, "private"),
    ]
    stats = details.marker_stats
    assert (stats["entries"], stats["nested"], stats["inherited_private"]) == (5, 3, 1)
    assert details.feed.latest_reply_text == "Thanks, it works now."


def test_replies_that_could_not_be_read_count_as_unreadable() -> None:
    entries = [entry(10, DESK, _at(30), "Hello", replies=1)]

    details = api_source.map_feed(1, entries, {}, TZ)

    assert details.feed.unreadable == 1
    assert details.unreadable_shapes == ("replies not read",)


def test_entries_without_an_author_or_a_date_are_counted_like_the_feed_page() -> None:
    entries = [entry(1, "", _at(1), "x"), entry(2, DESK, _at(2), "y")]
    entries[1]["CreatedDate"] = "yesterday"

    details = api_source.map_feed(1, entries, {}, TZ)

    assert details.unreadable_shapes == ("no author", "undated: aaaaaaaaa")
    assert [e.created for e in details.feed.entries] == [None]


def test_a_desk_signature_marks_the_author_but_never_the_requester() -> None:
    signed = "Fixed.<br>Thanks,<br>Jamie<br>SCIS Commons Support"
    entries = [
        entry(1, "Jamie Student", _at(5), signed),
        entry(2, REQUESTER, _at(4), f"Great!<br>{signed}"),
        entry(3, "Casey Other", _at(3), "<blockquote>SCIS Commons Support</blockquote>Hi"),
    ]
    signatures = ("SCIS Commons Support",)

    details = api_source.map_feed(
        1, entries, {}, TZ, desk_signatures=signatures, requester=REQUESTER
    )
    without = api_source.map_feed(1, entries, {}, TZ, desk_signatures=signatures)

    assert details.feed.desk_authors == ("Jamie Student",)
    assert details.marker_stats["signature_desk_replies"] == 1
    assert without.feed.desk_authors == ()  # no requester known: the rule is off


def test_no_comment_text_is_kept_beyond_the_newest_reply() -> None:
    entries = [
        entry(1, DESK, _at(5), "Older desk text"),
        entry(2, REQUESTER, _at(1), "Newest requester text"),
    ]

    feed = api_source.map_feed(1, entries, {}, TZ).feed

    assert feed.latest_reply_text == "Newest requester text"
    assert "Older desk text" not in repr(feed) + repr(feed.entries)
    assert "Newest requester text" not in repr(feed)
    assert not any(hasattr(e, "text") or hasattr(e, "body") for e in feed.entries)


def test_a_feed_answer_that_is_not_a_list_is_an_error() -> None:
    with pytest.raises(DashboardStructureError):
        api_source.map_feed(1, {"Message": "no"}, {}, TZ)


def test_read_feed_asks_for_replies_only_where_there_are_some() -> None:
    api = FakeApi(
        {
            f"{API}/{APP}/tickets/1/feed": [
                entry(10, DESK, _at(5), "Hi", replies=1),
                entry(20, DESK, _at(6), "Hello"),
            ],
            f"{API}/feed/10": {"ID": 10, "Replies": [reply(11, REQUESTER, _at(1), "Thanks")]},
        }
    )

    details = api_source.read_feed(api_client(api), APP, 1, TZ)

    assert api.paths() == [f"{API}/{APP}/tickets/1/feed", f"{API}/feed/10"]
    assert len(details.feed.entries) == 3


# --- event forms ------------------------------------------------------------------------

FORM = (
    "<div><b>Recipient Data:</b></div><div>IP: 203.0.113.7</div>"
    "<div>What is your proposed event date(s):</div><div>1/1/2020</div>"
    "<div><b>Response Summary:</b></div>"
    "<div><b>Is this a recurring event:</b></div><div>No</div>"
    "<div><b>What is your proposed event date(s):</b></div><div>11/5/2026</div>"
    "<div><b>What is your event proposed start/end times:</b></div><div>2:00 PM - 4:00 PM</div>"
    "<div><b>I am looking:</b></div><div>To host an event in SCIS space</div>"
    "<div><b>What type of space are you looking for:</b></div><div>Classroom Space</div>"
    "<div><b>Please select all that apply:</b></div><div>A/V &amp; Technical Support</div>"
)


def test_the_description_reaches_the_form_parser_after_the_recipient_data_cut() -> None:
    api = FakeApi({f"{API}/{APP}/tickets/7": {"ID": 7, "Description": FORM, "IsRichHtml": True}})

    form = api_source.read_form(api_client(api), APP, 7, TZ, NOW, EventFormOptions())

    assert form is not None
    assert [d.date() for d in form.dates] == [datetime(2026, 11, 5).date()]  # not 1/1/2020
    assert (form.wants_av, form.wants_room, form.recurring) == (True, True, False)


@pytest.mark.parametrize("description", ["", None, "<div>Recipient Data:</div><div>IP: x</div>"])
def test_a_ticket_without_a_form_gives_none(description: str | None) -> None:
    api = FakeApi({f"{API}/{APP}/tickets/7": {"ID": 7, "Description": description}})

    assert api_source.read_form(api_client(api), APP, 7, TZ, NOW, EventFormOptions()) is None


# --- many feeds ---------------------------------------------------------------------------


def test_read_feeds_counts_failures_and_defers_beyond_the_limit() -> None:
    api = FakeApi(
        {
            f"{API}/{APP}/tickets/1/feed": [entry(1, DESK, _at(1), "Hi")],
            f"{API}/{APP}/tickets/2/feed": Answer(404),
            f"{API}/{APP}/tickets/3/feed": [],
        }
    )
    tdx = api_tdx().model_copy(update={"max_feeds_per_run": 3})

    read = api_source.read_feeds(api_client(api), tdx, APP, {}, [1, 2, 3, 4, 5], NOW)

    assert sorted(read.feeds) == [1, 3]
    assert (read.failed, read.deferred) == ((2,), (4, 5))
    assert (read.blocked, read.allowed_non_get, read.live_updates_blocked) == ((), (), ())


def test_three_failed_feeds_in_a_row_stop_the_run() -> None:
    api = FakeApi()  # every feed: 404

    with pytest.raises(ApiError) as raised:
        api_source.read_feeds(api_client(api), api_tdx(), APP, {}, [1, 2, 3, 4], NOW)

    assert raised.value.reason == "not_found"
    assert len(api.calls) == 3


@pytest.mark.parametrize("status", [401, 429])
def test_a_refused_token_or_the_rate_limit_stops_the_run_at_once(status: int) -> None:
    api = FakeApi({f"{API}/{APP}/tickets/1/feed": Answer(status)})

    with pytest.raises(ApiError):
        api_source.read_feeds(api_client(api), api_tdx(), APP, {}, [1, 2], NOW)


# --- parity with the browser pages --------------------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 17, 0, 5, tzinfo=TZ),
        datetime(2026, 10, 17, 12, 0, tzinfo=TZ),
        datetime(2026, 11, 1, 1, 30, tzinfo=TZ),  # the night the clocks go back
        datetime(2027, 1, 1, 9, 0, tzinfo=TZ),
    ],
)
def test_the_rules_give_the_same_result_over_both_sources(now: datetime) -> None:
    tickets, rows, feeds, forms = demo_browser_side(now, TZ)
    api = FakeApi(demo_routes(tickets, now, TZ))
    tdx = api_tdx().model_copy(update={"max_feeds_per_run": 200})

    queue = api_source.read_queue(tdx, api_client(api), now, RuleSettings(), EventFormOptions())

    by_api = evaluate(
        list(queue.rows), queue.feeds.feeds, now, RuleSettings(), TZ, forms=queue.feeds.forms
    )
    by_browser = evaluate(
        rows, {i: feeds[i] for i in queue.feeds.feeds}, now, RuleSettings(), TZ, forms=forms
    )
    assert len(queue.rows) == len(rows) == 27
    assert [
        (r.ticket_id, r.title, r.requestor, r.created, r.modified, r.prim_resp, r.status)
        for r in sorted(queue.rows, key=lambda r: r.ticket_id)
    ] == [
        (r.ticket_id, r.title, r.requestor, r.created, r.modified, r.prim_resp, r.status)
        for r in sorted(rows, key=lambda r: r.ticket_id)
    ]
    assert by_api == by_browser
    assert queue.feeds.forms == {i: f for i, f in forms.items() if i in queue.feeds.feeds}
    for ticket_id, feed in queue.feeds.feeds.items():
        assert feed.entries == feeds[ticket_id].entries
        assert feed.latest_reply_text == feeds[ticket_id].latest_reply_text
        assert feed.desk_authors == feeds[ticket_id].desk_authors
    assert queue.feeds.failed == () and queue.feeds.deferred == ()
    # Only the GETs on the allowlist, and only the feeds and forms the plan asks for.
    assert {method for method, _, _ in api.calls} == {"GET"}
    feed_reads = [p for p in api.paths() if p.endswith("/feed")]
    assert len(feed_reads) == len(queue.feeds.feeds)
