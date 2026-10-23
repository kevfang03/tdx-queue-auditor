"""The shift summary's "Events this week": which events, their state
(from the form and the feed), the order and the row text."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.event_form import EventForm
from ops_suite.tdx.rows import TicketRow, WidgetKind
from ops_suite.tdx.rules import EventStatus, RuleSettings
from ops_suite.tdx.week import WeekEvent, event_details, event_when, events_this_week

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 19, 7, 30, tzinfo=TZ)  # a Monday
RULES = RuleSettings()
FORM_TITLE = "CCI Event Request Form"


def row(ticket_id: int, title: str = FORM_TITLE, widget: WidgetKind = "unclaimed") -> TicketRow:
    return TicketRow(
        widget=widget,
        ticket_id=ticket_id,
        title=title,
        requestor="Jordan Sampleton",
        resp_group="IT Service Desk",
        created=NOW - timedelta(days=3),
        modified=None,
        prim_resp=None,
        status=None,
        detail_url="",
    )


def form(
    *days: float,
    av: bool = True,
    room: bool = False,
    marketing: bool = False,
    recurring: bool | None = False,
    space: str | None = "lobby",
    has_time: bool = True,
    end: time | None = None,
) -> EventForm:
    return EventForm(
        dates=tuple(NOW + timedelta(days=day) for day in days),
        recurring=recurring,
        wants_av=av,
        wants_room=room,
        marketing_only=marketing,
        unknown_options=0,
        has_time=has_time,
        end_time=end,
        space=space,  # type: ignore[arg-type]
    )


def week(
    rows: list[TicketRow],
    forms: dict[int, EventForm] | None = None,
    event_states: dict[int, EventStatus] | None = None,
    *,
    rules: RuleSettings = RULES,
    days: int = 7,
) -> tuple[WeekEvent, ...]:
    return events_this_week(rows, forms or {}, event_states or {}, NOW, TZ, rules, days)


def states(events: tuple[WeekEvent, ...]) -> dict[int, str]:
    return {event.ticket_id: event.state for event in events}


def test_forms_and_titled_events_inside_the_window_only() -> None:
    rows = [
        row(1),  # form, 3 days out
        row(2),  # form, 10 days out
        row(3),  # form, yesterday
        row(4, "EVENT [10/21 2:00 PM - 4:00 PM Room 210] Panel"),  # in 2 days
        row(5, "EVENT [10/30 2pm Room 210] Panel"),  # in 11 days
        row(6, "EVENT [10/12 2pm Room 210] Panel"),  # a week ago
    ]
    forms = {1: form(3), 2: form(10), 3: form(-1)}

    assert [event.ticket_id for event in week(rows, forms)] == [4, 1]


def test_the_window_runs_from_the_summary_time_for_events_days() -> None:
    rows = [row(1), row(2)]
    forms = {1: form(-0.01), 2: form(2.5)}

    assert [e.ticket_id for e in week(rows, forms, days=7)] == [2]
    assert week(rows, forms, days=2) == ()


def test_a_multi_date_event_shows_its_next_date_in_the_window() -> None:
    rows = [row(1, "EVENT [10/18, 10/22, 10/29 3pm Room 101] Series")]

    (event,) = week(rows)

    assert event.when == datetime(2026, 10, 22, 15, 0, tzinfo=TZ)


def test_recurring_unreadable_and_claimed_events_are_left_out() -> None:
    rows = [
        row(1),  # recurring form
        row(2),  # form that could not be read
        row(3, "EVENT [Weekly, Mon 5pm Room 210] Club"),
        row(4, "EVENT [someday] Club"),
        row(5, "EVENT [10/21 2pm Room 210] Panel", widget="claimed"),
        row(6, "Laptop will not connect"),
    ]
    forms = {1: form(2, recurring=True)}

    assert week(rows, forms) == ()


def test_each_form_state() -> None:
    rows = [row(i) for i in range(1, 8)]
    forms = {
        1: form(2),
        2: form(3),
        3: form(4),
        4: form(5, marketing=True),
        5: form(5, av=False, room=False),
        6: form(6),
        7: form(6, room=True, av=False),
    }
    event_states = {
        1: EventStatus("no_outreach_yet"),
        2: EventStatus("waiting_on_requester", NOW - timedelta(hours=60)),
        3: EventStatus("requester_replied", NOW - timedelta(hours=50)),
        6: EventStatus("in_progress"),
    }

    assert states(week(rows, forms, event_states)) == {
        1: "no_outreach_yet",
        2: "waiting_on_requester",
        3: "requester_replied",
        4: "marketing_only",
        5: "no_outreach",
        6: "in_progress",
        7: "not_read",  # its page could not be read in this run
    }


def test_renamed_event_states_come_from_the_title_then_the_form_and_feed() -> None:
    rows = [
        row(1, "EVENT [10/21 2pm Room 210] Panel"),
        row(2, "EVENT [10/22 2pm TBD] Talk"),
        row(3, "EVENT [10/22 6PM-7PM] Resume Review"),
        row(4, "EVENT [10/23 2pm Room 210] Panel"),
        row(5, "EVENT [10/23 3pm Room 210] Panel"),
        row(6, "EVENT [10/24 3pm Room 210] Panel"),
    ]
    forms = {1: form(2, space="classroom", room=True, av=False), 5: form(4, marketing=True)}
    event_states = {
        1: EventStatus("waiting_on_requester", NOW - timedelta(hours=3)),
        2: EventStatus("requester_replied", NOW - timedelta(hours=1)),
        3: EventStatus("in_progress"),
        4: EventStatus("no_outreach_yet"),  # a renamed ticket without a form
    }

    events = week(rows, forms, event_states)

    assert states(events) == {
        1: "waiting_on_requester",
        2: "location_tbd",
        3: "location_missing",
        4: "no_outreach_yet",
        5: "marketing_only",
        6: "not_read",
    }
    by_id = {event.ticket_id: event for event in events}
    assert (by_id[1].space, by_id[1].av) == ("classroom", False)  # from the form
    assert (by_id[4].space, by_id[4].av) == (None, None)  # no form: unknown
    assert by_id[1].since == NOW - timedelta(hours=3)
    assert by_id[2].since is None  # a time is shown for waiting on the requester only


def test_sorted_by_date_then_id() -> None:
    rows = [row(3), row(1), row(2), row(4, "EVENT [10/20 9am Room 1] Early")]
    forms = {1: form(4), 2: form(2), 3: form(4)}

    assert [event.ticket_id for event in week(rows, forms)] == [4, 2, 1, 3]


def test_the_row_text_has_date_times_space_av_and_state_only() -> None:
    timed = WeekEvent(1, datetime(2026, 10, 21, 15, 0, tzinfo=TZ), True, time(17, 0), "lobby",
                      True, "no_outreach_yet")  # fmt: skip
    start_only = WeekEvent(2, datetime(2026, 10, 22, 9, 30, tzinfo=TZ), True, None,
                           "classroom", False, "in_progress")  # fmt: skip
    untimed = WeekEvent(3, datetime(2026, 10, 23, 23, 59, tzinfo=TZ), False, None, None, None,
                        "location_missing")  # fmt: skip
    asked = datetime(2026, 10, 20, 8, 26, tzinfo=TZ)
    waiting = WeekEvent(4, datetime(2026, 10, 23, 14, 0, tzinfo=TZ), True, None, None, True,
                        "waiting_on_requester", asked)  # fmt: skip

    assert event_details(timed, TZ) == (
        "Wed 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · not emailed yet"
    )
    assert event_details(start_only, TZ) == (
        "Thu 10/22 9:30 AM · Classroom · A/V on form: no · in progress (requester answered)"
    )
    assert event_details(untimed, TZ) == "Fri 10/23 · no form · location missing"
    assert event_details(waiting, TZ) == (
        "Fri 10/23 2:00 PM · — · A/V on form: yes · waiting on requester since Tue 8:26 AM"
    )


def test_an_event_across_noon_names_both_halves_of_the_day() -> None:
    event = WeekEvent(1, datetime(2026, 10, 21, 11, 30, tzinfo=TZ), True, time(12, 30),
                      "classroom", True, "requester_replied")  # fmt: skip

    assert event_when(event, TZ) == "Wed 10/21 11:30 AM–12:30 PM"


def test_event_times_are_shown_in_the_display_time_zone() -> None:
    # The same instant, read with a different display time zone: the card follows it.
    asked = datetime(2026, 10, 20, 12, 26, tzinfo=ZoneInfo("UTC"))
    event = WeekEvent(1, datetime(2026, 10, 23, 18, 0, tzinfo=ZoneInfo("UTC")), True, None,
                      None, True, "waiting_on_requester", asked)  # fmt: skip

    assert event_details(event, ZoneInfo("America/Los_Angeles")) == (
        "Fri 10/23 11:00 AM · — · A/V on form: yes · waiting on requester since Tue 5:26 AM"
    )


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ("no_outreach_yet", "not emailed yet"),
        ("waiting_on_requester", "waiting on requester"),
        ("requester_replied", "requester replied"),
        ("in_progress", "in progress (requester answered)"),
        ("location_missing", "location missing"),
        ("location_tbd", "location TBD"),
        ("marketing_only", "marketing only"),
        ("no_outreach", "no email needed"),
        ("not_read", "not checked yet"),
    ],
)
def test_state_labels(state: str, label: str) -> None:
    event = WeekEvent(1, NOW, True, None, None, None, state)  # type: ignore[arg-type]

    assert event_details(event, TZ).endswith(f" · {label}")


@pytest.mark.parametrize(
    ("space", "label"), [("lobby", "Lobby"), ("classroom", "Classroom"), ("other", "Other")]
)
def test_space_labels(space: str, label: str) -> None:
    rows = [row(1)]

    (event,) = week(rows, {1: form(2, space=space)})

    assert f" · {label} · " in event_details(event, TZ)


def test_a_form_without_a_time_shows_the_date_only() -> None:
    (event,) = week([row(1)], {1: form(2, has_time=False)})

    assert event_when(event, TZ) == "Wed 10/21"


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        events_this_week([], {}, {}, NOW.replace(tzinfo=None), TZ, RULES, 7)
