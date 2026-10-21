"""Possible duplicates in the shift summary: same requester, similar title."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.duplicates import possible_duplicates, similar_titles, title_tokens
from ops_suite.tdx.rows import TicketRow, WidgetKind

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 19, 7, 30, tzinfo=TZ)
REQUESTOR = "Jordan Sampleton"
OTHER = "Casey Testperson"
FORMS = ("CCI Event Request Form", "SCIS Event Request Form")


def row(
    ticket_id: int,
    title: str,
    *,
    requestor: str = REQUESTOR,
    hours_ago: float | None = 50,
    widget: WidgetKind = "unclaimed",
) -> TicketRow:
    return TicketRow(
        widget=widget,
        ticket_id=ticket_id,
        title=title,
        requestor=requestor,
        resp_group="IT Service Desk",
        created=NOW - timedelta(hours=hours_ago) if hours_ago is not None else None,
        modified=None,
        prim_resp=None,
        status=None,
        detail_url="",
    )


def test_tokens_drop_case_punctuation_and_stop_words() -> None:
    assert title_tokens("RE: Can't log in to the Wi-Fi!") == ("cant", "log", "wi", "fi")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Printer in lab will not connect", "Lab printer will not connect"),  # same words
        ("VPN keeps disconnecting", "RE: VPN keeps disconnecting off campus"),  # contained
        ("Password reset for portal", "password reset for the portal!"),
    ],
)
def test_similar_titles(first: str, second: str) -> None:
    assert similar_titles(first, second, 0.6)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Printer in lab will not connect", "Monitor flickering in study room"),
        ("Laptop loaner request", "Laptop battery replacement"),  # 1 of 5 words shared
        ("The", "A"),  # nothing left after the stop words
    ],
)
def test_dissimilar_titles(first: str, second: str) -> None:
    assert not similar_titles(first, second, 0.6)


def test_the_threshold_is_the_jaccard_cut_off() -> None:
    first, second = "laptop screen cracked badly", "laptop screen cracked"  # contained
    assert similar_titles(first, second, 1.0)
    # 2 shared of 4 distinct words: 0.5.
    assert similar_titles("laptop screen cracked", "laptop screen flicker", 0.5)
    assert not similar_titles("laptop screen cracked", "laptop screen flicker", 0.6)


def test_same_requester_and_similar_title_points_the_newer_at_the_older() -> None:
    rows = [
        row(2, "Lab printer will not connect", hours_ago=50),
        row(1, "Printer in lab will not connect", hours_ago=74),
    ]

    assert possible_duplicates(rows, 0.6, FORMS) == {2: 1}


def test_names_are_matched_leniently() -> None:
    rows = [
        row(1, "Printer in lab will not connect", hours_ago=74),
        row(2, "Lab printer will not connect", requestor="sampleton, jordan"),
    ]

    assert possible_duplicates(rows, 0.6, FORMS) == {2: 1}


def test_a_different_requester_or_a_dissimilar_title_is_not_marked() -> None:
    rows = [
        row(1, "Printer in lab will not connect", hours_ago=74),
        row(2, "Lab printer will not connect", requestor=OTHER),
        row(3, "Monitor flickering in the study room"),
    ]

    assert possible_duplicates(rows, 0.6, FORMS) == {}


def test_three_alike_all_point_at_the_oldest() -> None:
    rows = [
        row(3, "VPN keeps disconnecting again", hours_ago=10),
        row(1, "VPN keeps disconnecting", hours_ago=100),
        row(2, "VPN keeps disconnecting off campus", hours_ago=50),
    ]

    assert possible_duplicates(rows, 0.6, FORMS) == {2: 1, 3: 1}


def test_the_ticket_id_orders_tickets_without_a_created_time() -> None:
    rows = [row(7, "VPN keeps disconnecting", hours_ago=None), row(5, "VPN keeps disconnecting")]

    assert possible_duplicates(rows, 0.6, FORMS) == {7: 5}


def test_claimed_event_form_and_nameless_tickets_are_left_out() -> None:
    rows = [
        row(1, "Printer in lab will not connect", hours_ago=74),
        row(2, "Lab printer will not connect", widget="claimed"),
        row(3, "EVENT [10/20 2pm Room 101] Club meeting", hours_ago=60),
        row(4, "EVENT [10/27 2pm Room 101] Club meeting"),
        row(5, "CCI Event Request Form", hours_ago=60),
        row(6, "CCI Event Request Form"),
        row(8, "Printer in lab will not connect", requestor=" "),
        row(9, "Printer in lab will not connect", requestor=""),
    ]

    assert possible_duplicates(rows, 0.6, FORMS) == {}


def test_a_repeated_row_is_not_its_own_duplicate() -> None:
    same = row(1, "VPN keeps disconnecting")

    assert possible_duplicates([same, same], 0.6, FORMS) == {}
