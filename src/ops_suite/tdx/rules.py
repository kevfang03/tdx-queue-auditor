"""The stale rules: dashboard rows and ticket feeds in, findings out. Pure functions.

The rules are in docs/ARCHITECTURE.md (Module 3, "Stale rules, revised 2026-10-08 after the
first live audit") and in plain language in docs/tdx-rules.md. Only the desk's unclaimed queue
is audited: ``plan_feeds`` picks the unclaimed, non-event tickets old enough to need their
feed, and ``evaluate`` settles every row from the grid plus those feeds. Claimed tickets (the
full-timers') are only counted. No browser, network, config or LLM here.

Event request forms (payload 10): unclaimed tickets still titled like the form
(``RuleSettings.form_titles``) always get their page read, and ``evaluate`` judges them from
the parsed form (``tdx/event_form.py``) and the feed with the outreach rules instead of the
client-waiting rule. Payload 22: renamed ``EVENT [...]`` tickets whose next date is within
``outreach_days`` get their page read too, since the desk renames at first handling, and
every such event's state comes from who wrote last on the feed (``event_status``).

``apply_verdicts`` (payload 06b) takes the follow-up classifier's verdicts on
"requestor wrote last" findings; it is pure too, and the LLM step itself lives in
``tdx/followup.py``.

Personal data rule: titles and names are student data. Findings, counts, summary lines,
``repr()`` and exceptions carry ticket ids, reason codes and hours only.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from ops_suite.tdx.event_form import EventForm
from ops_suite.tdx.events import is_event_title, is_recurring_event, parse_event_title
from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.names import same_person
from ops_suite.tdx.rows import TicketRow

FindingKind = Literal[
    "client_waiting",
    "follow_up_with_client",
    "event_outreach_due",
    "event_details_pending",
    "event_over",
    "location_tbd_soon",
    "needs_review",
    "ready_to_close",
]
Priority = Literal["high", "normal", "low"]
# The follow-up classifier's answer about a requestor's latest reply (payload 06b).
Verdict = Literal["expects_reply", "client_will_follow_up", "no_reply_needed", "unsure"]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FINDING_KINDS: tuple[FindingKind, ...] = get_args(FindingKind)
_PRIORITY_ORDER: dict[Priority, int] = {"high": 0, "normal": 1, "low": 2}
_KIND_LABELS: dict[FindingKind, str] = {
    "client_waiting": "client waiting",
    "follow_up_with_client": "follow up with client",
    "event_outreach_due": "event outreach due",
    "event_details_pending": "event details pending",
    "event_over": "event over",
    "location_tbd_soon": "location TBD soon",
    "needs_review": "needs review",
    "ready_to_close": "ready to close",
}
# A non-requestor seen in the feeds of this many different tickets counts as desk.
DESK_TICKETS_THRESHOLD = 2

# Every key of AuditResult.counts, in summary order.
COUNT_KEYS: tuple[str, ...] = (
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
    "event_in_progress",
    "event_location_missing",
    "desk_members_known",
    *FINDING_KINDS,
)


@dataclass(frozen=True, slots=True)
class RuleSettings:
    stale_hours: int = 48
    event_over_hours: int = 24
    event_tbd_days: int = 3
    extra_staff_names: tuple[str, ...] = ()
    skip_desk_requestors: bool = True
    # Event request forms (tdx.events).
    form_titles: tuple[str, ...] = ("CCI Event Request Form", "SCIS Event Request Form")
    outreach_days: int = 14
    details_pending_days: int = 7
    urgent_days: int = 3
    # An event this close raises its feed-state finding at once, at high priority (payload 22).
    near_event_days: int = 2
    # Desk replied last this many days ago or more, client silent since: ready to close.
    close_after_days: int = 3

    def __repr__(self) -> str:
        return (
            f"RuleSettings(stale_hours={self.stale_hours}, "
            f"event_over_hours={self.event_over_hours}, "
            f"event_tbd_days={self.event_tbd_days}, "
            f"extra_staff_names=<{len(self.extra_staff_names)} names>, "
            f"skip_desk_requestors={self.skip_desk_requestors}, "
            f"form_titles=<{len(self.form_titles)} titles>, "
            f"outreach_days={self.outreach_days}, "
            f"details_pending_days={self.details_pending_days}, "
            f"urgent_days={self.urgent_days}, "
            f"near_event_days={self.near_event_days}, "
            f"close_after_days={self.close_after_days})"
        )


@dataclass(frozen=True, slots=True)
class Finding:
    ticket_id: int
    kind: FindingKind
    priority: Priority
    reason: str  # a code from the rules table, never free text
    age_hours: int | None  # whole hours waiting / since the event / until the event
    # Event outreach findings: whole hours until the next event date (payload 10).
    event_in_hours: int | None = None
    # Outreach due when the requester also wrote last (payload 22 review): whole hours since
    # the requester's latest entry, shown on the one outreach row.
    requester_hours: int | None = None


# Who wrote last on an upcoming event ticket (payload 22), from the visible entries only.
EventFeedState = Literal[
    "no_outreach_yet", "waiting_on_requester", "requester_replied", "in_progress"
]


@dataclass(frozen=True, slots=True)
class EventStatus:
    """An upcoming event's feed state. ``since``: when the desk's latest entry was written
    (waiting on the requester) or the requester's (requester replied); None otherwise or when
    the entry has no date. A time only, never who or what."""

    state: EventFeedState
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditPlan:
    feed_ticket_ids: tuple[int, ...]  # tickets whose feed must be opened, oldest Created first
    # Of those, the ones whose description (the event request form) is read too, ascending.
    form_ticket_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditResult:
    findings: tuple[Finding, ...]  # priority, then age_hours descending, then ticket_id
    counts: Mapping[str, int]  # every key in COUNT_KEYS
    # Event tickets whose title gave no date, ascending; listed by the summary with show_ids.
    event_date_unreadable_ids: tuple[int, ...] = ()
    # Event request forms that could not be read, ascending; listed the same way.
    event_form_unreadable_ids: tuple[int, ...] = ()
    # Renamed event tickets whose page was read but held no form (payload 22), ascending.
    event_form_missing_ids: tuple[int, ...] = ()
    # The feed state of each upcoming event whose page was read and that needs outreach (or
    # whose services are unknown), by ticket id; for the summary's "Events this week".
    event_states: Mapping[int, EventStatus] = field(default_factory=lambda: MappingProxyType({}))


# --- time --------------------------------------------------------------------------------


def _hours(earlier: datetime, later: datetime) -> float:
    """Real elapsed hours. Converting to UTC first matters: Python subtracts two datetimes
    that share a tzinfo as wall-clock times, which is an hour off across a DST change."""
    return (later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds() / 3600


def _whole(hours: float) -> int:
    return int(hours // 1)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _is_stale(then: datetime, now: datetime, rules: RuleSettings) -> bool:
    return _hours(then, now) >= rules.stale_hours


# --- rows --------------------------------------------------------------------------------


def _unique(rows: Sequence[TicketRow]) -> list[TicketRow]:
    """Rows with duplicate ticket ids removed (a ticket can repeat across a page turn)."""
    seen: set[int] = set()
    unique = []
    for row in rows:
        if row.ticket_id not in seen:
            seen.add(row.ticket_id)
            unique.append(row)
    return unique


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def is_form_title(title: str, form_titles: Sequence[str]) -> bool:
    """The title is one of the event request form's titles, ignoring case and spacing."""
    folded = _fold(title)
    return any(folded == _fold(form) for form in form_titles if form.strip())


def is_form_ticket(row: TicketRow, rules: RuleSettings) -> bool:
    """An unclaimed ticket still titled like the event request form (payload 10)."""
    return row.widget == "unclaimed" and is_form_title(row.title, rules.form_titles)


def _next_title_date(
    row: TicketRow, now: datetime, rules: RuleSettings, tz: ZoneInfo
) -> datetime | None:
    """For an unclaimed, non-recurring renamed event ticket (payload 22): its next date when
    that is 0 to ``outreach_days`` days away, else None. Its page is read for that."""
    if row.widget != "unclaimed" or not is_event_title(row.title):
        return None
    if is_recurring_event(row.title):
        return None
    info = parse_event_title(row.title, tz, now)
    upcoming = next((when for when in info.dates if when >= now), None)
    if upcoming is None or _hours(now, upcoming) > rules.outreach_days * 24:
        return None
    return upcoming


def _reads_form(row: TicketRow, now: datetime, rules: RuleSettings, tz: ZoneInfo) -> bool:
    """An event request form at any age, or a renamed event within ``outreach_days``."""
    return is_form_ticket(row, rules) or _next_title_date(row, now, rules, tz) is not None


def _needs_feed(row: TicketRow, now: datetime, rules: RuleSettings, tz: ZoneInfo) -> bool:
    """An unclaimed, non-event ticket created stale_hours ago or more (or with no Created), an
    event request form at any age, or a renamed event within ``outreach_days``."""
    if _reads_form(row, now, rules, tz):
        return True
    if row.widget != "unclaimed" or is_event_title(row.title):
        return False
    return row.created is None or _is_stale(row.created, now, rules)


def _zone(now: datetime, tz: ZoneInfo | None) -> ZoneInfo:
    if tz is not None:
        return tz
    return now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo("UTC")


def plan_feeds(
    rows: Sequence[TicketRow], now: datetime, rules: RuleSettings, tz: ZoneInfo | None = None
) -> AuditPlan:
    """The unclaimed tickets whose page must be opened, oldest Created first (unknown first):
    stale ones for their feed, and event request forms and renamed events within
    ``outreach_days`` for their description and feed. ``tz`` reads the event titles' dates
    (``now``'s zone when omitted)."""
    _require_aware(now)
    zone = _zone(now, tz)
    needed = [row for row in _unique(rows) if _needs_feed(row, now, rules, zone)]
    needed.sort(
        key=lambda row: (
            row.created is not None,
            row.created.astimezone(UTC) if row.created is not None else _EPOCH,
            row.ticket_id,
        )
    )
    forms = sorted(row.ticket_id for row in needed if _reads_form(row, now, rules, zone))
    return AuditPlan(
        feed_ticket_ids=tuple(row.ticket_id for row in needed), form_ticket_ids=tuple(forms)
    )


def event_status(feed: TicketFeed, requestor: str, is_desk: Callable[[str], bool]) -> EventStatus:
    """Who wrote last on an event ticket, from its visible entries (docs/tdx-rules.md,
    "Event state"). The requestor is checked first; anyone who is not desk counts as the
    requester's side (an on-site contact answering for the event, say).

    - no visible desk reply: ``no_outreach_yet``;
    - the requester's side wrote last: ``requester_replied``;
    - the desk wrote last and the requester's side has written since the first desk reply
      (the first outreach): ``in_progress``;
    - the desk wrote last, nothing from the requester's side since the first outreach:
      ``waiting_on_requester``.
    """
    visible = [entry for entry in feed.entries if entry.kind == "reply"]  # newest first

    def by_desk(entry: FeedEntry) -> bool:
        return not same_person(entry.author, requestor) and is_desk(entry.author)

    desk = [index for index, entry in enumerate(visible) if by_desk(entry)]
    if not desk:
        return EventStatus("no_outreach_yet")
    latest = visible[0]
    if not by_desk(latest):
        return EventStatus("requester_replied", latest.created)
    first_outreach = desk[-1]
    if any(not by_desk(entry) for entry in visible[:first_outreach]):
        return EventStatus("in_progress")
    return EventStatus("waiting_on_requester", latest.created)


# --- desk members ------------------------------------------------------------------------


class _People:
    """Distinct people, compared with ``same_person``; each with the tickets it was seen on."""

    def __init__(self) -> None:
        self._names: list[str] = []
        self._tickets: list[set[int]] = []

    def add(self, name: str, ticket_id: int | None = None) -> None:
        if not name:
            return
        index = next((i for i, known in enumerate(self._names) if same_person(name, known)), None)
        if index is None:
            index = len(self._names)
            self._names.append(name)
            self._tickets.append(set())
        if ticket_id is not None:
            self._tickets[index].add(ticket_id)

    def seen_on_at_least(self, tickets: int) -> list[str]:
        return [
            name
            for name, seen in zip(self._names, self._tickets, strict=True)
            if len(seen) >= tickets
        ]

    def names(self) -> list[str]:
        return list(self._names)


def _add_strong(desk: _People, rows: Sequence[TicketRow], feeds: Mapping[int, TicketFeed]) -> None:
    for row in rows:
        if row.widget == "claimed" and row.prim_resp:
            desk.add(row.prim_resp)
    in_run = {row.ticket_id for row in rows}
    for ticket_id, feed in feeds.items():
        for name in feed.desk_authors:
            desk.add(name)
        if ticket_id not in in_run:
            continue  # a feed for a row not in this run
        for entry in feed.entries:
            if entry.kind == "private":
                desk.add(entry.author)


def strong_desk_names(rows: Iterable[TicketRow], feeds: Mapping[int, TicketFeed]) -> list[str]:
    """People this run shows to be desk staff for certain, one name per person.

    Prim Resp names of claimed rows, authors of private notes (only staff can write those)
    and the feed's own ``desk_authors`` (merge notices, reassignments and replies signed with
    a desk signature, payload 18). These are the names the local
    desk-member memory may keep; the 2+-tickets guess and extra_staff_names are not here.
    """
    desk = _People()
    _add_strong(desk, list(rows), feeds)
    return desk.names()


def _desk_members(
    rows: Iterable[TicketRow], feeds: Mapping[int, TicketFeed], rules: RuleSettings
) -> list[str]:
    """Everyone known to be desk staff in this run, one name per person.

    The ``strong_desk_names``; anyone other than the ticket's requestor seen in the feeds of
    2+ different tickets; and the local extra_staff_names list.
    """
    rows = list(rows)
    requestors = {row.ticket_id: row.requestor for row in rows}
    desk = _People()
    _add_strong(desk, rows, feeds)
    for name in rules.extra_staff_names:
        desk.add(name)
    others = _People()
    for ticket_id, feed in feeds.items():
        requestor = requestors.get(ticket_id)
        if requestor is None:
            continue  # a feed for a row not in this run
        for entry in feed.entries:
            if not same_person(entry.author, requestor):
                others.add(entry.author, ticket_id)
    for name in others.seen_on_at_least(DESK_TICKETS_THRESHOLD):
        desk.add(name)
    return desk.names()


# --- evaluation --------------------------------------------------------------------------


def evaluate(
    rows: Sequence[TicketRow],
    feeds: Mapping[int, TicketFeed],
    now: datetime,
    rules: RuleSettings,
    tz: ZoneInfo,
    *,
    known_desk: Callable[[str], bool] | None = None,
    forms: Mapping[int, EventForm] | None = None,
) -> AuditResult:
    """Apply the stale rules to every row. See docs/tdx-rules.md for the table.

    ``forms``: the parsed event request forms by ticket id (payload 10), renamed event
    tickets' included (payload 22). A form ticket whose page was read but has no entry here
    counts as an unreadable form; a renamed one, as ``event_form_missing``.

    ``known_desk``: an extra test for desk staff, such as the local desk-member memory. An
    author it accepts counts as desk; the requestor check still comes first.

    With ``rules.skip_desk_requestors``, a ticket whose requestor is desk staff through a
    strong signal (``strong_desk_names``, extra_staff_names or ``known_desk``; never the
    2+-tickets guess) raises no client_waiting finding and is counted ``internal_requestor``.
    """
    _require_aware(now)
    rows = _unique(rows)
    counts: Counter[str] = Counter({key: 0 for key in COUNT_KEYS})
    findings: list[Finding] = []
    unreadable_events: list[int] = []
    unreadable_forms: list[int] = []
    missing_forms: list[int] = []
    states: dict[int, EventStatus] = {}
    forms = forms or {}
    staff = _desk_members(rows, feeds, rules)
    counts["desk_members_known"] = len(staff)
    # For internal tickets: certain desk staff only, never the 2+-tickets guess.
    strong_staff = [*strong_desk_names(rows, feeds), *rules.extra_staff_names]

    def internal(row: TicketRow) -> bool:
        """The requestor is desk staff for certain: nobody is waiting on this ticket."""
        if not rules.skip_desk_requestors or not row.requestor:
            return False
        if any(same_person(row.requestor, name) for name in strong_staff):
            return True
        return known_desk is not None and known_desk(row.requestor)

    def is_desk(name: str) -> bool:
        return any(same_person(name, known) for known in staff) or (
            known_desk is not None and known_desk(name)
        )

    def add(
        row: TicketRow,
        kind: FindingKind,
        priority: Priority,
        reason: str,
        age: float | None,
        event_in: float | None = None,
    ) -> None:
        whole = _whole(age) if age is not None else None
        until = _whole(event_in) if event_in is not None else None
        findings.append(Finding(row.ticket_id, kind, priority, reason, whole, until))
        counts[kind] += 1

    def judge_event(
        row: TicketRow, feed: TicketFeed, form: EventForm | None, upcoming: datetime
    ) -> None:
        """The feed-state rules for an upcoming event whose page was read (docs/tdx-rules.md,
        "Event state"). ``form`` None: a renamed ticket without a form, services unknown."""
        until = _hours(now, upcoming)
        near = until <= rules.near_event_days * 24
        status = event_status(feed, row.requestor, is_desk)
        states[row.ticket_id] = status
        since = _hours(status.since, now) if status.since is not None else None
        # Whatever the services and whether or not the desk has replied yet: the requester
        # wrote last, so the desk owes a reply, as on any ticket (payload 22 review).
        visible = [entry for entry in feed.entries if entry.kind == "reply"]
        latest = visible[0] if visible else None
        replied = latest is not None and (
            same_person(latest.author, row.requestor) or not is_desk(latest.author)
        )
        waited = _hours(latest.created, now) if replied and latest.created is not None else None
        owed: Priority | None = None  # the client_waiting priority, when one is due
        if replied:
            if internal(row):
                counts["internal_requestor"] += 1
            elif near:
                owed = "high"
            elif waited is not None and waited >= rules.stale_hours:
                owed = "normal"
            elif status.state == "requester_replied":
                counts["client_recent"] += 1
        needs_outreach = (
            form is not None and not form.marketing_only and (form.wants_av or form.wants_room)
        )
        if (
            needs_outreach
            and status.state == "no_outreach_yet"
            and until <= rules.outreach_days * 24
        ):
            # One row per ticket: the outreach email answers the requester too, so a reply
            # owed is folded into the outreach row, at the higher of the two priorities.
            urgent = until <= rules.urgent_days * 24
            priority: Priority = "high" if urgent or owed == "high" else "normal"
            if owed is not None and waited is not None:
                findings.append(
                    Finding(
                        row.ticket_id,
                        "event_outreach_due",
                        priority,
                        "outreach_due_requester_wrote",
                        _whole(until),
                        _whole(until),
                        _whole(waited),
                    )
                )
                counts["event_outreach_due"] += 1
            else:
                add(row, "event_outreach_due", priority, "outreach_due", until, until)
            return
        if owed is not None:
            add(row, "client_waiting", owed, "event_requester_replied", waited, until)
        if form is not None and form.marketing_only:
            counts["event_marketing_only"] += 1
            return
        if form is not None and not (form.wants_av or form.wants_room):
            counts["event_no_outreach"] += 1
            return
        if status.state == "no_outreach_yet":
            # Outside the outreach window; without a form the services are unknown and
            # nothing more is counted (event_form_missing already was).
            if form is not None:
                counts["event_outreach_not_due"] += 1
        elif status.state == "waiting_on_requester":
            if near:
                add(row, "event_details_pending", "high", "details_pending_near", since, until)
            elif (
                since is not None
                and since >= rules.stale_hours
                and until <= rules.details_pending_days * 24
            ):
                add(row, "event_details_pending", "normal", "details_pending", since, until)
            else:
                counts["event_outreach_done"] += 1
        elif status.state == "requester_replied":
            pass  # judged above, with every requester-last event
        else:
            # The requester has answered and the desk wrote last: without the events
            # spreadsheet, finished cannot be told from unfinished, so nothing is raised.
            counts["event_in_progress"] += 1

    for row in rows:
        counts[f"rows_{row.widget}"] += 1
        if row.widget == "claimed":
            continue  # the full-timers' tickets: counted only

        if is_form_ticket(row, rules):
            counts["event_forms"] += 1
            counts["feeds_needed"] += 1
            feed = feeds.get(row.ticket_id)
            if feed is None:
                counts["feed_missing"] += 1
                continue
            form = forms.get(row.ticket_id)
            if form is None or not form.dates:
                counts["event_forms_unreadable"] += 1
                unreadable_forms.append(row.ticket_id)
                continue
            if form.recurring:
                counts["event_forms_recurring"] += 1
                continue
            since_last = _hours(form.dates[-1], now)
            if since_last > rules.event_over_hours:
                add(row, "event_over", "low", "event_over", since_last)
                continue
            upcoming = [when for when in form.dates if when >= now]
            if upcoming:  # else under way or just over: nothing until event_over
                judge_event(row, feed, form, upcoming[0])
            continue

        if is_event_title(row.title):
            counts["event_tickets"] += 1
            if is_recurring_event(row.title):
                counts["event_recurring"] += 1
                continue
            info = parse_event_title(row.title, tz, now)
            if not info.dates:
                counts["event_date_unreadable"] += 1
                unreadable_events.append(row.ticket_id)
                continue
            if len(info.dates) >= 2:
                counts["event_multi_date"] += 1
            # Over only once the last date is past; TBD counts down to the next date.
            since_last = _hours(info.dates[-1], now)
            if since_last > rules.event_over_hours:
                add(row, "event_over", "low", "event_over", since_last)
            upcoming = [when for when in info.dates if when >= now]
            if info.location_missing and upcoming:
                counts["event_location_missing"] += 1
            if (info.location_tbd or info.location_missing) and upcoming:
                until = _hours(now, upcoming[0])
                if until <= rules.event_tbd_days * 24:
                    reason = "tbd_soon" if info.location_tbd else "location_missing"
                    add(row, "location_tbd_soon", "normal", reason, until)
            next_date = _next_title_date(row, now, rules, tz)
            if next_date is None:
                continue  # further out, past or under way: the title is enough
            # Renamed early, at first handling: the form and the feed tell the state.
            counts["feeds_needed"] += 1
            feed = feeds.get(row.ticket_id)
            if feed is None:
                counts["feed_missing"] += 1
                continue
            form = forms.get(row.ticket_id)
            if form is None:
                counts["event_form_missing"] += 1
                missing_forms.append(row.ticket_id)
            judge_event(row, feed, form, next_date)
            continue

        if row.created is None:
            counts["unclaimed_created_unreadable"] += 1
        if not _needs_feed(row, now, rules, tz):
            counts["unclaimed_recent"] += 1
            continue
        counts["feeds_needed"] += 1
        feed = feeds.get(row.ticket_id)
        if feed is None:
            counts["feed_missing"] += 1
            continue

        visible = [entry for entry in feed.entries if entry.kind == "reply"]
        if not visible:
            if internal(row):
                counts["internal_requestor"] += 1
                continue
            age = _hours(row.created, now) if row.created is not None else None
            add(row, "client_waiting", "high", "no_reply_yet", age)
            continue

        latest = visible[0]
        # The requestor is checked first, so staff who opened their own ticket count as
        # the requestor.
        if same_person(latest.author, row.requestor):
            if latest.created is None:
                add(row, "needs_review", "normal", "requestor_last_undated", None)
            elif not _is_stale(latest.created, now, rules):
                counts["client_recent"] += 1
            elif internal(row):
                counts["internal_requestor"] += 1
            else:
                add(row, "client_waiting", "high", "requestor_last", _hours(latest.created, now))
        elif any(same_person(latest.author, name) for name in staff) or (
            known_desk is not None and known_desk(latest.author)
        ):
            # The desk replied last and the requester has been silent since: once that is
            # close_after_days old, the ticket can be closed if nothing new comes in.
            quiet = _hours(latest.created, now) if latest.created is not None else None
            if quiet is not None and quiet >= rules.close_after_days * 24 and not internal(row):
                add(row, "ready_to_close", "low", "desk_replied_last", quiet)
            else:
                counts["desk_replied_last"] += 1
        elif latest.created is None:
            add(row, "needs_review", "normal", "other_last", None)
        elif _is_stale(latest.created, now, rules):
            add(row, "needs_review", "normal", "other_last", _hours(latest.created, now))
        else:
            counts["other_recent"] += 1  # someone acted recently: not stale yet

    findings.sort(key=_finding_order)
    return AuditResult(
        findings=tuple(findings),
        counts=MappingProxyType(dict(counts)),
        event_date_unreadable_ids=tuple(sorted(unreadable_events)),
        event_form_unreadable_ids=tuple(sorted(unreadable_forms)),
        event_form_missing_ids=tuple(sorted(missing_forms)),
        event_states=MappingProxyType(states),
    )


def _finding_order(finding: Finding) -> tuple[int, int, int, int, int]:
    age = finding.age_hours
    return (
        _PRIORITY_ORDER[finding.priority],
        age is None,  # unknown ages after known ones
        -(age or 0),
        finding.ticket_id,
        FINDING_KINDS.index(finding.kind),
    )


# --- the follow-up verdicts (payload 06b) ------------------------------------------------

# verdict -> (kind, priority, reason); "unsure" is absent: it leaves the finding unchanged.
_VERDICT_FINDINGS: dict[Verdict, tuple[FindingKind, Priority, str]] = {
    "expects_reply": ("client_waiting", "high", "expects_reply"),
    "client_will_follow_up": ("follow_up_with_client", "normal", "client_follow_up"),
    "no_reply_needed": ("follow_up_with_client", "normal", "client_no_reply_needed"),
}


def is_follow_up_candidate(finding: Finding) -> bool:
    """A finding the follow-up classifier looks at: client waiting, requestor wrote last."""
    return finding.kind == "client_waiting" and finding.reason == "requestor_last"


def apply_verdicts(result: AuditResult, verdicts: Mapping[int, Verdict]) -> AuditResult:
    """Pure. For each client_waiting/requestor_last finding whose ticket has a verdict:
    expects_reply -> reason "expects_reply"; client_will_follow_up -> kind
    follow_up_with_client, priority "normal", reason "client_follow_up"; no_reply_needed ->
    follow_up_with_client, "normal", "client_no_reply_needed"; unsure -> unchanged.
    Recomputes the kind counts and re-sorts. Every other finding is untouched."""
    findings = []
    for finding in result.findings:
        verdict = verdicts.get(finding.ticket_id)
        change = _VERDICT_FINDINGS.get(verdict) if verdict is not None else None
        if change is not None and is_follow_up_candidate(finding):
            kind, priority, reason = change
            finding = replace(finding, kind=kind, priority=priority, reason=reason)
        findings.append(finding)
    findings.sort(key=_finding_order)
    counts = dict(result.counts)
    for kind in FINDING_KINDS:
        counts[kind] = sum(finding.kind == kind for finding in findings)
    return replace(result, findings=tuple(findings), counts=MappingProxyType(counts))


# --- summary -----------------------------------------------------------------------------


def format_audit_summary(result: AuditResult, *, show_ids: bool) -> list[str]:
    """Run summary lines: counts, and with ``show_ids`` one line per finding, per event with
    an unreadable date, per unreadable event form and per renamed event without a form. Ids,
    reason codes and hours only."""
    counts = result.counts
    findings = ", ".join(f"{_KIND_LABELS[kind]} {counts[kind]}" for kind in FINDING_KINDS)
    internal = (
        f"Internal (desk requester): {counts['internal_requestor']}. "
        if counts["internal_requestor"]
        else ""
    )
    lines = [
        f"Findings: {findings}.",
        f"Checked: {counts['rows_unclaimed']} unclaimed ({counts['feeds_needed']} feeds needed, "
        f"{counts['feed_missing']} missing), {counts['rows_claimed']} claimed (counted only). "
        f"{internal}Events: {counts['event_tickets']}, {counts['event_recurring']} recurring, "
        f"{counts['event_multi_date']} multi-date, "
        f"{counts['event_date_unreadable']} with an unreadable date, "
        f"{counts['event_location_missing']} without a location. "
        f"Event forms: {counts['event_forms']} ({counts['event_forms_recurring']} recurring, "
        f"{counts['event_forms_unreadable']} unreadable); "
        f"{counts['event_form_missing']} renamed without a form. "
        f"Event states (no finding): {counts['event_outreach_done']} waiting on requester, "
        f"{counts['event_in_progress']} in progress, "
        f"{counts['event_outreach_not_due']} outreach not due yet, "
        f"{counts['event_marketing_only']} marketing only, "
        f"{counts['event_no_outreach']} no outreach. "
        f"Desk members known: {counts['desk_members_known']}.",
    ]
    if show_ids:
        for finding in result.findings:
            line = f"  {finding.kind}  {finding.ticket_id}  {finding.reason}"
            if finding.age_hours is not None:
                line += f"  {finding.age_hours}h"
            lines.append(line)
        lines.extend(
            f"  event_date_unreadable  {ticket_id}"
            for ticket_id in result.event_date_unreadable_ids
        )
        lines.extend(
            f"  event_form_unreadable  {ticket_id}"
            for ticket_id in result.event_form_unreadable_ids
        )
        lines.extend(
            f"  event_form_missing  {ticket_id}" for ticket_id in result.event_form_missing_ids
        )
    return lines
