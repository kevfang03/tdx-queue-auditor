"""The shift summary's "Events this week": the coming week's agenda of
unclaimed event tickets. Pure functions.

Every unclaimed event ticket with a date between the summary time and ``days`` later: event
request forms and renamed ``EVENT [...]`` tickets. One row per event, at its next
date in that window, with its state:

- a renamed title with TBD, or with a date and time but no location: ``location TBD`` or
  ``location missing``;
- a form needing no outreach: ``marketing only`` or ``no outreach needed``;
- otherwise the feed state from the rules (``rules.event_status``): ``no outreach yet``,
  ``waiting on requester since Tue 08:26``, ``requester replied`` or ``in progress
  (requester answered)``; ``page not read`` when this run could not read the ticket's page.

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
    "location_missing",
    "location_tbd",
    "marketing_only",
    "no_outreach",
    "not_read",
]
STATE_LABELS: dict[EventState, str] = {
    "no_outreach_yet": "no outreach yet",
    "waiting_on_requester": "waiting on requester",
    "requester_replied": "requester replied",
    "in_progress": "in progress (requester answered)",
    "location_missing": "location missing",
    "location_tbd": "location TBD",
    "marketing_only": "marketing only",
    "no_outreach": "no outreach needed",
    "not_read": "page not read",
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
    """``Tue 10/21 15:00–17:00``; ``Tue 10/21 15:00`` without an end; ``Tue 10/21`` without
    a time."""
    local = event.when.astimezone(tz)
    text = f"{local:%a} {local.month}/{local.day}"
    if not event.has_time:
        return text
    text += f" {local:%H:%M}"
    if event.end_time is not None:
        text += f"–{event.end_time:%H:%M}"
    return text


def event_state_label(event: WeekEvent, tz: ZoneInfo) -> str:
    """``waiting on requester since Tue 08:26`` when that time is known, else the label."""
    label = STATE_LABELS[event.state]
    if event.since is not None:
        local = event.since.astimezone(tz)
        label += f" since {local:%a %H:%M}"
    return label


def event_details(event: WeekEvent, tz: ZoneInfo) -> str:
    """``Tue 10/21 15:00–17:00 · Lobby · A/V on form: yes · no outreach yet``: no names, no
    free text. "A/V on form" because only the form's checklist is read: A/V asked for later
    is not seen, so "no" does not mean "no A/V needed"."""
    space = SPACE_LABELS[event.space] if event.space is not None else "—"
    av = av_on_form_label(event.av)
    return f"{event_when(event, tz)} · {space} · A/V on form: {av} · {event_state_label(event, tz)}"
