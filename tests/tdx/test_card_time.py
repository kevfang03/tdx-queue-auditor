"""Card times: 12-hour like the ticket titles, built from the hour and minute so they read
the same on every platform, and always in the display time zone."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.card_time import card_time, clock_range, clock_text

NEW_YORK = ZoneInfo("America/New_York")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (time(0, 0), "12:00 AM"),
        (time(0, 5), "12:05 AM"),
        (time(9, 5), "9:05 AM"),
        (time(7, 30), "7:30 AM"),
        (time(11, 59), "11:59 AM"),
        (time(12, 0), "12:00 PM"),
        (time(13, 37), "1:37 PM"),
        (time(23, 59), "11:59 PM"),
    ],
)
def test_clock_text_has_no_leading_zero_and_a_space_before_am_pm(value: time, text: str) -> None:
    assert clock_text(value) == text


@pytest.mark.parametrize(
    ("start", "end", "text"),
    [
        (time(9, 0), time(10, 30), "9:00–10:30 AM"),
        (time(13, 30), time(14, 30), "1:30–2:30 PM"),
        (time(11, 30), time(12, 30), "11:30 AM–12:30 PM"),
        (time(0, 0), time(1, 0), "12:00–1:00 AM"),
    ],
)
def test_a_range_names_am_pm_once_when_both_ends_share_it(
    start: time, end: time, text: str
) -> None:
    assert clock_range(start, end) == text


@pytest.mark.parametrize(
    ("tz", "text", "with_day"),
    [
        (NEW_YORK, "1:37 PM", "Tue 1:37 PM"),
        (LOS_ANGELES, "10:37 AM", "Tue 10:37 AM"),
        (ZoneInfo("UTC"), "5:37 PM", "Tue 5:37 PM"),
    ],
)
def test_card_time_reads_the_instant_in_the_display_time_zone(
    tz: ZoneInfo, text: str, with_day: str
) -> None:
    instant = datetime(2026, 10, 27, 17, 37, tzinfo=UTC)

    assert card_time(instant, tz) == text
    assert card_time(instant, tz, weekday=True) == with_day


def test_midnight_and_noon_in_the_display_time_zone() -> None:
    midnight = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)  # 00:00 in New York
    noon = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)  # 12:00 in New York

    assert card_time(midnight, NEW_YORK, weekday=True) == "Tue 12:00 AM"
    assert card_time(noon, NEW_YORK) == "12:00 PM"
    assert card_time(midnight, LOS_ANGELES, weekday=True) == "Mon 9:00 PM"
