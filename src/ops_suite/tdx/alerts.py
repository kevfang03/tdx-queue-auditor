"""Which audit findings go on the Teams card, the state that remembers what was sent, and
the card itself. Pure, apart from ``AlertState`` reading and writing its file.

The rules are in plain language in docs/tdx-rules.md: alert once per ticket and finding kind,
then again every ``realert_days`` (or ``old_realert_days`` once the finding is
``old_finding_days`` old) while it stays open. A finding that is gone from a ticket judged this
run, or from a queue read in full, deletes its record. The state is recorded only after a
successful post.

The shift summary (``build_summary_card``) lists **all** open findings at a configured time,
marks the ones added since the last post "(added)", and may mention people. It shows the
"Events this week" section first, "(possible duplicate of #<id>)" on rows, and "Ready to
close" last. "Ready to close" findings
stay out of the alert cards and the alert state unless ``alert_ready_to_close``
(``alertable``). The day's first summary posted is that full card; later slots post a delta
(``fit_delta_card``: added findings and due reminders as rows, cleared ones as a count), and
only when it has a row: in a shared chat, an update with nothing to act on is noise.
``SummaryState`` remembers the last slot dealt with, the last one posted and the (ticket id,
kind) set open at that post; ``summary_change`` compares. Between slots, an event finding
within ``near_event_days`` of its event (``urgent_event``) goes to the chat on a short card
of its own (``fit_urgent_card``) or, in a summary run, marked "(event soon)" in the summary.

Every card speaks plainly to its ``Reader``: a desk card names the team that should act
(``teams.desk.team_name``), a personal card speaks to the person running the auditor ("you").
Labels are in the desk's own words, and card times are 12-hour like the ticket titles
(``card_time``).

Personal data rule: the card body is the only place a ticket title goes, and only with
``show_titles`` (on for the personal chat, off for the desk chat by default, because titles
can contain names). The state file,
log records, ``repr()`` and exceptions carry ticket ids, kind strings, times and counts only,
and the card never carries a requestor name, an email address, Prim Resp or feed text.
"""

import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from ops_suite.common.config import DEFAULT_TEAM_NAME, TdxAlertSettings, TdxSettings
from ops_suite.common.teams import CARD_SCHEMA, CARD_VERSION, MentionMode, with_mentions
from ops_suite.tdx.card_time import card_time
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import Finding
from ops_suite.tdx.tickets import ticket_addresses
from ops_suite.tdx.week import WeekEvent, event_details

log = logging.getLogger(__name__)

STATE_FILE_NAME = "alerts-state.json"
DESK_STATE_FILE_NAME = "alerts-state-desk.json"
SUMMARY_STATE_FILE_NAMES = {
    "personal": "summary-state-personal.json",
    "desk": "summary-state-desk.json",
}
CARD_MAX_BYTES = 25_000
_SCHEMA = 1

AlertWhy = Literal["new", "reminder"]
StateKey = tuple[int, str]  # (ticket id, finding kind); kinds are plain strings here

# Card sections, in order. Kinds are plain strings so that a new kind needs only a label
# and a wording entry; an unknown kind gets its own section after these.
SECTION_LABELS: dict[str, str] = {
    "client_waiting": "Requester waiting",
    "follow_up_with_client": "Follow up with requester",
    "needs_review": "Someone else wrote last",
    "event_outreach_due": "Event: email the requester",
    "event_details_pending": "Event: follow up with the requester",
    "location_tbd_soon": "Event soon: no room yet",
    "event_over": "Event over: resolve",
    "ready_to_close": "Ready to close",
}
EVENTS_SECTION_LABEL = "Events this week"
EVENT_SOON_MARK = "(event soon)"
# The outreach rows' alternative-host mailbox when tdx.events.alt_host_mailbox is empty.
SHARED_MAILBOX = "our shared mailbox"
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
# (kind, reason) -> wording; a reason of None matches any reason of that kind. ``{age}`` is
# the finding's age, ``{days}`` the time until the event (event outreach) and
# ``{when}`` the same as "in 5 days" and ``{wrote}`` the time since the requester's latest
# entry. ``{team}`` is the team that answers tickets as the card's reader names it
# (``{Team}`` at the start of a sentence). Cards call the person who opened the ticket "the
# requester", whatever the code or TDX calls them. The event outreach rows are a fixed
# checklist, never a drafted reply: the requester books the room and creates any Zoom
# meeting; the desk asks for the room number to prepare A/V and, for a hybrid or remote
# event, for the link, asks the requester to add ``{mailbox}`` (the desk's mailbox as the
# reader names it) as an alternative host so the desk can start and record the meeting, and
# asks whether to record. The form has no hybrid or remote question, so every outreach row
# lists the Zoom asks.
_OUTREACH_ASK = (
    "the room number. If it's hybrid or remote, also ask for the Zoom link, ask them to add "
    "{mailbox} as an alternative host, and ask whether to record. Then update the events "
    "spreadsheet."
)
WORDING: dict[tuple[str, str | None], str] = {
    ("client_waiting", "requestor_last"): (
        "The requester wrote last, {age} ago: reply or check in"
    ),
    ("client_waiting", "no_reply_yet"): "No reply yet, opened {age} ago",
    # The follow-up classifier's verdicts; "unsure" keeps requestor_last.
    ("client_waiting", "expects_reply"): "The requester has waited {age} for a reply",
    ("client_waiting", "event_requester_replied"): (
        "Event {when}. The requester replied {age} ago: read it and confirm the details"
    ),
    ("follow_up_with_client", "client_follow_up"): (
        "The requester said they'd follow up; quiet {age}: check in"
    ),
    ("follow_up_with_client", "client_no_reply_needed"): (
        "The requester's last message needed no reply; quiet {age}: check in or resolve"
    ),
    ("needs_review", "other_last"): (
        "The last message, {age} ago, came from someone other than the requester or {team}: "
        "check whether the requester still needs a reply"
    ),
    # Every event row starts "Event in 5 days." and goes on in full sentences.
    ("event_outreach_due", "outreach_due_requester_wrote"): (
        "Event in {days}. The requester wrote {wrote} ago. Email them for " + _OUTREACH_ASK
    ),
    ("event_outreach_due", None): "Event in {days}. Email the requester for " + _OUTREACH_ASK,
    ("event_details_pending", "details_pending_near"): (
        "Event {when}. Still waiting on the requester (asked {age} ago): follow up now"
    ),
    # A room in the title means the main detail is in: what may be left is the Zoom side.
    ("event_details_pending", "details_room_set"): (
        "Event {when}. The room is set; confirm the Zoom link and recording if they're still open"
    ),
    ("event_details_pending", None): (
        "Event in {days}. The requester hasn't answered for {age}: follow up"
    ),
    # A title with a date and time but no room never says TBD, so its row says what is there.
    ("location_tbd_soon", "location_missing"): (
        "Event in {age}. No room on the ticket yet. Check the events spreadsheet; if a room is "
        "listed, update the ticket."
    ),
    ("location_tbd_soon", None): (
        "Event in {age}. Location still TBD on the ticket. Check the events spreadsheet; if a "
        "room is listed, update the ticket."
    ),
    ("event_over", None): "Event ended {age} ago: ready to resolve?",
    ("ready_to_close", None): "{Team} replied {age} ago, no reply since: close if nothing new",
}
# A renamed event's feed finding that also stands for its missing room (``Finding.no_room``)
# ends with the room check, so one ticket is one row.
NO_ROOM_WORDING: dict[str, str] = {
    "tbd": (
        "Location still TBD on the ticket: check the events spreadsheet, and if a room is "
        "listed, update the ticket."
    ),
    "missing": (
        "No room on the ticket yet: check the events spreadsheet, and if a room is listed, "
        "update the ticket."
    ),
}
# Summary keys build_card reads from summary_counts.
SUMMARY_KEYS = ("unclaimed", "feeds_read", "feeds_failed")

_MARKDOWN_SPECIAL = re.compile(r"([\\*_\[\]()~`])")
_LEADING_SPECIAL = re.compile(r"^([#>])")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    realert_days: int
    old_finding_days: int
    old_realert_days: int
    max_rows_per_card: int

    @classmethod
    def from_settings(cls, alerts: TdxAlertSettings) -> "AlertPolicy":
        return cls(
            realert_days=alerts.realert_days,
            old_finding_days=alerts.old_finding_days,
            old_realert_days=alerts.old_realert_days,
            max_rows_per_card=alerts.max_rows_per_card,
        )


Audience = Literal["personal", "desk"]


@dataclass(frozen=True, slots=True)
class Reader:
    """Who a card speaks to. A desk card names the team that should act (``team_name``, set
    in ``teams.desk.team_name``); a personal card speaks to the person running the auditor
    ("you"), and its rows name the team generically. Both name the mailbox requesters add as
    a Zoom alternative host (``alt_host_mailbox``, set in ``tdx.events.alt_host_mailbox``)."""

    audience: Audience = "personal"
    team_name: str = DEFAULT_TEAM_NAME
    alt_host_mailbox: str = ""

    @property
    def mailbox(self) -> str:
        """The alternative-host mailbox as a sentence names it: the configured address, or
        ``our shared mailbox`` when none is set."""
        return self.alt_host_mailbox or SHARED_MAILBOX

    @property
    def team(self) -> str:
        """The team as a sentence names it: ``team_name`` on desk cards, the generic name on
        personal ones."""
        return self.team_name if self.audience == "desk" else DEFAULT_TEAM_NAME

    def preview(self, count: int) -> str:
        """The counts-only preview line, plain text: ``3 tickets for the help desk`` on a
        desk card, ``3 tickets need attention`` on a personal one. A desk card says "for"
        the team rather than building its possessive, which no single rule spells well
        for every name."""
        if self.audience == "desk":
            if count == 0:
                return f"Nothing for {self.team} right now"
            return f"{_plural(count, 'ticket')} for {self.team}"
        if count == 0:
            return "Nothing needs your attention right now"
        return "1 ticket needs attention" if count == 1 else f"{count} tickets need attention"

    def summary_lead(self, has_findings: bool) -> str:
        """The full summary's first sentence, as card Markdown (the team's name escaped):
        ``Tickets for the help desk to look at.`` on a desk card, ``Tickets that need your
        attention.`` on a personal one, and ``Nothing ... right now.`` with the queue
        clear."""
        if self.audience == "desk":
            team = escape_markdown(self.team)
            if has_findings:
                return f"Tickets for {team} to look at."
            return f"Nothing for {team} to look at right now."
        if has_findings:
            return "Tickets that need your attention."
        return "Nothing needs your attention right now."


PERSONAL = Reader()


def capitalised(text: str) -> str:
    """``text`` with a capital first letter, the rest unchanged: ``the help desk`` becomes
    ``The help desk``."""
    return text[:1].upper() + text[1:]


@dataclass(frozen=True, slots=True)
class DueAlert:
    finding: Finding
    why: AlertWhy


# --- state -------------------------------------------------------------------------------


class AlertState:
    """``alerts-state.json``: ``{"schema": 1, "alerts": {"<id>:<kind>": {"first_alerted":
    "<ISO UTC>", "last_alerted": "<ISO UTC>"}}}``. Ticket ids, kinds and times only.

    An unreadable file (corrupt, or an unknown schema) is moved aside to ``.bad`` and the
    state starts empty, which re-alerts everything once: the safe failure.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._alerts: dict[StateKey, tuple[datetime, datetime]] = {}
        if path.exists():
            try:
                self._alerts = _parse_state(path.read_text(encoding="utf-8"))
            except (ValueError, KeyError, TypeError, AttributeError):
                log.warning("alerts state unreadable")
                try:
                    os.replace(path, path.with_name(path.name + ".bad"))
                except OSError as error:
                    log.warning(
                        "alerts state not moved aside", extra={"error": type(error).__name__}
                    )

    def __repr__(self) -> str:
        return f"AlertState(records={len(self._alerts)})"

    def __len__(self) -> int:
        return len(self._alerts)

    def get(self, ticket_id: int, kind: str) -> tuple[datetime, datetime] | None:
        """(first_alerted, last_alerted), or None when never alerted (or cleared since)."""
        return self._alerts.get((ticket_id, kind))

    def keys(self) -> list[StateKey]:
        return list(self._alerts)

    def put(self, ticket_id: int, kind: str, first: datetime, last: datetime) -> None:
        self._alerts[(ticket_id, kind)] = (first.astimezone(UTC), last.astimezone(UTC))

    def delete(self, ticket_id: int, kind: str) -> None:
        self._alerts.pop((ticket_id, kind), None)

    def save(self) -> None:
        """Write the file atomically (a temporary file, then ``os.replace``)."""
        data = {
            "schema": _SCHEMA,
            "alerts": {
                f"{ticket_id}:{kind}": {
                    "first_alerted": first.isoformat(),
                    "last_alerted": last.isoformat(),
                }
                for (ticket_id, kind), (first, last) in sorted(self._alerts.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


def _aware(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError("naive time")
    return when


def _parse_state(text: str) -> dict[StateKey, tuple[datetime, datetime]]:
    data = json.loads(text)
    if data["schema"] != _SCHEMA:
        raise ValueError("unknown schema")
    alerts: dict[StateKey, tuple[datetime, datetime]] = {}
    for key, record in data["alerts"].items():
        ticket, kind = key.split(":", 1)
        if not kind:
            raise ValueError("empty kind")
        first = _aware(record["first_alerted"])
        last = _aware(record["last_alerted"])
        alerts[(int(ticket), kind)] = (first, last)
    return alerts


# --- plan --------------------------------------------------------------------------------


def alertable(findings: Sequence[Finding], *, alert_ready_to_close: bool) -> tuple[Finding, ...]:
    """The findings that may go on alert cards and into the alert state: all of them, less
    "ready to close" unless ``alert_ready_to_close`` (it is listed in summaries only)."""
    if alert_ready_to_close:
        return tuple(findings)
    return tuple(finding for finding in findings if finding.kind != "ready_to_close")


# The event findings that need the desk before the event: every one of them is already high
# priority within ``near_event_days``. A requester's reply on an event ticket counts too.
URGENT_EVENT_KINDS = frozenset({"event_details_pending", "event_outreach_due", "location_tbd_soon"})
URGENT_EVENT_REASONS = frozenset({("client_waiting", "event_requester_replied")})


def hours_to_event(finding: Finding) -> int | None:
    """Whole hours until the finding's next event date, or None when it has none.
    ``location_tbd_soon`` carries them as its age; the feed-state findings separately."""
    if finding.kind == "location_tbd_soon":
        return finding.age_hours
    return finding.event_in_hours


def urgent_event(finding: Finding, near_event_days: int) -> bool:
    """Whether ``finding`` is an event finding within ``near_event_days`` of its event: one
    the desk chat hears about at once instead of waiting for the next summary."""
    if finding.kind not in URGENT_EVENT_KINDS and (
        (finding.kind, finding.reason) not in URGENT_EVENT_REASONS
    ):
        return False
    hours = hours_to_event(finding)
    return hours is not None and hours <= near_event_days * 24


@dataclass(frozen=True, slots=True)
class AlertPlan:
    on_card: tuple[DueAlert, ...]
    overflow: int  # due but not on this card
    cleared: tuple[StateKey, ...]

    @property
    def new(self) -> int:
        return sum(due.why == "new" for due in self.on_card)

    @property
    def reminders(self) -> int:
        return sum(due.why == "reminder" for due in self.on_card)

    def trimmed(self, rows: int) -> "AlertPlan":
        """The same plan with the last ``rows`` card rows moved into the overflow count."""
        keep = max(len(self.on_card) - rows, 0)
        moved = len(self.on_card) - keep
        return replace(self, on_card=self.on_card[:keep], overflow=self.overflow + moved)

    def apply_cleared(self, state: AlertState) -> None:
        for ticket_id, kind in self.cleared:
            state.delete(ticket_id, kind)

    def record(self, state: AlertState, now: datetime) -> None:
        """Mark the card's rows as alerted at ``now``. Call only after a successful post."""
        for due in self.on_card:
            finding = due.finding
            existing = state.get(finding.ticket_id, finding.kind)
            first = existing[0] if existing is not None else now
            state.put(finding.ticket_id, finding.kind, first, now)


def _interval(finding: Finding, policy: AlertPolicy) -> timedelta:
    age = finding.age_hours
    if age is not None and age >= policy.old_finding_days * 24:
        return timedelta(days=policy.old_realert_days)
    return timedelta(days=policy.realert_days)


def _kind_order(kind: str) -> tuple[int, str]:
    sections = list(SECTION_LABELS)
    return (sections.index(kind), "") if kind in SECTION_LABELS else (len(sections), kind)


def _due_order(due: DueAlert) -> tuple[object, ...]:
    finding = due.finding
    age = finding.age_hours
    return (
        _PRIORITY_ORDER.get(finding.priority, len(_PRIORITY_ORDER)),
        _kind_order(finding.kind),
        age is None,  # unknown ages after known ones
        -(age or 0),  # oldest first
        finding.ticket_id,
    )


def plan_alerts(
    findings: Sequence[Finding],
    state: AlertState,
    now: datetime,
    policy: AlertPolicy,
    *,
    seen_ticket_ids: Set[int],
    unsettled_ticket_ids: Set[int],
    queue_complete: bool,
) -> AlertPlan:
    """The findings due now (new, or a reminder), and the records to clear.

    A record not among ``findings`` is cleared when its ticket was judged this run (seen,
    and its feed neither failed nor deferred), or when its ticket left a queue that was read
    in full. Otherwise it is kept.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    due: list[DueAlert] = []
    current: set[StateKey] = set()
    for finding in findings:
        key = (finding.ticket_id, finding.kind)
        if key in current:
            continue
        current.add(key)
        record = state.get(*key)
        if record is None:
            due.append(DueAlert(finding, "new"))
        elif now - record[1] >= _interval(finding, policy):
            due.append(DueAlert(finding, "reminder"))
    cleared = [
        key
        for key in state.keys()
        if key not in current
        and _settled(key[0], seen_ticket_ids, unsettled_ticket_ids, queue_complete)
    ]
    due.sort(key=_due_order)
    on_card = tuple(due[: policy.max_rows_per_card])
    return AlertPlan(on_card=on_card, overflow=len(due) - len(on_card), cleared=tuple(cleared))


def _settled(
    ticket_id: int, seen_ticket_ids: Set[int], unsettled_ticket_ids: Set[int], queue_complete: bool
) -> bool:
    """Whether a finding missing from this run is really gone: its ticket was judged (seen,
    and its feed neither failed nor deferred), or it left a queue that was read in full."""
    judged = ticket_id in seen_ticket_ids and ticket_id not in unsettled_ticket_ids
    left_queue = ticket_id not in seen_ticket_ids and queue_complete
    return judged or left_queue


# --- card --------------------------------------------------------------------------------


def escape_markdown(text: str) -> str:
    """Backslash-escape Markdown so a title renders literally: ``\\ * _ [ ] ( ) ~ ` ``
    anywhere, and ``#`` or ``>`` at the start."""
    return _LEADING_SPECIAL.sub(r"\\\1", _MARKDOWN_SPECIAL.sub(r"\\\1", text))


def clean_title(title: str, limit: int) -> str:
    """Control characters removed, whitespace collapsed, cut to ``limit`` with ``…``."""
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in title)
    text = _SPACES.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def format_age(hours: int) -> str:
    """A card's age in words: ``less than an hour``, ``1 hour``, ``N hours`` under 48 hours,
    then ``N days`` (whole days, rounded down). Desk staff read words faster than unit symbols;
    terminal output and logs keep their own short forms."""
    if hours >= 48:
        return f"{hours // 24} days"
    if hours < 1:
        return "less than an hour"
    return "1 hour" if hours == 1 else f"{hours} hours"


def section_label(kind: str) -> str:
    return SECTION_LABELS.get(kind) or kind.replace("_", " ").capitalize()


def finding_wording(finding: Finding, reader: Reader = PERSONAL) -> str:
    """The card wording for a finding, naming the team as ``reader`` does; never fails on an
    unknown kind, reason or age. A finding that also stands for its event's missing room
    (``no_room``) ends with the room check (``NO_ROOM_WORDING``), after a full stop."""
    text = _base_wording(finding, reader)
    room = NO_ROOM_WORDING.get(finding.no_room) if finding.no_room is not None else None
    if room is None:
        return text
    return f"{text if text.endswith('.') else text + '.'} {room}"


def _base_wording(finding: Finding, reader: Reader) -> str:
    template = WORDING.get((finding.kind, finding.reason)) or WORDING.get((finding.kind, None))
    if template is not None and finding.age_hours is not None:
        team = escape_markdown(reader.team)
        values = {
            "age": format_age(finding.age_hours),
            "team": team,
            "Team": capitalised(team),
            "mailbox": escape_markdown(reader.mailbox),
        }
        if finding.event_in_hours is not None:
            values["days"] = format_age(finding.event_in_hours)
            values["when"] = f"in {values['days']}"
        if finding.requester_hours is not None:
            values["wrote"] = format_age(finding.requester_hours)
        try:
            return template.format(**values)
        except KeyError:
            pass  # the template needs a value this finding lacks: the plain wording below
    label = section_label(finding.kind)
    if finding.age_hours is None:
        return label
    return f"{label}, {format_age(finding.age_hours)}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def counts_phrase(new: int, reminders: int) -> str:
    """``3 new, 1 reminder``; just ``3 new`` when no reminder is due."""
    if reminders == 0:
        return f"{new} new"
    return f"{new} new, {_plural(reminders, 'reminder')}"


def fallback_text(count: int, reader: Reader = PERSONAL) -> str:
    """A card's counts-only preview line (see ``Reader.preview``): ``3 tickets for the help
    desk`` on a desk card, ``3 tickets need attention`` on a personal one. ``count`` is a
    number of tickets, not findings, since one ticket can carry two findings (see
    ``ticket_count``). No ids and no titles, so banners and chat previews stay minimal."""
    return reader.preview(count)


def ticket_count(findings: Iterable[Finding]) -> int:
    """How many distinct tickets ``findings`` are about: the number a heading or preview line
    states, since one ticket can carry two findings (one per kind) and would otherwise be
    counted twice."""
    return len({finding.ticket_id for finding in findings})


def plan_ticket_counts(plan: AlertPlan) -> tuple[int, int]:
    """(new, reminders) for the alert card's heading, counting tickets like every other
    heading, since one ticket can carry two findings: the tickets with a new row, then the
    other tickets with a reminder row. Row markers stay per finding."""
    new = {due.finding.ticket_id for due in plan.on_card if due.why == "new"}
    reminders = {due.finding.ticket_id for due in plan.on_card if due.why == "reminder"} - new
    return len(new), len(reminders)


def checked_line(now: datetime, tz: ZoneInfo, summary_counts: Mapping[str, int]) -> str:
    """``Checked Thu 12:05 PM: 26 unclaimed tickets, 7 ticket histories read, 1 could not be
    read.``: the alert card's plain account of the run."""
    failed = summary_counts.get("feeds_failed", 0)
    unread = f", {failed} could not be read" if failed else ""
    return (
        f"Checked {card_time(now, tz, weekday=True, now=now)}: "
        f"{summary_counts.get('unclaimed', 0)} unclaimed tickets, "
        f"{summary_counts.get('feeds_read', 0)} ticket histories read{unread}."
    )


def card_links(
    rows_by_id: Mapping[int, TicketRow], ticket_ids: Sequence[int], tdx: TdxSettings
) -> dict[int, str | None]:
    """The first TDX-host address of each ticket (grid link, then template), else None."""
    links: dict[int, str | None] = {}
    for ticket_id in ticket_ids:
        addresses = ticket_addresses(rows_by_id.get(ticket_id), ticket_id, tdx)
        links[ticket_id] = addresses[0] if addresses else None
    return links


def _link_target(url: str) -> str:
    """Characters that would end a Markdown link target, percent-encoded."""
    for char, code in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        url = url.replace(char, code)
    return url


def _text(text: str, **options: object) -> dict[str, object]:
    return {"type": "TextBlock", "text": text, "wrap": True, **options}


def _card(
    plan: AlertPlan,
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    title_chars: int,
    show_titles: bool,
    reader: Reader,
) -> dict[str, object]:
    checked = checked_line(now, tz, summary_counts)
    if reader.audience == "desk":
        team = capitalised(escape_markdown(reader.team))
        checked = f"{team}, please follow up on these. {checked}"
    new, reminders = plan_ticket_counts(plan)
    body: list[dict[str, object]] = [
        _text(f"TDX queue: {counts_phrase(new, reminders)}", weight="Bolder", size="Medium"),
        _text(checked, isSubtle=True, size="Small", spacing="None"),
    ]
    kinds = sorted({due.finding.kind for due in plan.on_card}, key=_kind_order)
    for kind in kinds:
        rows = [due for due in plan.on_card if due.finding.kind == kind]
        body.append(
            _text(f"{section_label(kind)} ({len(rows)})", weight="Bolder", spacing="Medium")
        )
        for due in rows:
            ticket_id = due.finding.ticket_id
            link = links.get(ticket_id)
            line = f"[#{ticket_id}]({_link_target(link)})" if link else f"#{ticket_id}"
            wording = finding_wording(due.finding, reader)
            if due.why == "reminder":
                wording += " · reminder"
            if not show_titles:
                # One line, no title: "#<id> · <finding wording>".
                body.append(_text(f"{line} · {wording}", spacing="Small"))
                continue
            row = rows_by_id.get(ticket_id)
            if row is not None and row.title.strip():
                line += f" {escape_markdown(clean_title(row.title, title_chars))}"
            body.append(_text(line, spacing="Small"))
            body.append(_text(wording, isSubtle=True, spacing="None"))
    if plan.overflow:
        waiting = (
            "1 more waiting; it comes on the next card."
            if plan.overflow == 1
            else f"{plan.overflow} more waiting; they come on the next cards."
        )
        body.append(_text(waiting, isSubtle=True, spacing="Medium"))
    return {
        "type": "AdaptiveCard",
        "$schema": CARD_SCHEMA,
        "version": CARD_VERSION,
        "fallbackText": fallback_text(ticket_count(due.finding for due in plan.on_card), reader),
        "msteams": {"width": "Full"},
        "body": body,
    }


def card_size(card: Mapping[str, object]) -> int:
    """Bytes of the card as JSON, as it is posted."""
    return len(json.dumps(card).encode("utf-8"))


def fit_card(
    plan: AlertPlan,
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    *,
    title_chars: int,
    show_titles: bool = True,
    reader: Reader = PERSONAL,
) -> tuple[AlertPlan, dict[str, object]]:
    """The card, and the plan actually on it: rows are dropped from the end into the
    overflow count until the card is under ``CARD_MAX_BYTES``. Record the returned plan."""
    while True:
        card = _card(
            plan, rows_by_id, links, now, tz, summary_counts, title_chars, show_titles, reader
        )
        if card_size(card) < CARD_MAX_BYTES or not plan.on_card:
            return plan, card
        plan = plan.trimmed(1)


def build_card(
    plan: AlertPlan,
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    *,
    title_chars: int,
    show_titles: bool = True,
    reader: Reader = PERSONAL,
) -> dict[str, object]:
    """An Adaptive Card 1.4: the heading, one section per kind, and the overflow line.

    Without ``show_titles`` a row is one line, ``#<id> · <finding wording>``, still linked,
    and no ticket title appears anywhere in the card.
    """
    return fit_card(
        plan,
        rows_by_id,
        links,
        now,
        tz,
        summary_counts,
        title_chars=title_chars,
        show_titles=show_titles,
        reader=reader,
    )[1]


# --- shift summary ----------------------------------------------------------------------


_SUMMARY_SCHEMA = 3
_OPEN_SET_SCHEMA = 2  # the same, before "known"


class SummaryState:
    """``summary-state-<destination>.json``: ``{"schema": 3, "last_slot": "<ISO local>",
    "last_posted": "<ISO local>", "open": ["<id>:<kind>", ...], "known": [...]}``. Ticket ids,
    kinds and slot times only.

    ``last_slot`` is the last slot dealt with (posted, or skipped with nothing new to show);
    ``last_posted`` the last slot actually posted, which a delta counts "since"; ``open`` the
    alertable findings open at that post, plus any posted on an urgent event card since.
    ``known`` is every finding the chat has been shown, by a summary or an urgent card, that
    has not cleared since: unlike ``open`` it drops a finding the run it clears, so an urgent
    finding that clears and comes back is posted again. A schema 2 file has no ``known`` and
    starts it as its open set; a schema 1 file (``last_slot`` only) keeps its slot and has no
    post, so the next slot posted is full. An unreadable file is moved aside to ``.bad`` and
    the state starts empty, so the current slot may be posted once more, in full: the safe
    failure.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_slot: datetime | None = None
        self.last_posted: datetime | None = None
        self.open: frozenset[StateKey] = frozenset()
        self.known: frozenset[StateKey] = frozenset()
        if path.exists():
            try:
                self._parse(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError, AttributeError):
                self.last_slot = self.last_posted = None
                self.open = self.known = frozenset()
                log.warning("summary state unreadable")
                try:
                    os.replace(path, path.with_name(path.name + ".bad"))
                except OSError as error:
                    log.warning(
                        "summary state not moved aside", extra={"error": type(error).__name__}
                    )

    def _parse(self, data: dict[str, object]) -> None:
        if data["schema"] == _SCHEMA:
            self.last_slot = _aware(str(data["last_slot"]))
            return
        if data["schema"] not in (_OPEN_SET_SCHEMA, _SUMMARY_SCHEMA):
            raise ValueError("unknown schema")
        last_slot, last_posted = data["last_slot"], data["last_posted"]
        self.last_slot = None if last_slot is None else _aware(str(last_slot))
        self.last_posted = None if last_posted is None else _aware(str(last_posted))
        self.open = _key_set(data["open"])
        self.known = _key_set(data["known"]) if data["schema"] == _SUMMARY_SCHEMA else self.open

    def __repr__(self) -> str:
        return (
            f"SummaryState(last_slot={self.last_slot!r}, last_posted={self.last_posted!r}, "
            f"open={len(self.open)}, known={len(self.known)})"
        )

    def posted(self, slot: datetime) -> bool:
        """Whether ``slot`` (or a later one) was dealt with already: posted, or skipped
        with nothing new to show."""
        return self.last_slot is not None and slot <= self.last_slot

    def full_due(self, slot: datetime) -> bool:
        """Whether ``slot`` gets the full summary: nothing was posted yet on its day (in the
        slot's time zone), so the day's first slot posted, missed first slot or not, is full."""
        if self.last_posted is None:
            return True
        return self.last_posted.astimezone(slot.tzinfo).date() != slot.date()

    def record(
        self, slot: datetime, *, posted: bool = True, open_keys: Set[StateKey] | None = None
    ) -> None:
        """Remember ``slot`` as dealt with (``posted``, or skipped with nothing new to show)
        and the open set, which the chat has now been shown (``known``), and write the file
        atomically. Only after a post, or the decision to skip."""
        self.last_slot = slot
        if posted:
            self.last_posted = slot
        if open_keys is not None:
            self.open = frozenset(open_keys)
            self.known = self.known | self.open
        self._save()

    def update_known(self, known: Set[StateKey], *, shown: Set[StateKey] = frozenset()) -> None:
        """Between slots: set ``known`` (cleared findings already dropped) and add ``shown``,
        the findings an urgent card just posted, to both ``known`` and ``open`` so the next
        delta does not list them as new. Writes the file only when something changed."""
        known = frozenset(known) | frozenset(shown)
        opened = self.open | frozenset(shown)
        if known == self.known and opened == self.open:
            return
        self.known, self.open = known, opened
        self._save()

    def _save(self) -> None:
        data = {
            "schema": _SUMMARY_SCHEMA,
            "last_slot": self.last_slot.isoformat() if self.last_slot else None,
            "last_posted": self.last_posted.isoformat() if self.last_posted else None,
            "open": _key_list(self.open),
            "known": _key_list(self.known),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)


def _key_set(keys: object) -> frozenset[StateKey]:
    if not isinstance(keys, list):
        raise ValueError("not a list")
    return frozenset(_state_key(str(key)) for key in keys)


def _key_list(keys: Set[StateKey]) -> list[str]:
    return [f"{ticket_id}:{kind}" for ticket_id, kind in sorted(keys)]


def _state_key(text: str) -> StateKey:
    ticket, kind = text.split(":", 1)
    if not kind:
        raise ValueError("empty kind")
    return int(ticket), kind


@dataclass(frozen=True, slots=True)
class SummaryChange:
    """How the open findings changed since the last summary post."""

    new: frozenset[StateKey]  # open now, not open at the last post
    cleared: frozenset[StateKey]  # open at the last post and really gone now
    open: frozenset[StateKey]  # what to remember: open now, plus earlier ones not judged now


def summary_change(
    findings: Sequence[Finding],
    previous: Set[StateKey],
    *,
    seen_ticket_ids: Set[int],
    unsettled_ticket_ids: Set[int],
    queue_complete: bool,
) -> SummaryChange:
    """New and cleared findings against ``previous``, the set open at the last post. A finding
    missing now counts as cleared only by the alert state's rule (its ticket judged this run,
    or gone from a queue read in full); otherwise it is neither cleared nor forgotten."""
    current = {(finding.ticket_id, finding.kind) for finding in findings}
    gone = set(previous) - current
    cleared = {
        key
        for key in gone
        if _settled(key[0], seen_ticket_ids, unsettled_ticket_ids, queue_complete)
    }
    return SummaryChange(
        new=frozenset(current - set(previous)),
        cleared=frozenset(cleared),
        open=frozenset(current | (gone - cleared)),
    )


def open_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Each (ticket, kind) once, grouped by kind in section order, then priority, then oldest
    first: the summary's row order."""
    seen: set[StateKey] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.ticket_id, finding.kind)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return tuple(sorted(unique, key=_summary_order))


def _summary_order(finding: Finding) -> tuple[object, ...]:
    age = finding.age_hours
    return (
        _kind_order(finding.kind),
        _PRIORITY_ORDER.get(finding.priority, len(_PRIORITY_ORDER)),
        age is None,
        -(age or 0),
        finding.ticket_id,
    )


def summary_heading(
    total: int,
    new: int,
    at: str,
    late_due: str | None = None,
    since: str | None = None,
    *,
    who: str = "you",
) -> str:
    """``TDX queue at 7:30 AM: 6 tickets for the help desk (2 added since yesterday)`` (just
    ``(2 added)`` without ``since``; nothing when none was added) or ``Queue clear at 7:30
    AM: nothing needs attention.``, with ``(late, due 7:30 AM)`` after the time for a late
    summary. ``total`` and ``new`` count tickets, not findings, because one ticket can carry
    two findings (see ``ticket_count``); ``new`` is the tickets with at least one added
    finding. ``who`` is the card's reader as ``reader_name`` gives it. "Tickets for" the
    reader rather than "open", which reads as open tickets in the queue; "added" rather than
    "new", because these are older tickets newly on the list, not tickets that just came in."""
    when = at if late_due is None else f"{at} (late, due {late_due})"
    if total == 0:
        return f"Queue clear at {when}: nothing needs attention."
    heading = f"TDX queue at {when}: {_plural(total, 'ticket')} for {who}"
    if new == 0:
        return heading
    added = f"{new} added" if since is None else f"{new} added since {since}"
    return f"{heading} ({added})"


def reader_name(reader: Reader) -> str:
    """Who a heading names, as card Markdown: the team on a desk card (escaped), ``you`` on
    a personal one."""
    return escape_markdown(reader.team) if reader.audience == "desk" else "you"


def since_text(since: datetime, slot: datetime, tz: ZoneInfo) -> str:
    """When the last summary was posted, as the full heading says it: ``yesterday`` for the day
    before ``slot``, ``7:30 AM`` for the same day, else ``Fri 5:00 PM`` (``Fri 10/16 5:00 PM`` a
    week or more before ``slot``)."""
    posted = since.astimezone(tz)
    day = slot.astimezone(tz).date()
    if posted.date() == day - timedelta(days=1):
        return "yesterday"
    return card_time(posted, tz, weekday=posted.date() != day, now=slot)


def delta_heading(new: int, reminders: int, since: str, *, who: str = "you") -> str:
    """``Since 7:30 AM: 1 more ticket and 2 reminders for the help desk``: the heading of a
    later slot's update, naming only the parts it has. Both count tickets, so together they
    are the tickets in the rows below, since one ticket can carry two rows: ``new`` the
    tickets with a row added since the last post (marked "(added)"), ``reminders`` the other
    tickets in the rows (due reminders, and urgent findings open at the last post); ``who``
    as ``reader_name`` gives it. "More", because the earlier posts already listed the
    rest."""
    parts = [
        _plural(count, word)
        for count, word in ((new, "more ticket"), (reminders, "reminder"))
        if count
    ]
    return f"Since {since}: {' and '.join(parts)} for {who}"


def _need(count: int) -> str:
    return "needs" if count == 1 else "need"


def delta_lead(total_tickets: int, shown_tickets: int, cleared: int, checked: str) -> str:
    """The update's lead, counting tickets (one ticket can carry two findings): ``4 tickets
    need attention in total: the 2 below, plus 2 posted earlier today.``, or when nothing
    else is open ``1 ticket needs attention: the one below.``, ``2 tickets need attention,
    both below.``, ``3 tickets need attention, all below.``; then ``1 no longer needs
    attention.`` for the tickets cleared since the last post, and ``Checked at 12:00 PM.``

    ``shown_tickets`` are the tickets the update selected, before the card is fitted to size
    (a ``+N more`` line says when rows were dropped). "No longer needs attention" rather than
    "handled": a finding also clears when its event has passed."""
    total = max(total_tickets, shown_tickets)
    earlier = total - shown_tickets
    if total == 0:
        need = "Nothing needs attention."
    elif shown_tickets <= 0:
        need = f"{_plural(total, 'ticket')} {_need(total)} attention, all posted earlier today."
    elif earlier:
        below = "the one below" if shown_tickets == 1 else f"the {shown_tickets} below"
        need = (
            f"{_plural(total, 'ticket')} {_need(total)} attention in total: {below}, "
            f"plus {earlier} posted earlier today."
        )
    elif total == 1:
        need = "1 ticket needs attention: the one below."
    elif total == 2:
        need = "2 tickets need attention, both below."
    else:
        need = f"{total} tickets need attention, all below."
    if cleared:
        need += f" {cleared} no longer {_need(cleared)} attention."
    return f"{need} Checked at {checked}."


def _summary_card(
    findings: Sequence[Finding],
    new_keys: Set[StateKey],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    heading: str,
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    events: Sequence[WeekEvent],
    duplicates: Mapping[int, int],
    urgent_keys: Set[StateKey] = frozenset(),
    reader: Reader = PERSONAL,
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    lead = reader.summary_lead(bool(findings))
    checked = (
        f"Checked {summary_counts.get('unclaimed', 0)} unclaimed tickets at {card_time(now, tz)}."
    )
    body: list[dict[str, object]] = [
        _text(heading, weight="Bolder", size="Medium"),
        _text(f"{lead} {checked}", isSubtle=True, size="Small", spacing="None"),
    ]
    if events:
        # The week's agenda, first: each event also keeps any finding row of its own below.
        body.append(
            _text(f"{EVENTS_SECTION_LABEL} ({len(events)})", weight="Bolder", spacing="Medium")
        )
        for event in events[: max(max_rows, 0)]:
            line = _id_link(event.ticket_id, links)
            details = event_details(event, tz, now)
            title = _shown_title(rows_by_id.get(event.ticket_id), title_chars, show_titles)
            if title is None:
                body.append(_text(f"{line} · {details}", spacing="Small"))
                continue
            body.append(_text(f"{line} {title}", spacing="Small"))
            body.append(_text(details, isSubtle=True, spacing="None"))
        if len(events) > max_rows:
            body.append(_text(f"+{len(events) - max_rows} more", isSubtle=True, spacing="Small"))
    shown = _finding_sections(
        body,
        findings,
        new_keys,
        frozenset(),
        rows_by_id,
        links,
        title_chars,
        show_titles,
        max_rows,
        duplicates,
        urgent_keys,
        reader,
    )
    return shown, _summary_shell(fallback_text(ticket_count(findings), reader), body)


def _summary_shell(fallback: str, body: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "AdaptiveCard",
        "$schema": CARD_SCHEMA,
        "version": CARD_VERSION,
        "fallbackText": fallback,
        "msteams": {"width": "Full"},
        "body": body,
    }


def _finding_sections(
    body: list[dict[str, object]],
    findings: Sequence[Finding],
    new_keys: Set[StateKey],
    reminder_keys: Set[StateKey],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    duplicates: Mapping[int, int],
    urgent_keys: Set[StateKey] = frozenset(),
    reader: Reader = PERSONAL,
) -> tuple[Finding, ...]:
    """One section per kind with its count, at most ``max_rows`` rows in all (``+N more`` per
    section past that), appended to ``body``. Rows in ``urgent_keys`` are marked ``(event
    soon)``; the wording names the team as ``reader`` does. Returns the findings shown by
    id."""
    shown: list[Finding] = []
    budget = max(max_rows, 0)
    for kind in sorted({finding.kind for finding in findings}, key=_kind_order):
        rows = [finding for finding in findings if finding.kind == kind]
        body.append(
            _text(f"{section_label(kind)} ({len(rows)})", weight="Bolder", spacing="Medium")
        )
        visible = rows[:budget]
        budget -= len(visible)
        for finding in visible:
            shown.append(finding)
            ticket_id = finding.ticket_id
            key = (ticket_id, finding.kind)
            line = _id_link(ticket_id, links)
            if key in new_keys:
                line += " (added)"
            if key in urgent_keys:
                line += f" {EVENT_SOON_MARK}"
            if ticket_id in duplicates:
                line += f" (possible duplicate of #{duplicates[ticket_id]})"
            wording = finding_wording(finding, reader)
            if key in reminder_keys:
                wording += " · reminder"
            if not show_titles:
                # One line, no title: "#<id> (added) · <finding wording>".
                body.append(_text(f"{line} · {wording}", spacing="Small"))
                continue
            title = _shown_title(rows_by_id.get(ticket_id), title_chars, show_titles)
            if title is not None:
                line += f" {title}"
            body.append(_text(line, spacing="Small"))
            body.append(_text(wording, isSubtle=True, spacing="None"))
        if len(rows) > len(visible):
            body.append(_text(f"+{len(rows) - len(visible)} more", isSubtle=True, spacing="Small"))
    return tuple(shown)


def _id_link(ticket_id: int, links: Mapping[int, str | None]) -> str:
    link = links.get(ticket_id)
    return f"[#{ticket_id}]({_link_target(link)})" if link else f"#{ticket_id}"


def _shown_title(row: TicketRow | None, title_chars: int, show_titles: bool) -> str | None:
    """The escaped, shortened title when titles are shown and the row has one."""
    if not show_titles or row is None or not row.title.strip():
        return None
    return escape_markdown(clean_title(row.title, title_chars))


def fit_summary_card(
    findings: Sequence[Finding],
    new_keys: Set[StateKey],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    slot: datetime,
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    *,
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    late: bool = False,
    mention: MentionMode = "none",
    mention_people: Sequence[str] = (),
    events: Sequence[WeekEvent] = (),
    duplicates: Mapping[int, int] | None = None,
    since: datetime | None = None,
    urgent_keys: Set[StateKey] = frozenset(),
    reader: Reader = PERSONAL,
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """The summary card and the findings it shows by id (the rest are counted as ``+N
    more``). Rows are dropped from the end until the card, mentions included, is under
    ``CARD_MAX_BYTES``. Record only the returned findings.

    ``events`` is the "Events this week" section, first (at most ``max_rows`` rows);
    ``duplicates`` maps a ticket id to the older ticket it may duplicate; ``since`` is the
    last summary posted, which the heading's "added since" names; ``urgent_keys`` are the
    findings marked ``(event soon)`` instead of getting a card of their own; ``reader``,
    who the card speaks to (the team on a desk card, "you" on a personal one).

    The heading and preview line count tickets, not rows, because one ticket can carry two
    findings: every open finding's ticket for the total (rows trimmed to fit still count),
    and the tickets with at least one added finding for "added". Events this week are not
    counted; an event with no finding is not a ticket to act on."""
    ordered = open_findings(findings)
    total = ticket_count(ordered)
    new = ticket_count(f for f in ordered if (f.ticket_id, f.kind) in new_keys)
    slot_text = card_time(slot, tz)
    since_label = since_text(since, slot, tz) if since is not None else None
    who = reader_name(reader)
    if late:
        heading = summary_heading(total, new, card_time(now, tz), slot_text, since_label, who=who)
    else:
        heading = summary_heading(total, new, slot_text, since=since_label, who=who)
    rows = max_rows
    while True:
        shown, card = _summary_card(
            ordered,
            new_keys,
            rows_by_id,
            links,
            heading,
            now,
            tz,
            summary_counts,
            title_chars,
            show_titles,
            rows,
            events,
            duplicates or {},
            urgent_keys,
            reader,
        )
        card = with_mentions(card, mention, mention_people)
        if card_size(card) < CARD_MAX_BYTES or not shown:
            return shown, card
        rows = len(shown) - 1


def build_summary_card(
    findings: Sequence[Finding],
    new_keys: Set[StateKey],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    slot: datetime,
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    *,
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    late: bool = False,
    mention: MentionMode = "none",
    mention_people: Sequence[str] = (),
    events: Sequence[WeekEvent] = (),
    duplicates: Mapping[int, int] | None = None,
    since: datetime | None = None,
    reader: Reader = PERSONAL,
) -> dict[str, object]:
    """The shift-summary Adaptive Card: the heading (``Queue clear`` when nothing is open, a
    late marker when late), "Events this week" when there are any, every open finding
    grouped by kind with its count, linked ids marked ``(added)`` and ``(possible duplicate of
    #<id>)``, titles only with ``show_titles``, ``+N more`` past ``max_rows``, the counts-only
    preview line, and the optional mentions."""
    return fit_summary_card(
        findings,
        new_keys,
        rows_by_id,
        links,
        slot,
        now,
        tz,
        summary_counts,
        title_chars=title_chars,
        show_titles=show_titles,
        max_rows=max_rows,
        late=late,
        mention=mention,
        mention_people=mention_people,
        events=events,
        duplicates=duplicates,
        since=since,
        reader=reader,
    )[1]


# --- delta summary ----------------------------------------------------------------------


def fit_delta_card(
    findings: Sequence[Finding],
    new_keys: Set[StateKey],
    reminder_keys: Set[StateKey],
    cleared: int,
    total: int,
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    since: datetime,
    now: datetime,
    tz: ZoneInfo,
    summary_counts: Mapping[str, int],
    *,
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    duplicates: Mapping[int, int] | None = None,
    urgent_keys: Set[StateKey] = frozenset(),
    reader: Reader = PERSONAL,
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """A later slot's update card and the findings it shows by id: the heading ``Since 7:30
    AM: N more tickets and M reminders for {who}``, the lead (``delta_lead``: ``total``
    tickets with an open finding in all, ``cleared`` tickets with none left since the last
    post), then rows only for ``findings``: the added ones (``new_keys``, marked
    ``(added)``) and the due reminders (``reminder_keys``, marked ``· reminder``), grouped by
    kind; urgent event
    findings (``urgent_keys``) are marked ``(event soon)``. Cleared findings are a count only;
    no "Events this week" and no mention. Titles only with ``show_titles``; rows are dropped
    from the end until the card is under ``CARD_MAX_BYTES``. The caller posts it only when
    ``findings`` has a row."""
    ordered = open_findings(findings)
    added_ids = {f.ticket_id for f in ordered if (f.ticket_id, f.kind) in new_keys}
    added_tickets = len(added_ids)
    # Tickets too: a ticket with an added row counts once, as added, whatever else it has.
    reminders = len({f.ticket_id for f in ordered} - added_ids)
    heading = delta_heading(added_tickets, reminders, card_time(since, tz), who=reader_name(reader))
    shown_tickets = len({f.ticket_id for f in ordered})
    lead = delta_lead(total, shown_tickets, cleared, card_time(now, tz))
    rows = max_rows
    while True:
        body: list[dict[str, object]] = [
            _text(heading, weight="Bolder", size="Medium"),
            _text(lead, isSubtle=True, size="Small", spacing="None"),
        ]
        shown = _finding_sections(
            body,
            ordered,
            new_keys,
            reminder_keys,
            rows_by_id,
            links,
            title_chars,
            show_titles,
            rows,
            duplicates or {},
            urgent_keys,
            reader,
        )
        card = _summary_shell(fallback_text(total, reader), body)
        if card_size(card) < CARD_MAX_BYTES or not shown:
            return shown, card
        rows = len(shown) - 1


# --- urgent event card ------------------------------------------------------------------


def urgent_heading(count: int) -> str:
    """``Event soon: 2 need follow-up`` (``1 needs follow-up`` for one). ``count`` is the
    number of tickets, not findings: an event with two urgent findings is still one event
    to follow up."""
    return f"Event soon: {count} {'needs' if count == 1 else 'need'} follow-up"


def fit_urgent_card(
    findings: Sequence[Finding],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    now: datetime,
    tz: ZoneInfo,
    *,
    title_chars: int,
    show_titles: bool,
    max_rows: int,
    reader: Reader = PERSONAL,
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """The short card that takes urgent event findings to the desk chat between summaries,
    and the findings it shows by id: the heading ``Event soon: N need follow-up``, the team
    ``reader`` names asked to follow up before the event, then rows like the summary's (linked
    id, the title only with ``show_titles``, the wording), grouped by kind. No mention, and
    the counts-only preview line. Rows are dropped from the end
    until the card is under ``CARD_MAX_BYTES``; record only the returned findings."""
    ordered = open_findings(findings)
    tickets = ticket_count(ordered)
    heading = urgent_heading(tickets)
    ask = "please follow up on these before the event."
    if reader.audience == "desk":
        ask = f"{capitalised(escape_markdown(reader.team))}, {ask}"
    lead = f"{capitalised(ask)} Checked {card_time(now, tz, weekday=True, now=now)}."
    rows = max_rows
    while True:
        body: list[dict[str, object]] = [
            _text(heading, weight="Bolder", size="Medium"),
            _text(lead, isSubtle=True, size="Small", spacing="None"),
        ]
        shown = _finding_sections(
            body,
            ordered,
            frozenset(),
            frozenset(),
            rows_by_id,
            links,
            title_chars,
            show_titles,
            rows,
            {},
            reader=reader,
        )
        card = _summary_shell(fallback_text(tickets, reader), body)
        if card_size(card) < CARD_MAX_BYTES or not shown:
            return shown, card
        rows = len(shown) - 1
