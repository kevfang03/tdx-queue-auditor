"""read_feeds against the synthetic ticket pages on a local server.

Headless Chromium with the read-only guard, as ``tdx audit`` runs it. No real site is contacted.
"""

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common.config import TdxRulesSettings, TdxSettings
from ops_suite.tdx import tickets
from ops_suite.tdx.browser import TdxBrowser
from ops_suite.tdx.dashboard import DashboardStructureError, NotSignedInError
from ops_suite.tdx.demo.app import FEED_SEARCH_PATH, WorkMgmtServer
from ops_suite.tdx.event_form import EventFormOptions
from ops_suite.tdx.guard import BUILT_IN_ALLOWED
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import RuleSettings, plan_feeds
from ops_suite.tdx.tickets import FeedRead, read_feeds, ticket_addresses

pytestmark = pytest.mark.browser

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
FEED_POST = "POST /TDNext/Apps/Feed/Search"
WIDGET_POST = "POST /TDWorkManagement/WorkManagement/Widgets/#"
SOCKET = "WS /TDSignalR/signalr/connect"
# What each synthetic ticket page asks of SignalR; all refused, counted as live updates.
LIVE_UPDATES = {"GET /TDSignalR/signalr/hubs", "GET /TDSignalR/signalr/negotiate", SOCKET}
STRAY_POST = "POST /TDNext/Apps/43/Tickets/Update"
MIXED, OLDEST_FIRST, EMPTY, NEVER_LOADS = 5501001, 5501002, 5501003, 5501004
HIDDEN_MARKERS = 5501005


def _row(ticket_id: int, detail_url: str | None) -> TicketRow:
    return TicketRow(
        widget="claimed",
        ticket_id=ticket_id,
        title="Projector in Room 204 shows no picture",
        requestor="Alex Example",
        resp_group="IT Service Desk",
        created=None,
        modified=datetime(2026, 10, 1, 9, 0, tzinfo=TZ),
        prim_resp="Pat Helpdesk",
        status="In Process",
        detail_url=detail_url,
    )


def _settings(server: WorkMgmtServer, **overrides: object) -> TdxSettings:
    values: dict[str, object] = {
        "base_url": server.base_url,
        "page_timeout_seconds": 10,
        "feed_delay_seconds": 0,
    }
    values.update(overrides)
    return TdxSettings(**values)


def _read(
    tmp_path: Path, settings: TdxSettings, rows: list[TicketRow], ids: list[int] | None = None
) -> FeedRead:
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )
    rows_by_id = {row.ticket_id: row for row in rows}
    with browser:
        return read_feeds(browser, settings, rows_by_id, ids or list(rows_by_id), NOW)


def test_reads_several_tickets_behind_the_guard(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    server = workmgmt_server
    rows = [
        # The grid's link: the shell, with the dashboard loaded in a background frame.
        _row(MIXED, server.ticket_shell(MIXED)),
        # The TDNext ticket page on its own.
        _row(OLDEST_FIRST, server.ticket_page(OLDEST_FIRST)),
        _row(EMPTY, server.ticket_page(EMPTY)),
    ]

    read = _read(tmp_path, _settings(server), rows)

    assert set(read.feeds) == {MIXED, OLDEST_FIRST, EMPTY}
    assert (read.failed, read.deferred) == ((), ())
    mixed = read.feeds[MIXED]
    assert [entry.kind for entry in mixed.entries] == [
        "reply", "private", "reply", "reply", "system", "reply", "reply", "private",
    ]  # fmt: skip
    assert mixed.entries[0].author == "Alex Example"
    assert mixed.unreadable == 2
    # The newest reply's comment text is read in the real browser too (payload 06b).
    assert mixed.latest_reply_text is not None
    assert mixed.latest_reply_text.startswith("Hi,\nThe HDMI input shows no picture either.")
    assert read.feeds[EMPTY].latest_reply_text is None
    assert read.unreadable_shapes == ("undated: aaaaaaaaa aaaaaaa", "no author")
    assert [entry.author for entry in read.feeds[OLDEST_FIRST].entries] == [
        "Riley Testperson",
        "Sam Synthetic",
        "Pat Helpdesk",
    ]
    assert read.feeds[EMPTY].entries == ()

    # One feed search per ticket, allowed and counted; the background dashboard's widget
    # reads are the only other non-GET requests.
    assert read.allowed_non_get.count(FEED_POST) == 3
    assert set(read.allowed_non_get) == {FEED_POST, WIDGET_POST}
    # Every ticket page asked SignalR for live updates (two GETs and a WebSocket); all were
    # refused and counted apart from the blocked requests, and none reached the server. The
    # feeds loaded anyway.
    assert len(read.live_updates_blocked) == 9
    assert set(read.live_updates_blocked) == LIVE_UPDATES
    assert read.blocked == ()
    assert not any(path.startswith("/TDSignalR") for _, path in server.received)
    assert server.received.count(("POST", FEED_SEARCH_PATH)) == 3
    # Visibility was read in the browser for every feed: no fallback to presence.
    assert read.marker_stats["visibility_fallback"] == 0
    assert read.marker_stats["entries"] == 12  # 9 in the mixed feed (2 nested) + 3
    assert read.marker_stats["private_visible"] == 3


def test_a_stray_post_on_the_ticket_page_is_blocked(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    rows = [_row(OLDEST_FIRST, workmgmt_server.ticket_page(OLDEST_FIRST, "stray"))]

    read = _read(tmp_path, _settings(workmgmt_server), rows)

    assert set(read.feeds) == {OLDEST_FIRST}
    # The stray write is the one blocked request; SignalR's are counted on their own.
    assert read.blocked == (STRAY_POST,)
    assert set(read.live_updates_blocked) == LIVE_UPDATES
    assert not any("/Update" in path for _, path in workmgmt_server.received)


def test_a_feed_that_never_loads_counts_as_failed(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    server = workmgmt_server
    rows = [
        _row(NEVER_LOADS, server.ticket_page(NEVER_LOADS)),
        _row(EMPTY, server.ticket_page(EMPTY)),
    ]

    read = _read(tmp_path, _settings(server, page_timeout_seconds=5), rows)

    assert read.failed == (NEVER_LOADS,)
    assert set(read.feeds) == {EMPTY}


def test_three_failures_in_a_row_stop_the_run(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    server = workmgmt_server
    # A ticket without any address is counted as failed but does not open a page.
    rows = [_row(5501090 + n, server.ticket_page(5501090 + n)) for n in range(3)]
    rows.insert(1, _row(5501099, None))

    with pytest.raises(DashboardStructureError) as error:
        _read(tmp_path, _settings(server, page_timeout_seconds=5), rows)

    message = str(error.value)
    assert message == (
        "no activity feed was found on 3 tickets in a row (step: open each ticket and wait up "
        "to 5 s for its feed)"
    )
    assert error.value.blocked == ()


def test_max_feeds_per_run_defers_the_rest(workmgmt_server: WorkMgmtServer, tmp_path: Path) -> None:
    server = workmgmt_server
    rows = [_row(ticket_id, server.ticket_page(ticket_id)) for ticket_id in (EMPTY, OLDEST_FIRST)]
    rows.append(_row(MIXED, server.ticket_page(MIXED)))

    read = _read(
        tmp_path, _settings(server, max_feeds_per_run=2), rows, [EMPTY, OLDEST_FIRST, MIXED]
    )

    assert set(read.feeds) == {EMPTY, OLDEST_FIRST}
    assert read.deferred == (MIXED,)
    assert server.received.count(("POST", FEED_SEARCH_PATH)) == 2


def test_the_sign_in_redirect_raises_not_signed_in(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    rows = [_row(MIXED, workmgmt_server.ticket_page(MIXED, "signin"))]

    with pytest.raises(NotSignedInError):
        _read(tmp_path, _settings(workmgmt_server), rows)


def test_the_template_is_used_without_a_link(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    template = f"{workmgmt_server.origin()}/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID={{id}}"

    read = _read(
        tmp_path, _settings(workmgmt_server, ticket_url_template=template), [_row(MIXED, None)]
    )

    assert set(read.feeds) == {MIXED}
    assert read.failed == ()


def test_the_template_is_tried_when_the_link_has_no_feed(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    server = workmgmt_server
    template = f"{server.origin()}/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID={{id}}"
    # The link opens a page without a feed (here: a dashboard frame).
    rows = [_row(OLDEST_FIRST, server.frame("d=2"))]
    settings = _settings(server, ticket_url_template=template, page_timeout_seconds=5)

    read = _read(tmp_path, settings, rows)

    assert set(read.feeds) == {OLDEST_FIRST}


def test_addresses_stay_on_the_tdx_host() -> None:
    settings = TdxSettings(
        base_url="https://tdx.example.edu/TDWorkManagement/",
        ticket_url_template="https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID={id}",
    )

    assert ticket_addresses(_row(7, "https://elsewhere.example.edu/t/7"), 7, settings) == [
        "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet.aspx?TicketID=7"
    ]
    assert ticket_addresses(None, 7, settings.model_copy(update={"ticket_url_template": ""})) == []


def test_the_page_sees_its_websocket_closed(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    settings = _settings(workmgmt_server)
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )
    with browser:
        page = browser.open_page(workmgmt_server.ticket_page(EMPTY))
        closed = False
        for _ in range(40):
            closed = any('data-socket="closed"' in f.html for f in browser.page_frames(page))
            if closed:
                break
            page.wait_for_timeout(100)
        sockets = [record.describe() for record in browser.live_updates_blocked()]
        blocked = browser.blocked_requests()
        browser.close_page(page)

    assert closed
    assert SOCKET in sockets
    assert blocked == []


def test_hidden_private_markers_are_ignored_in_the_browser(
    workmgmt_server: WorkMgmtServer, tmp_path: Path
) -> None:
    # Every entry carries a private marker; a stylesheet class or the hidden attribute hides
    # all but one. The browser reports what it renders, and only that one counts.
    rows = [_row(HIDDEN_MARKERS, workmgmt_server.ticket_shell(HIDDEN_MARKERS))]

    read = _read(tmp_path, _settings(workmgmt_server), rows)

    feed = read.feeds[HIDDEN_MARKERS]
    assert [(entry.kind, entry.author) for entry in feed.entries] == [
        ("system", "Pat Helpdesk"),
        ("reply", "Alex Example"),
        ("private", "Riley Testperson"),
        ("private", "Riley Testperson"),
        ("reply", "Alex Example"),
        ("reply", "Pat Helpdesk"),
    ]
    assert dict(read.marker_stats) == {
        "entries": 6,
        "nested": 3,
        "private_present": 5,
        "private_visible": 1,
        "inherited_private": 1,
        "visibility_fallback": 0,
        "merge_notes": 0,
        "reassignments": 0,
        "signature_desk_replies": 0,
    }


def test_progress_is_reported_per_ticket(workmgmt_server: WorkMgmtServer, tmp_path: Path) -> None:
    server = workmgmt_server
    rows = [_row(EMPTY, server.ticket_page(EMPTY)), _row(5501099, None)]
    calls: list[tuple[int, int]] = []
    settings = _settings(server)
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )
    with browser:
        read_feeds(
            browser,
            settings,
            {row.ticket_id: row for row in rows},
            [EMPTY, 5501099],
            NOW,
            lambda done, total: calls.append((done, total)),
        )

    assert calls == [(1, 2), (2, 2)]


# --- event request forms (payload 10) -----------------------------------------------------

FORM, FORM_WITHOUT_DESCRIPTION = 5501012, 5501013
# Invented values in the form fixture that must never leave the parser.
FORM_SECRETS = (
    "203.0.113.7",
    "R_3FakeResponse0001",
    "fake-report-0001",
    "Sam Synthetic",
    "sam.synthetic@example.edu",
    "Jo Fixture",
    "jo.fixture@example.edu",
    "19990001",
    "Fake Robotics Society",
)


def _read_forms(
    tmp_path: Path, server: WorkMgmtServer, rows: list[TicketRow], form_ids: set[int]
) -> FeedRead:
    settings = _settings(server)
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )
    with browser:
        return read_feeds(
            browser,
            settings,
            {row.ticket_id: row for row in rows},
            [row.ticket_id for row in rows],
            NOW,
            form_ticket_ids=form_ids,
            form_options=EventFormOptions(),
        )


def test_the_form_is_read_from_the_description_behind_the_guard(
    workmgmt_server: WorkMgmtServer,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    # Without the HTML fallback, so the form must come from the browser's innerText.
    monkeypatch.setattr(tickets, "description_text", lambda html: None)
    server = workmgmt_server
    rows = [
        # The grid's link: the shell, the ticket two frames deep.
        _row(FORM, server.ticket_shell(FORM)),
        _row(FORM_WITHOUT_DESCRIPTION, server.ticket_page(FORM_WITHOUT_DESCRIPTION)),
        _row(EMPTY, server.ticket_page(EMPTY)),  # not a form ticket: its description is not read
    ]

    read = _read_forms(tmp_path, server, rows, {FORM, FORM_WITHOUT_DESCRIPTION})

    assert set(read.feeds) == {FORM, FORM_WITHOUT_DESCRIPTION, EMPTY}
    assert set(read.forms) == {FORM}  # no description: no form, so the rules count it unreadable
    form = read.forms[FORM]
    assert form.dates == (datetime(2026, 10, 20, 14, 0, tzinfo=TZ),)
    assert (form.wants_av, form.wants_room, form.marketing_only) == (True, True, False)
    assert (form.recurring, form.unknown_options) == (False, 0)
    assert not any(record.msg == "description not read by script" for record in caplog.records)
    # The description is part of the page: no request beyond the usual ones, none blocked.
    assert read.blocked == ()
    assert set(read.allowed_non_get) == {FEED_POST, WIDGET_POST}
    # Nothing from the description reaches the result, a repr or a log record.
    shown = (
        repr(read)
        + repr(read.forms)
        + "".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    )
    for secret in FORM_SECRETS:
        assert secret not in shown
    assert [
        record.__dict__.get("readable") for record in caplog.records if record.msg == "form read"
    ] == [
        True,
        False,
    ]


def test_the_row_s_requester_and_the_desk_signatures_reach_the_feed_reader(
    workmgmt_server: WorkMgmtServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passed: list[dict[str, object]] = []
    real = tickets.read_feed_frames

    def spy(*args, **kwargs):
        passed.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(tickets, "read_feed_frames", spy)
    rules = TdxRulesSettings(desk_signatures=["Example Desk Team"])
    settings = _settings(workmgmt_server, rules=rules)

    read = _read(tmp_path, settings, [_row(EMPTY, workmgmt_server.ticket_page(EMPTY))])

    assert EMPTY in read.feeds
    assert [(kw["desk_signatures"], kw["requester"]) for kw in passed] == [
        (["Example Desk Team"], "Alex Example")
    ]
    assert read.marker_stats["signature_desk_replies"] == 0


# --- renamed events (payload 22) ----------------------------------------------------------


def test_renamed_events_get_their_form_read_within_max_feeds_per_run(
    workmgmt_server: WorkMgmtServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The rules plan renamed events within 14 days; the reader opens them under the same
    guard and cap, reads their description, and keeps only the parsed form."""
    caplog.set_level(logging.DEBUG)
    server = workmgmt_server

    def renamed(ticket_id: int, title: str) -> TicketRow:
        row = _row(ticket_id, server.ticket_page(ticket_id))
        return replace(row, widget="unclaimed", title=title, modified=None, prim_resp=None)

    rows = [
        renamed(FORM, "EVENT [10/18 2pm Room 101] Zoom recording"),
        renamed(EMPTY, "EVENT [10/9 6PM-7PM] Zoom recording"),
        renamed(MIXED, "EVENT [10/12 2pm Room 101] Zoom recording"),
        renamed(OLDEST_FIRST, "EVENT [10/30 2pm Room 101] Zoom recording"),  # too far out
    ]
    plan = plan_feeds(rows, NOW, RuleSettings(), TZ)
    settings = _settings(server, max_feeds_per_run=2)
    browser = TdxBrowser(
        tmp_path / "profile", settings.base_url, headless=True, allowed_requests=BUILT_IN_ALLOWED
    )
    with browser:
        read = read_feeds(
            browser,
            settings,
            {row.ticket_id: row for row in rows},
            plan.feed_ticket_ids,
            NOW,
            form_ticket_ids=set(plan.form_ticket_ids),
            form_options=EventFormOptions(),
        )

    assert set(plan.feed_ticket_ids) == set(plan.form_ticket_ids) == {FORM, EMPTY, MIXED}
    assert len(read.feeds) == 2 and len(read.deferred) == 1
    assert server.received.count(("POST", FEED_SEARCH_PATH)) == 2
    assert set(read.forms) <= {FORM} and read.blocked == ()
    assert OLDEST_FIRST not in read.feeds
    shown = (
        repr(read)
        + repr(read.forms)
        + "".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    )
    for secret in (*FORM_SECRETS, "Zoom recording", "Room 101"):
        assert secret not in shown
