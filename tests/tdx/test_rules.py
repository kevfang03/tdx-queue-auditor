from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.event_form import EventForm
from ops_suite.tdx.feed_types import EntryKind, FeedEntry, TicketFeed
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import TicketRow, WidgetKind
from ops_suite.tdx.rules import (
    COUNT_KEYS,
    FINDING_KINDS,
    AuditResult,
    EventRead,
    EventStatus,
    Finding,
    RuleSettings,
    apply_verdicts,
    evaluate,
    event_status,
    format_audit_summary,
    is_follow_up_candidate,
    plan_feeds,
    strong_desk_names,
)

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)
RULES = RuleSettings()

REQUESTOR = "Alex Example"
DESK = "Pat Helpdesk"
OTHER = "Casey Testperson"
EXTRA_STAFF = "Morgan Fixture"
# Unclaimed-only runs have no Prim Resp names, so the desk member comes from the local list.
STAFF = RuleSettings(extra_staff_names=(DESK,))
TITLE = "Laptop will not connect"
URL = "https://tdx.example.edu/TDNext/Apps/1/Tickets/TicketDet?TicketID=1"
# Invented strings from the test data that must never show up in output or repr().
INVENTED = [
    REQUESTOR,
    DESK,
    OTHER,
    EXTRA_STAFF,
    TITLE,
    "Zoom recording",
    "example.edu",
    "Room 101",
    "someday",
    "Sam Synthetic",
    "Jordan Thirdparty",
    "Dana Desk",
    "Max Manager",
    "Pat Private",
    "Mo Memory",
    "Tess Twice",
]


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def row(
    ticket_id: int,
    widget: WidgetKind = "claimed",
    *,
    title: str = TITLE,
    requestor: str = REQUESTOR,
    created: datetime | None = None,
    modified: datetime | None = None,
    prim_resp: str | None = DESK,
) -> TicketRow:
    return TicketRow(
        widget=widget,
        ticket_id=ticket_id,
        title=title,
        requestor=requestor,
        resp_group="Example Group",
        created=created if created is not None else ago(500),
        modified=modified if widget == "claimed" else None,
        prim_resp=prim_resp if widget == "claimed" else None,
        status="Open" if widget == "claimed" else None,
        detail_url=URL,
    )


def unclaimed(
    ticket_id: int, created: datetime | None, title: str = TITLE, *, requestor: str = REQUESTOR
) -> TicketRow:
    row_ = row(ticket_id, "unclaimed", title=title, requestor=requestor)
    return TicketRow(**{**_fields(row_), "created": created})


def _fields(r: TicketRow) -> dict:
    return {name: getattr(r, name) for name in TicketRow.__slots__}


def entry(author: str, hours_ago: float | None, kind: EntryKind = "reply") -> FeedEntry:
    return FeedEntry(
        author=author, created=ago(hours_ago) if hours_ago is not None else None, kind=kind
    )


def feed(ticket_id: int, *entries: FeedEntry) -> TicketFeed:
    return TicketFeed(ticket_id=ticket_id, entries=entries)


def run(
    rows: list[TicketRow],
    feeds: dict[int, TicketFeed] | None = None,
    now: datetime = NOW,
    rules: RuleSettings = RULES,
    known_desk: Callable[[str], bool] | None = None,
) -> AuditResult:
    return evaluate(rows, feeds or {}, now, rules, TZ, known_desk=known_desk)


def only(result: AuditResult) -> Finding:
    assert len(result.findings) == 1, result.findings
    return result.findings[0]


# --- claimed: counted only ---------------------------------------------------------------


def test_claimed_rows_give_no_finding_and_no_plan_entry() -> None:
    rows = [
        row(1, modified=ago(500)),
        row(2, modified=None),
        row(3, modified=ago(1)),
        row(4, title="EVENT [10/1 2pm Room 101] Zoom recording", modified=ago(200)),
        row(5, title="EVENT [10/9 TBD] Zoom recording", modified=ago(200)),
    ]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()
    # Even a feed showing the requestor waiting changes nothing for a claimed ticket.
    result = run(rows, {1: feed(1, entry(REQUESTOR, 300))})
    assert result.findings == ()
    assert result.counts["rows_claimed"] == 5
    for key in ("feeds_needed", "feed_missing", "event_tickets", "event_date_unreadable"):
        assert result.counts[key] == 0


def test_claimed_prim_resp_names_still_mark_desk_members() -> None:
    rows = [unclaimed(1, ago(60)), row(2, modified=ago(1), prim_resp=DESK)]
    result = run(rows, {1: feed(1, entry("helpdesk, pat", 50), entry(REQUESTOR, 55))})
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1
    assert result.counts["desk_members_known"] == 1


# --- unclaimed: grid ---------------------------------------------------------------------


def test_unclaimed_recent() -> None:
    rows = [unclaimed(1, ago(47.9))]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()
    result = run(rows)
    assert result.findings == ()
    assert result.counts["unclaimed_recent"] == 1
    assert result.counts["feeds_needed"] == 0


def test_unclaimed_stale_or_undated_needs_a_feed() -> None:
    rows = [unclaimed(1, ago(48)), unclaimed(2, None)]
    assert set(plan_feeds(rows, NOW, RULES).feed_ticket_ids) == {1, 2}
    result = run(rows)
    assert result.counts["feeds_needed"] == 2
    assert result.counts["unclaimed_created_unreadable"] == 1
    # Being unclaimed for 48 hours is no longer a finding on its own.
    assert result.findings == ()


def test_plan_orders_by_created_with_unknown_first() -> None:
    rows = [
        unclaimed(10, ago(50)),
        unclaimed(11, ago(300)),
        unclaimed(12, None),
        unclaimed(13, ago(1)),
        unclaimed(14, ago(100)),
        unclaimed(11, ago(300)),  # repeated across a page turn
        unclaimed(15, ago(400), title="EVENT [10/1] Zoom recording"),
        row(16, modified=ago(400)),
    ]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == (12, 11, 14, 10)


def test_stale_hours_is_configurable() -> None:
    rows = [unclaimed(1, ago(25))]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()
    assert plan_feeds(rows, NOW, RuleSettings(stale_hours=24)).feed_ticket_ids == (1,)


# --- unclaimed: feeds --------------------------------------------------------------------


def test_feed_missing() -> None:
    result = run([unclaimed(1, ago(60))])
    assert result.findings == ()
    assert result.counts["feeds_needed"] == 1
    assert result.counts["feed_missing"] == 1


def test_no_visible_entries_is_client_waiting_aged_from_created() -> None:
    entries = (entry(DESK, 70, "private"), entry(DESK, 80, "system"))
    result = run([unclaimed(1, ago(90.2))], {1: feed(1, *entries)})
    assert only(result) == Finding(1, "client_waiting", "high", "no_reply_yet", 90)


def test_no_visible_entries_and_no_created() -> None:
    result = run([unclaimed(1, None)], {1: feed(1)})
    assert only(result) == Finding(1, "client_waiting", "high", "no_reply_yet", None)


def test_requestor_last_and_stale_is_client_waiting() -> None:
    entries = (entry(REQUESTOR, 73), entry(DESK, 100))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)}, rules=STAFF)
    assert only(result) == Finding(1, "client_waiting", "high", "requestor_last", 73)


def test_requestor_matched_leniently() -> None:
    entries = (entry("Example, Alex Q.", 73),)
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)})
    assert only(result).reason == "requestor_last"


def test_private_note_after_the_client_does_not_count() -> None:
    entries = (entry(DESK, 2, "private"), entry(DESK, 3, "system"), entry(REQUESTOR, 73))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)})
    assert only(result).reason == "requestor_last"


def test_requestor_last_undated_needs_review() -> None:
    result = run([unclaimed(1, ago(120))], {1: feed(1, entry(REQUESTOR, None))})
    assert only(result) == Finding(1, "needs_review", "normal", "requestor_last_undated", None)


def test_requestor_last_recent() -> None:
    entries = (entry(REQUESTOR, 10), entry(DESK, 100))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)}, rules=STAFF)
    assert result.findings == ()
    assert result.counts["client_recent"] == 1


def test_the_desk_replying_without_claiming_gives_nothing() -> None:
    entries = (entry("helpdesk, pat", 70), entry(REQUESTOR, 100))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)}, rules=STAFF)
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1


def test_desk_member_from_extra_staff_names() -> None:
    rows = [unclaimed(1, ago(120))]
    feeds = {1: feed(1, entry(EXTRA_STAFF, 70))}
    assert only(run(rows, feeds)).reason == "other_last"
    result = run(rows, feeds, rules=RuleSettings(extra_staff_names=(EXTRA_STAFF,)))
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1


def test_a_private_note_author_counts_as_desk() -> None:
    rows = [unclaimed(1, ago(120))]
    feeds = {1: feed(1, entry(OTHER, 70), entry(OTHER, 90, "private"))}
    result = run(rows, feeds)
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1


def test_a_private_note_author_on_one_ticket_is_desk_on_another() -> None:
    # OTHER opened ticket 2 themselves (so is its requestor there, which the two-ticket rule
    # leaves out) and wrote a private note on it; only staff can.
    rows = [unclaimed(1, ago(120)), unclaimed(2, ago(120), requestor=OTHER)]
    feeds = {
        1: feed(1, entry(OTHER, 70), entry(REQUESTOR, 100)),
        2: feed(2, entry(OTHER, 90, "private")),
    }
    result = run(rows, feeds)
    assert result.counts["desk_replied_last"] == 1
    # Ticket 2 has no visible entry at all, and a desk member opened it: internal (06c).
    assert result.findings == ()
    assert result.counts["internal_requestor"] == 1
    without_the_note = run(rows, {1: feeds[1], 2: feed(2)})
    assert [f.reason for f in without_the_note.findings] == ["no_reply_yet", "other_last"]


def test_a_non_requestor_on_two_tickets_is_desk_but_on_one_needs_review() -> None:
    one = [unclaimed(1, ago(120))]
    assert only(run(one, {1: feed(1, entry(OTHER, 70))})).reason == "other_last"

    rows = [*one, unclaimed(2, ago(120), requestor="Sam Synthetic")]
    feeds = {
        1: feed(1, entry(OTHER, 70)),
        2: feed(2, entry("Sam Synthetic", 60), entry("testperson, casey", 80)),
    }
    result = run(rows, feeds)
    assert result.counts["desk_replied_last"] == 1  # ticket 1
    assert result.counts["client_recent"] == 0
    assert [(f.ticket_id, f.reason) for f in result.findings] == [(2, "requestor_last")]
    assert result.counts["desk_members_known"] == 1


def test_the_requestor_on_two_tickets_does_not_become_desk() -> None:
    rows = [unclaimed(1, ago(120)), unclaimed(2, ago(120))]  # both from REQUESTOR
    feeds = {1: feed(1, entry(REQUESTOR, 70)), 2: feed(2, entry(REQUESTOR, 75))}
    result = run(rows, feeds)
    assert [f.reason for f in result.findings] == ["requestor_last", "requestor_last"]
    assert result.counts["desk_members_known"] == 0


def test_desk_members_are_counted_once_per_person() -> None:
    rows = [unclaimed(1, ago(120)), row(2, modified=ago(1)), row(3, modified=ago(1))]
    feeds = {1: feed(1, entry("Helpdesk, Pat", 70, "private"))}
    result = run(rows, feeds, rules=RuleSettings(extra_staff_names=("pat helpdesk",)))
    assert result.counts["desk_members_known"] == 1


def test_staff_who_opened_their_own_ticket_counts_as_the_requestor() -> None:
    rows = [unclaimed(1, ago(120), requestor=DESK), row(2, modified=ago(1))]
    keep = RuleSettings(skip_desk_requestors=False)
    result = run(rows, {1: feed(1, entry(DESK, 73))}, rules=keep)
    assert only(result).reason == "requestor_last"


# --- events (unclaimed) ------------------------------------------------------------------


def test_event_over_gives_only_event_over() -> None:
    # 10/6 2pm was 46 hours before NOW; the ticket is old and would otherwise need a feed.
    rows = [unclaimed(1, ago(300), title="EVENT [10/6 2pm Room 101] Zoom recording")]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()
    result = run(rows, {1: feed(1, entry(REQUESTOR, 200))})
    assert only(result) == Finding(1, "event_over", "low", "event_over", 46)
    assert result.counts["event_tickets"] == 1
    assert result.counts["feeds_needed"] == 0


def test_event_without_a_time_is_over_a_day_after_its_end_of_day() -> None:
    # 10/7 counts as 10/7 23:59: 12h01m before NOW, not yet 24 hours over.
    assert run([unclaimed(1, ago(5), title="EVENT 10/7 Zoom recording")]).findings == ()
    # 10/6 23:59 is 36 hours before NOW.
    assert only(run([unclaimed(1, ago(5), title="EVENT 10/6 Zoom recording")])).age_hours == 36


def test_event_just_under_event_over_hours_is_quiet() -> None:
    assert run([unclaimed(1, ago(5), title="EVENT 10/7 1pm Zoom recording")]).findings == ()


def test_event_location_tbd_soon() -> None:
    result = run([unclaimed(1, ago(60), title="EVENT [10/10 2pm TBD] Zoom recording")])
    assert only(result) == Finding(1, "location_tbd_soon", "normal", "tbd_soon", 50)


def test_event_location_tbd_not_yet_soon() -> None:
    # 10/12 noon is 96 hours away, more than 3 days.
    assert run([unclaimed(1, ago(5), title="EVENT [10/12 noon tbd] Zoom recording")]).findings == ()


def test_event_location_tbd_exactly_three_days_away() -> None:
    result = run([unclaimed(1, ago(5), title="EVENT [10/11 noon tbd] Zoom recording")])
    assert only(result).age_hours == 72


def test_event_location_tbd_in_the_past_is_not_soon() -> None:
    assert run([unclaimed(1, ago(5), title="EVENT [10/8 11am TBD] Zoom recording")]).findings == ()


def test_event_date_unreadable() -> None:
    result = run([unclaimed(1, ago(100), title="EVENT [date TBD] Zoom recording")])
    assert result.findings == ()
    assert result.counts["event_date_unreadable"] == 1
    assert result.event_date_unreadable_ids == (1,)


def test_a_recurring_event_is_counted_and_gives_no_finding() -> None:
    rows = [unclaimed(1, ago(300), title="EVENT [Bi-Weekly, Mon/Wed 2-3pm TBD] Zoom recording")]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()
    result = run(rows)
    assert result.findings == ()
    assert result.counts["event_tickets"] == 1
    assert result.counts["event_recurring"] == 1
    assert result.counts["event_date_unreadable"] == 0
    assert result.event_date_unreadable_ids == ()


def test_unclaimed_cci_form_follows_the_normal_rules() -> None:
    rows = [unclaimed(1, ago(60), title="CCI Event Request Form")]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == (1,)
    assert run(rows).counts["event_tickets"] == 0


# --- events with several dates -----------------------------------------------------------


def test_a_multi_date_event_is_not_over_while_a_later_date_is_ahead() -> None:
    title = "EVENT [10/1, 10/20, 10/27 3:00 PM - 5:00 PM Room 101] Zoom recording"
    result = run([unclaimed(1, ago(300), title=title)])
    assert result.findings == ()
    assert result.counts["event_multi_date"] == 1


def test_a_multi_date_event_is_over_a_day_after_its_last_date() -> None:
    # 10/4 2pm, the last date, was 94 hours before NOW; 9/20 was far longer ago.
    title = "EVENT [9/20, 9/27, 10/4 2pm Room 101] Zoom recording"
    result = run([unclaimed(1, ago(300), title=title)])
    assert only(result) == Finding(1, "event_over", "low", "event_over", 94)


def test_a_multi_date_event_whose_last_date_was_under_a_day_ago_is_quiet() -> None:
    title = "EVENT [10/1, 10/7 1pm Room 101] Zoom recording"
    assert run([unclaimed(1, ago(300), title=title)]).findings == ()


def test_location_tbd_counts_down_to_the_next_upcoming_date() -> None:
    # 10/1 is past; 10/10 2pm is 50 hours away.
    title = "EVENT [10/1, 10/10, 10/17 2pm TBD] Zoom recording"
    result = run([unclaimed(1, ago(300), title=title)])
    assert only(result) == Finding(1, "location_tbd_soon", "normal", "tbd_soon", 50)


def test_location_tbd_is_skipped_when_every_date_is_past() -> None:
    title = "EVENT [10/1, 10/5 2pm TBD] Zoom recording"
    result = run([unclaimed(1, ago(300), title=title)])
    assert only(result) == Finding(1, "event_over", "low", "event_over", 70)


def test_event_multi_date_counts_event_tickets_with_two_or_more_dates() -> None:
    rows = [
        unclaimed(1, ago(5), title="EVENT [10/20, 10/27 3pm Room 101] Zoom recording"),
        unclaimed(2, ago(5), title="EVENT Oct 20-22 Zoom recording"),
        unclaimed(3, ago(5), title="EVENT [10/20 3pm Room 101] Zoom recording"),
        unclaimed(4, ago(5), title="EVENT [10/20, 10/20] Zoom recording"),  # one date
        unclaimed(5, ago(5), title="EVENT Weekly, 10/20, 10/27 Zoom recording"),
        row(6, title="EVENT [10/20, 10/27] Zoom recording", modified=ago(1)),
    ]
    result = run(rows)
    assert result.counts["event_multi_date"] == 2
    assert result.counts["event_tickets"] == 5


# --- time --------------------------------------------------------------------------------


def test_age_across_a_dst_change_is_in_real_hours() -> None:
    # Clocks go back at 2am on Sunday 2026-11-01 in New York: 2026-10-31 13:00 EDT to
    # 2026-11-02 12:00 EST is 47 hours on the wall clock but 48 real hours.
    now = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
    created = datetime(2026, 10, 31, 13, 0, tzinfo=TZ)
    rows = [unclaimed(1, created)]
    assert plan_feeds(rows, now, RULES).feed_ticket_ids == (1,)
    result = run(rows, {1: feed(1)}, now=now)
    assert only(result).age_hours == 48


def test_dates_in_another_zone_compare_by_instant() -> None:
    created = ago(49).astimezone(ZoneInfo("UTC"))
    assert only(run([unclaimed(1, created)], {1: feed(1)})).age_hours == 49


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        run([unclaimed(1, ago(60))], now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="aware"):
        plan_feeds([], NOW.replace(tzinfo=None), RULES)


# --- result shape ------------------------------------------------------------------------


def test_finding_sort_order() -> None:
    rows = [
        unclaimed(5, ago(60), title="EVENT [10/10 2pm TBD] Zoom recording"),
        unclaimed(4, ago(90)),
        unclaimed(3, ago(90)),
        unclaimed(2, ago(60)),
        unclaimed(1, None),
        unclaimed(6, ago(60)),
        unclaimed(7, ago(5), title="EVENT 10/1 Zoom recording"),
    ]
    feeds = {
        4: feed(4, entry("Jordan Thirdparty", 80.5)),
        3: feed(3, entry(OTHER, 80.2)),
        2: feed(2, entry(REQUESTOR, 50)),
        1: feed(1),
        6: feed(6, entry(REQUESTOR, None)),
    }
    order = [(f.priority, f.ticket_id, f.age_hours) for f in run(rows, feeds).findings]
    assert order == [
        ("high", 2, 50),
        ("high", 1, None),
        ("normal", 3, 80),  # same age: lower ticket id first
        ("normal", 4, 80),
        ("normal", 5, 50),
        ("normal", 6, None),
        ("low", 7, 156),
    ]


def test_every_count_key_is_present() -> None:
    expected = {
        "rows_unclaimed",
        "rows_claimed",
        "unclaimed_recent",
        "unclaimed_created_unreadable",
        "feeds_needed",
        "feed_missing",
        "client_recent",
        "desk_replied_last",
        "internal_requestor",
        "other_recent",
        "event_tickets",
        "event_recurring",
        "event_multi_date",
        "event_date_unreadable",
        "event_forms",
        "event_forms_unreadable",
        "event_forms_recurring",
        "event_outreach_done",
        "event_outreach_not_due",
        "event_marketing_only",
        "event_no_outreach",
        "event_form_missing",
        "event_renamed_with_form",
        "event_in_progress",
        "event_location_missing",
        "desk_members_known",
        "client_waiting",
        "follow_up_with_client",
        "event_outreach_due",
        "event_details_pending",
        "event_over",
        "location_tbd_soon",
        "needs_review",
        "ready_to_close",
    }
    assert set(COUNT_KEYS) == expected
    assert set(FINDING_KINDS) == {
        "client_waiting",
        "follow_up_with_client",
        "event_outreach_due",
        "event_details_pending",
        "event_over",
        "location_tbd_soon",
        "needs_review",
        "ready_to_close",
    }
    assert dict(run([]).counts) == dict.fromkeys(expected, 0)
    assert set(run([unclaimed(1, ago(60))]).counts) == expected


def test_counts_cannot_be_changed() -> None:
    with pytest.raises(TypeError):
        run([]).counts["client_waiting"] = 5  # type: ignore[index]


def test_duplicate_rows_count_once() -> None:
    result = run([unclaimed(1, ago(60)), unclaimed(1, ago(60))], {1: feed(1)})
    assert result.counts["rows_unclaimed"] == 1
    assert len(result.findings) == 1


def test_rule_settings_repr_shows_no_name() -> None:
    text = repr(RuleSettings(extra_staff_names=("Alex Example",)))
    assert "Alex" not in text and "Example" not in text
    assert "stale_hours=48" in text
    assert "1 names" in text
    assert "skip_desk_requestors=True" in text


# --- summary -----------------------------------------------------------------------------


def mixed_run() -> AuditResult:
    rows = [
        unclaimed(1001, ago(80)),
        unclaimed(1002, ago(80)),
        unclaimed(1003, ago(80)),
        unclaimed(1004, ago(80)),
        unclaimed(1005, ago(5)),
        unclaimed(1006, ago(100), title="EVENT [10/9 TBD] Zoom recording"),
        unclaimed(1007, ago(100), title="EVENT [10/1 2pm Room 101] Zoom recording"),
        unclaimed(1008, ago(100), title="EVENT [someday] Zoom recording"),
        unclaimed(1009, ago(100), title="EVENT Weekly Zoom recording"),
        unclaimed(1010, ago(100), title="EVENT [10/1, 10/20 2pm Room 101] Zoom recording"),
        row(2001, modified=ago(80)),
        row(2002, modified=ago(3)),
    ]
    feeds = {
        1001: feed(1001, entry(REQUESTOR, 73)),
        1002: feed(1002, entry(DESK, 70)),
        1003: feed(1003, entry(OTHER, 75)),
    }
    return run(rows, feeds)


def test_summary_default_lines() -> None:
    assert format_audit_summary(mixed_run(), show_ids=False) == [
        "Findings: client waiting 1, follow up with client 0, event outreach due 0, "
        "event details pending 0, event over 1, location TBD soon 1, needs review 1, "
        "ready to close 0.",
        "Checked: 10 unclaimed (6 feeds needed, 3 missing), 2 claimed (counted only). "
        "Events: 5, 1 recurring, 1 multi-date, 1 with an unreadable date, 0 without a location. "
        "Form-titled tickets: 0 (0 recurring, 0 unreadable); renamed: 0 with a form, 0 without. "
        "Event states (no finding): 0 waiting on requester, 0 in progress, "
        "0 outreach not due yet, 0 marketing only, 0 no outreach. "
        "Desk members known: 1.",
    ]


def test_summary_with_ids() -> None:
    lines = format_audit_summary(mixed_run(), show_ids=True)
    assert lines[2:] == [
        "  client_waiting  1001  requestor_last  73h",
        "  needs_review  1003  other_last  75h",
        "  location_tbd_soon  1006  tbd_soon  35h",
        "  event_over  1007  event_over  166h",
        "  event_date_unreadable  1008",
    ]


def test_summary_line_without_an_age() -> None:
    result = run([unclaimed(1, ago(60))], {1: feed(1, entry(REQUESTOR, None))})
    assert format_audit_summary(result, show_ids=True)[2] == (
        "  needs_review  1  requestor_last_undated"
    )


def test_no_personal_data_in_any_output() -> None:
    result = mixed_run()
    rules = RuleSettings(extra_staff_names=(EXTRA_STAFF,))
    texts = [
        *format_audit_summary(result, show_ids=True),
        *format_audit_summary(result, show_ids=False),
        repr(result),
        repr(rules),
        repr(plan_feeds([unclaimed(1, ago(60))], NOW, rules)),
    ]
    for text in texts:
        for invented in INVENTED:
            assert invented not in text


# --- merged tickets ----------------------------------------------------------------------


def test_a_carried_over_requestor_reply_older_than_a_desk_reply_is_not_waiting() -> None:
    # Ticket 2 was merged into ticket 1: Alex's older reply came over with it.
    entries = (entry(DESK, 45, "system"), entry(DESK, 70), entry(REQUESTOR, 100))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)}, rules=STAFF)
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1


def test_a_carried_over_reply_from_another_client_needs_review() -> None:
    entries = (entry("Jordan Carryover", 60), entry(REQUESTOR, 100))
    result = run([unclaimed(1, ago(120))], {1: feed(1, *entries)}, rules=STAFF)
    assert only(result).reason == "other_last"


def test_a_merge_notice_author_counts_as_desk() -> None:
    merged = TicketFeed(1, (entry(DESK, 50, "system"), entry(DESK, 70)), desk_authors=(DESK,))
    result = run([unclaimed(1, ago(120))], {1: merged})
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1
    assert result.counts["desk_members_known"] == 1


def test_a_reassignment_author_counts_as_desk_on_another_ticket() -> None:
    # The reader passes a reassignment's author on in desk_authors, as for a merge notice.
    # OTHER is ticket 1's requestor, so the two-ticket rule leaves ticket 1 out: only
    # desk_authors can make OTHER a desk member here.
    rows = [unclaimed(1, ago(120), requestor=OTHER), unclaimed(2, ago(120))]
    ticket_2 = feed(2, entry(OTHER, 70), entry(REQUESTOR, 100))
    reassigned = TicketFeed(1, (entry(OTHER, 50, "system"),), desk_authors=(OTHER,))

    result = run(rows, {1: reassigned, 2: ticket_2})
    assert result.counts["desk_replied_last"] == 1
    # Ticket 1 was opened by a desk member, so it is internal.
    assert result.findings == ()
    assert result.counts["internal_requestor"] == 1

    without = run(rows, {1: TicketFeed(1, (entry(OTHER, 50, "system"),)), 2: ticket_2})
    assert sorted(f.reason for f in without.findings) == ["no_reply_yet", "other_last"]


# --- the 48-hour gate on other_last ------------------------------------------------------


def test_a_recent_reply_by_someone_else_is_counted_not_flagged() -> None:
    result = run([unclaimed(1, ago(120))], {1: feed(1, entry(OTHER, 1), entry(REQUESTOR, 100))})
    assert result.findings == ()
    assert result.counts["other_recent"] == 1
    assert result.counts["needs_review"] == 0


@pytest.mark.parametrize("hours", [48, 49, 200])
def test_a_reply_by_someone_else_48_hours_old_needs_review(hours: float) -> None:
    result = run([unclaimed(1, ago(300))], {1: feed(1, entry(OTHER, hours))})
    assert only(result) == Finding(1, "needs_review", "normal", "other_last", hours)
    assert result.counts["other_recent"] == 0


def test_an_undated_reply_by_someone_else_still_needs_review() -> None:
    result = run([unclaimed(1, ago(120))], {1: feed(1, entry(OTHER, None))})
    assert only(result) == Finding(1, "needs_review", "normal", "other_last", None)
    assert result.counts["other_recent"] == 0


def test_the_gate_leaves_requestor_last_alone() -> None:
    stale = run([unclaimed(1, ago(120))], {1: feed(1, entry(REQUESTOR, 60))})
    assert only(stale) == Finding(1, "client_waiting", "high", "requestor_last", 60)
    recent = run([unclaimed(1, ago(120))], {1: feed(1, entry(REQUESTOR, 1))})
    assert recent.findings == ()
    assert (recent.counts["client_recent"], recent.counts["other_recent"]) == (1, 0)


def test_other_recent_is_not_on_the_default_summary_line() -> None:
    result = run([unclaimed(1, ago(120))], {1: feed(1, entry(OTHER, 1))})
    assert not any("recent" in line for line in format_audit_summary(result, show_ids=True))


# --- strong desk names -------------------------------------------------------------------


def test_strong_desk_names_are_prim_resp_private_notes_and_merge_notices() -> None:
    rows = [
        row(1, prim_resp="Helpdesk, Pat"),
        row(2, prim_resp="Pat Helpdesk"),  # the same person, listed once
        unclaimed(3, ago(120)),
        unclaimed(4, ago(120), requestor="Sam Synthetic"),
    ]
    feeds = {
        3: feed(3, entry(OTHER, 70), entry(OTHER, 90, "private")),
        4: TicketFeed(4, (entry(EXTRA_STAFF, 50, "system"),), desk_authors=(EXTRA_STAFF,)),
    }
    assert strong_desk_names(rows, feeds) == ["Helpdesk, Pat", OTHER, EXTRA_STAFF]


def test_strong_desk_names_leave_out_the_two_ticket_guess_and_extra_staff() -> None:
    rows = [unclaimed(1, ago(120)), unclaimed(2, ago(120), requestor="Sam Synthetic")]
    feeds = {
        1: feed(1, entry(OTHER, 70)),
        2: feed(2, entry("Sam Synthetic", 60), entry(OTHER, 80)),
    }
    # The in-run rules treat OTHER as desk (seen on 2 tickets)...
    assert run(rows, feeds).counts["desk_members_known"] == 1
    # ...but OTHER is not a strong name, so the memory never learns them.
    assert strong_desk_names(rows, feeds) == []
    assert strong_desk_names([unclaimed(1, ago(120))], {}) == []


# --- evaluate with known_desk ------------------------------------------------------------


def _knows(*names: str) -> Callable[[str], bool]:
    return lambda author: any(same_person(author, name) for name in names)


def test_a_reply_by_a_remembered_name_is_desk_replied_last() -> None:
    rows = [unclaimed(1, ago(120))]
    feeds = {1: feed(1, entry(OTHER, 70), entry(REQUESTOR, 100))}
    assert only(run(rows, feeds)).reason == "other_last"
    result = run(rows, feeds, known_desk=_knows("testperson, casey"))
    assert result.findings == ()
    assert result.counts["desk_replied_last"] == 1
    assert result.counts["desk_members_known"] == 0  # the in-run count is unchanged


def test_a_remembered_name_who_is_the_requestor_still_counts_as_the_requestor() -> None:
    rows = [unclaimed(1, ago(120))]
    feeds = {1: feed(1, entry(REQUESTOR, 60))}
    asked: list[str] = []

    def knows(author: str) -> bool:
        asked.append(author)
        return True

    keep = RuleSettings(skip_desk_requestors=False)
    result = run(rows, feeds, rules=keep, known_desk=knows)
    assert only(result) == Finding(1, "client_waiting", "high", "requestor_last", 60)
    assert asked == []  # the requestor check came first


def test_known_desk_none_behaves_as_before() -> None:
    rows = [
        unclaimed(1, ago(120)),
        unclaimed(2, ago(120)),
        unclaimed(3, ago(120)),
        unclaimed(4, ago(120)),
        row(5, prim_resp=DESK),
    ]
    feeds = {
        1: feed(1, entry(OTHER, 70)),
        2: feed(2, entry(DESK, 70)),
        3: feed(3, entry(REQUESTOR, 60)),
        4: feed(4, entry("Jordan Thirdparty", 3)),
    }
    default = evaluate(rows, feeds, NOW, RULES, TZ)
    explicit = evaluate(rows, feeds, NOW, RULES, TZ, known_desk=None)
    assert default == explicit
    assert [(f.ticket_id, f.reason) for f in default.findings] == [
        (3, "requestor_last"),
        (1, "other_last"),
    ]
    assert (default.counts["desk_replied_last"], default.counts["other_recent"]) == (1, 1)


# --- tickets a desk member opened --------------------------------------------------------

PRIM_RESP = "Dana Desk"
MANAGER = "Max Manager"
PRIVATE = "Pat Private"
REMEMBERED = "Mo Memory"


def _desk_requestor_run(signal: str, case: str, *, skip: bool = True) -> AuditResult:
    """Ticket 1, unclaimed, opened by someone desk through one strong signal."""
    requestor = {
        "prim_resp": PRIM_RESP,
        "extra_staff": MANAGER,
        "private_note": PRIVATE,
        "memory": REMEMBERED,
    }[signal]
    own = (entry(requestor, 60),) if case == "requestor_last" else ()
    rows = [
        unclaimed(1, ago(120), requestor=requestor),
        unclaimed(2, ago(5)),  # recent: no finding, but its feed was read
        row(3, prim_resp=PRIM_RESP if signal == "prim_resp" else DESK),
    ]
    feeds = {1: feed(1, *own)}
    if signal == "private_note":
        feeds[2] = feed(2, entry(PRIVATE, 3, "private"))
    rules = RuleSettings(
        extra_staff_names=(MANAGER,) if signal == "extra_staff" else (),
        skip_desk_requestors=skip,
    )
    known = _knows(REMEMBERED) if signal == "memory" else None
    return run(rows, feeds, rules=rules, known_desk=known)


@pytest.mark.parametrize("signal", ["prim_resp", "extra_staff", "private_note", "memory"])
@pytest.mark.parametrize("case", ["no_reply_yet", "requestor_last"])
def test_a_desk_member_who_opened_the_ticket_is_not_waiting(signal: str, case: str) -> None:
    result = _desk_requestor_run(signal, case)
    assert result.findings == ()
    assert result.counts["internal_requestor"] == 1
    assert result.counts["client_waiting"] == 0
    assert (result.counts["feeds_needed"], result.counts["feed_missing"]) == (1, 0)


@pytest.mark.parametrize("signal", ["prim_resp", "extra_staff", "private_note", "memory"])
@pytest.mark.parametrize("case", ["no_reply_yet", "requestor_last"])
def test_skip_desk_requestors_off_gives_client_waiting_as_before(signal: str, case: str) -> None:
    result = _desk_requestor_run(signal, case, skip=False)
    assert only(result).reason == case
    assert result.counts["internal_requestor"] == 0


def test_a_requestor_seen_only_on_two_tickets_is_still_waiting() -> None:
    twice = "Tess Twice"
    rows = [
        unclaimed(1, ago(120)),
        unclaimed(2, ago(120)),
        unclaimed(3, ago(120), requestor=twice),
        unclaimed(4, ago(120), requestor=twice),
    ]
    feeds = {
        1: feed(1, entry(twice, 70), entry(REQUESTOR, 100)),
        2: feed(2, entry(twice, 70), entry(REQUESTOR, 100)),
        3: feed(3),
        4: feed(4, entry(twice, 60)),
    }
    result = run(rows, feeds)
    assert result.counts["desk_replied_last"] == 2  # the guess still counts for replies
    assert [(f.ticket_id, f.reason) for f in result.findings] == [
        (3, "no_reply_yet"),
        (4, "requestor_last"),
    ]
    assert result.counts["internal_requestor"] == 0


def test_a_desk_requestor_leaves_the_other_paths_alone() -> None:
    rows = [
        unclaimed(1, ago(5), requestor=MANAGER),  # recent
        unclaimed(2, ago(120), requestor=MANAGER),  # feed missing
        unclaimed(3, ago(120), requestor=MANAGER),  # wrote last, 3 hours ago
        unclaimed(4, ago(120), requestor=MANAGER),  # wrote last, undated
        unclaimed(5, ago(120), requestor=MANAGER),  # someone else wrote last
        unclaimed(6, ago(120), requestor=MANAGER),  # the desk replied last
        unclaimed(7, ago(100), title="EVENT [10/1 2pm Room 101] Zoom recording", requestor=MANAGER),
    ]
    feeds = {
        3: feed(3, entry(MANAGER, 3)),
        4: feed(4, entry(MANAGER, None)),
        5: feed(5, entry(OTHER, 70)),
        6: feed(6, entry(DESK, 70)),
    }
    rules = RuleSettings(extra_staff_names=(MANAGER, DESK))
    result = run(rows, feeds, rules=rules)
    assert [(f.ticket_id, f.reason) for f in result.findings] == [
        (5, "other_last"),
        (4, "requestor_last_undated"),
        (7, "event_over"),
    ]
    counts = result.counts
    assert counts["internal_requestor"] == 0
    assert (counts["unclaimed_recent"], counts["feed_missing"]) == (1, 1)
    assert (counts["client_recent"], counts["desk_replied_last"]) == (1, 1)


def test_an_empty_requestor_is_never_desk() -> None:
    result = run(
        [unclaimed(1, ago(120), requestor="")],
        {1: feed(1)},
        known_desk=lambda name: True,
    )
    assert only(result).reason == "no_reply_yet"


def test_internal_requestor_counts_add_up() -> None:
    rows = [
        unclaimed(1, ago(120), requestor=MANAGER),  # internal, no reply
        unclaimed(2, ago(120), requestor=MANAGER),  # internal, requestor wrote last
        unclaimed(3, ago(120)),  # client waiting
        unclaimed(4, ago(120)),  # desk replied last
        unclaimed(5, ago(120)),  # feed missing
    ]
    feeds = {
        1: feed(1),
        2: feed(2, entry(MANAGER, 60)),
        3: feed(3),
        4: feed(4, entry(DESK, 70)),
    }
    counts = run(rows, feeds, rules=RuleSettings(extra_staff_names=(MANAGER, DESK))).counts
    assert counts["feeds_needed"] == 5
    assert counts["internal_requestor"] == 2
    assert counts["feeds_needed"] == sum(
        counts[key]
        for key in (
            "feed_missing",
            "client_recent",
            "desk_replied_last",
            "internal_requestor",
            "other_recent",
            "client_waiting",
            "needs_review",
        )
    )


def test_the_checked_line_shows_internal_tickets_only_when_there_are_some() -> None:
    rows = [unclaimed(1, ago(120), requestor=MANAGER), unclaimed(2, ago(120))]
    feeds = {1: feed(1), 2: feed(2)}
    with_one = run(rows, feeds, rules=RuleSettings(extra_staff_names=(MANAGER,)))
    assert format_audit_summary(with_one, show_ids=False)[1] == (
        "Checked: 2 unclaimed (2 feeds needed, 0 missing), 0 claimed (counted only). "
        "Internal (desk requester): 1. "
        "Events: 0, 0 recurring, 0 multi-date, 0 with an unreadable date, 0 without a location. "
        "Form-titled tickets: 0 (0 recurring, 0 unreadable); renamed: 0 with a form, 0 without. "
        "Event states (no finding): 0 waiting on requester, 0 in progress, "
        "0 outreach not due yet, 0 marketing only, 0 no outreach. "
        "Desk members known: 1."
    )
    off = RuleSettings(extra_staff_names=(MANAGER,), skip_desk_requestors=False)
    without = run(rows, feeds, rules=off)
    assert "Internal" not in format_audit_summary(without, show_ids=False)[1]
    assert "Internal" not in format_audit_summary(mixed_run(), show_ids=False)[1]


# --- follow-up verdicts -------------------------------------------------------------------


def _result(*findings: Finding) -> AuditResult:
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["rows_unclaimed"] = 7
    for finding in findings:
        counts[finding.kind] += 1
    return AuditResult(findings=findings, counts=counts, event_date_unreadable_ids=(9,))


WAITING_OLD = Finding(201, "client_waiting", "high", "requestor_last", 120)
WAITING_NEW = Finding(202, "client_waiting", "high", "requestor_last", 60)
NO_REPLY = Finding(203, "client_waiting", "high", "no_reply_yet", 90)
REVIEW = Finding(204, "needs_review", "normal", "other_last", 70)
EVENT = Finding(205, "event_over", "low", "event_over", 30)
ALL = (WAITING_OLD, NO_REPLY, WAITING_NEW, REVIEW, EVENT)


@pytest.mark.parametrize(
    ("verdict", "kind", "priority", "reason"),
    [
        ("expects_reply", "client_waiting", "high", "expects_reply"),
        ("client_will_follow_up", "follow_up_with_client", "normal", "client_follow_up"),
        ("no_reply_needed", "follow_up_with_client", "normal", "client_no_reply_needed"),
        ("unsure", "client_waiting", "high", "requestor_last"),
    ],
)
def test_each_verdict_changes_a_requestor_last_finding(
    verdict: str, kind: str, priority: str, reason: str
) -> None:
    result = apply_verdicts(_result(*ALL), {201: verdict})

    changed = next(f for f in result.findings if f.ticket_id == 201)
    assert (changed.kind, changed.priority, changed.reason, changed.age_hours) == (
        kind,
        priority,
        reason,
        120,
    )
    assert {f for f in result.findings if f.ticket_id != 201} == set(ALL) - {WAITING_OLD}


def test_verdicts_on_other_findings_are_ignored() -> None:
    verdicts = {203: "no_reply_needed", 204: "client_will_follow_up", 205: "no_reply_needed"}

    result = apply_verdicts(_result(*ALL), verdicts)

    assert set(result.findings) == set(ALL)


def test_counts_are_recomputed_and_findings_re_sorted() -> None:
    before = _result(*ALL)

    result = apply_verdicts(before, {201: "client_will_follow_up", 202: "expects_reply"})

    assert [(f.ticket_id, f.kind, f.reason) for f in result.findings] == [
        (203, "client_waiting", "no_reply_yet"),
        (202, "client_waiting", "expects_reply"),
        (201, "follow_up_with_client", "client_follow_up"),  # normal now: after the high ones
        (204, "needs_review", "other_last"),
        (205, "event_over", "event_over"),
    ]
    assert result.counts["client_waiting"] == 2
    assert result.counts["follow_up_with_client"] == 1
    assert result.counts["needs_review"] == 1
    assert result.counts["rows_unclaimed"] == 7  # other counts kept
    assert result.event_date_unreadable_ids == (9,)
    assert before.counts["client_waiting"] == 3  # the input is not changed
    assert format_audit_summary(result, show_ids=False)[0] == (
        "Findings: client waiting 2, follow up with client 1, event outreach due 0, "
        "event details pending 0, event over 1, location TBD soon 0, needs review 1, "
        "ready to close 0."
    )


def test_no_verdicts_leave_the_result_as_it_was() -> None:
    before = _result(*ALL)

    after = apply_verdicts(before, {})

    assert sorted(after.findings, key=lambda f: f.ticket_id) == sorted(
        before.findings, key=lambda f: f.ticket_id
    )
    assert dict(after.counts) == dict(before.counts)


# --- event request forms ------------------------------------------------------------------

FORM_TITLE = "CCI Event Request Form"


def form_row(ticket_id: int, created_hours_ago: float = 30) -> TicketRow:
    return unclaimed(ticket_id, ago(created_hours_ago), FORM_TITLE)


def event_form(
    *days: float,
    av: bool = True,
    room: bool = False,
    marketing: bool = False,
    recurring: bool | None = False,
) -> EventForm:
    return EventForm(
        dates=tuple(NOW + timedelta(days=day) for day in days),
        recurring=recurring,
        wants_av=av,
        wants_room=room,
        marketing_only=marketing,
        unknown_options=0,
    )


def form_run(
    form: EventForm | None,
    *entries: FeedEntry,
    rules: RuleSettings = STAFF,
    known_desk: Callable[[str], bool] | None = None,
) -> AuditResult:
    forms = {1: form} if form is not None else {}
    return evaluate(
        [form_row(1)], {1: feed(1, *entries)}, NOW, rules, TZ, known_desk=known_desk, forms=forms
    )


def test_form_tickets_are_planned_at_any_age_oldest_first() -> None:
    rows = [
        form_row(1, created_hours_ago=2),
        unclaimed(2, ago(5), "  cci   EVENT request form "),
        unclaimed(3, ago(100)),
        row(4, title=FORM_TITLE, modified=ago(300)),  # claimed: counted only
        unclaimed(5, ago(3)),  # an ordinary recent ticket: not opened
    ]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == (3, 2, 1)


@pytest.mark.parametrize("title", ["CCI Event Request Form", "SCIS Event Request Form"])
def test_a_form_titled_with_either_college_name_is_a_form_ticket(title: str) -> None:
    """The college is now the SCIS (formerly CCI); the default matches both titles."""
    result = evaluate(
        [unclaimed(1, ago(30), title)], {1: feed(1)}, NOW, STAFF, TZ, forms={1: event_form(5)}
    )

    assert plan_feeds([unclaimed(1, ago(3), title)], NOW, RULES).feed_ticket_ids == (1,)
    assert [(finding.kind, finding.reason) for finding in result.findings] == [
        ("event_outreach_due", "outreach_due")
    ]
    assert result.counts["event_forms"] == 1


def test_a_form_title_is_matched_whole_not_as_a_prefix() -> None:
    rows = [unclaimed(1, ago(3), FORM_TITLE + " (copy)")]
    assert plan_feeds(rows, NOW, RULES).feed_ticket_ids == ()


def test_a_form_without_its_feed_is_missing_not_judged() -> None:
    result = evaluate([form_row(1)], {}, NOW, STAFF, TZ, forms={1: event_form(5)})

    assert result.findings == ()
    counts = result.counts
    assert (counts["event_forms"], counts["feeds_needed"], counts["feed_missing"]) == (1, 1, 1)
    assert counts["event_forms_unreadable"] == 0


@pytest.mark.parametrize("form", [None, event_form()], ids=["no-form", "no-dates"])
def test_an_unreadable_form_is_counted_and_listed_by_id(form: EventForm | None) -> None:
    result = form_run(form)

    assert result.findings == ()
    assert result.counts["event_forms_unreadable"] == 1
    assert result.event_form_unreadable_ids == (1,)
    assert format_audit_summary(result, show_ids=True)[-1] == "  event_form_unreadable  1"


def test_a_recurring_form_gives_no_finding() -> None:
    result = form_run(event_form(5, recurring=True))

    assert result.findings == () and result.counts["event_forms_recurring"] == 1


@pytest.mark.parametrize("marketing", [False, True])
def test_a_form_whose_event_is_over_is_ready_to_resolve_whatever_the_services(
    marketing: bool,
) -> None:
    result = form_run(event_form(-5, -2, marketing=marketing))

    finding = only(result)
    assert (finding.kind, finding.priority, finding.reason) == ("event_over", "low", "event_over")
    assert finding.age_hours == 48


def test_marketing_only_gets_no_outreach() -> None:
    result = form_run(event_form(5, marketing=True))

    assert result.findings == () and result.counts["event_marketing_only"] == 1


def test_no_av_and_no_classroom_gets_no_outreach() -> None:
    result = form_run(event_form(5, av=False, room=False))

    assert result.findings == () and result.counts["event_no_outreach"] == 1


@pytest.mark.parametrize(("av", "room"), [(True, False), (False, True), (True, True)])
def test_outreach_is_due_with_no_desk_reply_within_14_days(av: bool, room: bool) -> None:
    result = form_run(event_form(10, av=av, room=room))

    finding = only(result)
    assert (finding.kind, finding.priority, finding.reason) == (
        "event_outreach_due",
        "normal",
        "outreach_due",
    )
    assert finding.age_hours == finding.event_in_hours == 240


def test_outreach_due_is_high_at_3_days_or_less_and_uses_the_next_date() -> None:
    result = form_run(event_form(-1, 3, 9))

    finding = only(result)
    assert finding.kind == "event_outreach_due" and finding.priority == "high"
    assert finding.event_in_hours == 72


def test_outreach_is_not_due_more_than_14_days_out() -> None:
    result = form_run(event_form(15))

    assert result.findings == () and result.counts["event_outreach_not_due"] == 1


def test_a_requester_message_alone_is_not_outreach() -> None:
    result = form_run(event_form(10), entry(REQUESTOR, 10))

    assert only(result).kind == "event_outreach_due"


def test_details_pending_when_the_desk_asked_2_days_ago_and_the_event_is_within_7_days() -> None:
    """Normal priority; high only at the near-event override."""
    result = form_run(event_form(5, room=True, av=False), entry(DESK, 72))

    finding = only(result)
    assert (finding.kind, finding.priority, finding.reason) == (
        "event_details_pending",
        "normal",
        "details_pending",
    )
    assert (finding.age_hours, finding.event_in_hours) == (72, 120)
    assert result.event_states[1] == EventStatus("waiting_on_requester", ago(72))


@pytest.mark.parametrize(
    ("days", "desk_hours_ago"),
    [(10, 72), (5, 20)],
    ids=["outside-the-7-day-window", "desk-replied-recently"],
)
def test_outreach_done_when_the_desk_replied(days: float, desk_hours_ago: float) -> None:
    result = form_run(event_form(days), entry(DESK, desk_hours_ago))

    assert result.findings == () and result.counts["event_outreach_done"] == 1


def test_the_requester_waiting_on_the_desk_is_client_waiting() -> None:
    """The event's own reason, normal priority, and no follow-up classifier."""
    result = form_run(event_form(5), entry(REQUESTOR, 60), entry(DESK, 100))

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority) == (
        "client_waiting",
        "event_requester_replied",
        "normal",
    )
    assert (finding.age_hours, finding.event_in_hours) == (60, 120)
    assert not is_follow_up_candidate(finding)


def test_a_recent_requester_reply_after_the_desk_is_counted_not_flagged() -> None:
    result = form_run(event_form(5), entry(REQUESTOR, 10), entry(DESK, 100))

    assert result.findings == () and result.counts["client_recent"] == 1
    assert result.event_states[1].state == "requester_replied"


def test_form_tickets_never_raise_needs_review() -> None:
    """Someone other than the desk writing last counts as the requester's side."""
    result = form_run(event_form(5), entry(OTHER, 100), entry(DESK, 120))

    assert [(f.kind, f.reason) for f in result.findings] == [
        ("client_waiting", "event_requester_replied")
    ]
    assert result.counts["needs_review"] == 0


def test_the_desk_is_known_through_the_desk_memory_too() -> None:
    result = form_run(
        event_form(5), entry(DESK, 72), rules=RULES, known_desk=lambda name: name == DESK
    )

    assert only(result).kind == "event_details_pending"


def test_without_knowing_the_desk_its_reply_is_not_outreach() -> None:
    """An unknown author counts as the requester's side: folded into the outreach row."""
    result = form_run(event_form(5), entry(DESK, 72), rules=RULES)

    assert kinds(result) == [("event_outreach_due", "outreach_due_requester_wrote", "normal")]


def test_a_desk_member_requester_waiting_counts_as_internal() -> None:
    rules = RuleSettings(extra_staff_names=(DESK, MANAGER))
    row_ = unclaimed(1, ago(30), FORM_TITLE, requestor=MANAGER)
    feeds = {1: feed(1, entry(MANAGER, 60), entry(DESK, 100))}

    result = evaluate([row_], feeds, NOW, rules, TZ, forms={1: event_form(5)})

    assert result.findings == () and result.counts["internal_requestor"] == 1


def test_the_windows_come_from_the_rule_settings() -> None:
    rules = RuleSettings(
        extra_staff_names=(DESK,), outreach_days=21, details_pending_days=10, urgent_days=0
    )
    due = form_run(event_form(20), rules=rules)
    pending = form_run(event_form(9), entry(DESK, 72), rules=rules)

    assert only(due).kind == "event_outreach_due" and only(due).priority == "normal"
    assert only(pending).kind == "event_details_pending"


def test_the_checked_line_counts_the_forms() -> None:
    rows = [form_row(ticket_id) for ticket_id in range(1, 7)]
    feeds = {ticket_id: feed(ticket_id) for ticket_id in range(1, 7)}
    feeds[1] = feed(1, entry(DESK, 10))
    forms = {
        1: event_form(5),
        2: event_form(20),
        3: event_form(5, marketing=True),
        4: event_form(5, av=False),
        5: event_form(5, recurring=True),
    }

    result = evaluate(rows, feeds, NOW, STAFF, TZ, forms=forms)

    assert result.findings == ()
    assert format_audit_summary(result, show_ids=False)[1] == (
        "Checked: 6 unclaimed (6 feeds needed, 0 missing), 0 claimed (counted only). "
        "Events: 0, 0 recurring, 0 multi-date, 0 with an unreadable date, 0 without a location. "
        "Form-titled tickets: 6 (1 recurring, 1 unreadable); renamed: 0 with a form, 0 without. "
        "Event states (no finding): 1 waiting on requester, 0 in progress, "
        "1 outreach not due yet, 1 marketing only, 1 no outreach. Desk members known: 1."
    )


# --- ready to close -----------------------------------------------------------------------


def _desk_last(hours: float, *, rules: RuleSettings = STAFF, requestor: str = REQUESTOR):
    rows = [unclaimed(1, ago(hours + 30), requestor=requestor)]
    return run(rows, {1: feed(1, entry(DESK, hours), entry(requestor, hours + 20))}, rules=rules)


def test_ready_to_close_fires_at_close_after_days_not_before() -> None:
    before = _desk_last(71.9)
    at = _desk_last(72)

    assert before.findings == () and before.counts["desk_replied_last"] == 1
    assert only(at) == Finding(1, "ready_to_close", "low", "desk_replied_last", 72)
    assert at.counts["desk_replied_last"] == 0 and at.counts["ready_to_close"] == 1


def test_close_after_days_comes_from_the_rule_settings() -> None:
    rules = RuleSettings(extra_staff_names=(DESK,), close_after_days=5)

    assert _desk_last(100, rules=rules).findings == ()
    assert only(_desk_last(120, rules=rules)).kind == "ready_to_close"


def test_ready_to_close_needs_the_desk_to_have_written_last() -> None:
    rows = [unclaimed(1, ago(200)), unclaimed(2, ago(200))]
    feeds = {
        1: feed(1, entry(REQUESTOR, 80), entry(DESK, 100)),  # the requester wrote last
        2: feed(2, entry(OTHER, 100), entry(DESK, 120)),  # someone else wrote last
    }

    result = run(rows, feeds, rules=STAFF)

    assert {finding.kind for finding in result.findings} == {"client_waiting", "needs_review"}


def test_a_private_note_after_the_desk_reply_does_not_stop_ready_to_close() -> None:
    rows = [unclaimed(1, ago(200))]
    feeds = {1: feed(1, entry(DESK, 2, "private"), entry(DESK, 90), entry(REQUESTOR, 150))}

    assert only(run(rows, feeds, rules=STAFF)).age_hours == 90


def test_ready_to_close_is_not_for_internal_claimed_event_or_form_tickets() -> None:
    internal = _desk_last(
        100, rules=RuleSettings(extra_staff_names=(DESK, MANAGER)), requestor=MANAGER
    )
    claimed = run([row(1, modified=ago(100))], {1: feed(1, entry(DESK, 100))}, rules=STAFF)
    event = run([unclaimed(1, ago(200), "EVENT [10/20 2pm Room 101] Talk")], rules=STAFF)
    form = form_run(event_form(30), entry(DESK, 100))

    assert internal.findings == () and internal.counts["desk_replied_last"] == 1
    for result in (claimed, event, form):
        assert all(finding.kind != "ready_to_close" for finding in result.findings)


def test_an_undated_desk_reply_is_counted_not_ready_to_close() -> None:
    result = run([unclaimed(1, ago(200))], {1: feed(1, entry(DESK, None))}, rules=STAFF)

    assert result.findings == () and result.counts["desk_replied_last"] == 1


def test_ready_to_close_is_on_the_findings_line() -> None:
    line = format_audit_summary(_desk_last(80), show_ids=True)

    assert line[0].endswith("needs review 0, ready to close 1.")
    assert "  ready_to_close  1  desk_replied_last  80h" in line


# --- event state from the feed ------------------------------------------------------------

# Thursday 2026-10-08 12:00. Renamed early, at first handling: the title says nothing about
# whether the details are in.
IN_10_DAYS = "EVENT [10/18 2pm Room 101] Zoom recording"  # 242 h away
IN_5_DAYS = "EVENT [10/13 2pm Room 101] Zoom recording"  # 122 h away
TOMORROW = "EVENT [10/9 2pm Room 101] Zoom recording"  # 26 h away
IN_17_DAYS = "EVENT [10/25 2pm Room 101] Zoom recording"
NO_ROOM_TOMORROW = "EVENT [10/9/26 6PM-7PM] Zoom recording"  # 30 h away
NO_ROOM_IN_5_DAYS = "EVENT [10/13/26 6PM-7PM] Zoom recording"
# Location TBD: the desk still waits on the requester for the room.
TBD_TOMORROW = "EVENT [10/9 2pm TBD] Zoom recording"  # 26 h away
TBD_IN_5_DAYS = "EVENT [10/13 2pm TBD] Zoom recording"  # 122 h away
TBD_IN_10_DAYS = "EVENT [10/18 2pm TBD] Zoom recording"  # 242 h away


def renamed_run(
    title: str,
    *entries: FeedEntry,
    form: EventForm | None = None,
    rules: RuleSettings = STAFF,
) -> AuditResult:
    forms = {1: form} if form is not None else {}
    rows = [unclaimed(1, ago(200), title)]
    return evaluate(rows, {1: feed(1, *entries)}, NOW, rules, TZ, forms=forms)


def kinds(result: AuditResult) -> list[tuple[str, str, str]]:
    return [(f.kind, f.reason, f.priority) for f in result.findings]


def test_renamed_events_within_14_days_get_their_page_and_form_read() -> None:
    rows = [
        unclaimed(1, ago(300), IN_10_DAYS),
        unclaimed(2, ago(200), IN_17_DAYS),  # further out: title only
        unclaimed(3, ago(100), "EVENT [10/1 2pm Room 101] Zoom recording"),  # past
        unclaimed(4, ago(100), "EVENT [Weekly, Mon 5pm Room 101] Zoom recording"),
        unclaimed(5, ago(5), TOMORROW),  # created hours ago: still read
        unclaimed(6, ago(400), "EVENT [someday] Zoom recording"),
        row(7, title=TOMORROW, modified=ago(300)),  # claimed: counted only
        unclaimed(8, ago(2), FORM_TITLE),
        unclaimed(9, ago(100)),  # an ordinary stale ticket: feed only
    ]

    plan = plan_feeds(rows, NOW, RULES, TZ)

    assert plan.feed_ticket_ids == (1, 9, 5, 8)
    assert plan.form_ticket_ids == (1, 5, 8)
    wider = plan_feeds(rows, NOW, RuleSettings(outreach_days=20), TZ)
    assert wider.form_ticket_ids == (1, 2, 5, 8)


def test_plan_feeds_reads_titles_in_the_zone_of_now_without_tz() -> None:
    rows = [unclaimed(1, ago(300), IN_10_DAYS)]
    assert plan_feeds(rows, NOW, RULES).form_ticket_ids == (1,)


def test_a_renamed_av_event_with_no_desk_reply_is_outreach_due() -> None:
    result = renamed_run(IN_10_DAYS, form=event_form(10))

    assert kinds(result) == [("event_outreach_due", "outreach_due", "normal")]
    assert result.event_states[1] == EventStatus("no_outreach_yet")
    assert result.counts["feeds_needed"] == 1 and result.counts["event_tickets"] == 1


def test_the_desk_asked_an_hour_ago_and_the_event_is_tomorrow_is_high_at_once() -> None:
    result = renamed_run(TBD_TOMORROW, entry(DESK, 1), entry(REQUESTOR, 200), form=event_form(1))

    finding = next(f for f in result.findings if f.kind == "event_details_pending")
    assert (finding.kind, finding.reason, finding.priority) == (
        "event_details_pending",
        "details_pending_near",
        "high",
    )
    assert (finding.age_hours, finding.event_in_hours) == (1, 26)


def test_the_desk_asked_3_days_ago_and_the_event_is_in_5_days_is_normal() -> None:
    result = renamed_run(TBD_IN_5_DAYS, entry(DESK, 72), entry(REQUESTOR, 200), form=event_form(5))

    assert kinds(result) == [("event_details_pending", "details_pending", "normal")]
    assert result.event_states[1] == EventStatus("waiting_on_requester", ago(72))


def test_waiting_on_the_requester_before_the_window_is_counted_only() -> None:
    result = renamed_run(TBD_IN_10_DAYS, entry(DESK, 72), form=event_form(10))

    assert result.findings == () and result.counts["event_outreach_done"] == 1
    assert result.event_states[1].state == "waiting_on_requester"


def test_the_requester_replied_two_days_ago_and_the_event_is_in_5_days_is_normal() -> None:
    result = renamed_run(IN_5_DAYS, entry(REQUESTOR, 49), entry(DESK, 100), form=event_form(5))

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority) == (
        "client_waiting",
        "event_requester_replied",
        "normal",
    )
    assert (finding.age_hours, finding.event_in_hours) == (49, 122)
    assert result.event_states[1] == EventStatus("requester_replied", ago(49))


def test_the_requester_replied_an_hour_ago_and_the_event_is_tomorrow_is_high_at_once() -> None:
    result = renamed_run(TOMORROW, entry(REQUESTOR, 1), entry(DESK, 100), form=event_form(1))

    assert kinds(result) == [("client_waiting", "event_requester_replied", "high")]


def test_a_recent_requester_reply_before_the_near_window_waits() -> None:
    result = renamed_run(IN_5_DAYS, entry(REQUESTOR, 10), entry(DESK, 100), form=event_form(5))

    assert result.findings == () and result.counts["client_recent"] == 1


def test_the_requester_answered_and_the_desk_confirmed_is_in_progress_and_quiet() -> None:
    """Without the events spreadsheet, finished cannot be told from unfinished."""
    entries = (entry(DESK, 2), entry(REQUESTOR, 20), entry(DESK, 50), entry(REQUESTOR, 200))

    result = renamed_run(TOMORROW, *entries, form=event_form(1))

    assert result.findings == ()
    assert result.event_states[1] == EventStatus("in_progress")
    assert result.counts["event_in_progress"] == 1


def test_a_renamed_title_without_a_room_is_location_missing_like_tbd() -> None:
    entries = (entry(DESK, 2), entry(REQUESTOR, 20), entry(DESK, 50))
    soon = renamed_run(NO_ROOM_TOMORROW, *entries)
    later = renamed_run(NO_ROOM_IN_5_DAYS, *entries)

    finding = only(soon)
    assert (finding.kind, finding.reason, finding.priority) == (
        "location_tbd_soon",
        "location_missing",
        "normal",
    )
    assert finding.age_hours == 30
    assert later.findings == ()
    assert soon.counts["event_location_missing"] == later.counts["event_location_missing"] == 1


def test_the_checked_line_counts_renamed_tickets_with_and_without_a_form() -> None:
    """A queue of renamed tickets with forms must not read as "no forms": the title-form count
    and the renamed counts are separate."""
    with_form = renamed_run(IN_10_DAYS, form=event_form(1))
    without = renamed_run(IN_10_DAYS)

    assert with_form.counts["event_renamed_with_form"] == 1
    assert with_form.counts["event_form_missing"] == 0
    assert without.counts["event_renamed_with_form"] == 0
    assert (
        "Form-titled tickets: 0 (0 recurring, 0 unreadable); renamed: 1 with a form, 0 "
        "without." in format_audit_summary(with_form, show_ids=False)[1]
    )
    assert "renamed: 0 with a form, 1 without." in format_audit_summary(without, show_ids=False)[1]


def test_a_renamed_ticket_without_a_form_is_counted_and_raises_no_outreach() -> None:
    result = renamed_run(IN_10_DAYS)

    assert result.findings == ()
    assert result.counts["event_form_missing"] == 1
    assert result.event_form_missing_ids == (1,)
    assert result.event_states[1] == EventStatus("no_outreach_yet")
    assert format_audit_summary(result, show_ids=True)[-2:] == [
        "  event_form_missing  1",
        # Services unknown without a form: the feed state stands, A/V and space are "—".
        "  event_state  1  no_outreach_yet  A/V on form: —  —",
    ]
    assert result.event_reads == (EventRead(1, "no_outreach_yet", None, None),)


def test_show_ids_lists_every_upcoming_event_read_with_its_state_av_and_space() -> None:
    """So every event can be checked by hand, findings or not; ascending by id."""
    rows = [form_row(4), form_row(2), form_row(3), form_row(1), form_row(5)]
    forms = {
        1: replace(event_form(3, av=True), space="classroom"),  # outreach due: a finding too
        2: replace(event_form(3, av=False, room=False), space="lobby"),
        3: replace(event_form(3, marketing=True, av=False), space="other"),
        4: replace(event_form(20, av=False, room=True), space="classroom"),  # not due yet
        5: event_form(-3),  # over: no event state
    }
    feeds = {ticket_id: feed(ticket_id) for ticket_id in forms}

    result = evaluate(rows, feeds, NOW, STAFF, TZ, forms=forms)

    lines = format_audit_summary(result, show_ids=True)
    assert [line for line in lines if line.startswith("  event_state")] == [
        "  event_state  1  no_outreach_yet  A/V on form: yes  classroom",
        "  event_state  2  no_outreach  A/V on form: no  lobby",
        "  event_state  3  marketing_only  A/V on form: no  other",
        "  event_state  4  no_outreach_yet  A/V on form: no  classroom",
    ]
    assert not any("event_state" in line for line in format_audit_summary(result, show_ids=False))


def test_a_renamed_ticket_without_a_form_still_gets_the_feed_state_findings() -> None:
    waiting = renamed_run(TBD_TOMORROW, entry(DESK, 1))
    replied = renamed_run(IN_5_DAYS, entry(REQUESTOR, 60), entry(DESK, 100))

    # The TBD room folds into the follow-up: one row for the ticket.
    assert kinds(waiting) == [("event_details_pending", "details_pending_near", "high")]
    assert only(waiting).no_room == "tbd"
    assert kinds(replied) == [("client_waiting", "event_requester_replied", "normal")]
    assert waiting.counts["event_form_missing"] == replied.counts["event_form_missing"] == 1


@pytest.mark.parametrize(
    ("title", "no_room"), [(TBD_TOMORROW, "tbd"), (NO_ROOM_TOMORROW, "missing")]
)
def test_a_renamed_event_with_no_room_and_a_feed_finding_is_one_finding(
    title: str, no_room: str
) -> None:
    """A ticket waiting on the requester with no room yet is one row, not two: the follow-up
    stands for the room too, and the counts drop the room row with it."""
    result = renamed_run(title, entry(DESK, 1))

    assert kinds(result) == [("event_details_pending", "details_pending_near", "high")]
    assert only(result).no_room == no_room
    assert result.counts["location_tbd_soon"] == 0
    assert result.counts["event_details_pending"] == 1


def test_any_feed_finding_takes_the_room_row() -> None:
    outreach = renamed_run(TBD_TOMORROW, form=event_form(1))  # nobody has emailed yet
    replied = renamed_run(TBD_TOMORROW, entry(REQUESTOR, 2), entry(DESK, 30))

    assert kinds(outreach) == [("event_outreach_due", "outreach_due", "high")]
    assert kinds(replied) == [("client_waiting", "event_requester_replied", "high")]
    assert only(outreach).no_room == only(replied).no_room == "tbd"


def test_a_tbd_event_without_a_feed_finding_keeps_its_room_row() -> None:
    # The requester has answered and the desk wrote last: in progress, no feed finding.
    in_progress = renamed_run(TBD_TOMORROW, entry(DESK, 2), entry(REQUESTOR, 20), entry(DESK, 50))

    assert in_progress.findings == (Finding(1, "location_tbd_soon", "normal", "tbd_soon", 26),)
    assert in_progress.counts["location_tbd_soon"] == 1


def test_only_the_ticket_with_both_findings_is_merged() -> None:
    rows = [unclaimed(1, ago(200), TBD_TOMORROW), unclaimed(2, ago(200), TBD_IN_5_DAYS),
            unclaimed(3, ago(200), NO_ROOM_TOMORROW)]  # fmt: skip
    feeds = {1: feed(1, entry(DESK, 1)), 2: feed(2, entry(DESK, 1))}  # 3's page not read

    result = evaluate(rows, feeds, NOW, STAFF, TZ)

    assert [(f.ticket_id, f.kind, f.no_room) for f in result.findings] == [
        (1, "event_details_pending", "tbd"),
        (3, "location_tbd_soon", None),
    ]
    assert result.counts["location_tbd_soon"] == 1


@pytest.mark.parametrize(
    ("form", "count"),
    [
        (event_form(5, marketing=True), "event_marketing_only"),
        (event_form(5, av=False), "event_no_outreach"),
    ],
)
def test_a_renamed_event_needing_no_outreach_raises_no_event_finding(
    form: EventForm, count: str
) -> None:
    quiet = renamed_run(IN_5_DAYS, entry(DESK, 100), form=form)
    waiting = renamed_run(IN_5_DAYS, entry(REQUESTOR, 60), entry(DESK, 100), form=form)

    assert quiet.findings == () and quiet.counts[count] == 1
    # The requester wrote last 48+ hours ago: the desk owes a reply whatever the services.
    assert kinds(waiting) == [("client_waiting", "event_requester_replied", "normal")]


# --- a requester-last event always raises client waiting ---------------------------------


@pytest.mark.parametrize(
    "form",
    [
        event_form(5, marketing=True),
        event_form(5, av=False),
        event_form(20),  # no desk reply, outside the outreach window
        event_form(20, marketing=True),
    ],
    ids=["marketing-only", "no-outreach", "not-due-yet", "marketing-far-out"],
)
def test_a_stale_requester_entry_on_any_form_is_client_waiting(form: EventForm) -> None:
    result = form_run(form, entry(REQUESTOR, 60))

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority) == (
        "client_waiting",
        "event_requester_replied",
        "normal",
    )
    assert finding.age_hours == 60
    assert not is_follow_up_candidate(finding)


def test_a_recent_requester_entry_far_from_the_event_raises_nothing() -> None:
    result = form_run(event_form(5, marketing=True), entry(REQUESTOR, 10))

    assert result.findings == ()


def test_a_requester_entry_near_the_event_is_high_at_once_whatever_the_services() -> None:
    result = form_run(event_form(1, marketing=True), entry(REQUESTOR, 1))

    assert kinds(result) == [("client_waiting", "event_requester_replied", "high")]


def test_outreach_due_and_a_reply_owed_give_one_outreach_row() -> None:
    """One row per ticket: the outreach email answers the requester too."""
    result = form_run(event_form(10), entry(REQUESTOR, 60))

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority) == (
        "event_outreach_due",
        "outreach_due_requester_wrote",
        "normal",
    )
    assert (finding.age_hours, finding.event_in_hours, finding.requester_hours) == (240, 240, 60)
    assert result.counts["client_waiting"] == 0 and result.counts["event_outreach_due"] == 1


@pytest.mark.parametrize(
    ("days", "hours_ago", "priority"),
    [
        (1, 1, "high"),  # the reply owed is high (near the event): the row is high
        (3, 60, "high"),  # outreach is high (urgent_days): the row is high
        (10, 60, "normal"),
    ],
    ids=["reply-high", "outreach-high", "both-normal"],
)
def test_the_one_outreach_row_takes_the_higher_priority(
    days: float, hours_ago: float, priority: str
) -> None:
    result = form_run(event_form(days), entry(REQUESTOR, hours_ago))

    finding = only(result)
    assert (finding.kind, finding.reason) == ("event_outreach_due", "outreach_due_requester_wrote")
    assert finding.priority == priority


def test_a_recent_requester_entry_leaves_the_plain_outreach_row() -> None:
    result = form_run(event_form(10), entry(REQUESTOR, 10))

    assert kinds(result) == [("event_outreach_due", "outreach_due", "normal")]
    assert only(result).requester_hours is None


def test_an_undated_requester_entry_near_the_event_raises_the_outreach_row_high() -> None:
    result = form_run(event_form(1), entry(REQUESTOR, None))

    assert kinds(result) == [("event_outreach_due", "outreach_due", "high")]


def test_a_reply_owed_after_the_outreach_window_is_still_client_waiting() -> None:
    result = form_run(event_form(20), entry(REQUESTOR, 60))

    assert kinds(result) == [("client_waiting", "event_requester_replied", "normal")]


def test_a_renamed_ticket_without_a_form_and_no_desk_reply_still_owes_a_reply() -> None:
    result = renamed_run(IN_10_DAYS, entry(REQUESTOR, 60))

    assert kinds(result) == [("client_waiting", "event_requester_replied", "normal")]
    assert result.counts["event_form_missing"] == 1


def test_a_desk_member_requester_on_any_event_counts_as_internal() -> None:
    rules = RuleSettings(extra_staff_names=(DESK, MANAGER))
    rows = [unclaimed(1, ago(30), FORM_TITLE, requestor=MANAGER)]
    forms = {1: event_form(5, marketing=True)}

    result = evaluate(rows, {1: feed(1, entry(MANAGER, 60))}, NOW, rules, TZ, forms=forms)

    assert result.findings == () and result.counts["internal_requestor"] == 1


def test_a_renamed_event_further_out_is_title_only_even_with_a_requester_reply() -> None:
    """A known limit: renamed tickets more than 14 days out are not opened."""
    rows = [unclaimed(1, ago(200), IN_17_DAYS)]

    result = evaluate(rows, {1: feed(1, entry(REQUESTOR, 60))}, NOW, STAFF, TZ)

    assert plan_feeds(rows, NOW, STAFF, TZ).feed_ticket_ids == ()
    assert result.findings == ()


def test_a_renamed_event_further_out_or_without_its_page_uses_the_title_only() -> None:
    further = evaluate([unclaimed(1, ago(200), IN_17_DAYS)], {}, NOW, STAFF, TZ)
    tbd = unclaimed(1, ago(200), "EVENT [10/9 2pm TBD] Zoom recording")
    unread = evaluate([tbd], {}, NOW, STAFF, TZ)

    assert further.findings == () and further.counts["feeds_needed"] == 0
    assert kinds(unread) == [("location_tbd_soon", "tbd_soon", "normal")]
    assert (unread.counts["feeds_needed"], unread.counts["feed_missing"]) == (1, 1)
    assert unread.event_states == {}


def test_the_near_event_window_comes_from_the_rule_settings() -> None:
    rules = RuleSettings(extra_staff_names=(DESK,), near_event_days=0)

    result = renamed_run(TOMORROW, entry(DESK, 1), form=event_form(1), rules=rules)

    assert result.findings == () and result.counts["event_outreach_done"] == 1


def test_a_form_ticket_follows_the_same_feed_state_rules() -> None:
    near = form_run(event_form(1), entry(DESK, 1))
    in_progress = form_run(event_form(1), entry(DESK, 2), entry(REQUESTOR, 20), entry(DESK, 50))

    assert kinds(near) == [("event_details_pending", "details_pending_near", "high")]
    assert in_progress.findings == () and in_progress.event_states[1].state == "in_progress"


def is_staff(name: str) -> bool:
    return same_person(name, DESK)


@pytest.mark.parametrize(
    ("entries", "state", "since"),
    [
        ((), "no_outreach_yet", None),
        ((entry(REQUESTOR, 5), entry(OTHER, 9)), "no_outreach_yet", None),
        ((entry(DESK, 5),), "waiting_on_requester", 5),
        ((entry(DESK, 5), entry(DESK, 30), entry(REQUESTOR, 90)), "waiting_on_requester", 5),
        ((entry(REQUESTOR, 5), entry(DESK, 30)), "requester_replied", 5),
        ((entry(OTHER, 5), entry(DESK, 30)), "requester_replied", 5),
        ((entry(DESK, 5), entry(REQUESTOR, 9), entry(DESK, 30)), "in_progress", None),
        ((entry(DESK, 5), entry(OTHER, 9), entry(DESK, 30)), "in_progress", None),
        # Private notes and status changes are not visible to the requester: skipped.
        (
            (entry(DESK, 1, "private"), entry(REQUESTOR, 5), entry(DESK, 30)),
            "requester_replied",
            5,
        ),
        ((entry(REQUESTOR, 1, "system"), entry(DESK, 5)), "waiting_on_requester", 5),
        ((entry(DESK, None),), "waiting_on_requester", None),
    ],
)
def test_event_status_is_who_wrote_last(
    entries: tuple[FeedEntry, ...], state: str, since: float | None
) -> None:
    status = event_status(feed(1, *entries), REQUESTOR, is_staff)

    assert status.state == state
    assert status.since == (ago(since) if since is not None else None)


def test_a_desk_member_who_is_the_requestor_counts_as_the_requestor() -> None:
    status = event_status(feed(1, entry(DESK, 5)), DESK, is_staff)

    assert status.state == "no_outreach_yet"


def test_no_personal_data_in_the_event_state_output() -> None:
    entries = (entry(DESK, 2), entry(REQUESTOR, 20), entry(DESK, 50))
    results = [
        renamed_run(TOMORROW, *entries, form=event_form(1)),
        renamed_run(IN_5_DAYS, entry(REQUESTOR, 60), entry(DESK, 100)),
        renamed_run(NO_ROOM_TOMORROW, entry(DESK, 1)),
    ]
    texts = [repr(plan_feeds([unclaimed(1, ago(60), TOMORROW)], NOW, STAFF, TZ))]
    for result in results:
        texts += [*format_audit_summary(result, show_ids=True), repr(result)]
        texts += [repr(status) for status in result.event_states.values()]
    for text in texts:
        for invented in INVENTED:
            assert invented not in text


# --- the desk acted after the requester's message ----------------------------------------


def handled_feed(*newer: FeedEntry) -> tuple[FeedEntry, ...]:
    """The desk asked, the requester answered 60 hours ago, then ``newer`` (newest first)."""
    return (*newer, entry(REQUESTOR, 60), entry(DESK, 100))


@pytest.mark.parametrize("kind", ["private", "system"])
def test_a_desk_entry_after_the_requester_keeps_an_event_8_days_out_quiet(
    kind: EntryKind,
) -> None:
    result = form_run(event_form(8), *handled_feed(entry(DESK, 40, kind)))

    assert result.findings == ()
    assert result.event_states[1] == EventStatus("in_progress")
    assert [read.state for read in result.event_reads] == ["in_progress"]
    assert result.counts["event_in_progress"] == 1 and result.counts["client_recent"] == 0


@pytest.mark.parametrize(
    ("days", "priority"), [(3, "normal"), (1, "high")], ids=["3-days", "1-day"]
)
def test_within_urgent_days_a_handled_reply_returns_as_today(days: float, priority: str) -> None:
    result = form_run(event_form(days), *handled_feed(entry(DESK, 40, "private")))

    assert kinds(result) == [("client_waiting", "event_requester_replied", priority)]
    assert result.event_states[1] == EventStatus("requester_replied", ago(60))


def test_a_requester_entry_after_the_desk_action_brings_the_row_back() -> None:
    result = form_run(
        event_form(8), entry(REQUESTOR, 50), *handled_feed(entry(DESK, 55, "private"))
    )

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority, finding.age_hours) == (
        "client_waiting",
        "event_requester_replied",
        "normal",
        50,
    )
    assert result.event_states[1].state == "requester_replied"


def test_a_private_entry_by_the_requester_themself_does_not_count() -> None:
    rules = RuleSettings(extra_staff_names=(DESK,), skip_desk_requestors=False)
    entries = handled_feed(entry(REQUESTOR, 40, "private"))

    result = form_run(event_form(8), *entries, rules=rules)

    assert kinds(result) == [("client_waiting", "event_requester_replied", "normal")]


def test_an_undated_desk_entry_counts_by_its_place_in_the_feed() -> None:
    result = form_run(event_form(8), *handled_feed(entry(DESK, None, "private")))

    assert result.findings == () and result.event_states[1].state == "in_progress"


def test_a_desk_entry_older_than_the_requester_reply_does_not_count() -> None:
    entries = (entry(REQUESTOR, 60), entry(DESK, 80, "private"), entry(DESK, 100))

    result = form_run(event_form(8), *entries)

    assert kinds(result) == [("client_waiting", "event_requester_replied", "normal")]


def test_outreach_still_fires_when_the_desk_acted_only_privately() -> None:
    """No visible desk reply yet: the email is still owed; only the folded "the requester
    wrote" part goes quiet."""
    result = form_run(event_form(10), entry(DESK, 40, "private"), entry(REQUESTOR, 60))

    assert kinds(result) == [("event_outreach_due", "outreach_due", "normal")]
    assert only(result).requester_hours is None
    assert result.event_states[1] == EventStatus("no_outreach_yet")


def test_a_renamed_event_with_a_desk_entry_after_the_requester_is_quiet() -> None:
    result = renamed_run(IN_10_DAYS, *handled_feed(entry(DESK, 40, "private")))

    assert result.findings == () and result.event_states[1].state == "in_progress"


# --- a room in the title -----------------------------------------------------------------

ROOM_IN_6_DAYS = "EVENT [10/14 2pm Room 101] Zoom recording"  # 146 h away
ROOM_IN_3_DAYS = "EVENT [10/11 10am Room 101] Zoom recording"  # 70 h away
ZOOM_IN_3_DAYS = "EVENT [10/11 10am Zoom] Zoom recording"


def test_a_room_in_the_title_keeps_waiting_on_the_requester_quiet_6_days_out() -> None:
    result = renamed_run(ROOM_IN_6_DAYS, entry(DESK, 96), entry(REQUESTOR, 200))

    assert result.findings == () and result.counts["event_outreach_done"] == 1
    assert result.event_states[1] == EventStatus("in_progress")
    assert [read.state for read in result.event_reads] == ["in_progress"]


@pytest.mark.parametrize("title", [ROOM_IN_3_DAYS, ZOOM_IN_3_DAYS], ids=["room", "zoom"])
def test_a_room_in_the_title_gets_the_last_check_3_days_out(title: str) -> None:
    result = renamed_run(title, entry(DESK, 96), entry(REQUESTOR, 200))

    finding = only(result)
    assert (finding.kind, finding.reason, finding.priority) == (
        "event_details_pending",
        "details_room_set",
        "high",
    )
    assert (finding.age_hours, finding.event_in_hours) == (96, 70)
    assert result.event_states[1] == EventStatus("waiting_on_requester", ago(96))


def test_the_room_set_check_waits_stale_hours_after_the_ask_outside_near_days() -> None:
    result = renamed_run(ROOM_IN_3_DAYS, entry(DESK, 10))

    assert result.findings == () and result.counts["event_outreach_done"] == 1


def test_the_room_set_check_is_at_once_within_near_days() -> None:
    result = renamed_run(TOMORROW, entry(DESK, 1), form=event_form(1))

    assert kinds(result) == [("event_details_pending", "details_room_set", "high")]


@pytest.mark.parametrize("title", [TBD_IN_5_DAYS, NO_ROOM_IN_5_DAYS], ids=["tbd", "missing"])
def test_without_a_room_in_the_title_details_pending_is_unchanged(title: str) -> None:
    result = renamed_run(title, entry(DESK, 96), entry(REQUESTOR, 200))

    assert kinds(result) == [("event_details_pending", "details_pending", "normal")]
    assert result.event_states[1].state == "waiting_on_requester"


def test_a_form_ticket_waiting_on_the_requester_is_unchanged() -> None:
    result = form_run(event_form(5), entry(DESK, 96))

    assert kinds(result) == [("event_details_pending", "details_pending", "normal")]
    assert result.event_states[1].state == "waiting_on_requester"


@pytest.mark.parametrize(
    "title",
    ["EVENT [10/13] Example Talk", "EVENT 10/13 2pm Example Talk"],
    ids=["date-only", "no-bracket"],
)
def test_a_title_that_names_no_location_keeps_details_pending(title: str) -> None:
    """Only a location clearly named in the leading bracket quiets the follow-up row."""
    result = renamed_run(title, entry(DESK, 96), entry(REQUESTOR, 200))

    assert kinds(result) == [("event_details_pending", "details_pending", "normal")]
    assert result.event_states[1].state == "waiting_on_requester"
