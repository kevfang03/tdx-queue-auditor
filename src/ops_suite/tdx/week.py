"""The shift summary's "Events this week": the coming week's agenda of
unclaimed event tickets. Pure functions.

Every unclaimed event ticket with a date between the summary time and ``days`` later: event
request forms and renamed ``EVENT [...]`` tickets. One row per event, at its next
date in that window, with its state:

- a renamed title with TBD, or with a date and time but no location: ``location TBD`` or
  ``location missing``;
- a form needing no outreach: ``marketing only`` or ``no email needed``;
- otherwise the feed state from the rules (``AuditResult.event_states``): ``not emailed
  yet``, ``waiting on requester since Tue 8:26 AM``, ``requester replied`` or ``in
  progress``; ``not checked yet`` when this run could not read the ticket's page. "In
  progress" covers a requester who has answered and also the events the rules keep quiet
  until close to the date: a desk entry newer than the requester's message, or a room in
  the title while the desk waits on the requester. ``details confirmed``: a room in the title
  and a desk private note after the desk's last reply, so the desk has the rest in hand;
  like ``in progress``, it has no time.

The labels are in the desk's own words ("emailed", not "outreach"), and the times are
12-hour like the ticket titles (``card_time``).

Space and "A/V on form" come from the form, renamed tickets' included; "—" when there is
no form.
Recurring events and events whose date cannot be read are left out, as the rules leave them.

Personal data rule: a ``WeekEvent`` holds an id, times, a space category, an A/V flag and a
state code. Never a name, a title or a form answer; handler names are never read at all.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ops_suite.tdx.card_time import card_time, clock_range, clock_text
from ops_suite.tdx.event_form import EventForm, SpaceType
from ops_suite.tdx.events import is_event_title, is_recurring_event, parse_event_title
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import (
    EventStatus,
    RuleSettings,
    av_on_form_label,
    event_state,
    is_form_ticket,
)

EventState = Literal[
    "no_outreach_yet",
    "waiting_on_requester",
    "requester_replied",
    "in_progress",
    "details_confirmed",
    "location_missing",
    "location_tbd",
    "marketing_only",
    "no_outreach",
    "not_read",
]
STATE_LABELS: dict[EventState, str] = {
    "no_outreach_yet": "not emailed yet",
    "waiting_on_requester": "waiting on requester",
    "requester_replied": "requester replied",
    "in_progress": "in progress",
    "details_confirmed": "details confirmed",
    "location_missing": "location missing",
    "location_tbd": "location TBD",
    "marketing_only": "marketing only",
    "no_outreach": "no email needed",
    "not_read": "not checked yet",
}
SPACE_LABELS: dict[SpaceType, str] = {"lobby": "Lobby", "classroom": "Classroom", "other": "Other"}


@dataclass(frozen=True, slots=True)
class WeekEvent:
    ticket_id: int
    when: datetime  # the next date in the window; the end of the day when no time was given
    has_time: bool
    end_time: time | None
    space: SpaceType | None  # None: no form, or a form whose space was not read
    av: bool | None  # None: no form to read it from
    state: EventState
    # Waiting on the requester: when the desk's latest entry was written, if dated.
    since: datetime | None = None


def _next_in(dates: Sequence[datetime], start: datetime, end: datetime) -> datetime | None:
    return next((when for when in sorted(dates) if start <= when <= end), None)


def _state(form: EventForm | None, status: EventStatus | None) -> EventState:
    return event_state(form, status.state if status is not None else "not_read")


def _event(
    ticket_id: int,
    when: datetime,
    has_time: bool,
    end_time: time | None,
    form: EventForm | None,
    state: EventState,
    status: EventStatus | None,
) -> WeekEvent:
    space = form.space if form is not None else None
    av = form.wants_av if form is not None else None
    since = status.since if state == "waiting_on_requester" and status is not None else None
    return WeekEvent(ticket_id, when, has_time, end_time, space, av, state, since)


def events_this_week(
    rows: Sequence[TicketRow],
    forms: Mapping[int, EventForm],
    states: Mapping[int, EventStatus],
    now: datetime,
    tz: ZoneInfo,
    rules: RuleSettings,
    days: int,
) -> tuple[WeekEvent, ...]:
    """The unclaimed events with a date from ``now`` to ``days`` days later, sorted by that
    date, then id. ``forms`` are the parsed event request forms by ticket id; ``states``
    the rules' feed states (``AuditResult.event_states``)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    end = now + timedelta(days=days)
    seen: set[int] = set()
    events: list[WeekEvent] = []
    for row in rows:
        if row.widget != "unclaimed" or row.ticket_id in seen:
            continue
        seen.add(row.ticket_id)
        form = forms.get(row.ticket_id)
        status = states.get(row.ticket_id)
        if is_form_ticket(row, rules):
            if form is None or form.recurring:
                continue
            when = _next_in(form.dates, now, end)
            if when is None:
                continue
            state = _state(form, status)
            events.append(
                _event(row.ticket_id, when, form.has_time, form.end_time, form, state, status)
            )
        elif is_event_title(row.title) and not is_recurring_event(row.title):
            info = parse_event_title(row.title, tz, now)
            when = _next_in(info.dates, now, end)
            if when is None:
                continue
            if info.location_tbd:
                state = "location_tbd"
            elif info.location_missing:
                state = "location_missing"
            else:
                state = _state(form, status)
            events.append(
                _event(row.ticket_id, when, info.has_time, info.end_time, form, state, status)
            )
    events.sort(key=lambda event: (event.when, event.ticket_id))
    return tuple(events)


def event_when(event: WeekEvent, tz: ZoneInfo) -> str:
    """``Tue 10/21 3:00–5:00 PM``; ``Tue 10/21 3:00 PM`` without an end; ``Tue 10/21``
    without a time."""
    local = event.when.astimezone(tz)
    text = f"{local:%a} {local.month}/{local.day}"
    if not event.has_time:
        return text
    if event.end_time is not None:
        return f"{text} {clock_range(local, event.end_time)}"
    return f"{text} {clock_text(local)}"


def event_state_label(event: WeekEvent, tz: ZoneInfo, now: datetime | None = None) -> str:
    """``waiting on requester since Tue 8:26 AM`` when that time is known, else the label.
    A time a week or more from ``now``, the summary's own time, shows its date too (``since
    Tue 10/20 1:37 PM``), so it does not read as the past few days."""
    label = STATE_LABELS[event.state]
    if event.since is not None:
        label += f" since {card_time(event.since, tz, weekday=True, now=now)}"
    return label


def event_details(event: WeekEvent, tz: ZoneInfo, now: datetime | None = None) -> str:
    """``Tue 10/21 3:00–5:00 PM · Lobby · A/V on form: yes · not emailed yet``: no names, no
    free text. "A/V on form" because only the form's checklist is read: A/V asked for later
    is not seen, so "no" does not mean "no A/V needed". Without a form the line says ``no
    form`` once; a form whose space was not read keeps ``—`` for the space alone. ``now`` is
    the summary's time, which dates a waiting time a week or more away (``event_state_label``)."""
    when = event_when(event, tz)
    state = event_state_label(event, tz, now)
    if event.space is None and event.av is None:
        return f"{when} · no form · {state}"
    space = SPACE_LABELS[event.space] if event.space is not None else "—"
    av = av_on_form_label(event.av)
    return f"{when} · {space} · A/V on form: {av} · {state}"
