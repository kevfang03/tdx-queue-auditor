"""Edge shapes for the cards and the day-end notice: the inputs that one synthetic morning
with one finding per ticket never produces, so that a count or a time that reads wrong on
them fails a test instead of reaching a card.

- **Card shapes** (``CARD_SHAPES``): open findings plus what a summary needs around them (its
  slot, the last post, "Events this week"). Among them a ticket with two findings, a merged
  event row, an event waiting since eight days back, a summary whose last post was the
  day before, a ready-to-close row back after a desk note's hold and an event whose details
  a desk note confirmed. Every heading must count the distinct tickets of the action rows below it.
- **Compare records** (``COMPARE_SHAPES``): one cycle each, built by the real diff and record
  functions, so they keep the log's shape: identical reads, a feed read by one side only (with
  and without the other side's recorded read failure), a title that differs, a finding or a
  row in one source only, and one failed browser read.

Invented ids, times and codes only; no titles or names beyond the synthetic ones the card
tests already use.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ops_suite.tdx.feed_types import FeedEntry, TicketFeed
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.rules import Finding, NoRoom
from ops_suite.tdx.shadow import SourceRead, Timing, compare, failure_record, shadow_record

TZ = ZoneInfo("America/New_York")
# A Wednesday 7:30 AM slot, the morning "since Tue" first read as yesterday.
SLOT = datetime(2026, 10, 28, 7, 30, tzinfo=TZ)
SLOT_RUN = SLOT + timedelta(minutes=1)
REQUESTOR = "Jordan Sampleton"
DESK = "Casey Deskworker"


# --- card shapes -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardShape:
    """Open findings and the summary context around them. ``since`` is the last summary
    posted; ``waiting_since`` an "Events this week" row waiting on the requester since then;
    ``confirmed`` "Events this week" rows whose details a desk note confirmed."""

    findings: tuple[Finding, ...]
    since: datetime | None = None
    waiting_since: Mapping[int, datetime] = field(default_factory=dict)
    confirmed: tuple[int, ...] = ()

    @property
    def ticket_ids(self) -> set[int]:
        return {finding.ticket_id for finding in self.findings}


def follow_up(ticket_id: int, no_room: NoRoom | None = None) -> Finding:
    """An event follow-up two days out, waiting on the requester for three days; with
    ``no_room``, the row that also stands for the event's missing room."""
    return Finding(
        ticket_id, "event_details_pending", "normal", "details_pending", 72, 48, no_room=no_room
    )


def replied(ticket_id: int) -> Finding:
    """An event whose requester replied, with the event a day out."""
    return Finding(ticket_id, "client_waiting", "high", "event_requester_replied", 30, 24)


def requester_waiting(ticket_id: int) -> Finding:
    return Finding(ticket_id, "client_waiting", "high", "requestor_last", 73)


def ready_after_note(ticket_id: int) -> Finding:
    """Ready to close again: the desk replied ten days ago and its note's hold is over."""
    return Finding(ticket_id, "ready_to_close", "low", "desk_note_last", 250, note_hours=192)


CARD_SHAPES: dict[str, CardShape] = {
    # One ticket, two rows in two sections: headings say 2 tickets, not 3.
    "two_findings_one_ticket": CardShape((replied(301), follow_up(301), requester_waiting(302))),
    # A renamed event with no room, waiting on the requester: one row that also asks for
    # the room, beside an unrelated ticket.
    "merged_event_row": CardShape((follow_up(311, "tbd"), requester_waiting(312))),
    # An event waiting on the requester since eight days before the slot.
    "waiting_eight_days": CardShape(
        (follow_up(321),), waiting_since={321: SLOT - timedelta(days=8)}
    ),
    # The last post was the day before: the heading's "added since yesterday".
    "last_post_day_before": CardShape(
        (requester_waiting(331), replied(332), follow_up(332)),
        since=SLOT - timedelta(days=1),
    ),
    # Ready to close back after a desk note's hold, beside an event a day out.
    "ready_after_desk_note": CardShape((ready_after_note(341), replied(342))),
    # An agenda row whose details a desk note confirmed: never an action row.
    "details_confirmed_event": CardShape((replied(351),), confirmed=(352,)),
}


# --- compare records ---------------------------------------------------------------------

TIMING = Timing(browser_seconds=20.0, api_seconds=3.0, api_requests=12, browser_non_get=0)


def _row(ticket_id: int, title: str = "Synthetic ticket title") -> TicketRow:
    return TicketRow(
        widget="unclaimed",
        ticket_id=ticket_id,
        title=title,
        requestor=REQUESTOR,
        resp_group="IT Service Desk",
        created=SLOT - timedelta(days=3),
        modified=None,
        prim_resp=None,
        status=None,
        detail_url=None,
    )


def _feed(ticket_id: int) -> TicketFeed:
    return TicketFeed(
        ticket_id,
        (
            FeedEntry(REQUESTOR, SLOT - timedelta(hours=60), "reply"),
            FeedEntry(DESK, SLOT - timedelta(hours=70), "reply"),
        ),
    )


def _read(
    rows: Sequence[int] = (401, 402),
    feeds: Sequence[int] = (401,),
    findings: Sequence[int] = (401,),
    not_read: Sequence[int] = (),
    retitled: Sequence[int] = (),
) -> SourceRead:
    return SourceRead(
        [_row(i, "Synthetic ticket title, updated") if i in retitled else _row(i) for i in rows],
        {i: _feed(i) for i in feeds},
        [requester_waiting(i) for i in findings],
        frozenset(not_read),
    )


def _compared(browser: SourceRead, api: SourceRead) -> dict[str, object]:
    return shadow_record(compare(browser, api), SLOT, TIMING, primary="api")


def api_cycle(feeds: int, api_failed: int = 0) -> dict[str, object]:
    """One API-primary compared cycle with ``feeds`` feeds read by both sides, and
    ``api_failed`` more that the browser read and the API recorded as not read."""
    both = range(401, 401 + feeds)
    failed = range(401 + feeds, 401 + feeds + api_failed)
    return _compared(
        _read(feeds=(*both, *failed)), _read(feeds=tuple(both), not_read=tuple(failed))
    )


COMPARE_SHAPES: dict[str, dict[str, object]] = {
    "identical": _compared(_read(), _read()),
    # A feed read only by the browser while the API recorded no failure for it, which the
    # comparison cannot explain. Rows and findings are the same, so no card could differ.
    "feed_only_browser": _compared(_read(feeds=(401, 402)), _read()),
    # The same feed, recorded by the API as not read: the API's read failure, not a mismatch.
    "feed_not_read_api": _compared(_read(feeds=(401, 402)), _read(not_read=(402,))),
    # A title that changed between the two reads: a detail difference only.
    "title_differs": _compared(_read(), _read(retitled=(402,))),
    "finding_only_api": _compared(_read(), _read(findings=(401, 402))),
    # A row only the browser read, with the findings still the same on both sides.
    "row_only_browser": _compared(_read(rows=(401, 402, 403)), _read()),
    "browser_failure": failure_record("structure", SLOT, TIMING, primary="api"),
}
