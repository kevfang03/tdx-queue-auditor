import json
import logging
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops_suite.common.config import TdxAlertSettings, TdxSettings
from ops_suite.tdx.alerts import (
    CARD_MAX_BYTES,
    NO_ROOM_WORDING,
    AlertPlan,
    AlertPolicy,
    AlertState,
    Audience,
    DueAlert,
    Reader,
    SummaryState,
    alertable,
    build_card,
    build_summary_card,
    card_links,
    card_size,
    clean_title,
    counts_phrase,
    delta_heading,
    delta_lead,
    escape_markdown,
    fallback_text,
    finding_wording,
    fit_card,
    fit_delta_card,
    fit_summary_card,
    fit_urgent_card,
    format_age,
    plan_alerts,
    since_text,
    summary_change,
    summary_heading,
    urgent_event,
)
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import Finding, RuleSettings, evaluate
from ops_suite.tdx.week import WeekEvent

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 16, 14, 5, tzinfo=TZ)
POLICY = AlertPolicy.from_settings(TdxAlertSettings())
REQUESTOR = "Jordan Sampleton"
EMAIL = "jordan.sampleton@example.edu"
PRIM_RESP = "Casey Deskworker"
TITLES = {
    101: "Laptop will not connect to campus Wi-Fi",
    102: "EVENT [10/20 3pm Room 101] Club",
    103: "Printer jam in the library basement",
    104: "EVENT [TBD] Robotics club meeting",
    105: "Password reset for the course portal",
}
SUMMARY = {"unclaimed": 26, "feeds_read": 7, "feeds_failed": 0}
DESK = Reader("desk")
MAILBOX = "events@example.edu"
COMMONS = Reader("desk", "the Commons")


def row(ticket_id: int, title: str | None = None, **fields: object) -> TicketRow:
    values: dict[str, object] = {
        "widget": "unclaimed",
        "ticket_id": ticket_id,
        "title": title if title is not None else TITLES.get(ticket_id, "Synthetic ticket title"),
        "requestor": REQUESTOR,
        "resp_group": "IT Service Desk",
        "created": NOW - timedelta(days=5),
        "modified": None,
        "prim_resp": PRIM_RESP,
        "status": None,
        "detail_url": f"https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID={ticket_id}",
    }
    values.update(fields)
    return TicketRow(**values)


def waiting(ticket_id: int, age: int | None = 73, reason: str = "requestor_last") -> Finding:
    return Finding(ticket_id, "client_waiting", "high", reason, age)


def event_over(ticket_id: int, age: int = 50) -> Finding:
    return Finding(ticket_id, "event_over", "low", "event_over", age)


def plan(
    state: AlertState,
    findings: list[Finding],
    now: datetime = NOW,
    *,
    policy: AlertPolicy = POLICY,
    **overrides: object,
) -> AlertPlan:
    options: dict[str, object] = {
        "seen_ticket_ids": {finding.ticket_id for finding in findings},
        "unsettled_ticket_ids": set(),
        "queue_complete": True,
    }
    options.update(overrides)
    return plan_alerts(findings, state, now, policy, **options)


def sent(state: AlertState, findings: list[Finding], now: datetime = NOW, **overrides):
    """Plan, then record as if the post succeeded."""
    result = plan(state, findings, now, **overrides)
    result.apply_cleared(state)
    result.record(state, now)
    return result


@pytest.fixture
def state(tmp_path: Path) -> AlertState:
    return AlertState(tmp_path / "alerts-state.json")


# --- re-alert policy ---------------------------------------------------------------------


def test_new_then_quiet_then_reminder_after_three_days(state: AlertState) -> None:
    findings = [waiting(101)]
    first = sent(state, findings)
    assert [(d.finding.ticket_id, d.why) for d in first.on_card] == [(101, "new")]

    assert plan(state, findings, NOW + timedelta(days=2, hours=23)).on_card == ()

    later = NOW + timedelta(days=3)
    reminder = sent(state, findings, later)
    assert [d.why for d in reminder.on_card] == ["reminder"]
    first_alerted, last_alerted = state.get(101, "client_waiting")
    assert first_alerted == NOW.astimezone(UTC)
    assert last_alerted == later.astimezone(UTC)


def test_old_findings_come_weekly(state: AlertState) -> None:
    old = [waiting(101, age=30 * 24)]
    sent(state, old)
    assert plan(state, old, NOW + timedelta(days=6)).on_card == ()
    assert [d.why for d in plan(state, old, NOW + timedelta(days=7)).on_card] == ["reminder"]
    # Just under 30 days still uses the 3-day interval.
    young = [waiting(102, age=30 * 24 - 1)]
    sent(state, young)
    assert [d.why for d in plan(state, young, NOW + timedelta(days=3)).on_card] == ["reminder"]


def test_none_age_uses_the_short_interval(state: AlertState) -> None:
    finding = [Finding(101, "needs_review", "normal", "requestor_last_undated", None)]
    sent(state, finding)
    assert [d.why for d in plan(state, finding, NOW + timedelta(days=3)).on_card] == ["reminder"]


def test_a_cleared_finding_deletes_its_record_and_returns_as_new(state: AlertState) -> None:
    sent(state, [waiting(101), waiting(102)])

    # 101 is judged this run (seen, feed read) and no longer has the finding.
    result = sent(state, [waiting(102)], NOW + timedelta(hours=1), seen_ticket_ids={101, 102})
    assert result.cleared == ((101, "client_waiting"),)
    assert state.get(101, "client_waiting") is None

    back = plan(state, [waiting(101)], NOW + timedelta(hours=2), seen_ticket_ids={101, 102})
    assert [(d.finding.ticket_id, d.why) for d in back.on_card] == [(101, "new")]


def test_a_multi_date_event_over_record_is_cleared_once_the_event_is_judged_running(
    state: AlertState,
) -> None:
    # Reading only the first date (10/13, three days ago) would make this event "over".
    title = "EVENT [10/13/26, 10/27/26, 11/3/26 3:00 PM - 5:00 PM Room 101] Club"
    sent(state, [event_over(106, 71)])

    later = NOW + timedelta(hours=1)
    result = evaluate([row(106, title)], {}, later, RuleSettings(), TZ)
    assert result.findings == ()
    # Event tickets are always judged (they need no feed), so the old record is cleared.
    cleared = sent(state, list(result.findings), later, seen_ticket_ids={106})
    assert cleared.on_card == ()
    assert cleared.cleared == ((106, "event_over"),)
    assert state.keys() == []


def test_a_kind_change_on_one_ticket_alerts_again(state: AlertState) -> None:
    sent(state, [waiting(101)])

    changed = [Finding(101, "needs_review", "normal", "other_last", 80)]
    result = sent(state, changed, NOW + timedelta(hours=1))

    assert [(d.finding.kind, d.why) for d in result.on_card] == [("needs_review", "new")]
    assert result.cleared == ((101, "client_waiting"),)
    assert state.keys() == [(101, "needs_review")]


@pytest.mark.parametrize("why", ["failed", "deferred"])
def test_an_unsettled_feed_keeps_the_record(state: AlertState, why: str) -> None:
    sent(state, [waiting(101)])

    result = sent(
        state, [], NOW + timedelta(hours=1), seen_ticket_ids={101}, unsettled_ticket_ids={101}
    )

    assert result.cleared == ()
    assert state.get(101, "client_waiting") is not None


def test_a_ticket_gone_from_the_rows(state: AlertState) -> None:
    sent(state, [waiting(101), waiting(102)])
    later = NOW + timedelta(hours=1)

    kept = sent(state, [waiting(102)], later, seen_ticket_ids={102}, queue_complete=False)
    assert kept.cleared == ()
    assert state.get(101, "client_waiting") is not None

    gone = sent(state, [waiting(102)], later, seen_ticket_ids={102}, queue_complete=True)
    assert gone.cleared == ((101, "client_waiting"),)
    assert state.get(101, "client_waiting") is None


def test_a_failed_post_leaves_the_records_unchanged(state: AlertState) -> None:
    findings = [waiting(101)]
    result = plan(state, findings)
    result.apply_cleared(state)  # the post failed: no record()
    state.save()

    again = plan(AlertState(state.path), findings, NOW + timedelta(minutes=45))
    assert [d.why for d in again.on_card] == ["new"]


def test_overflow_is_counted_and_not_recorded(state: AlertState) -> None:
    policy = AlertPolicy(3, 30, 7, max_rows_per_card=2)
    findings = [waiting(101, 100), waiting(102, 90), waiting(103, 80)]

    result = sent(state, findings, policy=policy)

    assert [d.finding.ticket_id for d in result.on_card] == [101, 102]
    assert result.overflow == 1
    assert state.get(103, "client_waiting") is None
    nxt = plan(state, findings, NOW + timedelta(minutes=45), policy=policy)
    assert [(d.finding.ticket_id, d.why) for d in nxt.on_card] == [(103, "new")]
    assert nxt.overflow == 0


def test_due_order_is_priority_then_kind_then_oldest(state: AlertState) -> None:
    findings = [
        event_over(105, 200),
        Finding(104, "location_tbd_soon", "normal", "tbd_soon", 20),
        Finding(103, "needs_review", "normal", "other_last", 60),
        waiting(102, 50),
        waiting(101, 90),
        waiting(106, None, "no_reply_yet"),
    ]
    result = plan(state, findings)
    assert [d.finding.ticket_id for d in result.on_card] == [101, 102, 106, 103, 104, 105]


def test_naive_now_is_refused(state: AlertState) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        plan(state, [waiting(101)], datetime(2026, 10, 16, 14, 5))


# --- state file --------------------------------------------------------------------------


def test_state_round_trips_ids_kinds_and_times(tmp_path: Path) -> None:
    path = tmp_path / "data" / "alerts-state.json"
    state = AlertState(path)
    sent(
        state,
        [waiting(101), event_over(102), Finding(103, "follow_up_with_client", "normal", "x", 5)],
    )
    state.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert set(data["alerts"]) == {
        "101:client_waiting",
        "102:event_over",
        "103:follow_up_with_client",
    }
    assert data["alerts"]["101:client_waiting"] == {
        "first_alerted": "2026-10-16T18:05:00+00:00",
        "last_alerted": "2026-10-16T18:05:00+00:00",
    }
    reloaded = AlertState(path)
    assert reloaded.get(103, "follow_up_with_client") == (NOW, NOW)
    assert not (path.parent / "alerts-state.json.tmp").exists()
    assert repr(reloaded) == "AlertState(records=3)"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"schema": 2, "alerts": {}}',
        '{"alerts": {}}',
        '{"schema": 1, "alerts": {"abc:client_waiting": {}}}',
        '{"schema": 1, "alerts": {"101:client_waiting": {"first_alerted": "2026-10-16T18:05:00",'
        ' "last_alerted": "2026-10-16T18:05:00"}}}',
        '{"schema": 1, "alerts": []}',
        "[]",
    ],
)
def test_an_unreadable_state_is_moved_aside_and_re_alerts(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    path = tmp_path / "alerts-state.json"
    path.write_text(content, encoding="utf-8")

    state = AlertState(path)

    assert len(state) == 0
    assert not path.exists()
    assert (tmp_path / "alerts-state.json.bad").read_text(encoding="utf-8") == content
    assert [r.getMessage() for r in caplog.records] == ["alerts state unreadable"]
    assert [d.why for d in plan(state, [waiting(101)]).on_card] == ["new"]


def test_the_saved_state_holds_no_title_or_requestor(state: AlertState) -> None:
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    findings = [waiting(101), event_over(102), waiting(103, reason="no_reply_yet")]
    result = sent(state, findings)
    build_card(result, rows, {}, NOW, TZ, SUMMARY, title_chars=80)
    state.save()

    text = state.path.read_text(encoding="utf-8")
    for private in [*TITLES.values(), REQUESTOR, EMAIL, PRIM_RESP, "Laptop", "Club", "Jordan"]:
        assert private not in text


# --- card --------------------------------------------------------------------------------


def texts(card: dict) -> list[str]:
    return [block["text"] for block in card["body"]]


def test_card_heading_sections_and_counts(state: AlertState) -> None:
    sent(state, [waiting(103, 80)], NOW - timedelta(days=3))
    findings = [
        event_over(102, 60),
        Finding(104, "location_tbd_soon", "normal", "tbd_soon", 20),
        Finding(105, "needs_review", "normal", "other_last", 100),
        waiting(101, 73),
        waiting(103, 80),
    ]
    result = plan(state, findings)
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    links = card_links(rows, list(rows), TdxSettings(base_url="https://tdx.example.edu/TDNext/"))

    card = build_card(result, rows, links, NOW, TZ, SUMMARY, title_chars=80)

    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["msteams"] == {"width": "Full"}
    lines = texts(card)
    assert lines[0] == "TDX queue: 4 new, 1 reminder"
    assert lines[1] == "Checked Fri 2:05 PM: 26 unclaimed tickets, 7 ticket histories read."
    headings = [b["text"] for b in card["body"] if b.get("weight") == "Bolder"][1:]
    assert headings == [
        "Requester waiting (2)",
        "Someone else wrote last (1)",
        "Event soon: no room yet (1)",
        "Event over: resolve (1)",
    ]
    link = "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID=103"
    assert lines[3] == f"[#103]({link}) Printer jam in the library basement"
    assert lines[4] == "The requester wrote last, 3 days ago: reply or check in · reminder"
    assert lines[6] == "The requester wrote last, 3 days ago: reply or check in"
    assert lines[-1] == "Event ended 2 days ago: ready to resolve?"


def test_without_titles_a_row_is_one_linked_line_of_id_and_finding(state: AlertState) -> None:
    sent(state, [waiting(103, 80)], NOW - timedelta(days=3))
    findings = [waiting(101, 73), waiting(103, 80), event_over(102, 60)]
    result = plan(state, findings)
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    links = card_links(rows, list(rows), TdxSettings(base_url="https://tdx.example.edu/TDNext/"))

    card = build_card(result, rows, links, NOW, TZ, SUMMARY, title_chars=80, show_titles=False)

    link = "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID={}"
    assert texts(card)[2:] == [
        "Requester waiting (2)",
        f"[#103]({link.format(103)}) · The requester wrote last, 3 days ago: reply or check in"
        " · reminder",
        f"[#101]({link.format(101)}) · The requester wrote last, 3 days ago: reply or check in",
        "Event over: resolve (1)",
        f"[#102]({link.format(102)}) · Event ended 2 days ago: ready to resolve?",
    ]
    assert card["fallbackText"] == "3 tickets need attention"


def test_without_titles_no_title_text_appears_anywhere_in_the_card(state: AlertState) -> None:
    findings = [waiting(ticket_id) for ticket_id in TITLES]
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    links = card_links(rows, list(rows), TdxSettings(base_url="https://tdx.example.edu/TDNext/"))

    card = build_card(
        plan(state, findings), rows, links, NOW, TZ, SUMMARY, title_chars=80, show_titles=False
    )

    text = json.dumps(card)
    for private in [*TITLES.values(), "Laptop", "Printer", "Robotics", "Club", "Password"]:
        assert private not in text
    for private in [REQUESTOR, EMAIL, PRIM_RESP]:
        assert private not in text
    assert all(f"#{ticket_id}" in text for ticket_id in TITLES)


def test_with_titles_is_the_default(state: AlertState) -> None:
    rows = {101: row(101)}
    result = plan(state, [waiting(101)])
    assert build_card(result, rows, {}, NOW, TZ, SUMMARY, title_chars=80) == build_card(
        result, rows, {}, NOW, TZ, SUMMARY, title_chars=80, show_titles=True
    )


@pytest.mark.parametrize(
    ("finding", "expected"),
    [
        (waiting(1, 73), "The requester wrote last, 3 days ago: reply or check in"),
        (waiting(1, 47, "no_reply_yet"), "No reply yet, opened 47 hours ago"),
        (
            Finding(1, "needs_review", "normal", "other_last", 48),
            "The last message, 2 days ago, came from someone other than the requester or the "
            "help desk: check whether the requester still needs a reply",
        ),
        (
            Finding(1, "location_tbd_soon", "normal", "tbd_soon", 30),
            "Event in 30 hours. Location still TBD on the ticket. Check the events spreadsheet; if "
            "a room is listed, update the ticket.",
        ),
        (event_over(1, 25), "Event ended 25 hours ago: ready to resolve?"),
        (
            Finding(1, "needs_review", "normal", "requestor_last_undated", None),
            "Someone else wrote last",
        ),
        (
            Finding(1, "needs_review", "normal", "something_new", 100),
            "Someone else wrote last, 4 days",
        ),
        (waiting(1, None, "no_reply_yet"), "Requester waiting"),
        (
            Finding(1, "follow_up_with_client", "normal", "x", 5),
            "Follow up with requester, 5 hours",
        ),
        (waiting(1, 73, "expects_reply"), "The requester has waited 3 days for a reply"),
        (
            Finding(1, "follow_up_with_client", "normal", "client_follow_up", 50),
            "The requester said they'd follow up; quiet 2 days: check in",
        ),
        (
            Finding(1, "follow_up_with_client", "normal", "client_no_reply_needed", 96),
            "The requester's last message needed no reply; quiet 4 days: check in or resolve",
        ),
        (
            Finding(1, "event_outreach_due", "normal", "outreach_due", 240, 240),
            "Event in 10 days. Email the requester for the room number. If it's hybrid or "
            "remote, also ask for the Zoom link, ask them to add our shared mailbox as an "
            "alternative host, and ask whether to record. Then update the events spreadsheet.",
        ),
        (
            Finding(1, "event_details_pending", "high", "details_pending", 72, 30),
            "Event in 30 hours. The requester hasn't answered for 3 days: follow up",
        ),
        # The near-event override, and the requester replying on an event.
        (
            Finding(1, "event_outreach_due", "high", "outreach_due_requester_wrote", 30, 30, 50),
            "Event in 30 hours. The requester wrote 2 days ago. Email them for the room number. If "
            "it's hybrid or remote, also ask for the Zoom link, ask them to add our shared "
            "mailbox as an alternative host, and ask whether to record. Then update the events "
            "spreadsheet.",
        ),
        (
            Finding(1, "event_details_pending", "high", "details_pending_near", 1, 26),
            "Event in 26 hours. Still waiting on the requester (asked 1 hour ago): follow up now",
        ),
        (
            Finding(1, "event_details_pending", "high", "details_room_set", 96, 70),
            "Event in 2 days. The room is set; confirm the Zoom link and recording if they're "
            "still open",
        ),
        (
            Finding(1, "client_waiting", "normal", "event_requester_replied", 50, 122),
            "Event in 5 days. The requester replied 2 days ago: read it and confirm the details",
        ),
        (
            Finding(1, "location_tbd_soon", "normal", "location_missing", 30),
            "Event in 30 hours. No room on the ticket yet. Check the events spreadsheet; if a "
            "room is listed, update the ticket.",
        ),
        # Without the time until the event, the plain label and age.
        (
            Finding(1, "event_details_pending", "high", "details_pending", 72),
            "Event: follow up with the requester, 3 days",
        ),
        (Finding(1, "brand_new_kind", "normal", "x", None), "Brand new kind"),
    ],
)
def test_wording(finding: Finding, expected: str) -> None:
    assert finding_wording(finding) == expected


def test_unknown_kinds_get_their_own_section_without_crashing(state: AlertState) -> None:
    findings = [Finding(101, "brand_new_kind", "normal", "x", None), waiting(102)]
    card = build_card(plan(state, findings), {}, {}, NOW, TZ, SUMMARY, title_chars=80)
    lines = texts(card)
    assert "Requester waiting (1)" in lines
    assert lines.index("Brand new kind (1)") > lines.index("Requester waiting (1)")
    assert "#101" in lines  # no row, no link: the plain id


def test_follow_up_with_client_is_the_section_after_client_waiting(state: AlertState) -> None:
    findings = [
        Finding(101, "needs_review", "normal", "other_last", 80),
        Finding(102, "follow_up_with_client", "normal", "client_follow_up", 90),
        waiting(103),
    ]
    card = build_card(plan(state, findings), {}, {}, NOW, TZ, SUMMARY, title_chars=80)
    lines = texts(card)
    sections = [line for line in lines if line.endswith("(1)")]
    assert sections == [
        "Requester waiting (1)",
        "Follow up with requester (1)",
        "Someone else wrote last (1)",
    ]


def test_event_outreach_sections_come_before_location_tbd(state: AlertState) -> None:
    findings = [
        Finding(101, "location_tbd_soon", "normal", "tbd_soon", 30),
        Finding(102, "event_details_pending", "normal", "details_pending", 72, 100),
        Finding(103, "event_outreach_due", "normal", "outreach_due", 200, 200),
        Finding(104, "needs_review", "normal", "other_last", 80),
    ]
    card = build_card(plan(state, findings), {}, {}, NOW, TZ, SUMMARY, title_chars=80)
    sections = [line for line in texts(card) if line.endswith("(1)")]
    assert sections == [
        "Someone else wrote last (1)",
        "Event: email the requester (1)",
        "Event: follow up with the requester (1)",
        "Event soon: no room yet (1)",
    ]


def test_event_outreach_rows_carry_only_the_fixed_wording(state: AlertState) -> None:
    rows = {101: row(101, "CCI Event Request Form"), 102: row(102, "CCI Event Request Form")}
    links = {101: "https://tdx.example.edu/t/101", 102: None}
    findings = [
        Finding(101, "event_outreach_due", "high", "outreach_due", 50, 50),
        Finding(102, "event_details_pending", "high", "details_pending", 60, 100),
    ]

    card = build_card(
        plan(state, findings), rows, links, NOW, TZ, SUMMARY, title_chars=80, show_titles=False
    )

    lines = texts(card)
    assert (
        "[#101](https://tdx.example.edu/t/101) · Event in 2 days. Email the requester for the "
        "room number. If it's hybrid or remote, also ask for the Zoom link, ask them to add our "
        "shared mailbox as an alternative host, and ask whether to record. Then update the "
        "events spreadsheet."
    ) in lines
    assert "#102 · Event in 4 days. The requester hasn't answered for 2 days: follow up" in lines
    assert "CCI Event Request Form" not in json.dumps(card)


def test_a_verdict_to_follow_up_with_client_alerts_once_and_clears_client_waiting(
    state: AlertState,
) -> None:
    sent(state, [waiting(101)])
    follow_up = Finding(101, "follow_up_with_client", "normal", "client_follow_up", 74)

    first = sent(state, [follow_up], NOW + timedelta(hours=1))
    again = sent(state, [follow_up], NOW + timedelta(hours=2))

    assert [(d.finding.kind, d.why) for d in first.on_card] == [("follow_up_with_client", "new")]
    assert first.cleared == ((101, "client_waiting"),)
    assert again.on_card == ()
    assert state.keys() == [(101, "follow_up_with_client")]


def test_a_reason_change_within_client_waiting_does_not_re_alert(state: AlertState) -> None:
    sent(state, [waiting(101)])

    result = sent(state, [waiting(101, 74, "expects_reply")], NOW + timedelta(hours=1))

    assert result.on_card == ()
    assert result.cleared == ()
    assert state.get(101, "client_waiting") is not None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (0, "less than an hour"),
        (1, "1 hour"),
        (2, "2 hours"),
        (21, "21 hours"),
        (47, "47 hours"),
        (48, "2 days"),
        (71, "2 days"),
        (72, "3 days"),
    ],
)
def test_format_age(hours: int, expected: str) -> None:
    assert format_age(hours) == expected


def test_titles_are_cut_with_an_ellipsis_and_cleaned() -> None:
    assert clean_title("a" * 100, 80) == "a" * 79 + "…"
    assert len(clean_title("a" * 100, 80)) == 80
    assert clean_title("a" * 80, 80) == "a" * 80
    assert clean_title("Line one\r\n\tline\x00 two\x1b[31m", 80) == "Line one line two [31m"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[x](http://evil)", r"\[x\]\(http://evil\)"),
        ("*bold* and _it_", r"\*bold\* and \_it\_"),
        ("EVENT [10/20 3pm Room 101] Club", r"EVENT \[10/20 3pm Room 101\] Club"),
        ("# heading", r"\# heading"),
        ("> quote", r"\> quote"),
        ("Room #5 > 4", "Room #5 > 4"),
        (r"back\slash ~strike~ `code`", r"back\\slash \~strike\~ \`code\`"),
    ],
)
def test_escape_markdown(title: str, expected: str) -> None:
    assert escape_markdown(title) == expected


def test_card_renders_hostile_titles_literally(state: AlertState) -> None:
    rows = {
        101: row(101, "[x](http://evil) *bold* " + "y" * 100),
        102: row(102, "EVENT [10/20 3pm Room 101] Club"),
    }
    card = build_card(
        plan(state, [waiting(101), event_over(102)]), rows, {}, NOW, TZ, SUMMARY, title_chars=40
    )
    lines = texts(card)
    assert r"#101 \[x\]\(http://evil\) \*bold\* " in lines[3]
    assert lines[3].endswith("…")
    assert "](http://evil)" not in json.dumps(card)
    assert r"#102 EVENT \[10/20 3pm Room 101\] Club" in lines


def test_only_tdx_host_links(state: AlertState) -> None:
    tdx = TdxSettings(base_url="https://tdx.example.edu/TDNext/")
    rows = {
        101: row(101),
        102: row(102, detail_url="https://evil.example.com/TicketDet?TicketID=102"),
        103: row(103, detail_url=None),
    }
    links = card_links(rows, [101, 102, 103], tdx)
    assert links[101].startswith("https://tdx.example.edu/")
    assert links[102] is None and links[103] is None

    templated = card_links(
        rows,
        [102],
        TdxSettings(
            base_url="https://tdx.example.edu/TDNext/",
            ticket_url_template="https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID={id}",
        ),
    )
    assert templated[102] == "https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID=102"

    card = build_card(
        plan(state, [waiting(i) for i in rows]), rows, links, NOW, TZ, SUMMARY, title_chars=80
    )
    assert "evil.example.com" not in json.dumps(card)
    assert any(line.startswith("#102 ") for line in texts(card))


def test_overflow_line(state: AlertState) -> None:
    findings = [waiting(100 + i, 100 - i) for i in range(14)]
    result = plan(state, findings, policy=AlertPolicy(3, 30, 7, max_rows_per_card=2))
    lines = texts(build_card(result, {}, {}, NOW, TZ, SUMMARY, title_chars=80))
    assert lines[-1] == "12 more waiting; they come on the next cards."
    one = plan(state, findings[:3], policy=AlertPolicy(3, 30, 7, max_rows_per_card=2))
    assert texts(build_card(one, {}, {}, NOW, TZ, SUMMARY, title_chars=80))[-1] == (
        "1 more waiting; it comes on the next card."
    )


def test_the_card_stays_under_25_kb(state: AlertState) -> None:
    findings = [waiting(1000 + i, 100) for i in range(120)]
    rows = {f.ticket_id: row(f.ticket_id, "Ü" * 300) for f in findings}
    big = AlertPlan(tuple(DueAlert(f, "new") for f in findings), overflow=5, cleared=())

    fitted, card = fit_card(big, rows, {}, NOW, TZ, SUMMARY, title_chars=200)

    assert card_size(card) < CARD_MAX_BYTES
    assert 0 < len(fitted.on_card) < 120
    assert fitted.overflow == 5 + 120 - len(fitted.on_card)
    assert texts(card)[-1] == f"{fitted.overflow} more waiting; they come on the next cards."
    assert texts(card)[0] == f"TDX queue: {len(fitted.on_card)} new"
    assert build_card(big, rows, {}, NOW, TZ, SUMMARY, title_chars=200) == card


def test_the_card_fallback_text_is_counts_only(state: AlertState) -> None:
    sent(state, [waiting(103, 80)], NOW - timedelta(days=3))
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    findings = [waiting(101), event_over(102), waiting(103, 80)]
    tdx = TdxSettings(base_url="https://tdx.example.edu/TDNext/")

    card = build_card(
        plan(state, findings),
        rows,
        card_links(rows, list(rows), tdx),
        NOW,
        TZ,
        SUMMARY,
        title_chars=80,
    )

    fallback = card["fallbackText"]
    assert fallback == "3 tickets need attention"
    for private in [*TITLES.values(), "101", "102", "103", "#", "Laptop", REQUESTOR]:
        assert private not in fallback


def test_fallback_text_is_singular_for_one_ticket_and_names_the_team_on_desk_cards() -> None:
    assert fallback_text(1) == "1 ticket needs attention"
    assert fallback_text(2) == "2 tickets need attention"
    assert fallback_text(0) == "Nothing needs your attention right now"
    assert fallback_text(1, DESK) == "1 ticket for the help desk"
    assert fallback_text(0, DESK) == "Nothing for the help desk right now"
    assert fallback_text(3, COMMONS) == "3 tickets for the Commons"
    assert fallback_text(0, COMMONS) == "Nothing for the Commons right now"


def test_the_card_holds_no_requestor_name_or_email(state: AlertState) -> None:
    rows = {ticket_id: row(ticket_id) for ticket_id in TITLES}
    findings = [waiting(101), event_over(102), waiting(103, reason="no_reply_yet")]
    tdx = TdxSettings(base_url="https://tdx.example.edu/TDNext/")
    card = build_card(
        plan(state, findings),
        rows,
        card_links(rows, list(rows), tdx),
        NOW,
        TZ,
        SUMMARY,
        title_chars=80,
    )
    text = json.dumps(card, ensure_ascii=False)
    for private in [REQUESTOR, EMAIL, PRIM_RESP, "Jordan", "Sampleton", "Casey", "@"]:
        assert private not in text
    assert "Laptop will not connect to campus Wi-Fi" in text  # titles do go on the card


# --- shift summary -----------------------------------------------------------------------

SLOT = datetime(2026, 10, 19, 7, 30, tzinfo=TZ)
SLOT_RUN = datetime(2026, 10, 19, 7, 31, tzinfo=TZ)


def summary(
    findings: list[Finding],
    new: set[tuple[int, str]] = frozenset(),
    *,
    show_titles: bool = False,
    max_rows: int = 25,
    **options: object,
) -> tuple[tuple[Finding, ...], dict]:
    rows_by_id = {f.ticket_id: row(f.ticket_id) for f in findings}
    links = card_links(
        rows_by_id, list(rows_by_id), TdxSettings(base_url="https://tdx.example.edu/")
    )
    return fit_summary_card(
        findings,
        new,
        rows_by_id,
        links,
        SLOT,
        options.pop("now", SLOT_RUN),
        TZ,
        SUMMARY,
        title_chars=80,
        show_titles=show_titles,
        max_rows=max_rows,
        **options,
    )


def test_the_summary_lists_every_open_finding_by_kind_with_new_ones_marked() -> None:
    findings = [event_over(104), waiting(101), waiting(102, 100), waiting(101)]  # 101 twice
    shown, card = summary(findings, {(101, "client_waiting")})

    lines = texts(card)
    assert lines[0] == "TDX queue at 7:30 AM: 3 tickets for you (1 added)"
    assert lines[1] == "Tickets that need your attention. Checked 26 unclaimed tickets at 7:31 AM."
    assert lines[2] == "Requester waiting (2)"
    assert lines[3].startswith("[#102](https://tdx.example.edu/") and "(added)" not in lines[3]
    assert "#101](" in lines[4] and ") (added) · The requester wrote last, 3 days ago" in lines[4]
    assert lines[5] == "Event over: resolve (1)"
    assert [(f.ticket_id, f.kind) for f in shown] == [
        (102, "client_waiting"),
        (101, "client_waiting"),
        (104, "event_over"),
    ]
    assert card["fallbackText"] == "3 tickets need attention"


def test_desk_summaries_carry_no_titles_or_names_by_default() -> None:
    _, card = summary([waiting(101), event_over(102)])
    text = json.dumps(card)
    for secret in (*TITLES.values(), REQUESTOR, EMAIL, PRIM_RESP):
        assert secret not in text
    _, titled = summary([waiting(101)], show_titles=True)
    assert TITLES[101] in json.dumps(titled)


def test_an_empty_queue_says_queue_clear() -> None:
    shown, card = summary([])
    assert texts(card)[:2] == [
        "Queue clear at 7:30 AM: nothing needs attention.",
        "Nothing needs your attention right now. Checked 26 unclaimed tickets at 7:31 AM.",
    ]
    assert card["fallbackText"] == "Nothing needs your attention right now"
    assert shown == ()


def test_a_late_summary_says_when_it_was_due() -> None:
    late_run = datetime(2026, 10, 19, 8, 12, tzinfo=TZ)
    _, card = summary([waiting(101)], late=True, now=late_run)
    assert texts(card)[0] == "TDX queue at 8:12 AM (late, due 7:30 AM): 1 ticket for you"
    _, clear = summary([], late=True, now=late_run)
    assert texts(clear)[0] == (
        "Queue clear at 8:12 AM (late, due 7:30 AM): nothing needs attention."
    )


@pytest.mark.parametrize(
    ("added", "since", "late_due", "heading"),
    [
        (0, "yesterday", None, "TDX queue at 7:30 AM: 3 tickets for you"),
        (0, None, None, "TDX queue at 7:30 AM: 3 tickets for you"),
        (2, None, None, "TDX queue at 7:30 AM: 3 tickets for you (2 added)"),
        (2, "yesterday", None, "TDX queue at 7:30 AM: 3 tickets for you (2 added since yesterday)"),
        (
            1,
            "yesterday",
            "7:30 AM",
            "TDX queue at 7:30 AM (late, due 7:30 AM): 3 tickets for you (1 added since yesterday)",
        ),
    ],
    ids=["none-since", "none", "added", "added-since", "late"],
)
def test_the_full_heading_names_the_tickets_added_only_when_there_are_some(
    added: int, since: str | None, late_due: str | None, heading: str
) -> None:
    assert summary_heading(3, added, "7:30 AM", late_due, since) == heading


# One event ticket can carry two findings: a follow-up and a missing room.
FOLLOW_UP_102 = Finding(102, "event_details_pending", "high", "details_pending_near", 20, 30)
NO_ROOM_102 = Finding(102, "location_tbd_soon", "normal", "tbd_soon", 30)


def test_the_full_heading_and_preview_count_tickets_not_rows() -> None:
    findings = [FOLLOW_UP_102, NO_ROOM_102, waiting(101)]

    shown, card = summary(findings, reader=DESK)

    assert texts(card)[0] == "TDX queue at 7:30 AM: 2 tickets for the help desk"
    assert card["fallbackText"] == "2 tickets for the help desk"
    assert len(shown) == 3  # both of 102's rows are still listed


@pytest.mark.parametrize(
    ("new", "marked"),
    [
        ({(102, "location_tbd_soon")}, 1),  # one added finding, one older one
        ({(102, "event_details_pending"), (102, "location_tbd_soon")}, 2),  # both added
    ],
    ids=["one-of-two", "both"],
)
def test_added_counts_tickets_with_an_added_finding(new: set, marked: int) -> None:
    findings = [FOLLOW_UP_102, NO_ROOM_102, waiting(101)]

    _, card = summary(findings, new, since=SLOT - timedelta(days=1))

    lines = texts(card)
    assert lines[0] == "TDX queue at 7:30 AM: 2 tickets for you (1 added since yesterday)"
    assert sum("(added)" in line for line in lines) == marked  # row markers stay per finding


def test_the_late_heading_counts_tickets_too() -> None:
    late_run = datetime(2026, 10, 19, 8, 12, tzinfo=TZ)
    findings = [FOLLOW_UP_102, NO_ROOM_102, waiting(101)]

    _, card = summary(findings, {(102, "event_details_pending")}, late=True, now=late_run)

    assert texts(card)[0] == "TDX queue at 8:12 AM (late, due 7:30 AM): 2 tickets for you (1 added)"
    assert card["fallbackText"] == "2 tickets need attention"


def test_the_urgent_card_counts_one_event_with_two_findings_once() -> None:
    _, card = fit_urgent_card(
        [FOLLOW_UP_102, NO_ROOM_102], {102: row(102)}, {}, SLOT_RUN, TZ,
        title_chars=60, show_titles=False, max_rows=10, reader=DESK,
    )  # fmt: skip

    lines = texts(card)
    assert lines[0] == "Event soon: 1 needs follow-up"
    assert card["fallbackText"] == "1 ticket for the help desk"
    assert "Event: follow up with the requester (1)" in lines
    assert "Event soon: no room yet (1)" in lines


def test_the_alert_card_preview_counts_the_tickets_it_lists(state: AlertState) -> None:
    rows = {ticket_id: row(ticket_id) for ticket_id in (101, 102)}

    card = build_card(
        plan(state, [FOLLOW_UP_102, NO_ROOM_102, waiting(101)]),
        rows, {}, NOW, TZ, SUMMARY, title_chars=80,
    )  # fmt: skip

    assert texts(card)[0] == "TDX queue: 2 new"  # tickets, like the preview line
    assert card["fallbackText"] == "2 tickets need attention"


def test_two_findings_on_one_ticket_are_one_new_in_the_alert_heading(state: AlertState) -> None:
    card = build_card(
        plan(state, [FOLLOW_UP_102, NO_ROOM_102]), {102: row(102)}, {}, NOW, TZ, SUMMARY,
        title_chars=80,
    )  # fmt: skip

    lines = texts(card)
    assert lines[0] == "TDX queue: 1 new"
    assert "Event: follow up with the requester (1)" in lines  # row markers stay per finding
    assert "Event soon: no room yet (1)" in lines


def test_a_ticket_with_a_new_row_and_a_reminder_counts_once_as_new(state: AlertState) -> None:
    sent(state, [FOLLOW_UP_102, waiting(101)], NOW - timedelta(days=4))

    card = build_card(
        plan(state, [FOLLOW_UP_102, NO_ROOM_102, waiting(101)]),
        {i: row(i) for i in (101, 102)}, {}, NOW, TZ, SUMMARY, title_chars=80,
    )  # fmt: skip

    assert texts(card)[0] == "TDX queue: 1 new, 1 reminder"  # 102 new, 101 a reminder


def test_rows_past_the_cap_are_counted_as_more() -> None:
    findings = [waiting(i) for i in range(101, 106)] + [event_over(201), event_over(202)]
    shown, card = summary(findings, max_rows=4)
    lines = texts(card)
    assert "Requester waiting (5)" in lines and "Event over: resolve (2)" in lines
    assert lines.count("+1 more") == 1 and lines.count("+2 more") == 1
    assert len(shown) == 4


def test_summary_mentions_never_carry_a_tdx_name() -> None:
    _, card = summary(
        [waiting(101)], mention="people", mention_people=["mf123@example.edu"], show_titles=True
    )
    entities = card["msteams"]["entities"]
    assert entities == [
        {
            "type": "mention",
            "text": "<at>mf123</at>",
            "mentioned": {"id": "mf123@example.edu", "name": "mf123"},
        }
    ]
    assert REQUESTOR not in json.dumps(entities) and PRIM_RESP not in json.dumps(entities)
    assert card["msteams"]["width"] == "Full"


def test_build_summary_card_returns_the_card_alone() -> None:
    card = build_summary_card(
        [waiting(101)], set(), {101: row(101)}, {101: None}, SLOT, SLOT_RUN, TZ, SUMMARY,
        title_chars=80, show_titles=False, max_rows=25,
    )  # fmt: skip
    # No link (the address is not on the TDX host): the id stays plain text.
    assert texts(card)[3] == "#101 · The requester wrote last, 3 days ago: reply or check in"


def test_a_huge_summary_is_trimmed_under_the_size_limit() -> None:
    findings = [waiting(i) for i in range(1000, 1050)]
    shown, card = summary(findings, max_rows=50, show_titles=True)
    assert card_size(card) < CARD_MAX_BYTES
    assert 0 < len(shown) <= 50


def test_summary_state_remembers_the_slot_the_post_and_the_open_set(tmp_path: Path) -> None:
    path = tmp_path / "summary-state-desk.json"
    state = SummaryState(path)
    assert state.last_slot is None and not state.posted(SLOT) and state.full_due(SLOT)

    state.record(SLOT, posted=True, open_keys={(102, "event_over"), (101, "client_waiting")})

    again = SummaryState(path)
    noon = SLOT + timedelta(hours=4, minutes=30)
    assert again.posted(SLOT) and not again.posted(noon)
    assert again.last_posted == SLOT and not again.full_due(noon)
    assert again.full_due(SLOT + timedelta(days=1))  # a new day starts full
    assert again.open == {(101, "client_waiting"), (102, "event_over")}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": 3,
        "last_slot": "2026-10-19T07:30:00-04:00",
        "last_posted": "2026-10-19T07:30:00-04:00",
        "open": ["101:client_waiting", "102:event_over"],
        "known": ["101:client_waiting", "102:event_over"],
    }
    assert "client_waiting" not in repr(again) and "open=2" in repr(again)
    assert "known=2" in repr(again)


def test_a_skipped_delta_moves_the_slot_but_not_the_last_post(tmp_path: Path) -> None:
    path = tmp_path / "summary-state-desk.json"
    state = SummaryState(path)
    state.record(SLOT, posted=True, open_keys={(101, "client_waiting")})
    evening = SLOT + timedelta(hours=9, minutes=30)

    state.record(evening, posted=False, open_keys={(101, "client_waiting")})

    again = SummaryState(path)
    assert again.posted(evening) and again.last_slot == evening and again.last_posted == SLOT


def test_a_schema_1_summary_state_keeps_its_slot_and_makes_the_next_post_full(
    tmp_path: Path,
) -> None:
    path = tmp_path / "summary-state-desk.json"
    path.write_text(json.dumps({"schema": 1, "last_slot": SLOT.isoformat()}), encoding="utf-8")

    state = SummaryState(path)

    assert state.posted(SLOT)  # no second 07:30 post after the upgrade
    assert state.last_posted is None and state.open == frozenset()
    assert state.full_due(SLOT + timedelta(hours=4, minutes=30))


def test_a_schema_2_summary_state_starts_known_as_its_open_set(tmp_path: Path) -> None:
    path = tmp_path / "summary-state-desk.json"
    data = {
        "schema": 2,
        "last_slot": SLOT.isoformat(),
        "last_posted": SLOT.isoformat(),
        "open": ["101:client_waiting"],
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    state = SummaryState(path)

    assert state.posted(SLOT) and state.last_posted == SLOT
    assert state.open == state.known == {(101, "client_waiting")}


def test_known_drops_cleared_findings_and_an_urgent_post_joins_open_and_known(
    tmp_path: Path,
) -> None:
    path = tmp_path / "summary-state-desk.json"
    state = SummaryState(path)
    state.record(SLOT, posted=True, open_keys={(101, "client_waiting")})
    urgent = (103, "event_details_pending")

    state.update_known(frozenset(), shown={urgent})  # 101 cleared, 103 posted

    again = SummaryState(path)
    assert again.known == {urgent}
    assert again.open == {(101, "client_waiting"), urgent}  # the next delta counts from here
    assert again.last_slot == again.last_posted == SLOT
    before = path.read_text(encoding="utf-8")
    again.update_known({urgent})  # nothing changed: the file is not rewritten
    assert path.read_text(encoding="utf-8") == before


def test_the_urgent_card_lists_its_rows_like_the_summary_with_no_mention() -> None:
    findings = [
        Finding(103, "event_details_pending", "high", "details_pending_near", 20, 6),
        Finding(104, "location_tbd_soon", "normal", "tbd_soon", 30),
    ]
    rows_by_id = {i: row(i) for i in (103, 104)}

    shown, card = fit_urgent_card(
        findings,
        rows_by_id,
        {},
        SLOT_RUN,
        TZ,
        title_chars=60,
        show_titles=True,
        max_rows=10,
        reader=DESK,
    )

    lines = [block["text"] for block in card["body"]]
    assert lines[0] == "Event soon: 2 need follow-up"
    assert (
        lines[1]
        == "The help desk, please follow up on these before the event. Checked Mon 7:31 AM."
    )
    assert (
        "Event: follow up with the requester (1)" in lines
        and "Event soon: no room yet (1)" in lines
    )
    assert card["fallbackText"] == "2 tickets for the help desk"
    assert "entities" not in card["msteams"]
    assert {f.ticket_id for f in shown} == {103, 104}


@pytest.mark.parametrize("reader", [DESK, Reader()], ids=["desk", "personal"])
def test_a_title_without_a_room_says_no_room_yet_and_tbd_keeps_its_row(reader: Reader) -> None:
    """A title with a date and time but no room never says TBD, so its row says there is no
    room yet; a TBD title keeps the TBD row. Both sit under the same section."""
    findings = [
        Finding(104, "location_tbd_soon", "normal", "tbd_soon", 30),
        Finding(105, "location_tbd_soon", "normal", "location_missing", 20),
    ]
    rows_by_id = {i: row(i) for i in (104, 105)}

    _, card = fit_urgent_card(
        findings, rows_by_id, {}, SLOT_RUN, TZ,
        title_chars=60, show_titles=True, max_rows=10, reader=reader,
    )  # fmt: skip

    text = json.dumps(card)
    assert "Event soon: no room yet (2)" in texts(card)
    assert "Event in 20 hours. No room on the ticket yet. Check the events spreadsheet" in text
    assert "Event in 30 hours. Location still TBD on the ticket. Check the events" in text
    assert text.count("Location still TBD") == 1
    assert "room still TBD" not in text


# --- a renamed event with no room and a feed finding: one row --------------------------------


def merged_event(title: str) -> Finding:
    """The rules' one finding for a renamed event on 10/20 3 PM, 31 hours after the run, whose
    desk asked the requester an hour ago and has no room yet."""
    rows = [row(110, title)]
    asked = TicketFeed(110, (FeedEntry(PRIM_RESP, SLOT_RUN - timedelta(hours=1), "reply"),))
    rules = RuleSettings(extra_staff_names=(PRIM_RESP,))

    (finding,) = evaluate(rows, {110: asked}, SLOT_RUN, rules, TZ).findings
    return finding


TBD_EVENT = "EVENT [10/20 3pm TBD] Club"
NO_ROOM_EVENT = "EVENT [10/20/26 3PM-4PM] Club"


@pytest.mark.parametrize(
    ("title", "room"),
    [(TBD_EVENT, "Location still TBD on the ticket"), (NO_ROOM_EVENT, "No room on the ticket yet")],
    ids=["tbd", "missing"],
)
def test_a_merged_event_row_is_one_row_ending_in_the_room_check(title: str, room: str) -> None:
    finding = merged_event(title)

    shown, card = summary([finding], reader=DESK)

    lines = texts(card)
    assert (finding.kind, finding.reason) == ("event_details_pending", "details_pending_near")
    assert lines[0] == "TDX queue at 7:30 AM: 1 ticket for the help desk"
    assert "Event soon: no room yet (1)" not in lines
    (row_line,) = [line for line in lines if line.startswith("[#110](")]
    assert row_line.endswith(
        ") · Event in 31 hours. Still waiting on the requester (asked 1 hour ago): follow up "
        f"now. {room}: check the events spreadsheet, and if a room is listed, update the ticket."
    )
    assert len(shown) == 1


def test_the_room_check_follows_a_full_stop_once() -> None:
    outreach = Finding(110, "event_outreach_due", "high", "outreach_due", 30, 30, no_room="tbd")

    wording = finding_wording(outreach)

    assert wording.endswith("Then update the events spreadsheet. " + NO_ROOM_WORDING["tbd"])
    assert ".." not in wording


def test_the_urgent_card_still_fires_for_a_merged_event_row() -> None:
    finding = merged_event(TBD_EVENT)

    _, card = fit_urgent_card(
        [finding], {110: row(110)}, {}, SLOT_RUN, TZ,
        title_chars=60, show_titles=False, max_rows=10, reader=DESK,
    )  # fmt: skip

    assert urgent_event(finding, RuleSettings().near_event_days)
    assert texts(card)[0] == "Event soon: 1 needs follow-up"
    assert texts(card)[2] == "Event: follow up with the requester (1)"


def test_an_unreadable_summary_state_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "summary-state-desk.json"
    path.write_text("{not json", encoding="utf-8")
    assert SummaryState(path).last_slot is None
    assert (tmp_path / "summary-state-desk.json.bad").exists()


# --- ready to close, possible duplicates, events this week --------------------------------


def ready(ticket_id: int, age: int = 96) -> Finding:
    return Finding(ticket_id, "ready_to_close", "low", "desk_replied_last", age)


def week_event(ticket_id: int, day: int, state: str = "no_outreach_yet") -> WeekEvent:
    return WeekEvent(
        ticket_id, datetime(2026, 10, day, 15, 0, tzinfo=TZ), True, time(17, 0), "lobby", True,
        state,  # type: ignore[arg-type]
    )  # fmt: skip


def extras_summary(
    findings: list[Finding],
    events: list[WeekEvent] = (),  # type: ignore[assignment]
    duplicates: dict[int, int] | None = None,
    *,
    show_titles: bool = False,
    max_rows: int = 25,
) -> tuple[tuple[Finding, ...], dict]:
    ids = {f.ticket_id for f in findings} | {e.ticket_id for e in events}
    rows_by_id = {ticket_id: row(ticket_id) for ticket_id in ids}
    links = card_links(rows_by_id, sorted(ids), TdxSettings(base_url="https://tdx.example.edu/"))
    return fit_summary_card(
        findings, set(), rows_by_id, links, SLOT, SLOT_RUN, TZ, SUMMARY,
        title_chars=80, show_titles=show_titles, max_rows=max_rows, events=events,
        duplicates=duplicates,
    )  # fmt: skip


def test_ready_to_close_wording() -> None:
    assert finding_wording(ready(101)) == (
        "The help desk replied 4 days ago, no reply since: close if nothing new"
    )


def test_ready_to_close_after_a_desk_note_names_the_note() -> None:
    held = Finding(101, "ready_to_close", "low", "desk_note_last", 250, note_hours=192)

    assert finding_wording(held) == (
        "The help desk replied 10 days ago and added a note 8 days ago, no reply since: "
        "follow up or close"
    )
    assert finding_wording(held, COMMONS) == (
        "The Commons replied 10 days ago and added a note 8 days ago, no reply since: "
        "follow up or close"
    )


def test_a_details_confirmed_event_is_listed_without_a_time() -> None:
    _, card = extras_summary([waiting(101)], [week_event(102, 21, "details_confirmed")])

    lines = texts(card)
    assert lines[2] == "Events this week (1)"
    assert lines[3].endswith(
        ") · Wed 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · details confirmed"
    )


def test_the_summary_sections_are_in_the_check_in_order() -> None:
    kinds = [
        "ready_to_close",
        "event_over",
        "location_tbd_soon",
        "event_details_pending",
        "event_outreach_due",
        "needs_review",
        "follow_up_with_client",
        "client_waiting",
    ]
    findings = [Finding(100 + index, kind, "low", "x", 50, 50) for index, kind in enumerate(kinds)]

    _, card = extras_summary(findings, [week_event(150, 21)])

    sections = [line for line in texts(card) if line.endswith(")") and " (" in line[-5:]]
    assert sections == [
        "Events this week (1)",
        "Requester waiting (1)",
        "Follow up with requester (1)",
        "Someone else wrote last (1)",
        "Event: email the requester (1)",
        "Event: follow up with the requester (1)",
        "Event soon: no room yet (1)",
        "Event over: resolve (1)",
        "Ready to close (1)",
    ]


def test_events_this_week_rows_are_linked_with_details_and_counted_apart() -> None:
    events = [week_event(102, 21), week_event(103, 23, "in_progress")]

    shown, card = extras_summary([waiting(101)], events)

    lines = texts(card)
    assert lines[0] == "TDX queue at 7:30 AM: 1 ticket for you"  # events are not findings
    assert lines[2] == "Events this week (2)"
    assert lines[3].startswith("[#102](https://tdx.example.edu/")
    assert lines[3].endswith(
        ") · Wed 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · not emailed yet"
    )
    assert lines[4].endswith(") · Fri 10/23 3:00–5:00 PM · Lobby · A/V on form: yes · in progress")
    assert lines[5] == "Requester waiting (1)"
    assert [f.ticket_id for f in shown] == [101]


def test_events_this_week_dates_a_wait_from_eight_days_before_the_slot() -> None:
    # SLOT is Mon 10/19 7:30 AM: a wait since Sun 10/11 would read "since Sun", yesterday.
    asked = SLOT - timedelta(days=8)
    event = WeekEvent(102, datetime(2026, 10, 21, 15, 0, tzinfo=TZ), True, None, "lobby", True,
                      "waiting_on_requester", asked)  # fmt: skip

    _, card = extras_summary([], [event])

    assert texts(card)[3].endswith(" · waiting on requester since Sun 10/11 7:30 AM")


def test_an_event_keeps_its_finding_row_too() -> None:
    finding = Finding(102, "event_outreach_due", "high", "outreach_due", 48, 48)

    _, card = extras_summary([finding], [week_event(102, 21)])

    assert sum("#102](" in line for line in texts(card)) == 2


def test_events_show_titles_only_with_show_titles() -> None:
    _, plain = extras_summary([], [week_event(102, 21)])
    _, titled = extras_summary([], [week_event(102, 21)], show_titles=True)

    assert TITLES[102] not in json.dumps(plain)
    lines = texts(titled)
    assert lines[3].endswith(escape_markdown(TITLES[102]))
    assert lines[4] == "Wed 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · not emailed yet"


def test_events_past_the_cap_are_counted_as_more() -> None:
    events = [week_event(200 + day, day) for day in range(20, 26)]

    _, card = extras_summary([], events, max_rows=4)

    lines = texts(card)
    assert "Events this week (6)" in lines and "+2 more" in lines


def test_a_queue_with_events_but_no_findings_is_still_clear() -> None:
    _, card = extras_summary([], [week_event(102, 21)])

    assert texts(card)[0] == "Queue clear at 7:30 AM: nothing needs attention."
    assert "Events this week (1)" in texts(card)


def test_a_possible_duplicate_points_at_the_older_ticket() -> None:
    _, card = extras_summary([waiting(101), waiting(105)], duplicates={105: 101})
    _, titled = extras_summary([waiting(105)], duplicates={105: 101}, show_titles=True)

    (line,) = [line for line in texts(card) if "#105](" in line]
    assert ") (possible duplicate of #101) · The requester wrote last," in line
    assert not any("duplicate" in line for line in texts(card) if "#101](" in line)
    assert any("(possible duplicate of #101) Password reset" in line for line in texts(titled))


def test_the_summary_card_holds_no_names_or_free_text() -> None:
    events = [week_event(102, 21), week_event(104, 22, "location_tbd")]

    _, card = extras_summary([waiting(101), ready(105)], events, duplicates={105: 101})

    text = json.dumps(card)
    for secret in (*TITLES.values(), REQUESTOR, EMAIL, PRIM_RESP):
        assert secret not in text


def test_alertable_keeps_ready_to_close_out_unless_switched_on() -> None:
    findings = [waiting(101), ready(102)]

    assert alertable(findings, alert_ready_to_close=False) == (waiting(101),)
    assert alertable(findings, alert_ready_to_close=True) == tuple(findings)


# --- deltas -------------------------------------------------------------------------------

NOON = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)
NOON_RUN = datetime(2026, 10, 19, 12, 1, tzinfo=TZ)


def change(findings: list[Finding], previous: set[tuple[int, str]], **options: object):
    values: dict[str, object] = {
        "seen_ticket_ids": {f.ticket_id for f in findings},
        "unsettled_ticket_ids": set(),
        "queue_complete": True,
    }
    values.update(options)
    return summary_change(findings, previous, **values)


def test_summary_change_counts_new_and_cleared_against_the_last_post() -> None:
    previous = {(101, "client_waiting"), (102, "client_waiting")}
    result = change([waiting(101), waiting(103)], previous)

    assert result.new == {(103, "client_waiting")}
    assert result.cleared == {(102, "client_waiting")}
    assert result.open == {(101, "client_waiting"), (103, "client_waiting")}


def test_summary_change_keeps_findings_it_could_not_judge() -> None:
    previous = {(101, "client_waiting"), (102, "client_waiting")}

    # 101's feed failed this run, and 102 left a queue that was not read in full.
    result = change(
        [], previous, seen_ticket_ids={101}, unsettled_ticket_ids={101}, queue_complete=False
    )

    assert result.new == frozenset() and result.cleared == frozenset()
    assert result.open == previous


def delta(
    findings: list[Finding],
    new: set[tuple[int, str]],
    reminders: set[tuple[int, str]] = frozenset(),
    *,
    cleared: int = 0,
    total: int | None = None,
    show_titles: bool = False,
    max_rows: int = 25,
    **options: object,
) -> tuple[tuple[Finding, ...], dict]:
    rows_by_id = {f.ticket_id: row(f.ticket_id) for f in findings}
    links = card_links(
        rows_by_id, list(rows_by_id), TdxSettings(base_url="https://tdx.example.edu/")
    )
    return fit_delta_card(
        findings,
        new,
        reminders,
        cleared,
        len(findings) if total is None else total,
        rows_by_id,
        links,
        SLOT,
        NOON_RUN,
        TZ,
        SUMMARY,
        title_chars=80,
        show_titles=show_titles,
        max_rows=max_rows,
        **options,
    )


def test_a_delta_lists_new_findings_and_reminders_and_counts_cleared_ones() -> None:
    findings = [waiting(101), event_over(104, 200)]

    shown, card = delta(
        findings, {(101, "client_waiting")}, {(104, "event_over")}, cleared=2, total=6
    )

    lines = texts(card)
    assert lines[0] == "Since 7:30 AM: 1 more ticket and 1 reminder for you"
    assert lines[1] == (
        "6 tickets need attention in total: the 2 below, plus 4 posted earlier today. "
        "2 no longer need attention. Checked at 12:01 PM."
    )
    assert lines[2] == "Requester waiting (1)"
    assert lines[3].startswith("[#101](") and ") (added) · The requester wrote last," in lines[3]
    assert lines[4] == "Event over: resolve (1)"
    assert "(added)" not in lines[5] and lines[5].endswith("· reminder")
    assert len(lines) == 6  # no row for the cleared findings, no events section
    assert "Events this week" not in json.dumps(card)
    assert "entities" not in json.dumps(card)  # no mention on a delta
    assert card["fallbackText"] == "6 tickets need attention"
    assert [(f.ticket_id, f.kind) for f in shown] == [(101, "client_waiting"), (104, "event_over")]


@pytest.mark.parametrize(
    ("new", "reminders", "heading"),
    [
        (1, 0, "Since 7:30 AM: 1 more ticket for you"),
        (2, 0, "Since 7:30 AM: 2 more tickets for you"),
        (0, 1, "Since 7:30 AM: 1 reminder for you"),
        (1, 2, "Since 7:30 AM: 1 more ticket and 2 reminders for you"),
    ],
)
def test_the_update_heading_names_only_the_parts_it_has(
    new: int, reminders: int, heading: str
) -> None:
    assert delta_heading(new, reminders, "7:30 AM") == heading


@pytest.mark.parametrize(
    ("total", "shown", "cleared", "lead"),
    [
        (1, 1, 0, "1 ticket needs attention: the one below. Checked at 5:00 PM."),
        (2, 2, 0, "2 tickets need attention, both below. Checked at 5:00 PM."),
        (3, 3, 0, "3 tickets need attention, all below. Checked at 5:00 PM."),
        (
            4,
            2,
            0,
            "4 tickets need attention in total: the 2 below, plus 2 posted earlier today. "
            "Checked at 5:00 PM.",
        ),
        (
            3,
            1,
            0,
            "3 tickets need attention in total: the one below, plus 2 posted earlier today. "
            "Checked at 5:00 PM.",
        ),
        (
            1,
            1,
            1,
            "1 ticket needs attention: the one below. 1 no longer needs attention. "
            "Checked at 5:00 PM.",
        ),
        (
            5,
            2,
            2,
            "5 tickets need attention in total: the 2 below, plus 3 posted earlier today. "
            "2 no longer need attention. Checked at 5:00 PM.",
        ),
    ],
    ids=["one", "both", "all", "others-earlier", "one-and-others", "cleared-1", "cleared-2"],
)
def test_the_update_lead_counts_tickets_below_earlier_and_cleared(
    total: int, shown: int, cleared: int, lead: str
) -> None:
    assert delta_lead(total, shown, cleared, "5:00 PM") == lead


def test_the_update_counts_tickets_not_findings() -> None:
    """One ticket with two findings is one ticket in the heading and the lead."""
    findings = [waiting(101), Finding(101, "location_tbd_soon", "normal", "tbd_soon", 30, 30)]
    keys = {(f.ticket_id, f.kind) for f in findings}

    _, card = delta(findings, keys, total=1)

    assert texts(card)[:2] == [
        "Since 7:30 AM: 1 more ticket for you",
        "1 ticket needs attention: the one below. Checked at 12:01 PM.",
    ]


def test_an_urgent_finding_open_at_the_last_post_counts_as_a_reminder() -> None:
    urgent = Finding(102, "event_details_pending", "high", "details_pending_near", 20, 6)

    _, card = delta(
        [waiting(101), urgent],
        {(101, "client_waiting")},
        urgent_keys={(102, "event_details_pending")},
        total=4,
    )

    assert texts(card)[:2] == [
        "Since 7:30 AM: 1 more ticket and 1 reminder for you",
        "4 tickets need attention in total: the 2 below, plus 2 posted earlier today. "
        "Checked at 12:01 PM.",
    ]


def test_two_reminder_rows_on_one_ticket_are_one_reminder() -> None:
    keys = {(102, "event_details_pending"), (102, "location_tbd_soon")}

    _, card = delta([FOLLOW_UP_102, NO_ROOM_102], set(), keys, total=1)

    assert texts(card)[0] == "Since 7:30 AM: 1 reminder for you"


def test_an_added_row_and_a_reminder_on_one_ticket_count_once_as_added() -> None:
    _, card = delta(
        [FOLLOW_UP_102, NO_ROOM_102],
        {(102, "location_tbd_soon")},
        {(102, "event_details_pending")},
        total=1,
    )

    assert texts(card)[0] == "Since 7:30 AM: 1 more ticket for you"  # no reminder part


def test_delta_titles_follow_show_titles() -> None:
    _, card = delta([waiting(101)], {(101, "client_waiting")})
    text = json.dumps(card)
    for secret in (*TITLES.values(), REQUESTOR, EMAIL, PRIM_RESP):
        assert secret not in text
    _, titled = delta([waiting(101)], {(101, "client_waiting")}, show_titles=True)
    assert TITLES[101] in json.dumps(titled)


def test_delta_rows_past_the_cap_are_counted_as_more() -> None:
    findings = [waiting(100 + i) for i in range(5)]
    shown, card = delta(findings, {(f.ticket_id, f.kind) for f in findings}, max_rows=2)
    assert len(shown) == 2 and texts(card)[-1] == "+3 more"


def test_the_full_heading_says_added_since_the_last_post() -> None:
    previous_evening = datetime(2026, 10, 18, 17, 0, tzinfo=TZ)
    _, card = summary([waiting(101)], {(101, "client_waiting")}, since=previous_evening)
    assert texts(card)[0] == "TDX queue at 7:30 AM: 1 ticket for you (1 added since yesterday)"
    assert since_text(datetime(2026, 10, 16, 17, 0, tzinfo=TZ), SLOT, TZ) == "Fri 5:00 PM"
    assert since_text(SLOT, NOON, TZ) == "7:30 AM"
    # A week or more before the slot, the weekday alone would read as the past few days.
    assert since_text(datetime(2026, 10, 9, 17, 0, tzinfo=TZ), SLOT, TZ) == "Fri 10/9 5:00 PM"


# --- plain wording, per reader ----------------------------------------------------------

DETAILS = Finding(102, "event_details_pending", "high", "details_pending", 60, 100)
NEAR = Finding(103, "event_details_pending", "high", "details_pending_near", 20, 6)
EVERY_KIND = [
    waiting(101),
    waiting(105, reason="no_reply_yet"),
    Finding(106, "follow_up_with_client", "normal", "client_no_reply_needed", 80),
    Finding(107, "needs_review", "normal", "other_last", 100),
    Finding(108, "event_outreach_due", "high", "outreach_due", 50, 50),
    Finding(111, "event_outreach_due", "high", "outreach_due_requester_wrote", 30, 30, 50),
    waiting(112, reason="expects_reply"),
    Finding(113, "client_waiting", "high", "event_requester_replied", 50, 122),
    Finding(114, "follow_up_with_client", "normal", "client_follow_up", 90),
    DETAILS,
    NEAR,
    Finding(104, "location_tbd_soon", "normal", "tbd_soon", 30),
    event_over(109),
    ready(110),
]


def alert_card(reader: Reader, findings: list[Finding], tmp_path: Path) -> dict:
    result = plan(AlertState(tmp_path / f"state-{id(reader)}.json"), findings)
    rows = {f.ticket_id: row(f.ticket_id) for f in findings}
    return build_card(result, rows, {}, NOW, TZ, SUMMARY, title_chars=80, reader=reader)


def urgent_card(reader: Reader, findings: list[Finding]) -> dict:
    rows = {f.ticket_id: row(f.ticket_id) for f in findings}
    return fit_urgent_card(
        findings, rows, {}, SLOT_RUN, TZ, title_chars=80, show_titles=False, max_rows=25,
        reader=reader,
    )[1]  # fmt: skip


def every_card(reader: Reader, tmp_path: Path) -> list[dict]:
    """Each card builder's output for ``reader``, with every finding kind and row wording on
    it."""
    return [
        alert_card(reader, EVERY_KIND, tmp_path),
        summary(EVERY_KIND, reader=reader)[1],
        summary([], reader=reader)[1],
        delta(EVERY_KIND, {(f.ticket_id, f.kind) for f in EVERY_KIND}, reader=reader)[1],
        urgent_card(reader, [DETAILS, NEAR]),
    ]


@pytest.mark.parametrize(
    ("reader", "subtitle", "preview"),
    [
        (
            Reader(),
            "Checked Fri 2:05 PM: 26 unclaimed tickets, 7 ticket histories read.",
            "2 tickets need attention",
        ),
        (
            DESK,
            "The help desk, please follow up on these. Checked Fri 2:05 PM: 26 unclaimed tickets, "
            "7 ticket histories read.",
            "2 tickets for the help desk",
        ),
        (
            COMMONS,
            "The Commons, please follow up on these. Checked Fri 2:05 PM: 26 unclaimed tickets, "
            "7 ticket histories read.",
            "2 tickets for the Commons",
        ),
    ],
)
def test_alert_card_wording(reader: Reader, subtitle: str, preview: str, tmp_path: Path) -> None:
    card = alert_card(reader, [waiting(101), event_over(102)], tmp_path)
    assert texts(card)[:2] == ["TDX queue: 2 new", subtitle]
    assert card["fallbackText"] == preview


def test_alert_card_says_how_many_ticket_histories_could_not_be_read(tmp_path: Path) -> None:
    result = plan(AlertState(tmp_path / "state.json"), [waiting(101)])
    counts = {"unclaimed": 26, "feeds_read": 6, "feeds_failed": 1}
    card = build_card(result, {}, {}, NOW, TZ, counts, title_chars=80)
    assert texts(card)[1] == (
        "Checked Fri 2:05 PM: 26 unclaimed tickets, 6 ticket histories read, 1 could not be read."
    )


@pytest.mark.parametrize(
    ("reader", "who", "lead", "preview", "clear_lead", "clear_preview"),
    [
        (
            Reader(),
            "you",
            "Tickets that need your attention.",
            "2 tickets need attention",
            "Nothing needs your attention right now.",
            "Nothing needs your attention right now",
        ),
        (
            DESK,
            "the help desk",
            "Tickets for the help desk to look at.",
            "2 tickets for the help desk",
            "Nothing for the help desk to look at right now.",
            "Nothing for the help desk right now",
        ),
        (
            COMMONS,
            "the Commons",
            "Tickets for the Commons to look at.",
            "2 tickets for the Commons",
            "Nothing for the Commons to look at right now.",
            "Nothing for the Commons right now",
        ),
    ],
)
def test_full_summary_wording(
    reader: Reader, who: str, lead: str, preview: str, clear_lead: str, clear_preview: str
) -> None:
    _, card = summary([waiting(101), event_over(104)], {(101, "client_waiting")}, reader=reader)
    assert texts(card)[:2] == [
        f"TDX queue at 7:30 AM: 2 tickets for {who} (1 added)",
        f"{lead} Checked 26 unclaimed tickets at 7:31 AM.",
    ]
    assert card["fallbackText"] == preview

    _, clear = summary([], reader=reader)
    assert texts(clear)[:2] == [
        "Queue clear at 7:30 AM: nothing needs attention.",
        f"{clear_lead} Checked 26 unclaimed tickets at 7:31 AM.",
    ]
    assert clear["fallbackText"] == clear_preview


@pytest.mark.parametrize(
    ("reader", "who", "preview"),
    [
        (Reader(), "you", "6 tickets need attention"),
        (DESK, "the help desk", "6 tickets for the help desk"),
        (COMMONS, "the Commons", "6 tickets for the Commons"),
    ],
)
def test_delta_wording(reader: Reader, who: str, preview: str) -> None:
    _, card = delta([waiting(101)], {(101, "client_waiting")}, cleared=1, total=6, reader=reader)
    assert texts(card)[:2] == [
        f"Since 7:30 AM: 1 more ticket for {who}",
        "6 tickets need attention in total: the one below, plus 5 posted earlier today. "
        "1 no longer needs attention. Checked at 12:01 PM.",
    ]
    assert card["fallbackText"] == preview


@pytest.mark.parametrize(
    ("reader", "ask", "preview"),
    [
        (Reader(), "Please", "1 ticket needs attention"),
        (DESK, "The help desk, please", "1 ticket for the help desk"),
        (COMMONS, "The Commons, please", "1 ticket for the Commons"),
    ],
)
def test_urgent_card_wording(reader: Reader, ask: str, preview: str) -> None:
    card = urgent_card(reader, [NEAR])
    assert texts(card)[:2] == [
        "Event soon: 1 needs follow-up",
        f"{ask} follow up on these before the event. Checked Mon 7:31 AM.",
    ]
    assert card["fallbackText"] == preview
    assert texts(urgent_card(reader, [NEAR, DETAILS]))[0] == "Event soon: 2 need follow-up"


@pytest.mark.parametrize(
    ("reader", "replied"),
    [
        (Reader(), "The help desk replied 4 days ago"),
        (DESK, "The help desk replied 4 days ago"),
        (COMMONS, "The Commons replied 4 days ago"),
    ],
)
def test_row_wording_names_the_team_as_the_reader_does(reader: Reader, replied: str) -> None:
    assert finding_wording(ready(101), reader) == (
        f"{replied}, no reply since: close if nothing new"
    )
    # Wording without a team is the same for every reader: the requester went quiet, so
    # the details row names them, not the team.
    assert finding_wording(waiting(101), reader) == finding_wording(waiting(101))
    assert finding_wording(DETAILS, reader) == (
        "Event in 4 days. The requester hasn't answered for 2 days: follow up"
    )


def test_a_personal_reader_never_uses_the_desk_team_name() -> None:
    personal = Reader("personal", "the Commons")
    assert "Commons" not in finding_wording(DETAILS, personal)
    assert fallback_text(2, personal) == "2 tickets need attention"


def test_the_team_name_is_escaped_in_the_card_text_but_not_the_preview() -> None:
    reader = Reader("desk", "*Team* [one]")
    card = urgent_card(reader, [NEAR])
    assert texts(card)[1].startswith(r"\*Team\* \[one\], please follow up")
    assert card["fallbackText"] == "1 ticket for *Team* [one]"
    _, full = summary([waiting(101)], reader=reader)
    assert texts(full)[0] == r"TDX queue at 7:30 AM: 1 ticket for \*Team\* \[one\]"
    assert texts(full)[1].startswith(r"Tickets for \*Team\* \[one\] to look at. Checked ")
    _, update = delta([waiting(101)], {(101, "client_waiting")}, reader=reader)
    assert texts(update)[0] == r"Since 7:30 AM: 1 more ticket for \*Team\* \[one\]"
    assert full["fallbackText"] == "1 ticket for *Team* [one]"
    _, clear = summary([], reader=reader)
    assert texts(clear)[1].startswith(r"Nothing for \*Team\* \[one\] to look at right now.")
    assert clear["fallbackText"] == "Nothing for *Team* [one] right now"


def test_no_desk_card_builds_the_possessive_of_the_team_name(tmp_path: Path) -> None:
    # A possessive built from the name reads badly for some names ("the Commons's"), so
    # desk cards say "for the Commons" instead.
    for card in every_card(COMMONS, tmp_path):
        text = json.dumps(card, ensure_ascii=False)
        assert "Commons" in text
        assert "Commons's" not in text and "Commons'" not in text, card["fallbackText"]


@pytest.mark.parametrize("reader", [Reader(), DESK, COMMONS], ids=["personal", "desk", "commons"])
def test_no_card_says_read_only_audit_or_the_desk(reader: Reader, tmp_path: Path) -> None:
    for card in every_card(reader, tmp_path):
        text = json.dumps(card, ensure_ascii=False).lower()
        for banned in ("read-only audit", "the desk", "feeds read", "ops-suite"):
            assert banned not in text, (banned, card["body"][0]["text"])


@pytest.mark.parametrize(
    ("reader", "team", "mailbox"),
    [
        (Reader(), "the help desk", "our shared mailbox"),
        (COMMONS, "the Commons", "our shared mailbox"),
        (Reader(alt_host_mailbox=MAILBOX), "the help desk", MAILBOX),
        (Reader("desk", "the Commons", MAILBOX), "the Commons", MAILBOX),
    ],
    ids=["personal", "commons", "personal-mailbox", "commons-mailbox"],
)
def test_rows_call_the_person_the_requester_on_every_card(
    reader: Reader, team: str, mailbox: str
) -> None:
    ask = (
        f"the room number. If it's hybrid or remote, also ask for the Zoom link, ask them to add "
        f"{mailbox} as an alternative host, and ask whether to record. Then update the events "
        "spreadsheet."
    )
    expected = {
        waiting(1, 73): "The requester wrote last, 3 days ago: reply or check in",
        waiting(1, 73, "expects_reply"): "The requester has waited 3 days for a reply",
        Finding(1, "client_waiting", "normal", "event_requester_replied", 50, 122): (
            "Event in 5 days. The requester replied 2 days ago: read it and confirm the details"
        ),
        Finding(1, "follow_up_with_client", "normal", "client_follow_up", 50): (
            "The requester said they'd follow up; quiet 2 days: check in"
        ),
        Finding(1, "follow_up_with_client", "normal", "client_no_reply_needed", 96): (
            "The requester's last message needed no reply; quiet 4 days: check in or resolve"
        ),
        Finding(1, "needs_review", "normal", "other_last", 48): (
            f"The last message, 2 days ago, came from someone other than the requester or "
            f"{team}: check whether the requester still needs a reply"
        ),
        Finding(1, "event_outreach_due", "normal", "outreach_due", 240, 240): (
            f"Event in 10 days. Email the requester for {ask}"
        ),
        Finding(1, "event_outreach_due", "high", "outreach_due_requester_wrote", 30, 30, 50): (
            f"Event in 30 hours. The requester wrote 2 days ago. Email them for {ask}"
        ),
        Finding(1, "event_details_pending", "normal", "details_pending", 72, 100): (
            "Event in 4 days. The requester hasn't answered for 3 days: follow up"
        ),
        Finding(1, "event_details_pending", "high", "details_pending_near", 1, 26): (
            "Event in 26 hours. Still waiting on the requester (asked 1 hour ago): follow up now"
        ),
        Finding(1, "event_details_pending", "high", "details_room_set", 96, 70): (
            "Event in 2 days. The room is set; confirm the Zoom link and recording if they're "
            "still open"
        ),
        Finding(1, "location_tbd_soon", "normal", "tbd_soon", 30): (
            "Event in 30 hours. Location still TBD on the ticket. Check the events spreadsheet; "
            "if a room is listed, update the ticket."
        ),
        Finding(1, "location_tbd_soon", "normal", "location_missing", 30): (
            "Event in 30 hours. No room on the ticket yet. Check the events spreadsheet; if a "
            "room is listed, update the ticket."
        ),
        Finding(1, "client_waiting", "normal", "event_requester_replied", 50, 122): (
            "Event in 5 days. The requester replied 2 days ago: read it and confirm the details"
        ),
    }
    for finding, wording in expected.items():
        assert finding_wording(finding, reader) == wording


def test_the_reader_names_the_configured_mailbox_or_our_shared_mailbox() -> None:
    assert Reader().mailbox == "our shared mailbox"
    assert DESK.mailbox == "our shared mailbox"
    assert Reader("desk", "the Commons", MAILBOX).mailbox == MAILBOX
    assert Reader(alt_host_mailbox=MAILBOX).mailbox == MAILBOX


@pytest.mark.parametrize("audience", ["personal", "desk"])
def test_the_outreach_mailbox_is_escaped_for_card_markdown(audience: Audience) -> None:
    reader = Reader(audience, alt_host_mailbox="desk_events*av@example.edu")
    finding = Finding(1, "event_outreach_due", "normal", "outreach_due", 240, 240)

    wording = finding_wording(finding, reader)

    assert r"ask them to add desk\_events\*av@example.edu as an alternative host" in wording


@pytest.mark.parametrize(
    "reader", [Reader(alt_host_mailbox=MAILBOX), Reader("desk", "the Commons", MAILBOX)],
    ids=["personal", "desk"],
)  # fmt: skip
def test_cards_show_the_configured_mailbox_on_outreach_rows(
    reader: Reader, state: AlertState
) -> None:
    findings = [Finding(101, "event_outreach_due", "high", "outreach_due", 50, 50)]

    card = build_card(
        plan(state, findings), {}, {}, NOW, TZ, SUMMARY, title_chars=80, reader=reader
    )

    text = json.dumps(card, ensure_ascii=False)
    assert f"ask them to add {MAILBOX} as an alternative host" in text
    assert "our shared mailbox" not in text


def test_requester_section_labels(state: AlertState) -> None:
    findings = [waiting(101), Finding(102, "follow_up_with_client", "normal", "x", 90)]
    lines = texts(build_card(plan(state, findings), {}, {}, NOW, TZ, SUMMARY, title_chars=80))
    assert "Requester waiting (1)" in lines and "Follow up with requester (1)" in lines


@pytest.mark.parametrize(
    ("reminders", "phrase"), [(0, "12 new"), (1, "12 new, 1 reminder"), (2, "12 new, 2 reminders")]
)
def test_counts_phrase_leaves_out_zero_reminders(reminders: int, phrase: str) -> None:
    assert counts_phrase(12, reminders) == phrase


@pytest.mark.parametrize("reader", [Reader(), DESK, COMMONS], ids=["personal", "desk", "commons"])
def test_no_card_uses_another_word_for_the_requester(reader: Reader, tmp_path: Path) -> None:
    # Cards use one word for the person who opened the ticket, and never shorthand for the
    # event checklist.
    banned = re.compile(r"Requestor|\bclient\b|alt\s+host|desk\s+mailbox", re.IGNORECASE)
    for card in every_card(reader, tmp_path):
        text = json.dumps(card, ensure_ascii=False)
        assert not banned.search(text), (banned.search(text), card["body"][0]["text"])
