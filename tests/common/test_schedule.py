from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ops_suite.common.config import ScheduleSettings, Settings
from ops_suite.common.schedule import DeskHours, SummarySlots

NY = ZoneInfo("America/New_York")
DESK = DeskHours.from_settings(ScheduleSettings())


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def test_default_windows_follow_the_desk_hours() -> None:
    assert DESK.windows[0] == (time(7, 30), time(22, 0))  # Monday
    assert DESK.windows[5] == (time(8, 0), time(18, 0))  # Saturday
    assert DESK.windows[6] == (time(12, 0), time(17, 0))  # Sunday


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2026, 10, 16, 7, 29), False),  # Friday, before the start
        (at(2026, 10, 16, 7, 30), True),  # the start is open
        (at(2026, 10, 16, 21, 59), True),
        (at(2026, 10, 16, 22, 0), False),  # the end is closed
        (at(2026, 10, 17, 8, 0), True),  # Saturday
        (at(2026, 10, 17, 18, 0), False),
        (at(2026, 10, 18, 11, 59), False),  # Sunday
        (at(2026, 10, 18, 12, 0), True),
        (at(2026, 10, 18, 17, 0), False),
    ],
)
def test_window_edges(now: datetime, expected: bool) -> None:
    assert DESK.is_open(now) is expected


def test_is_open_converts_other_zones() -> None:
    # 12:00 UTC on Friday 2026-10-16 is 08:00 in New York.
    assert DESK.is_open(datetime(2026, 10, 16, 12, 0, tzinfo=UTC))
    assert not DESK.is_open(datetime(2026, 10, 16, 11, 0, tzinfo=UTC))


def test_a_closed_day() -> None:
    hours = DeskHours.from_settings(ScheduleSettings(desk_hours={"mon": "07:30-22:00", "sat": ""}))
    assert not hours.is_open(at(2026, 10, 17, 12))  # Saturday: ""
    assert not hours.is_open(at(2026, 10, 18, 12))  # Sunday: missing
    assert hours.next_open(at(2026, 10, 17, 12)) == at(2026, 10, 19, 7, 30)


def test_next_open_inside_a_window_is_now() -> None:
    now = at(2026, 10, 16, 14, 5)
    assert DESK.next_open(now) == now


def test_next_open_from_friday_night_is_saturday_morning() -> None:
    assert DESK.next_open(at(2026, 10, 16, 22, 30)) == at(2026, 10, 17, 8, 0)


def test_next_open_from_sunday_close_is_monday_morning() -> None:
    assert DESK.next_open(at(2026, 10, 18, 17, 0)) == at(2026, 10, 19, 7, 30)


def test_next_open_early_in_the_day_is_the_same_day() -> None:
    assert DESK.next_open(at(2026, 10, 16, 3, 0)) == at(2026, 10, 16, 7, 30)


def test_dst_ends_2026_11_01() -> None:
    # Sunday 2026-11-01: clocks go back at 02:00, so 12:00 local is 17:00 UTC (EST).
    assert not DESK.is_open(datetime(2026, 11, 1, 16, 30, tzinfo=UTC))  # 11:30 EST
    assert DESK.is_open(datetime(2026, 11, 1, 17, 0, tzinfo=UTC))  # 12:00 EST
    result = DESK.next_open(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))  # 01:30 EDT
    assert result == at(2026, 11, 1, 12, 0)
    assert result.astimezone(UTC) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    # Saturday 22:30 EDT to Sunday noon EST.
    assert DESK.next_open(at(2026, 10, 31, 22, 30)).utcoffset().total_seconds() == -5 * 3600


def test_dst_starts_2027_03_14() -> None:
    # Sunday 2027-03-14: clocks go forward at 02:00, so 12:00 local is 16:00 UTC (EDT).
    assert not DESK.is_open(datetime(2027, 3, 14, 15, 30, tzinfo=UTC))  # 11:30 EDT
    assert DESK.is_open(datetime(2027, 3, 14, 16, 0, tzinfo=UTC))  # 12:00 EDT
    result = DESK.next_open(datetime(2027, 3, 14, 6, 30, tzinfo=UTC))  # 01:30 EST
    assert result.astimezone(UTC) == datetime(2027, 3, 14, 16, 0, tzinfo=UTC)
    # Sunday 17:00 EDT to Monday 07:30 EDT.
    monday = DESK.next_open(at(2027, 3, 14, 17, 0))
    assert monday.astimezone(UTC) == datetime(2027, 3, 15, 11, 30, tzinfo=UTC)


def test_every_day_closed_raises() -> None:
    hours = DeskHours.from_settings(ScheduleSettings(desk_hours={}))
    assert not hours.is_open(at(2026, 10, 16, 12))
    with pytest.raises(ValueError, match="closed on every day"):
        hours.next_open(at(2026, 10, 16, 12))


def test_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DESK.is_open(datetime(2026, 10, 16, 12))


@pytest.mark.parametrize(
    "window", ["7:30-22:00", "07:30-22", "22:00-07:30", "08:00-08:00", "25:00-26:00", "x"]
)
def test_bad_window_strings_are_rejected(window: str) -> None:
    with pytest.raises(ValidationError, match="desk hours"):
        ScheduleSettings(desk_hours={"mon": window})


def test_unknown_day_and_time_zone_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ScheduleSettings(desk_hours={"monday": "07:30-22:00"})
    with pytest.raises(ValidationError, match="unknown time zone"):
        ScheduleSettings(timezone="Mars/Olympus")


def test_desk_hours_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SUITE_SCHEDULE__DESK_HOURS", '{"sun": ""}')
    monkeypatch.setenv("OPS_SUITE_TDX__WATCH__INTERVAL_MINUTES", "30")
    settings = Settings()
    assert settings.schedule.desk_hours == {"sun": ""}
    assert settings.tdx.watch.interval_minutes == 30
    assert settings.tdx.alerts.realert_days == 3
    assert settings.teams.timeout_seconds == 30


# --- shift-summary slots ------------------------------------------------------------------


def summary_slots(**summary: object) -> SummarySlots:
    settings = Settings(tdx={"summary": {"enabled": True, **summary}})
    return SummarySlots.from_settings(settings.tdx.summary, settings.schedule)


SLOTS = summary_slots()


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2026, 10, 19, 7, 0), at(2026, 10, 19, 7, 30)),  # Monday morning
        (at(2026, 10, 19, 7, 30), at(2026, 10, 19, 12, 0)),  # strictly after
        (at(2026, 10, 19, 12, 1), at(2026, 10, 19, 17, 0)),
        (at(2026, 10, 23, 17, 1), at(2026, 10, 24, 8, 0)),  # Friday evening -> Saturday 08:00
        (at(2026, 10, 24, 8, 0), at(2026, 10, 24, 12, 0)),
        (at(2026, 10, 24, 17, 0), at(2026, 10, 25, 12, 0)),  # Saturday -> Sunday's only slot
        (at(2026, 10, 25, 12, 0), at(2026, 10, 26, 7, 30)),  # Sunday -> Monday
    ],
)
def test_next_summary_slot_across_a_weekday_saturday_and_sunday(
    now: datetime, expected: datetime
) -> None:
    assert SLOTS.next_after(now) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2026, 10, 19, 7, 29), at(2026, 10, 18, 12, 0)),  # Monday before 07:30 -> Sunday
        (at(2026, 10, 19, 7, 30), at(2026, 10, 19, 7, 30)),  # at the slot itself
        (at(2026, 10, 19, 13, 0), at(2026, 10, 19, 12, 0)),
        (at(2026, 10, 24, 9, 0), at(2026, 10, 24, 8, 0)),  # Saturday
        (at(2026, 10, 25, 16, 0), at(2026, 10, 25, 12, 0)),  # Sunday
    ],
)
def test_latest_summary_slot(now: datetime, expected: datetime) -> None:
    assert SLOTS.latest_at_or_before(now) == expected


def test_a_day_without_times_has_no_slots() -> None:
    slots = summary_slots(times={"mon": ["09:00"]})
    assert slots.next_after(at(2026, 10, 19, 9, 0)) == at(2026, 10, 26, 9, 0)
    assert summary_slots(times={}).next_after(at(2026, 10, 19, 9, 0)) is None


@pytest.mark.parametrize(
    ("times", "needle"),
    [
        ({"sat": ["07:30"]}, "07:30 on sat is outside desk hours (desk hours 08:00-18:00)"),
        ({"sun": ["17:00"]}, "17:00 on sun"),  # the end of the window is closed
        ({"mon": ["22:15"]}, "22:15 on mon"),
    ],
)
def test_a_summary_time_outside_desk_hours_is_rejected(
    times: dict[str, list[str]], needle: str
) -> None:
    with pytest.raises(ValidationError, match=needle.replace("(", r"\(").replace(")", r"\)")):
        Settings(tdx={"summary": {"enabled": True, "times": times}})


def test_a_summary_time_on_a_closed_day_is_rejected() -> None:
    with pytest.raises(ValidationError, match="the desk is closed"):
        Settings(
            tdx={"summary": {"enabled": True, "times": {"sun": ["12:00"]}}},
            schedule={"desk_hours": {"mon": "07:30-22:00", "sun": ""}},
        )


def test_summary_times_are_checked_only_when_enabled() -> None:
    settings = Settings(tdx={"summary": {"times": {"sun": ["06:00"]}}})
    assert not settings.tdx.summary.enabled


@pytest.mark.parametrize("bad", ["7:30", "24:00", "07:60", "noon"])
def test_a_badly_written_summary_time_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tdx={"summary": {"times": {"mon": [bad]}}})


def test_summary_defaults() -> None:
    summary = Settings().tdx.summary
    assert (summary.enabled, summary.destination, summary.grace_minutes) == (False, "desk", 90)
    assert summary.times["mon"] == ["07:30", "12:00", "17:00"]
    assert summary.times["sat"] == ["08:00", "12:00", "17:00"]
    assert summary.times["sun"] == ["12:00"]
    assert (summary.mention, summary.mention_people) == ("none", [])


@pytest.mark.parametrize("grace", [14, 241])
def test_grace_minutes_bounds(grace: int) -> None:
    with pytest.raises(ValidationError):
        Settings(tdx={"summary": {"grace_minutes": grace}})


def test_mention_people_needs_addresses_and_never_shows_them() -> None:
    with pytest.raises(ValidationError, match="needs at least one address"):
        Settings(tdx={"summary": {"mention": "people"}})
    with pytest.raises(ValidationError) as error:
        Settings(tdx={"summary": {"mention_people": ["Morgan Fixture"]}})
    assert "entry 1 is not a sign-in address" in str(error.value)
    settings = Settings(
        tdx={"summary": {"mention": "people", "mention_people": ["mf123@example.edu"]}}
    )
    assert "mf123" not in repr(settings.tdx.summary)
    assert "mention_people=1" in repr(settings.tdx.summary)
