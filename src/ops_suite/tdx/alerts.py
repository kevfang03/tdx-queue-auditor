"""Which audit findings go on the Teams card, the state that remembers what was sent, and
the card itself. Pure, apart from ``AlertState`` reading and writing its file.

The rules are in plain language in docs/tdx-rules.md: alert once per ticket and finding kind,
then again every ``realert_days`` (or ``old_realert_days`` once the finding is
``old_finding_days`` old) while it stays open. A finding that is gone from a ticket judged this
run, or from a queue read in full, deletes its record. The state is recorded only after a
successful post.

The shift summary (``build_summary_card``) lists **all** open findings at a configured time,
marks the new ones, and may mention people. It shows the "Events this week" section first,
"(possible duplicate of #<id>)" on rows, and "Ready to close" last. "Ready to close" findings
stay out of the alert cards and the alert state unless ``alert_ready_to_close``
(``alertable``). The day's first summary posted is that full card; later slots post a delta
(``fit_delta_card``: new findings and due reminders as rows, cleared ones as a count).
``SummaryState`` remembers the last slot dealt with, the last one posted and the (ticket id,
kind) set open at that post; ``summary_change`` compares. Between slots, an event finding
within ``near_event_days`` of its event (``urgent_event``) goes to the chat on a short card
of its own (``fit_urgent_card``) or, in a summary run, marked "(event soon)" in the summary.

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
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from ops_suite.common.config import TdxAlertSettings, TdxSettings
from ops_suite.common.teams import CARD_SCHEMA, CARD_VERSION, MentionMode, with_mentions
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
    "client_waiting": "Client waiting",
    "follow_up_with_client": "Follow up with client",
    "needs_review": "Needs review",
    "event_outreach_due": "Event outreach due",
    "event_details_pending": "Event details pending",
    "location_tbd_soon": "Location TBD soon",
    "event_over": "Event over",
    "ready_to_close": "Ready to close",
}
EVENTS_SECTION_LABEL = "Events this week"
EVENT_SOON_MARK = "(event soon)"
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
# (kind, reason) -> wording; a reason of None matches any reason of that kind. ``{age}`` is
# the finding's age, ``{days}`` the time until the event (event outreach) and
# ``{when}`` the same as "in 5 days" and ``{wrote}`` the time since the requester's latest
# entry. The event outreach rows are a fixed checklist, never a drafted reply.
WORDING: dict[tuple[str, str | None], str] = {
    ("client_waiting", "requestor_last"): "Requestor wrote last {age} ago: reply or check in",
    ("client_waiting", "no_reply_yet"): "No reply yet, opened {age} ago",
    # The follow-up classifier's verdicts; "unsure" keeps requestor_last.
    ("client_waiting", "expects_reply"): "Requestor is waiting for a reply, {age}",
    ("client_waiting", "event_requester_replied"): (
        "Event {when}: requester replied {age} ago: process and confirm"
    ),
    ("follow_up_with_client", "client_follow_up"): (
        "Client said they'd follow up; quiet {age}: check in"
    ),
    ("follow_up_with_client", "client_no_reply_needed"): (
        "Client's last message needs no reply; quiet {age}: check in or resolve"
    ),
    ("needs_review", "other_last"): "Someone else wrote last {age} ago: take a look",
    ("event_outreach_due", "outreach_due_requester_wrote"): (
        "Event in {days}: reach out. Confirm the room · Zoom link if hybrid · desk mailbox as "
        "Zoom alt host · recording? · then the events sheet; requester wrote {wrote} ago"
    ),
    ("event_outreach_due", None): (
        "Event in {days}: reach out. Confirm the room · Zoom link if hybrid · desk mailbox as "
        "Zoom alt host · recording? · then the events sheet"
    ),
    ("event_details_pending", "details_pending_near"): (
        "Event {when}: still waiting on the requester (asked {age} ago): follow up now"
    ),
    ("event_details_pending", None): "Event in {days}; no reply to the desk for {age}: follow up",
    ("location_tbd_soon", None): (
        "Location still TBD on the ticket, event in {age}. Check the events spreadsheet; if a "
        "room is listed, update the ticket."
    ),
    ("event_over", None): "Event ended {age} ago: ready to resolve?",
    ("ready_to_close", None): "Desk replied {age} ago, no reply since: close if nothing new",
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
    """``N h`` under 48 hours, ``N days`` (whole days, rounded down) from 48."""
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def section_label(kind: str) -> str:
    return SECTION_LABELS.get(kind) or kind.replace("_", " ").capitalize()


def finding_wording(finding: Finding) -> str:
    """The card wording for a finding; never fails on an unknown kind, reason or age."""
    template = WORDING.get((finding.kind, finding.reason)) or WORDING.get((finding.kind, None))
    if template is not None and finding.age_hours is not None:
        values = {"age": format_age(finding.age_hours)}
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
    """``3 new, 1 reminder``."""
    return f"{new} new, {_plural(reminders, 'reminder')}"


def fallback_text(new: int, reminders: int) -> str:
    """The card's counts-only preview line: ``ops-suite: 3 tickets need attention (2 new,
    1 reminder)``. No ids and no titles, so banners and chat previews stay minimal."""
    total = new + reminders
    tickets = "1 ticket needs" if total == 1 else f"{total} tickets need"
    return f"ops-suite: {tickets} attention ({counts_phrase(new, reminders)})"


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
) -> dict[str, object]:
    body: list[dict[str, object]] = [
        _text(
            f"TDX queue: {counts_phrase(plan.new, plan.reminders)}",
            weight="Bolder",
            size="Medium",
        ),
        _text(
            f"Read-only audit, {now.astimezone(tz):%a %H:%M}. "
            f"Checked {summary_counts.get('unclaimed', 0)} unclaimed tickets; "
            f"{summary_counts.get('feeds_read', 0)} feeds read, "
            f"{summary_counts.get('feeds_failed', 0)} failed.",
            isSubtle=True,
            size="Small",
            spacing="None",
        ),
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
            wording = finding_wording(due.finding)
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
        "fallbackText": fallback_text(plan.new, plan.reminders),
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
) -> tuple[AlertPlan, dict[str, object]]:
    """The card, and the plan actually on it: rows are dropped from the end into the
    overflow count until the card is under ``CARD_MAX_BYTES``. Record the returned plan."""
    while True:
        card = _card(plan, rows_by_id, links, now, tz, summary_counts, title_chars, show_titles)
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
    )[1]


# --- shift summary ----------------------------------------------------------------------


_SUMMARY_SCHEMA = 3
_OPEN_SET_SCHEMA = 2  # the same, before "known"


class SummaryState:
    """``summary-state-<destination>.json``: ``{"schema": 3, "last_slot": "<ISO local>",
    "last_posted": "<ISO local>", "open": ["<id>:<kind>", ...], "known": [...]}``. Ticket ids,
    kinds and slot times only.

    ``last_slot`` is the last slot dealt with (posted, or skipped because nothing changed);
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
        because nothing changed."""
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
        """Remember ``slot`` as dealt with (``posted``, or skipped as unchanged) and the open
        set, which the chat has now been shown (``known``), and write the file atomically.
        Only after a post, or the decision to skip."""
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
    total: int, new: int, at: str, late_due: str | None = None, since: str | None = None
) -> str:
    """``TDX queue at 07:30: 6 open (2 new since yesterday)`` (just ``(2 new)`` without
    ``since``) or ``Queue clear at 07:30: nothing needs attention.``, with ``(late: due
    07:30)`` after the time for a late summary."""
    when = at if late_due is None else f"{at} (late: due {late_due})"
    if total == 0:
        return f"Queue clear at {when}: nothing needs attention."
    new_text = f"{new} new" if since is None else f"{new} new since {since}"
    return f"TDX queue at {when}: {total} open ({new_text})"


def since_text(since: datetime, slot: datetime, tz: ZoneInfo) -> str:
    """When the last summary was posted, as the full heading says it: ``yesterday`` for the day
    before ``slot``, ``07:30`` for the same day, else ``Fri 17:00``."""
    posted = since.astimezone(tz)
    day = slot.astimezone(tz).date()
    if posted.date() == day - timedelta(days=1):
        return "yesterday"
    if posted.date() == day:
        return f"{posted:%H:%M}"
    return f"{posted:%a %H:%M}"


def delta_heading(new: int, cleared: int, total: int, since: str) -> str:
    """``Since 07:30: 1 new · 1 cleared · 6 open``: the heading of a later slot's delta."""
    return f"Since {since}: {new} new · {cleared} cleared · {total} open"


def summary_fallback(heading: str) -> str:
    """The counts-only preview line: ``ops-suite: TDX queue at 07:30: 6 open (2 new)``."""
    return f"ops-suite: {heading.removesuffix('.')}"


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
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    body: list[dict[str, object]] = [
        _text(heading, weight="Bolder", size="Medium"),
        _text(
            f"Shift summary, read-only audit {now.astimezone(tz):%a %H:%M}. "
            f"Checked {summary_counts.get('unclaimed', 0)} unclaimed tickets; "
            f"{summary_counts.get('feeds_read', 0)} feeds read, "
            f"{summary_counts.get('feeds_failed', 0)} failed.",
            isSubtle=True,
            size="Small",
            spacing="None",
        ),
    ]
    if events:
        # The week's agenda, first: each event also keeps any finding row of its own below.
        body.append(
            _text(f"{EVENTS_SECTION_LABEL} ({len(events)})", weight="Bolder", spacing="Medium")
        )
        for event in events[: max(max_rows, 0)]:
            line = _id_link(event.ticket_id, links)
            details = event_details(event, tz)
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
    )
    return shown, _summary_shell(heading, body)


def _summary_shell(heading: str, body: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "AdaptiveCard",
        "$schema": CARD_SCHEMA,
        "version": CARD_VERSION,
        "fallbackText": summary_fallback(heading),
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
) -> tuple[Finding, ...]:
    """One section per kind with its count, at most ``max_rows`` rows in all (``+N more`` per
    section past that), appended to ``body``. Rows in ``urgent_keys`` are marked ``(event
    soon)``. Returns the findings shown by id."""
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
                line += " (new)"
            if key in urgent_keys:
                line += f" {EVENT_SOON_MARK}"
            if ticket_id in duplicates:
                line += f" (possible duplicate of #{duplicates[ticket_id]})"
            wording = finding_wording(finding)
            if key in reminder_keys:
                wording += " · reminder"
            if not show_titles:
                # One line, no title: "#<id> (new) · <finding wording>".
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
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """The summary card and the findings it shows by id (the rest are counted as ``+N
    more``). Rows are dropped from the end until the card, mentions included, is under
    ``CARD_MAX_BYTES``. Record only the returned findings.

    ``events`` is the "Events this week" section, first (at most ``max_rows`` rows);
    ``duplicates`` maps a ticket id to the older ticket it may duplicate; ``since`` is the
    last summary posted, which the heading's "new since" names; ``urgent_keys`` are the
    findings marked ``(event soon)`` instead of getting a card of their own."""
    ordered = open_findings(findings)
    new = sum((f.ticket_id, f.kind) in new_keys for f in ordered)
    slot_text = f"{slot.astimezone(tz):%H:%M}"
    since_label = since_text(since, slot, tz) if since is not None else None
    if late:
        heading = summary_heading(
            len(ordered), new, f"{now.astimezone(tz):%H:%M}", slot_text, since_label
        )
    else:
        heading = summary_heading(len(ordered), new, slot_text, since=since_label)
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
) -> dict[str, object]:
    """The shift-summary Adaptive Card: the heading (``Queue clear`` when nothing is open, a
    late marker when late), "Events this week" when there are any, every open finding
    grouped by kind with its count, linked ids marked ``(new)`` and ``(possible duplicate of
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
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """A later slot's delta card and the findings it shows by id: the heading ``Since 07:30:
    N new · M cleared · K open`` (``total`` open findings in all), then rows only for
    ``findings``, the new ones (``new_keys``, marked ``(new)``) and the due reminders
    (``reminder_keys``, marked ``· reminder``), grouped by kind; urgent event findings
    (``urgent_keys``) are marked ``(event soon)``. Cleared findings are a count only; no
    "Events this week" and no mention. Titles only with ``show_titles``; rows are
    dropped from the end until the card is under ``CARD_MAX_BYTES``."""
    ordered = open_findings(findings)
    new = sum((f.ticket_id, f.kind) in new_keys for f in ordered)
    heading = delta_heading(new, cleared, total, f"{since.astimezone(tz):%H:%M}")
    rows = max_rows
    while True:
        body: list[dict[str, object]] = [
            _text(heading, weight="Bolder", size="Medium"),
            _text(
                f"Shift update, read-only audit {now.astimezone(tz):%a %H:%M}. "
                f"Checked {summary_counts.get('unclaimed', 0)} unclaimed tickets; "
                f"{summary_counts.get('feeds_read', 0)} feeds read, "
                f"{summary_counts.get('feeds_failed', 0)} failed.",
                isSubtle=True,
                size="Small",
                spacing="None",
            ),
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
        )
        card = _summary_shell(heading, body)
        if card_size(card) < CARD_MAX_BYTES or not shown:
            return shown, card
        rows = len(shown) - 1


# --- urgent event card ------------------------------------------------------------------


def urgent_heading(count: int) -> str:
    """``Event soon: 2 need action`` (``1 needs action`` for one)."""
    return f"Event soon: {count} {'needs' if count == 1 else 'need'} action"


def fit_urgent_card(
    findings: Sequence[Finding],
    rows_by_id: Mapping[int, TicketRow],
    links: Mapping[int, str | None],
    now: datetime,
    tz: ZoneInfo,
    near_event_days: int,
    *,
    title_chars: int,
    show_titles: bool,
    max_rows: int,
) -> tuple[tuple[Finding, ...], dict[str, object]]:
    """The short card that takes urgent event findings to the desk chat between summaries,
    and the findings it shows by id: the heading ``Event soon: N need action``, then rows
    like the summary's (linked id, the title only with ``show_titles``, the wording), grouped
    by kind. No mention, and the counts-only preview line. Rows are dropped from the end
    until the card is under ``CARD_MAX_BYTES``; record only the returned findings."""
    ordered = open_findings(findings)
    heading = urgent_heading(len(ordered))
    within = {0: "the hour", 1: "a day"}.get(near_event_days, f"{near_event_days} days")
    rows = max_rows
    while True:
        body: list[dict[str, object]] = [
            _text(heading, weight="Bolder", size="Medium"),
            _text(
                f"Read-only audit {now.astimezone(tz):%a %H:%M}: events within {within} "
                "that still need the desk. The next summary lists them too.",
                isSubtle=True,
                size="Small",
                spacing="None",
            ),
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
        )
        card = _summary_shell(heading, body)
        if card_size(card) < CARD_MAX_BYTES or not shown:
            return shown, card
        rows = len(shown) - 1
