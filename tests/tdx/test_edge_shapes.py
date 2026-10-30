"""One rule for every card: the number in its heading is the number of distinct tickets in
its action rows, plus any ``+N more`` it counts. Checked on every card builder over the edge
shapes (``edge_shapes.py``), so a heading that counts rows, or a fix made to one card only,
fails here instead of on a live card."""

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from edge_shapes import CARD_SHAPES, SLOT, SLOT_RUN, TZ, CardShape

from ops_suite.common.config import TdxAlertSettings, TdxSettings
from ops_suite.tdx.alerts import (
    AlertPolicy,
    AlertState,
    Reader,
    build_card,
    card_links,
    fit_delta_card,
    fit_summary_card,
    fit_urgent_card,
    plan_alerts,
)
from ops_suite.tdx.rows import TicketRow
from ops_suite.tdx.week import WeekEvent

DESK = Reader("desk")
SUMMARY_COUNTS = {"unclaimed": 26, "feeds_read": 7, "feeds_failed": 0}
TDX = TdxSettings(base_url="https://tdx.example.edu/")
ROW_ID = re.compile(r"^\[?#(\d+)")
SECTION = re.compile(r"^(.+) \((\d+)\)$")
MORE = re.compile(r"^\+(\d+) more$")
WAITING = re.compile(r"^(\d+) more waiting;")


def _row(ticket_id: int) -> TicketRow:
    return TicketRow(
        widget="unclaimed",
        ticket_id=ticket_id,
        title="Synthetic ticket title",
        requestor="Jordan Sampleton",
        resp_group="IT Service Desk",
        created=SLOT,
        modified=None,
        prim_resp=None,
        status=None,
        detail_url=f"https://tdx.example.edu/TDNext/Apps/43/Tickets/TicketDet?TicketID={ticket_id}",
    )


def _rows(shape: CardShape) -> dict[int, TicketRow]:
    return {ticket_id: _row(ticket_id) for ticket_id in shape.ticket_ids}


def _events(shape: CardShape) -> list[WeekEvent]:
    waiting = [
        WeekEvent(
            ticket_id,
            SLOT_RUN.replace(hour=15),
            True,
            None,
            "lobby",
            True,
            "waiting_on_requester",
            since,
        )  # fmt: skip
        for ticket_id, since in shape.waiting_since.items()
    ]
    confirmed = [
        WeekEvent(
            ticket_id, SLOT_RUN.replace(hour=16), True, None, "classroom", True, "details_confirmed"
        )
        for ticket_id in shape.confirmed
    ]
    return waiting + confirmed


# --- reading a card back -----------------------------------------------------------------


def _texts(card: dict) -> list[str]:
    return [block["text"] for block in card["body"]]


def action_tickets(card: dict) -> tuple[set[int], int]:
    """The distinct ticket ids in the card's action rows and its ``+N more`` / ``N more
    waiting`` count. Rows under "Events this week" are an agenda, not actions: left out."""
    ids: set[int] = set()
    more = 0
    in_events = False
    for text in _texts(card)[2:]:  # past the heading and the lead
        if section := SECTION.match(text):
            in_events = section.group(1) == "Events this week"
            continue
        if in_events:
            continue
        if found := MORE.match(text) or WAITING.match(text):
            more += int(found.group(1))
        elif found := ROW_ID.match(text):
            ids.add(int(found.group(1)))
    return ids, more


def heading_number(card: dict) -> int:
    """The tickets the heading states: the sum of its counts (``1 more ticket and 2
    reminders`` is 3), 0 for a clear queue."""
    heading = _texts(card)[0]
    if heading.startswith("Queue clear"):
        return 0
    counts = heading.split(": ", 1)[1]
    return sum(int(n) for n in re.findall(r"\b(\d+) (?:new|reminders?|tickets?|more|needs?)\b",
                                          counts))  # fmt: skip


# --- every card builder ------------------------------------------------------------------


def full_summary(shape: CardShape, _: Path) -> dict:
    rows = _rows(shape)
    added = {(f.ticket_id, f.kind) for f in shape.findings[:1]}
    return fit_summary_card(
        shape.findings, added, rows, card_links(rows, sorted(rows), TDX), SLOT, SLOT_RUN, TZ,
        SUMMARY_COUNTS, title_chars=80, show_titles=False, max_rows=25,
        events=_events(shape), since=shape.since, reader=DESK,
    )[1]  # fmt: skip


def _update(shape: CardShape, *, reminders: bool) -> dict:
    rows = _rows(shape)
    keys = {(f.ticket_id, f.kind) for f in shape.findings}
    new, due = (set(), keys) if reminders else (keys, set())
    return fit_delta_card(
        shape.findings, new, due, 0, len(shape.ticket_ids), rows,
        card_links(rows, sorted(rows), TDX), shape.since or SLOT.replace(hour=7), SLOT_RUN, TZ,
        SUMMARY_COUNTS, title_chars=80, show_titles=False, max_rows=25, reader=DESK,
    )[1]  # fmt: skip


def update_added(shape: CardShape, _: Path) -> dict:
    return _update(shape, reminders=False)


def update_reminders(shape: CardShape, _: Path) -> dict:
    return _update(shape, reminders=True)


def urgent(shape: CardShape, _: Path) -> dict:
    return fit_urgent_card(
        shape.findings, _rows(shape), {}, SLOT_RUN, TZ,
        title_chars=60, show_titles=False, max_rows=10, reader=DESK,
    )[1]  # fmt: skip


def personal_alert(shape: CardShape, tmp_path: Path) -> dict:
    state = AlertState(tmp_path / "alerts-state.json")
    plan = plan_alerts(
        shape.findings, state, SLOT_RUN, AlertPolicy.from_settings(TdxAlertSettings()),
        seen_ticket_ids=shape.ticket_ids, unsettled_ticket_ids=set(), queue_complete=True,
    )  # fmt: skip
    return build_card(plan, _rows(shape), {}, SLOT_RUN, TZ, SUMMARY_COUNTS, title_chars=80)


BUILDERS: dict[str, Callable[[CardShape, Path], dict]] = {
    "full_summary": full_summary,
    "update_added": update_added,
    "update_reminders": update_reminders,
    "urgent": urgent,
    "personal_alert": personal_alert,
}


@pytest.mark.parametrize("shape", sorted(CARD_SHAPES))
@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_every_heading_counts_the_distinct_tickets_of_its_rows(
    builder: str, shape: str, tmp_path: Path
) -> None:
    card = BUILDERS[builder](CARD_SHAPES[shape], tmp_path)

    ids, more = action_tickets(card)
    assert ids, "the card lists no action row"
    assert heading_number(card) == len(ids) + more, _texts(card)


def test_a_wait_eight_days_back_shows_its_date_on_the_full_summary(tmp_path: Path) -> None:
    card = full_summary(CARD_SHAPES["waiting_eight_days"], tmp_path)

    (event_row,) = [t for t in _texts(card) if t.startswith("[#321](") and "waiting on" in t]
    assert event_row.endswith(" · waiting on requester since Tue 10/20 7:30 AM")


def test_a_last_post_the_day_before_says_since_yesterday(tmp_path: Path) -> None:
    card = full_summary(CARD_SHAPES["last_post_day_before"], tmp_path)

    assert _texts(card)[0] == (
        "TDX queue at 7:30 AM: 2 tickets for the help desk (1 added since yesterday)"
    )


def test_the_details_confirmed_event_is_an_agenda_row_only(tmp_path: Path) -> None:
    card = full_summary(CARD_SHAPES["details_confirmed_event"], tmp_path)

    (event_row,) = [t for t in _texts(card) if ROW_ID.match(t) and "352" in t.split()[0]]
    assert event_row.endswith(" · details confirmed")
    assert action_tickets(card) == ({351}, 0)


def test_ready_to_close_after_a_desk_note_names_the_note(tmp_path: Path) -> None:
    card = full_summary(CARD_SHAPES["ready_after_desk_note"], tmp_path)

    (close_row,) = [t for t in _texts(card) if t.startswith("[#341](")]
    assert "and added a note 8 days ago, no reply since: follow up or close" in close_row


def test_the_merged_event_row_asks_for_the_room_once(tmp_path: Path) -> None:
    card = full_summary(CARD_SHAPES["merged_event_row"], tmp_path)

    text = "\n".join(_texts(card))
    assert text.count("Location still TBD on the ticket") == 1
    assert "Event soon: no room yet" not in text
