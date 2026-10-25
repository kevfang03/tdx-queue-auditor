from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from ops_suite.tdx.events import EventInfo, is_event_title, is_recurring_event, parse_event_title

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


def parse(title: str, now: datetime = NOW) -> EventInfo:
    return parse_event_title(title, TZ, now)


def at(month: int, day: int, hour: int = 23, minute: int = 59, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


# --- is_event_title ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/15 2pm Room 101] Example Talk",
        "Event 10/15 Example Talk",
        "event: 10/15",
        "  EvEnT-10/15",
        "EVENT(10/15)",
        "EVENT\t10/15",
        "EVENT [TBD]",
    ],
)
def test_event_titles(title: str) -> None:
    assert is_event_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "CCI Event Request Form",
        "Eventbrite login issue",
        "Events calendar access",
        "EVENT",
        "Laptop will not connect to the example.edu network",
        "",
        "Pre-event setup 10/15",
    ],
)
def test_not_event_titles(title: str) -> None:
    assert not is_event_title(title)


# --- dates -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("EVENT 10/15 Example Talk", at(10, 15)),
        ("EVENT 10/15/26 Example Talk", at(10, 15)),
        ("EVENT 10/15/2026 Example Talk", at(10, 15)),
        ("EVENT 10/15/2027 Example Talk", at(10, 15, year=2027)),
        ("EVENT 10-15 Example Talk", at(10, 15)),
        ("EVENT 10.15 Example Talk", at(10, 15)),
        ("EVENT Oct 15 Example Talk", at(10, 15)),
        ("EVENT October 15th Example Talk", at(10, 15)),
        ("EVENT Oct. 15, 2026 Example Talk", at(10, 15)),
        ("EVENT 15 Oct Example Talk", at(10, 15)),
        ("EVENT 1st November Example Talk", at(11, 1)),
        ("EVENT Sept 3 Example Talk", at(9, 3)),
        ("EVENT Wed 10/15 Example Talk", at(10, 15)),
        ("EVENT Wednesday, Oct 15 Example Talk", at(10, 15)),
    ],
)
def test_date_styles(title: str, expected: datetime) -> None:
    info = parse(title)
    assert info.when == expected
    assert not info.has_time


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/15] Example Talk",
        "EVENT [10/15 Room 101] Example Talk",
        "EVENT (Oct 15) Example Talk",
        "EVENT: 10/15 - Example Talk",
        "EVENT - [Wed, Oct 15] Example Talk",
        "EVENT 10/15: Example Talk",
        "EVENT[10/15]Example Talk",
    ],
)
def test_bracketed_and_unbracketed(title: str) -> None:
    assert parse(title).when == at(10, 15)


def test_the_first_date_is_read() -> None:
    assert parse("EVENT 10/15-10/16 Example Talk").when == at(10, 15)
    assert parse("EVENT Oct 15 (rain date Oct 22)").when == at(10, 15)


def test_no_time_means_end_of_day() -> None:
    info = parse("EVENT 10/15 Example Talk")
    assert info.when == at(10, 15, 23, 59)
    assert info.when is not None and info.when.tzinfo == TZ
    assert info.has_time is False


# --- times -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "hour", "minute"),
    [
        ("EVENT 10/15 2pm Example Talk", 14, 0),
        ("EVENT 10/15 2:30 PM Example Talk", 14, 30),
        ("EVENT 10/15 2:30pm Example Talk", 14, 30),
        ("EVENT 10/15 2 p.m. Example Talk", 14, 0),
        ("EVENT 10/15 9am Example Talk", 9, 0),
        ("EVENT 10/15 12am Example Talk", 0, 0),
        ("EVENT 10/15 12pm Example Talk", 12, 0),
        ("EVENT 10/15 14:00 Example Talk", 14, 0),
        ("EVENT 10/15 noon Example Talk", 12, 0),
        ("EVENT [10/15, Noon, Room 101] Example Talk", 12, 0),
        ("EVENT Oct 15 2pm Example Talk", 14, 0),
        ("EVENT 15 Oct 14:00 Example Talk", 14, 0),
    ],
)
def test_time_styles(title: str, hour: int, minute: int) -> None:
    info = parse(title)
    assert info.when == at(10, 15, hour, minute)
    assert info.has_time


@pytest.mark.parametrize(
    ("title", "hour", "minute"),
    [
        ("EVENT 10/15 2-4pm Example Talk", 14, 0),
        ("EVENT 10/15 2:00-3:30 PM Example Talk", 14, 0),
        ("EVENT 10/15 2pm - 4pm Example Talk", 14, 0),
        ("EVENT 10/15 10am-12pm Example Talk", 10, 0),
        ("EVENT 10/15 11-1pm Example Talk", 11, 0),
        ("EVENT 10/15 12-2pm Example Talk", 12, 0),
        ("EVENT 10/15 9 to 11am Example Talk", 9, 0),
        ("EVENT 10/15 14:00-15:00 Example Talk", 14, 0),
    ],
)
def test_a_range_gives_its_start(title: str, hour: int, minute: int) -> None:
    assert parse(title).when == at(10, 15, hour, minute)


def test_bare_numbers_after_the_date_are_not_times() -> None:
    info = parse("EVENT 10/15 Room 101 Example Talk")
    assert info.when == at(10, 15)
    assert not info.has_time


def test_a_time_before_the_date_is_not_read_as_a_date() -> None:
    info = parse("EVENT 2-4pm 10/15 Example Talk")
    assert info.when == at(10, 15)


def test_full_bracketed_title() -> None:
    info = parse("EVENT [Wed 10/15 2:00-3:30 PM Room TBD] Example Talk")
    assert info == EventInfo(
        dates=(at(10, 15, 14, 0),), has_time=True, location_tbd=True, end_time=time(15, 30)
    )


# --- year --------------------------------------------------------------------------------


def test_missing_year_crosses_into_next_year() -> None:
    dec_30 = datetime(2026, 12, 30, 12, 0, tzinfo=TZ)
    assert parse("EVENT 1/2 Example Talk", dec_30).when == at(1, 2, year=2027)


def test_missing_year_crosses_into_previous_year() -> None:
    jan_2 = datetime(2027, 1, 2, 12, 0, tzinfo=TZ)
    assert parse("EVENT 12/30 Example Talk", jan_2).when == at(12, 30, year=2026)


def test_two_digit_year_is_20xx() -> None:
    assert parse("EVENT 1/2/27 Example Talk").when == at(1, 2, year=2027)


def test_feb_29_without_a_year_takes_the_nearest_leap_year() -> None:
    now = datetime(2028, 3, 1, 12, 0, tzinfo=TZ)
    assert parse("EVENT 2/29 Example Talk", now).when == at(2, 29, year=2028)
    assert parse("EVENT 2/29 Example Talk").when is None  # no leap year in 2025-2027


def test_now_in_another_time_zone() -> None:
    now_utc = NOW.astimezone(ZoneInfo("UTC"))
    assert parse("EVENT 10/15 Example Talk", now_utc).when == at(10, 15)


# --- TBD ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/15 2pm TBD] Example Talk",
        "EVENT 10/15 2pm Location: tbd",
        "EVENT 10/15 (Tbd) Example Talk",
        "EVENT TBD 10/15",
    ],
)
def test_tbd_in_any_case(title: str) -> None:
    assert parse(title).location_tbd


@pytest.mark.parametrize(
    "title", ["EVENT [10/15 2pm Room 101] Example Talk", "EVENT 10/15 TBDX Talk"]
)
def test_tbd_is_a_whole_word(title: str) -> None:
    assert not parse(title).location_tbd


# --- unreadable --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "EVENT 13/40 Example Talk",
        "EVENT 2/30 Example Talk",
        "EVENT Oct 40 Example Talk",
        "EVENT [date TBD] Example Talk",
        "EVENT",
        "",
        "EVENT ////----....",
        "EVENT 99:99pm 1/",
        "EVENT \x00￿\U0001f600",
        "EVENT " + "1/" * 500,
        "EVENT 10/15/99999",
    ],
)
def test_garbage_gives_no_date_without_raising(title: str) -> None:
    info = parse(title)
    assert info.when is None
    assert info.has_time is False


def test_impossible_date_is_skipped_for_a_later_one() -> None:
    assert parse("EVENT 13/40 then 10/15 Example Talk").when == at(10, 15)


def test_non_event_title_is_still_read_without_raising() -> None:
    assert parse("CCI Event Request Form").when is None


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [Bi-Weekly, Mon/Wed 2-3pm] Club",
        "EVENT [Biweekly Fri 1pm] Robotics club",
        "event: bi weekly lab hours",
        "EVENT Weekly standup, Room 101",
        "Event (DAILY 9am) Front desk check",
        "EVENT - Monthly town hall",
        "EVENT [Recurring] Office hours",
        "EVENT every Tuesday at noon",
    ],
)
def test_recurring_event_titles(title: str) -> None:
    assert is_recurring_event(title)


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/15 2pm Room 101] Club meeting",
        "EVENT [10/15] Everyone welcome",  # "every" only as part of a word
        "EVENT 10/15 Dailyshow screening",
        "Weekly backup failed",  # not an event title
        "CCI Event Request Form weekly",
        "",
    ],
)
def test_not_recurring(title: str) -> None:
    assert not is_recurring_event(title)


# --- several dates -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "days"),
    [
        (
            "Event [10/20/26, 10/27/26 3:00 PM - 5:00 PM Room 101] Example Workshop",
            [(10, 20), (10, 27)],
        ),
        (
            "Event [10/20/26, 10/27/26, 11/3/26 3:00 PM - 5:00 PM Room 101] Example Workshop",
            [(10, 20), (10, 27), (11, 3)],
        ),
    ],
)
def test_a_date_list_shares_the_time_after_the_last_date(
    title: str, days: list[tuple[int, int]]
) -> None:
    info = parse(title)
    assert info.dates == tuple(at(month, day, 15, 0) for month, day in days)
    assert info.has_time
    assert info.when == info.dates[0]


def test_each_date_takes_its_own_time() -> None:
    info = parse("EVENT [10/20 2pm, 10/27 4pm] Example Talk")
    assert info.dates == (at(10, 20, 14, 0), at(10, 27, 16, 0))


def test_a_date_without_a_time_takes_the_time_after_the_last_date() -> None:
    info = parse("EVENT [10/20 2pm, 10/27, 11/3 4pm Room 101] Example Talk")
    assert info.dates == (at(10, 20, 14, 0), at(10, 27, 16, 0), at(11, 3, 16, 0))


def test_a_list_with_no_time_ends_each_day() -> None:
    info = parse("EVENT [10/20, 10/27] Example Talk")
    assert info.dates == (at(10, 20), at(10, 27))
    assert not info.has_time


@pytest.mark.parametrize(
    "title",
    [
        "EVENT Oct 20-22 Example Talk",
        "EVENT [October 20 – 22] Example Talk",
        "EVENT Oct 20 - 22 Example Talk",
        "EVENT [Oct 20th-22nd, Room 101] Example Talk",
    ],
)
def test_a_month_name_day_range_is_two_dates(title: str) -> None:
    assert parse(title).dates == (at(10, 20), at(10, 22))


def test_a_month_name_day_range_shares_its_time() -> None:
    assert parse("EVENT Oct 20-22 2pm Example Talk").dates == (
        at(10, 20, 14, 0),
        at(10, 22, 14, 0),
    )


def test_a_backwards_month_name_range_is_its_first_date() -> None:
    assert parse("EVENT Oct 30-2 Example Talk").dates == (at(10, 30),)


@pytest.mark.parametrize("title", ["EVENT 10/20 - 10/22 Example Talk", "EVENT 10/20-10/22 Talk"])
def test_a_full_numeric_range_is_two_dates(title: str) -> None:
    assert parse(title).dates == (at(10, 20), at(10, 22))


def test_a_numeric_day_range_is_not_guessed() -> None:
    assert parse("EVENT 10/20-22 Example Talk").dates == (at(10, 20),)


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/20, 10/27 3pm] Example Talk, follow-up 11/15",
        "EVENT 10/20, 10/27 3pm Example Talk, follow-up 11/15",
    ],
)
def test_a_date_in_the_event_name_is_not_read(title: str) -> None:
    assert parse(title).dates == (at(10, 20, 15, 0), at(10, 27, 15, 0))


def test_a_list_without_a_bracket() -> None:
    info = parse("EVENT 10/20, 10/27 3pm Room 101 Example Talk")
    assert info.dates == (at(10, 20, 15, 0), at(10, 27, 15, 0))


@pytest.mark.parametrize(
    "title",
    [
        "EVENT Oct 20 & Oct 27 and Nov 3 Example Talk",
        "EVENT 10/20; 10/27 / 11/3 Example Talk",
        "EVENT 10/20 to 10/27, 11/3 Example Talk",
    ],
)
def test_list_separators(title: str) -> None:
    assert parse(title).dates == (at(10, 20), at(10, 27), at(11, 3))


def test_a_bracket_without_a_date_falls_back_to_the_run_after_it() -> None:
    info = parse("EVENT [Room TBD] 10/20, 10/27 Example Talk")
    assert info.dates == (at(10, 20), at(10, 27))
    assert info.location_tbd


def test_a_year_less_list_across_new_year_is_sorted_with_the_right_years() -> None:
    dec_20 = datetime(2026, 12, 20, 12, 0, tzinfo=TZ)
    expected = (at(12, 30), at(1, 6, year=2027))
    assert parse("EVENT 12/30, 1/6 Example Talk", dec_20).dates == expected
    assert parse("EVENT [1/6, 12/30] Example Talk", dec_20).dates == expected


def test_duplicate_dates_are_removed() -> None:
    assert parse("EVENT [10/20, 10/20, 10/27] Example Talk").dates == (at(10, 20), at(10, 27))


def test_an_unresolvable_date_in_a_list_is_dropped() -> None:
    assert parse("EVENT [2/29, 10/20] Example Talk").dates == (at(10, 20),)


# --- end time ("Events this week") ----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "end"),
    [
        ("EVENT [10/15 2-4pm Room 101] Example Talk", time(16, 0)),
        ("EVENT [10/15 3:00 PM - 5:30 PM Room 101] Example Talk", time(17, 30)),
        ("EVENT [10/15 14:00-16:00 Room 101] Example Talk", time(16, 0)),
        ("EVENT [10/15 11-1pm Room 101] Example Talk", time(13, 0)),
        ("EVENT [10/14, 10/15 2pm - 4pm Room 101] Example Talk", time(16, 0)),
    ],
)
def test_a_time_range_gives_its_end(title: str, end: time) -> None:
    assert parse(title).end_time == end


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/15 2pm Room 101] Example Talk",  # no range
        "EVENT [10/15 Room 101] Example Talk",  # no time at all
        "EVENT [10/15 2pm-4 Room 101] Example Talk",  # the end has no am/pm of its own
        "EVENT [10/15 2pm-13pm Room 101] Example Talk",  # not a time
    ],
)
def test_no_end_time_without_a_readable_range(title: str) -> None:
    assert parse(title).end_time is None


# --- location missing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/21/26 6PM-7PM] Resume Review",
        "EVENT [10/21 6pm] Resume Review",
        "EVENT [Tue 10/21 6:00 PM - 7:00 PM] Resume Review",
        "EVENT [Tuesday, Oct 21 at 6pm] Resume Review",
        "EVENT (10/21 noon) Resume Review",
        "EVENT [10/21, 10/28 6-7pm EST] Resume Review",
        "event [10/21 18:00 - 19:00] Resume Review",
    ],
)
def test_a_bracket_with_a_date_and_a_time_only_is_location_missing(title: str) -> None:
    info = parse(title)

    assert info.location_missing and not info.location_tbd
    assert info.dates


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/21/26 6PM-7PM Room 101] Resume Review",
        "EVENT [10/21 6pm, 3675 Market St] Resume Review",
        "EVENT [10/21 6pm Zoom] Resume Review",
        "EVENT [10/21 6pm 101] Resume Review",
        "EVENT [10/21 6pm Lobby] Resume Review",
        "EVENT [10/21 6pm TBD] Resume Review",  # TBD: location_tbd instead
        "EVENT [10/21] Resume Review",  # no time: not this rule
        "EVENT [someday] Resume Review",  # no date
        "EVENT 10/21 6pm Resume Review",  # no bracket: where a room would be is unknown
        "EVENT [Weekly, Mon 5pm] Club",
    ],
)
def test_a_location_or_no_date_and_time_bracket_is_not_location_missing(title: str) -> None:
    assert not parse(title).location_missing


def test_tbd_wins_over_location_missing() -> None:
    info = parse("EVENT [10/21 6pm] Resume Review, room TBD")

    assert info.location_tbd and not info.location_missing


# --- location named ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/28/26 10:30 AM - 11:30 AM 912] Talk",
        "EVENT [10/30/2026 12:30 PM - 2:00 PM Location 1103] Talk",
        "EVENT [10/21 6pm Zoom] Resume Review",
        "EVENT [10/21 Room 101] Resume Review",  # no time, but a room
    ],
)
def test_text_left_in_the_leading_bracket_is_a_named_location(title: str) -> None:
    info = parse(title)

    assert info.location_named and not info.location_missing and not info.location_tbd


@pytest.mark.parametrize(
    "title",
    [
        "EVENT [10/14] Talk",  # a date only
        "EVENT [10/21/26 6PM-7PM] Resume Review",  # a date and a time only
        "EVENT 10/30 6PM Talk",  # no bracket: where a room would be is unknown
        "EVENT [10/21 6pm TBD] Resume Review",
        "EVENT [10/21 6pm Room 101] Resume Review, room TBD",
        "EVENT [someday Room 101] Talk",  # no date in the bracket
    ],
)
def test_without_a_clear_location_in_the_bracket_none_is_named(title: str) -> None:
    assert not parse(title).location_named
